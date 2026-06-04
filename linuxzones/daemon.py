"""X11 event daemon — monitors mouse (and optionally keyboard) globally via
the RECORD extension.

Interaction model
-----------------
  Left-drag a window normally.
  Hold right mouse button    → zone overlay appears; move to choose a zone.
  Release right mouse button → window snaps to the highlighted zone and fills it.
  Quick right-click          → overlay flashes for one frame, same snap on release.
  Release left button        → cancel drag, overlay hides (no snap).

  Keyboard modifier snap (optional, disabled by default):
  Hold the chosen modifier (Shift, Alt or Ctrl) while left-dragging → overlay.
  Release the modifier                                               → snaps.
  This is an alternative to right-click, enabled via the Layout Editor.

The daemon runs on a background thread and communicates with the Tkinter
overlay via a thread-safe queue.Queue.
"""

import queue
import subprocess
import time
from typing import Optional, Tuple, Union

import Xlib.display
import Xlib.X as X
import Xlib.ext.record as record
import Xlib.protocol.rq as rq
import Xlib.protocol.event as xevent
import Xlib.Xatom

from .zones import Layout

DRAG_THRESHOLD = 8     # px of movement before left-drag is considered active
SNAP_DELAY     = 0.10  # seconds to wait after faking button-1 release

# Canonical modifier name → the X11 keysym names whose hardware keycodes should
# trigger the overlay.  Both left and right variants are included so either
# physical key works.
_MODIFIER_KEYSYMS = {
    "shift": ("XK_Shift_L",   "XK_Shift_R"),
    "alt":   ("XK_Alt_L",     "XK_Alt_R"),
    "ctrl":  ("XK_Control_L", "XK_Control_R"),
}


def _ev_time(event) -> int:
    """X server timestamp (ms) of a key/button event, or -2 if unavailable.

    The sentinel is negative and distinct from the daemon's initial
    "no release seen" value (-1) so a missing timestamp never accidentally
    compares equal to it and mis-classifies a genuine press as auto-repeat.
    """
    return getattr(event, "time", -2)


class _State:
    IDLE           = 0
    BUTTON1_DOWN   = 1   # left pressed, not yet moved enough
    DRAGGING       = 2   # left held + moved > threshold
    OVERLAY_ACTIVE = 3   # dragging + right button currently held


class ZoneDaemon:
    def __init__(self, layout: Layout, ui_queue: queue.Queue,
                 mod_snap: bool = False, mod_key: str = "shift"):
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
        self._a_workarea          = self.ctrl_dpy.intern_atom("_NET_WORKAREA")
        self._a_gtk_frame_extents = self.ctrl_dpy.intern_atom("_GTK_FRAME_EXTENTS")

        # Usable area (excludes taskbars/panels); zones are fractions of this.
        self._work_x, self._work_y, self._work_w, self._work_h = self._get_work_area()

        # Drag state
        self._state:     int              = _State.IDLE
        self._btn1_x:    int              = 0
        self._btn1_y:    int              = 0
        self._drag_win:  Optional[object] = None
        self._last_zone: Optional[Union[int, Tuple[int, int]]] = None
        # True while the user's physical B1 is held (set on press, cleared on
        # real release — fake releases are swallowed and do NOT clear this).
        self._b1_held:            bool    = False
        # Set True just before we send a fake B1 release via XTest so the RECORD
        # echo can be identified and swallowed without corrupting state.
        self._swallow_b1_release: bool    = False

        # Keyboard modifier snap (optional feature, disabled by default)
        # CPython bool assignment is atomic; no lock needed for this flag.
        self._mod_snap:         bool    = mod_snap
        self._mod_key:          str     = mod_key if mod_key in _MODIFIER_KEYSYMS else "shift"
        # True while we believe the modifier is physically held down.  Used to
        # dedupe events so a genuine press always registers exactly once.
        self._mod_held:         bool    = False
        # True when the modifier key (not B3) triggered the current overlay.
        # Determines which release event triggers the snap.
        self._overlay_by_mod:   bool    = False
        # X server timestamp (ms) of the last modifier KeyRelease.  X auto-repeat
        # is delivered as a KeyRelease immediately followed by a KeyPress that
        # share the SAME server timestamp; a genuine press never shares its
        # timestamp with the preceding release.  Comparing timestamps lets us
        # ignore auto-repeat precisely, without a wall-clock window that would
        # also swallow legitimate quick taps.  -1 = no release seen yet.
        self._mod_last_release_time: int = -1

        # Resolve the chosen modifier's hardware keycodes once at startup.
        self._mod_keycodes: frozenset = self._resolve_mod_keycodes(self._mod_key)

        # Active RECORD context handle (valid while record_enable_context is
        # blocking) and a flag used to break out of it intentionally when the
        # event subscription must change.  See run() / update_mod_snap().
        self._ctx = None
        self._reconfigure_requested: bool = False

    def _resolve_mod_keycodes(self, mod_key: str) -> frozenset:
        """Map a canonical modifier name to its hardware keycodes (L and R)."""
        names = _MODIFIER_KEYSYMS.get(mod_key, _MODIFIER_KEYSYMS["shift"])
        try:
            from Xlib import XK as _xk
            codes = (self.ctrl_dpy.keysym_to_keycode(getattr(_xk, n)) for n in names)
            return frozenset(c for c in codes if c)
        except Exception:
            return frozenset()

    # ------------------------------------------------------------------ work area

    def _get_work_area(self) -> Tuple[int, int, int, int]:
        """Return (x, y, w, h) of the usable work area from _NET_WORKAREA.

        Falls back to the full screen if the property is unavailable (minimal WM).
        """
        try:
            prop = self.root.get_full_property(self._a_workarea, X.AnyPropertyType)
            if prop and len(prop.value) >= 4:
                x, y, w, h = (int(v) for v in prop.value[:4])
                if w > 0 and h > 0:
                    return x, y, w, h
        except Exception:
            pass
        return 0, 0, self.screen_w, self.screen_h

    def _zone_at(self, root_x: int, root_y: int) -> Optional[Union[int, Tuple[int, int]]]:
        """Zone or margin pair at absolute screen position, using work-area fractions.

        Margins (within MARGIN_PX of a shared zone boundary) take priority
        over zone interiors so the strip always activates near a boundary.
        """
        sx, sy = root_x - self._work_x, root_y - self._work_y
        margin = self.layout.margin_at(sx, sy, self._work_w, self._work_h)
        if margin is not None:
            return margin
        return self.layout.zone_at(sx, sy, self._work_w, self._work_h)

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

    def _gtk_frame_extents(self, win) -> Tuple[int, int, int, int]:
        """Return (left, right, top, bottom) GTK CSD shadow margins, or zeros.

        GTK3/4 CSD windows set _GTK_FRAME_EXTENTS to declare how many pixels
        on each side of the client window are invisible shadow/resize-handle
        areas.  _NET_FRAME_EXTENTS is typically zero for these windows (no WM
        decorations), so wmctrl won't compensate — we must expand the snap
        rect outward by these amounts so the visible content fills the zone.
        """
        try:
            prop = win.get_full_property(self._a_gtk_frame_extents, X.AnyPropertyType)
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

    def _snap(self, target: Union[int, Tuple[int, int]]) -> None:
        # Read active window BEFORE faking any events — focus can change afterwards.
        # _NET_ACTIVE_WINDOW always holds the client window ID (not the WM frame),
        # which is required for _NET_MOVERESIZE_WINDOW and wmctrl to work.
        win = self._active_window() or self._drag_win
        if not win:
            print("[linuxzones] snap: no active window found")
            return

        # Re-read work area each snap so an auto-hide panel or late-starting
        # compositor strut doesn't leave us with stale (full-screen) dimensions.
        wx, wy, ww, wh = self._get_work_area()
        self._work_x, self._work_y, self._work_w, self._work_h = wx, wy, ww, wh

        win_id = win.id
        if isinstance(target, tuple):
            zone = self.layout.spanning_zone(*target)
            zone_desc = f"margin{target}"
        else:
            zone = self.layout.zones[target]
            zone_desc = str(target)
        zx = wx + int(zone.x * ww)
        zy = wy + int(zone.y * wh)
        zw = int(zone.w * ww)
        zh = int(zone.h * wh)

        fl, fr, ft, fb = self._frame_extents(win)
        gl, gr, gt, gb = self._gtk_frame_extents(win)

        # GTK CSD apps (Software Manager, GNOME apps, …) draw invisible
        # shadow/resize-handle margins inside the client window boundary.
        # Expand the target rect outward so the *visible* content fills the zone.
        if gl or gr or gt or gb:
            zx -= gl;  zy -= gt
            zw += gl + gr;  zh += gt + gb

        print(f"[linuxzones] snapping 0x{win_id:x} → zone {zone_desc} ({zx},{zy} {zw}×{zh})")

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
        #   wmctrl -ir <hex_id> -e 0,x,y,w,h  (gravity=0 = current gravity)
        #   wmctrl correctly converts outer-frame (x,y) to client position
        #   by adding _NET_FRAME_EXTENTS, but passes w,h to the WM unchanged
        #   as client dimensions.  We must supply client w,h (outer minus
        #   frame extents) so the outer frame lands exactly on the zone rect.
        #   For CSD windows frame extents are (0,0,0,0): no-op.
        wm_w = max(1, zw - fl - fr)
        wm_h = max(1, zh - ft - fb)
        try:
            r = subprocess.run(
                ["wmctrl", "-ir", hex(win_id), "-e", f"0,{zx},{zy},{wm_w},{wm_h}"],
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

    def _release_zone(self, event) -> Optional[Union[int, Tuple[int, int]]]:
        """Zone to snap to when a trigger (B3 or modifier) is released.

        Prefer the zone the overlay is currently HIGHLIGHTING (``_last_zone``,
        set on the trigger press and kept current by motion events) over
        re-deriving it from the release event's own coordinates.  A quick
        modifier *tap* could release with coordinates that don't resolve to a
        zone, which made the overlay flash without snapping; the highlighted
        zone is always what the user is aiming at and what the overlay shows.
        Fall back to the event coordinates only when nothing is highlighted.
        """
        if self._last_zone is not None:
            return self._last_zone
        return self._zone_at(event.root_x, event.root_y)

    def _handle(self, event) -> None:
        etype = event.type

        # ---- Keyboard events (modifier key snap) -------------------------
        if etype == X.KeyPress:
            kc = event.detail
            if kc not in self._mod_keycodes or not self._mod_snap:
                return
            # Ignore auto-repeat: an auto-repeat KeyPress carries the same X
            # server timestamp as the KeyRelease that immediately preceded it.
            # Also dedupe via _mod_held so a repeat can never re-open the overlay
            # after a snap.  A genuine first press (the only one that should open
            # the overlay) passes both checks — exactly like a B3 press, so a
            # quick tap works the same as a quick right-click.
            if self._mod_held or _ev_time(event) == self._mod_last_release_time:
                return
            self._mod_held = True
            if self._state == _State.DRAGGING:
                self._overlay_by_mod = True
                self._state = _State.OVERLAY_ACTIVE
                zone_idx = self._zone_at(event.root_x, event.root_y)
                self._last_zone = zone_idx
                self.ui_queue.put(("show",))
                self.ui_queue.put(("highlight", zone_idx))
            return

        elif etype == X.KeyRelease:
            kc = event.detail
            if kc not in self._mod_keycodes:
                return
            self._mod_last_release_time = _ev_time(event)
            self._mod_held = False
            if self._state == _State.OVERLAY_ACTIVE and self._overlay_by_mod:
                # Modifier released → snap and hide overlay.
                zone_idx = self._release_zone(event)
                self.ui_queue.put(("hide",))
                self._state = _State.DRAGGING
                self._overlay_by_mod = False
                if zone_idx is not None:
                    self._snap(zone_idx)
                if not self._b1_held:
                    self._state    = _State.IDLE
                    self._drag_win = None
            return

        # ---- Mouse button events -----------------------------------------
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
                # Right pressed while dragging → show overlay (B3 takes ownership;
                # any in-progress modifier-triggered overlay is handed over to B3).
                self._state = _State.OVERLAY_ACTIVE
                self._overlay_by_mod = False
                zone_idx = self._zone_at(event.root_x, event.root_y)
                self._last_zone = zone_idx
                self.ui_queue.put(("show",))
                self.ui_queue.put(("highlight", zone_idx))

        elif etype == X.ButtonRelease:
            btn = event.detail

            if btn == 3 and self._state == _State.OVERLAY_ACTIVE and not self._overlay_by_mod:
                # Right released (and overlay was B3-triggered) → snap, hide overlay.
                zone_idx = self._release_zone(event)
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
                zone_idx = self._zone_at(event.root_x, event.root_y)
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
            # Fast path: inspect the raw event type byte without invoking the
            # slow Xlib binary parser.  The low 7 bits (mask off the synthetic
            # event flag at bit 7) give the core X event number.
            raw_type = data[0] & 0x7f
            # Skip MotionNotify (6) events only while fully IDLE (no button held)
            # — there is nothing to track, so they are pure noise.  At
            # BUTTON1_DOWN motion MUST flow through: _handle() promotes
            # BUTTON1_DOWN → DRAGGING precisely by measuring motion past the drag
            # threshold, so filtering it here would break drag detection (and
            # therefore the overlay and snapping entirely).
            if raw_type == X.MotionNotify and self._state == _State.IDLE:
                data = data[32:]
                continue
            # Skip keyboard events entirely when modifier snap is disabled.
            if raw_type in (X.KeyPress, X.KeyRelease) and not self._mod_snap:
                data = data[32:]
                continue
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

    def _record_spec(self) -> dict:
        """Build the RECORD range spec for the current settings.

        Keyboard events (KeyPress=2, KeyRelease=3) are requested ONLY when
        modifier snap is enabled.  When it is off — the default — the range
        starts at ButtonPress=4, so keystrokes typed in other applications are
        never delivered to this process at all (principle of least privilege:
        we do not subscribe to a global keystroke feed we have no use for).
        """
        first_event = X.KeyPress if self._mod_snap else X.ButtonPress
        return {
            "core_requests":    (0, 0),
            "core_replies":     (0, 0),
            "ext_requests":     (0, 0, 0, 0),
            "ext_replies":      (0, 0, 0, 0),
            "delivered_events": (0, 0),
            # ButtonPress=4, ButtonRelease=5, MotionNotify=6.
            # KeyPress=2 / KeyRelease=3 are included only when modifier snap is on.
            "device_events":    (first_event, X.MotionNotify),
            "errors":           (0, 0),
            "client_started":   False,
            "client_died":      False,
        }

    # ------------------------------------------------------------------ RECORD loop

    def run(self) -> None:
        """Start the RECORD event loop.

        record_enable_context() blocks until another thread calls
        record_disable_context() on the same context XID.  We exploit that to
        rebuild the context with a new event range when modifier snap is
        toggled, so the keyboard subscription always matches the setting.  The loop only
        re-creates the context for an intentional reconfigure; any other return
        means the display went away, so we stop rather than busy-loop.
        """
        while True:
            self._reconfigure_requested = False
            try:
                self._ctx = self.record_dpy.record_create_context(
                    0, [record.AllClients], [self._record_spec()],
                )
            except Exception as e:
                print(f"[linuxzones] RECORD unavailable ({e}); input monitoring disabled.")
                return
            try:
                self.record_dpy.record_enable_context(self._ctx, self._record_callback)
            finally:
                try:
                    self.record_dpy.record_free_context(self._ctx)
                except Exception:
                    pass
                self._ctx = None
            if not self._reconfigure_requested:
                return   # display closed / error — do not respin the loop

    @property
    def is_dragging(self) -> bool:
        """True whenever a left-drag is in progress (overlay may appear soon)."""
        return self._state != _State.IDLE

    def update_layout(self, layout: Layout) -> None:
        """Thread-safe layout swap."""
        self.layout = layout

    def update_mod_snap(self, enabled: bool, mod_key: str = "shift") -> None:
        """Toggle modifier snap and/or change the modifier key.

        Called from the UI thread after the editor saves.  Re-resolves the
        modifier's keycodes immediately (cheap, no context change required when
        only the key changes, since the RECORD range is identical for any
        modifier).  When the *enabled* flag flips, the RECORD context is rebuilt
        so the keyboard-event subscription matches the new setting: keystrokes
        from other applications are intercepted only while modifier snap is on.
        """
        mod_key = mod_key if mod_key in _MODIFIER_KEYSYMS else "shift"
        self._mod_key = mod_key
        self._mod_keycodes = self._resolve_mod_keycodes(mod_key)

        if enabled == self._mod_snap:
            return
        self._mod_snap = enabled
        self._reconfigure_requested = True
        # Break the blocking record_enable_context in the daemon thread so run()
        # rebuilds the context.  Disabling from a separate display connection
        # (ctrl_dpy) is the documented python-xlib pattern; the editor is open
        # and the daemon idle when this runs, so there is no concurrent use of
        # ctrl_dpy from the record thread.
        ctx = self._ctx
        if ctx is not None:
            try:
                self.ctrl_dpy.record_disable_context(ctx)
                self.ctrl_dpy.flush()
            except Exception as e:
                print(f"[linuxzones] modifier snap reconfigure failed: {e}")
