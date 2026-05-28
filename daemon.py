"""X11 event daemon — monitors mouse globally via the RECORD extension.

Interaction model
-----------------
  Left-drag a window normally.
  Hold right mouse button   → zone overlay appears; move to choose a zone.
  Release right mouse button → window snaps to the highlighted zone and fills it.
  Quick right-click          → overlay flashes for one frame, same snap on release.
  Release left button        → cancel drag, overlay hides (no snap).

The daemon runs on a background thread and communicates with the Tkinter
overlay via a thread-safe queue.Queue.
"""

import queue
import subprocess
import time
from typing import Optional, Tuple

import Xlib.display
import Xlib.X as X
import Xlib.ext.record as record
import Xlib.protocol.rq as rq
import Xlib.protocol.event as xevent
import Xlib.Xatom

from zones import Layout

DRAG_THRESHOLD = 8     # px of movement before left-drag is considered active
SNAP_DELAY     = 0.10  # seconds to wait after faking button-1 release


class _State:
    IDLE           = 0
    BUTTON1_DOWN   = 1   # left pressed, not yet moved enough
    DRAGGING       = 2   # left held + moved > threshold
    OVERLAY_ACTIVE = 3   # dragging + right button currently held


class ZoneDaemon:
    def __init__(self, layout: Layout, ui_queue: queue.Queue):
        self.layout   = layout
        self.ui_queue = ui_queue

        # Two separate Display connections are required:
        #   record_dpy  – owned by the RECORD blocking loop
        #   ctrl_dpy    – used for all window queries and manipulation
        self.ctrl_dpy   = Xlib.display.Display()
        self.record_dpy = Xlib.display.Display()

        screen = self.ctrl_dpy.screen()
        self.root     = screen.root
        self.screen_w = screen.width_in_pixels
        self.screen_h = screen.height_in_pixels

        # Pre-interned atoms
        self._a_active_win    = self.ctrl_dpy.intern_atom("_NET_ACTIVE_WINDOW")
        self._a_moveresize    = self.ctrl_dpy.intern_atom("_NET_MOVERESIZE_WINDOW")
        self._a_wm_state      = self.ctrl_dpy.intern_atom("WM_STATE")
        self._a_frame_extents = self.ctrl_dpy.intern_atom("_NET_FRAME_EXTENTS")
        self._a_net_wm_state  = self.ctrl_dpy.intern_atom("_NET_WM_STATE")
        self._a_max_h         = self.ctrl_dpy.intern_atom("_NET_WM_STATE_MAXIMIZED_HORZ")
        self._a_max_v         = self.ctrl_dpy.intern_atom("_NET_WM_STATE_MAXIMIZED_VERT")

        # Drag state
        self._state:     int              = _State.IDLE
        self._btn1_x:    int              = 0
        self._btn1_y:    int              = 0
        self._drag_win:  Optional[object] = None
        self._last_zone: Optional[int]    = None
        # True while the user's physical B1 is held (set on press, cleared on
        # real release — fake releases are swallowed and do NOT clear this).
        self._b1_held:            bool    = False
        # Set True just before we send a fake B1 release via XTest so the RECORD
        # echo can be identified and swallowed without corrupting state.
        self._swallow_b1_release: bool    = False

    # ------------------------------------------------------------------ window helpers

    def _active_window(self) -> Optional[object]:
        """Return the client window from _NET_ACTIVE_WINDOW (always the correct ID)."""
        try:
            prop = self.root.get_full_property(self._a_active_win, X.AnyPropertyType)
            if prop and prop.value:
                return self.ctrl_dpy.create_resource_object("window", prop.value[0])
        except Exception:
            pass
        return None

    def _managed_window_at(self) -> Optional[object]:
        """Walk from the leaf window under the pointer up to a WM-managed client."""
        try:
            ptr = self.root.query_pointer()
            win = ptr.child
            if not win or win.id == self.root.id:
                return None
            while win and win.id != self.root.id:
                try:
                    if win.get_full_property(self._a_wm_state, X.AnyPropertyType):
                        return win  # found the client window (has WM_STATE)
                except Exception:
                    pass
                tree   = win.query_tree()
                parent = tree.parent
                if parent.id == self.root.id:
                    # Reached the WM frame without finding WM_STATE.
                    # Look inside for a child that has WM_STATE (client window).
                    try:
                        for child in tree.children:
                            if child.get_full_property(self._a_wm_state, X.AnyPropertyType):
                                return child
                    except Exception:
                        pass
                    return win  # fall back to frame
                win = parent
        except Exception:
            pass
        return None

    def _frame_extents(self, win) -> Tuple[int, int, int, int]:
        """Return (left, right, top, bottom) WM decoration sizes, or zeros."""
        try:
            prop = win.get_full_property(self._a_frame_extents, X.AnyPropertyType)
            if prop and len(prop.value) >= 4:
                return tuple(int(v) for v in prop.value[:4])
        except Exception:
            pass
        return (0, 0, 0, 0)

    def _unmaximize(self, win) -> None:
        """Remove maximised state — maximised windows ignore move/resize requests."""
        try:
            ev = xevent.ClientMessage(
                window=win,
                client_type=self._a_net_wm_state,
                data=(32, [0, self._a_max_h, self._a_max_v, 0, 0]),  # action=0: remove
            )
            self.root.send_event(
                ev,
                event_mask=X.SubstructureNotifyMask | X.SubstructureRedirectMask,
            )
            self.ctrl_dpy.sync()
            time.sleep(0.04)
        except Exception:
            pass

    # ------------------------------------------------------------------ snapping

    def _snap(self, zone_idx: int) -> None:
        # Read active window BEFORE faking any events — focus can change afterwards.
        # _NET_ACTIVE_WINDOW always holds the client window ID (not the WM frame),
        # which is required for _NET_MOVERESIZE_WINDOW and wmctrl to work.
        win = self._active_window() or self._drag_win
        if not win:
            print("[linuxzones] snap: no active window found")
            return

        win_id = win.id
        zone   = self.layout.zones[zone_idx]
        zx = int(zone.x * self.screen_w)
        zy = int(zone.y * self.screen_h)
        zw = int(zone.w * self.screen_w)
        zh = int(zone.h * self.screen_h)
        print(f"[linuxzones] snapping 0x{win_id:x} → zone {zone_idx} ({zx},{zy} {zw}×{zh})")

        # Step 1: Cancel the WM's pointer grab so it stops moving the window.
        #         Without this the WM continues tracking the drag and overrides us.
        #         Set the swallow flag BEFORE sending so the RECORD loop discards
        #         the echo of this synthetic event and leaves state untouched.
        try:
            from Xlib.ext import xtest
            self._swallow_b1_release = True
            xtest.fake_input(self.ctrl_dpy, X.ButtonRelease, 1)
            self.ctrl_dpy.sync()
            time.sleep(SNAP_DELAY)
        except Exception as e:
            self._swallow_b1_release = False   # XTest failed — nothing to swallow
            print(f"[linuxzones] XTest unavailable ({e}), snap may be unreliable")

        # Step 2: Remove maximised state.
        self._unmaximize(win)

        # Step 3a: Try wmctrl — the most reliable method on Cinnamon.
        #   wmctrl -ir <hex_id> -e 0,x,y,w,h
        #   The "0" gravity makes wmctrl position the OUTER frame at (x,y,w,h),
        #   which is exactly what we want. It also handles frame extents internally.
        try:
            r = subprocess.run(
                ["wmctrl", "-ir", hex(win_id), "-e", f"0,{zx},{zy},{zw},{zh}"],
                timeout=2,
                capture_output=True,
            )
            if r.returncode == 0:
                print("[linuxzones] snapped via wmctrl ✓")
                return
            print(f"[linuxzones] wmctrl exited {r.returncode}: {r.stderr.decode().strip()}")
        except FileNotFoundError:
            print("[linuxzones] wmctrl not found — falling back to EWMH")
            print("  Install it:  sudo apt install wmctrl")
        except Exception as e:
            print(f"[linuxzones] wmctrl error: {e}")

        # Step 3b: Fallback — _NET_MOVERESIZE_WINDOW (EWMH).
        #   Coordinates are for the CLIENT window (inner, no decorations).
        #   We adjust using _NET_FRAME_EXTENTS so the outer frame fills the zone.
        fl, fr, ft, fb = self._frame_extents(win)
        print(f"[linuxzones] frame extents: l={fl} r={fr} t={ft} b={fb}")
        cx = zx + fl
        cy = zy + ft
        cw = max(1, zw - fl - fr)
        ch = max(1, zh - ft - fb)

        # Flags: bits 8-11 = x/y/w/h present; bits 12-13 = source=2 (pager).
        # source=2 is required — Muffin/Mutter ignore source=1 from external apps.
        flags = (1 << 8) | (1 << 9) | (1 << 10) | (1 << 11) | (2 << 12)
        try:
            ev = xevent.ClientMessage(
                window=win,
                client_type=self._a_moveresize,
                data=(32, [flags, cx, cy, cw, ch]),
            )
            self.root.send_event(
                ev,
                event_mask=X.SubstructureNotifyMask | X.SubstructureRedirectMask,
            )
            self.ctrl_dpy.sync()
            print(f"[linuxzones] snapped via EWMH ✓  client=({cx},{cy} {cw}×{ch})")
        except Exception as e:
            print(f"[linuxzones] EWMH failed: {e}")
            # Step 3c: Last resort — direct XConfigureWindow.
            try:
                win.configure(x=cx, y=cy, width=cw, height=ch)
                self.ctrl_dpy.sync()
                print("[linuxzones] snapped via direct configure ✓")
            except Exception as e2:
                print(f"[linuxzones] all snap methods failed: {e2}")

    # ------------------------------------------------------------------ event handling

    def _handle(self, event) -> None:
        etype = event.type

        if etype == X.ButtonPress:
            btn = event.detail

            if btn == 1:
                self._state    = _State.BUTTON1_DOWN
                self._btn1_x   = event.root_x
                self._btn1_y   = event.root_y
                self._last_zone = None
                self._drag_win  = self._managed_window_at()
                self._b1_held   = True

            elif btn == 3 and self._state in (_State.DRAGGING, _State.OVERLAY_ACTIVE):
                # Right pressed while dragging → show overlay
                self._state = _State.OVERLAY_ACTIVE
                zone_idx = self.layout.zone_at(
                    event.root_x, event.root_y, self.screen_w, self.screen_h)
                self._last_zone = zone_idx
                self.ui_queue.put(("show",))
                self.ui_queue.put(("highlight", zone_idx))

        elif etype == X.ButtonRelease:
            btn = event.detail

            if btn == 3 and self._state == _State.OVERLAY_ACTIVE:
                # Right released → snap, hide overlay.
                zone_idx = self.layout.zone_at(
                    event.root_x, event.root_y, self.screen_w, self.screen_h)
                self.ui_queue.put(("hide",))
                self._state = _State.DRAGGING
                if zone_idx is not None:
                    self._snap(zone_idx)
                # If B1 is no longer physically held (released during the snap
                # or released while overlay was visible), go straight to IDLE so
                # a stale DRAGGING state doesn't linger.
                if not self._b1_held:
                    self._state    = _State.IDLE
                    self._drag_win = None

            elif btn == 1:
                if self._swallow_b1_release:
                    # Echo of our own XTest fake_input — discard it.
                    # _b1_held stays True because the physical button is still down.
                    self._swallow_b1_release = False
                    return
                self._b1_held = False
                if self._state == _State.OVERLAY_ACTIVE:
                    # B1 released while the overlay is visible.  The WM may have
                    # dropped its drag grab when the overlay window appeared; we
                    # don't cancel here so the B3 release can still trigger the
                    # snap.  _b1_held=False means post-snap we'll go to IDLE.
                    return
                if self._state in (_State.DRAGGING, _State.BUTTON1_DOWN):
                    if self._state == _State.DRAGGING:
                        self.ui_queue.put(("hide",))
                    self._state    = _State.IDLE
                    self._drag_win = None

        elif etype == X.MotionNotify:
            if self._state == _State.BUTTON1_DOWN:
                dx = abs(event.root_x - self._btn1_x)
                dy = abs(event.root_y - self._btn1_y)
                if dx > DRAG_THRESHOLD or dy > DRAG_THRESHOLD:
                    self._state = _State.DRAGGING

            if self._state == _State.OVERLAY_ACTIVE:
                zone_idx = self.layout.zone_at(
                    event.root_x, event.root_y, self.screen_w, self.screen_h)
                if zone_idx != self._last_zone:
                    self._last_zone = zone_idx
                    self.ui_queue.put(("highlight", zone_idx))

    # ------------------------------------------------------------------ RECORD loop

    def _record_callback(self, reply) -> None:
        if reply.category != record.FromServer:
            return
        if reply.client_swapped:
            return
        if not reply.data or reply.data[0] < 2:
            return
        data = reply.data
        while data:
            if len(data) < 32:
                break        # incomplete trailing bytes — stop cleanly
            try:
                event, data = rq.EventField(None).parse_binary_value(
                    data, self.record_dpy.display, None, None)
                self._handle(event)
            except Exception:
                # Skip exactly one 32-byte X event and continue.  Without this,
                # a single unparseable event (e.g. an unusual MotionNotify sub-
                # type) would silently drop all subsequent events in the same
                # packet — including a B3 release that should trigger a snap.
                data = data[32:]

    def run(self) -> None:
        """Start the RECORD event loop (blocks until display is closed)."""
        ctx = self.record_dpy.record_create_context(
            0,
            [record.AllClients],
            [{
                "core_requests":    (0, 0),
                "core_replies":     (0, 0),
                "ext_requests":     (0, 0, 0, 0),
                "ext_replies":      (0, 0, 0, 0),
                "delivered_events": (0, 0),
                # ButtonPress=4, ButtonRelease=5, MotionNotify=6
                "device_events":    (X.ButtonPress, X.MotionNotify),
                "errors":           (0, 0),
                "client_started":   False,
                "client_died":      False,
            }],
        )
        try:
            self.record_dpy.record_enable_context(ctx, self._record_callback)
        finally:
            self.record_dpy.record_free_context(ctx)

    def update_layout(self, layout: Layout) -> None:
        """Thread-safe layout swap."""
        self.layout = layout
