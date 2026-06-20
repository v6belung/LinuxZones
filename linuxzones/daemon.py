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

import ast
import queue
import subprocess
import time
from typing import Dict, List, Optional, Tuple, Union

import Xlib.display
import Xlib.X as X
import Xlib.ext.record as record
import Xlib.protocol.rq as rq
import Xlib.protocol.event as xevent
import Xlib.Xatom

from .zones import Layout, MonitorInfo

DRAG_THRESHOLD = 8     # px of movement before left-drag is considered active
SNAP_DELAY     = 0.10  # seconds to wait after faking button-1 release
P_RESIZE_INC   = 1 << 6  # WM_NORMAL_HINTS flag bit advertising resize increments
SNAP_RETRIES   = 4     # max resize attempts when a window won't fill its zone
SNAP_RETRY_GAP = 0.06  # seconds between a resize and reading back its geometry
GEOM_TOL       = 2     # px: treat a client this much under target as "filled"

# Canonical modifier name → the X11 keysym names whose hardware keycodes should
# trigger the overlay.  Both left and right variants are included so either
# physical key works.
_MODIFIER_KEYSYMS = {
    "shift": ("XK_Shift_L",   "XK_Shift_R"),
    "alt":   ("XK_Alt_L",     "XK_Alt_R"),
    "ctrl":  ("XK_Control_L", "XK_Control_R"),
}

# Arrow keysym → navigation direction, used by the Super+Arrow zone-move
# feature.  Resolved to hardware keycodes once at startup (see
# _resolve_arrow_keycodes), exactly like the modifier keysyms above.
_ARROW_KEYSYMS = {
    "XK_Left":  "left",
    "XK_Right": "right",
    "XK_Up":    "up",
    "XK_Down":  "down",
}

# gsettings schema holding Cinnamon/Muffin window keybindings, and the four
# Super+Arrow accelerators we must free so RECORD can observe the keys without
# the WM also acting.  See _free_super_arrows / _restore_super_arrows.
_WM_KEYBINDINGS_SCHEMA = "org.cinnamon.desktop.keybindings.wm"
_SUPER_ARROW_ACCELS = ("<Super>Left", "<Super>Right", "<Super>Up", "<Super>Down")


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
                 mod_snap: bool = False, mod_key: str = "shift",
                 monitors: Optional[List[MonitorInfo]] = None,
                 monitor_layouts: Optional[dict] = None,
                 layouts: Optional[dict] = None,
                 kbd_move: bool = False,
                 kbd_move_saved: Optional[dict] = None):
        self.layout   = layout
        self.ui_queue = ui_queue

        # Multi-monitor support
        self._monitors:        List[MonitorInfo] = monitors or []
        self._monitor_layouts: dict              = monitor_layouts or {}
        self._layouts:         dict              = layouts or {}
        self._multi = bool(self._monitors and len(self._monitors) > 1)
        # Tracks which monitor the cursor is currently on (set in _zone_at).
        self._current_monitor: Optional[MonitorInfo] = None

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

        # Keyboard zone navigation (Super+Arrow moves the active window between
        # zones).  Detected through the same passive RECORD path as modifier
        # snap — no key grab.  The conflicting WM shortcut is cleared separately
        # (see update_kbd_move / _free_super_arrows) so the WM doesn't also act.
        self._kbd_move: bool = kbd_move
        # Snapshot of WM keybindings we cleared so Super+Arrow is free; persisted
        # in config so the originals survive a crash and can always be restored.
        self._kbd_move_saved: dict = dict(kbd_move_saved or {})
        # keycode → direction ("left"/"right"/"up"/"down").
        self._arrow_keycodes: Dict[int, str] = self._resolve_arrow_keycodes()
        # Modifier mask bit that means "Super is held" in an event's state.
        self._super_mask: int = self._resolve_super_mask()
        # X server timestamp of the last arrow KeyRelease, for auto-repeat
        # rejection (same timestamp trick as _mod_last_release_time): one move
        # per physical press, holding the key does not repeat.
        self._arrow_last_release_time: int = -1

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

    def _resolve_arrow_keycodes(self) -> Dict[int, str]:
        """Map the arrow keys' hardware keycodes to navigation directions."""
        out: Dict[int, str] = {}
        try:
            from Xlib import XK as _xk
            for keysym_name, direction in _ARROW_KEYSYMS.items():
                kc = self.ctrl_dpy.keysym_to_keycode(getattr(_xk, keysym_name))
                if kc:
                    out[kc] = direction
        except Exception:
            pass
        return out

    def _resolve_super_mask(self) -> int:
        """Return the modifier mask bit that represents the Super key.

        Scans the modifier map for the Super_L keycode; the mask of modifier
        index i is ``1 << i`` (Shift=0 … Mod5=7).  Falls back to Mod4Mask, the
        near-universal binding for Super.
        """
        try:
            from Xlib import XK as _xk
            super_kc = self.ctrl_dpy.keysym_to_keycode(_xk.XK_Super_L)
            mapping = self.ctrl_dpy.get_modifier_mapping()
            for index, keycodes in enumerate(mapping):
                if super_kc and super_kc in keycodes:
                    return 1 << index
        except Exception:
            pass
        return X.Mod4Mask

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

    def _monitor_at(self, root_x: int, root_y: int) -> Optional[MonitorInfo]:
        """Return the monitor containing (root_x, root_y), or None."""
        for mon in self._monitors:
            if mon.x <= root_x < mon.x + mon.w and mon.y <= root_y < mon.y + mon.h:
                return mon
        return None

    def _layout_for_monitor(self, mon: MonitorInfo) -> Layout:
        """Return the Layout assigned to mon, falling back to self.layout."""
        name = self._monitor_layouts.get(mon.name)
        if name and name in self._layouts:
            return self._layouts[name]
        return self.layout

    def _zone_at(self, root_x: int, root_y: int) -> Optional[Union[int, Tuple[int, int]]]:
        """Zone or margin pair at absolute screen position.

        In multi-monitor mode uses per-monitor layout and geometry.
        Margins take priority over zone interiors near a shared boundary.
        """
        if self._multi:
            mon = self._monitor_at(root_x, root_y)
            if mon is not None:
                self._current_monitor = mon
                layout = self._layout_for_monitor(mon)
                sx, sy = root_x - mon.x, root_y - mon.y
                margin = layout.margin_at(sx, sy, mon.w, mon.h)
                if margin is not None:
                    return margin
                return layout.zone_at(sx, sy, mon.w, mon.h)

        self._current_monitor = None
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

    def _suppress_resize_increments(self, win) -> bool:
        """Clear WM_NORMAL_HINTS resize increments so the WM honours exact pixels.

        Terminals (gnome-terminal, xterm, …) advertise a character-cell resize
        increment (e.g. 19 px per row) plus a base size.  On a programmatic
        move/resize the WM rounds the window's size DOWN to a whole cell,
        leaving a sliver of dead space between the window and the zone's bottom
        (and sometimes right) edge.  OS-level maximize is exempt from increments
        per EWMH, which is why that fills the screen fully.

        Clearing the PResizeInc flag makes the WM honour our exact pixel size.
        The hints are intentionally not restored afterwards — see ``_snap``.
        Returns True if increments were present and cleared, else False.
        """
        try:
            hints = win.get_wm_normal_hints()
        except Exception:
            return False
        if not hints or not (hints.flags & P_RESIZE_INC):
            return False
        try:
            win.set_wm_normal_hints(
                flags=hints.flags & ~P_RESIZE_INC,
                min_width=hints.min_width, min_height=hints.min_height,
                max_width=hints.max_width, max_height=hints.max_height,
                width_inc=1, height_inc=1,
                min_aspect=hints.min_aspect, max_aspect=hints.max_aspect,
                base_width=hints.base_width, base_height=hints.base_height,
                win_gravity=hints.win_gravity,
            )
            self.ctrl_dpy.sync()
            return True
        except Exception:
            return False

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

        # Determine origin and size for this snap operation.
        mon = self._current_monitor
        if self._multi and mon is not None:
            snap_layout = self._layout_for_monitor(mon)
            ox, oy, ow, oh = mon.x, mon.y, mon.w, mon.h
        else:
            snap_layout = self.layout
            ox, oy, ow, oh = wx, wy, ww, wh

        if isinstance(target, tuple):
            zone = snap_layout.spanning_zone(*target)
        else:
            zone = snap_layout.zones[target]

        # Step 1: Cancel the WM's pointer grab so it stops moving the window.
        #         Without this the WM continues tracking the drag and overrides us.
        #         Set the swallow flag BEFORE sending so the RECORD loop discards
        #         the echo of this synthetic event and leaves state untouched.
        #         (A keyboard move has no drag grab and calls _apply_zone directly.)
        try:
            from Xlib.ext import xtest
            self._swallow_b1_release = True
            xtest.fake_input(self.ctrl_dpy, X.ButtonRelease, 1)
            self.ctrl_dpy.sync()
            time.sleep(SNAP_DELAY)
        except Exception as e:
            self._swallow_b1_release = False   # XTest failed — nothing to swallow
            print(f"[linuxzones] XTest unavailable ({e}), snap may be unreliable")

        self._apply_zone(win, zone, ox, oy, ow, oh)

    def _apply_zone(self, win, zone, ox: int, oy: int, ow: int, oh: int) -> None:
        """Drive ``win`` to fill ``zone`` (fractions of the ox/oy/ow/oh box).

        Shared by drag-release snapping (``_snap``, which first cancels the WM
        drag grab) and Super+Arrow keyboard moves (``_on_move_key``, which calls
        this directly).  Handles WM/GTK frame extents, un-maximising, and the
        resize-increment clear/retry loop that makes terminals fill fully.
        """
        win_id = win.id
        zx = ox + int(zone.x * ow)
        zy = oy + int(zone.y * oh)
        zw = int(zone.w * ow)
        zh = int(zone.h * oh)

        fl, fr, ft, fb = self._frame_extents(win)
        gl, gr, gt, gb = self._gtk_frame_extents(win)

        # GTK CSD apps (Software Manager, GNOME apps, …) draw invisible
        # shadow/resize-handle margins inside the client window boundary.
        # Expand the target rect outward so the *visible* content fills the zone.
        if gl or gr or gt or gb:
            zx -= gl;  zy -= gt
            zw += gl + gr;  zh += gt + gb

        print(f"[linuxzones] snapping 0x{win_id:x} → ({zx},{zy} {zw}×{zh})")

        # Remove maximised state — maximised windows ignore move/resize requests.
        self._unmaximize(win)

        # Target CLIENT size (outer zone minus WM decorations).  wmctrl and the
        # EWMH/configure fallbacks all drive the client to this size.
        cw = max(1, zw - fl - fr)
        ch = max(1, zh - ft - fb)

        # Terminals advertise character-cell resize increments, so the WM rounds
        # our resize DOWN to a whole cell and leaves a gap at the zone's bottom/
        # right edge.  Clearing the increments is unreliable on its own for two
        # reasons: (1) a *maximized* terminal temporarily drops its increment
        # hints, so a single check right after un-maximizing sees none and skips
        # the whole mechanism; (2) the GTK/VTE toolkit re-asserts its own hints
        # asynchronously and can win the race, re-applying the rounding.  So we
        # loop: clear increments, resize, read the geometry back, and if the
        # window came up short of the zone, clear and resize again.
        #
        # The cleared increments are deliberately NOT restored: re-applying them
        # makes the WM immediately re-validate the window against the cell grid
        # and shrink it back, reintroducing the gap.  Leaving them cleared keeps
        # the window filled; the terminal re-applies its own increments the next
        # time the user resizes it manually, so cell-snapping returns naturally.
        #
        # Normal windows have no increments, fill exactly on the first pass, and
        # break immediately (one geometry read-back, ~one SNAP_RETRY_GAP of added
        # latency).  Terminals converge in 2-3 passes.
        for attempt in range(SNAP_RETRIES):
            self._suppress_resize_increments(win)
            method = self._apply_geometry(win, win_id, zx, zy, fl, ft, cw, ch)
            if method is None:
                break  # every resize path failed; nothing more to try
            time.sleep(SNAP_RETRY_GAP)
            try:
                g = win.get_geometry()
            except Exception:
                break
            if g.width >= cw - GEOM_TOL and g.height >= ch - GEOM_TOL:
                print(f"[linuxzones] snapped via {method} ✓ "
                      f"({g.width}×{g.height}, attempt {attempt + 1})")
                break
            print(f"[linuxzones] via {method}: window came up "
                  f"{cw - g.width}×{ch - g.height}px short of zone "
                  f"(attempt {attempt + 1}/{SNAP_RETRIES}), retrying")

    def _apply_geometry(self, win, win_id, zx, zy, fl, ft, cw, ch) -> Optional[str]:
        """Drive one window to client size ``cw``×``ch`` at outer origin (zx, zy).

        Tries wmctrl → _NET_MOVERESIZE_WINDOW (EWMH) → direct XConfigureWindow,
        in that order, stopping at the first that succeeds.  ``fl``/``ft`` are the
        left/top frame extents used to convert the outer origin to a client
        origin for the EWMH/configure paths (wmctrl does that conversion itself).
        Returns the method name that ran, or ``None`` if all failed.  Called once
        per retry pass, so it does not log success itself — the caller does.
        """
        # Step 3a: wmctrl — the most reliable method on Cinnamon.  Takes the
        #   OUTER frame origin and CLIENT size (it adds frame extents itself).
        try:
            r = subprocess.run(
                ["wmctrl", "-ir", hex(win_id), "-e", f"0,{zx},{zy},{cw},{ch}"],
                timeout=2,
                capture_output=True,
            )
            if r.returncode == 0:
                return "wmctrl"
            print(f"[linuxzones] wmctrl exited {r.returncode}: {r.stderr.decode().strip()}")
        except FileNotFoundError:
            print("[linuxzones] wmctrl not found — falling back to EWMH")
            print("  Install it:  sudo apt install wmctrl")
        except Exception as e:
            print(f"[linuxzones] wmctrl error: {e}")

        # Step 3b: _NET_MOVERESIZE_WINDOW (EWMH).  Coordinates are for the
        #   CLIENT window, so shift the origin inward by the frame extents.
        cx = zx + fl
        cy = zy + ft

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
            return "EWMH"
        except Exception as e:
            print(f"[linuxzones] EWMH failed: {e}")
            # Step 3c: Last resort — direct XConfigureWindow.
            try:
                win.configure(x=cx, y=cy, width=cw, height=ch)
                self.ctrl_dpy.sync()
                return "configure"
            except Exception as e2:
                print(f"[linuxzones] all snap methods failed: {e2}")
                return None

    # ------------------------------------------------------------------ keyboard zone move

    def _abs_geometry(self, win) -> Optional[Tuple[int, int, int, int]]:
        """Return the window's (x, y, w, h) in absolute root coordinates."""
        try:
            g = win.get_geometry()
            t = self.root.translate_coords(win, 0, 0)
            return int(t.x), int(t.y), int(g.width), int(g.height)
        except Exception:
            return None

    def _move_context(self, cx: float, cy: float):
        """Resolve (layout, ox, oy, ow, oh, monitor) for the box at (cx, cy).

        In multi-monitor mode this is the monitor under the point and its
        per-monitor layout; otherwise the work area and the active layout.
        """
        if self._multi:
            mon = self._monitor_at(int(cx), int(cy))
            if mon is None and self._monitors:
                mon = self._monitors[0]
            if mon is not None:
                return self._layout_for_monitor(mon), mon.x, mon.y, mon.w, mon.h, mon
        wx, wy, ww, wh = self._get_work_area()
        return self.layout, wx, wy, ww, wh, None

    def _on_move_key(self, direction: str) -> None:
        """Move the active window to the next zone in ``direction``.

        Picks the current zone from the window's centre, then the next zone via
        ``Layout.zone_in_direction``; if there is none at a monitor edge, hops
        to the adjacent monitor's entry zone.  Reuses ``_apply_zone`` (no XTest
        drag-cancel — there is no drag).
        """
        win = self._active_window()
        if not win:
            return
        geom = self._abs_geometry(win)
        if geom is None:
            return
        ax, ay, aw, ah = geom
        cx, cy = ax + aw / 2.0, ay + ah / 2.0

        layout, ox, oy, ow, oh, mon = self._move_context(cx, cy)
        if not layout.zones or ow <= 0 or oh <= 0:
            return

        cur = layout.zone_for_point((cx - ox) / ow, (cy - oy) / oh)
        if cur is None:
            return
        nxt = layout.zone_in_direction(cur, direction)
        if nxt is not None:
            self._apply_zone(win, layout.zones[nxt], ox, oy, ow, oh)
            return

        # Nothing in that direction on this monitor — traverse to the next.
        if self._multi and mon is not None:
            target = self._cross_monitor_target(mon, direction, cx, cy)
            if target is not None:
                tmon, tlayout, tidx = target
                self._apply_zone(win, tlayout.zones[tidx],
                                 tmon.x, tmon.y, tmon.w, tmon.h)

    def _cross_monitor_target(self, mon: MonitorInfo, direction: str,
                              cx: float, cy: float):
        """Pick (monitor, layout, zone_idx) on the monitor adjacent in direction.

        Among monitors lying in ``direction`` of ``mon``, choose the nearest one
        whose perpendicular span overlaps ``mon``; then choose that monitor's
        entry zone (nearest the shared edge, best perpendicular alignment with
        the window centre).  Returns None when there is no monitor that way.
        """
        best = None
        best_key = None
        for m in self._monitors:
            if m is mon:
                continue
            if direction == "right":
                if m.x < mon.x + mon.w:
                    continue
                primary = m.x - (mon.x + mon.w)
                overlap = min(mon.y + mon.h, m.y + m.h) - max(mon.y, m.y)
            elif direction == "left":
                if m.x + m.w > mon.x:
                    continue
                primary = mon.x - (m.x + m.w)
                overlap = min(mon.y + mon.h, m.y + m.h) - max(mon.y, m.y)
            elif direction == "down":
                if m.y < mon.y + mon.h:
                    continue
                primary = m.y - (mon.y + mon.h)
                overlap = min(mon.x + mon.w, m.x + m.w) - max(mon.x, m.x)
            elif direction == "up":
                if m.y + m.h > mon.y:
                    continue
                primary = mon.y - (m.y + m.h)
                overlap = min(mon.x + mon.w, m.x + m.w) - max(mon.x, m.x)
            else:
                return None
            key = (overlap <= 0, primary)
            if best_key is None or key < best_key:
                best_key, best = key, m
        if best is None:
            return None
        tlayout = self._layout_for_monitor(best)
        if not tlayout.zones:
            return None
        tidx = self._entry_zone(tlayout, direction, best, cx, cy)
        if tidx is None:
            return None
        return best, tlayout, tidx

    def _entry_zone(self, layout: Layout, direction: str, mon: MonitorInfo,
                    cx: float, cy: float) -> Optional[int]:
        """Index of ``layout``'s entry zone when crossing into ``mon``.

        Entering from the left (moving right) prefers the leftmost zone, etc.;
        ties broken by alignment with the window centre's perpendicular position
        mapped into the new monitor's fraction space.
        """
        if direction in ("left", "right"):
            ref = (cy - mon.y) / mon.h if mon.h else 0.5
        else:
            ref = (cx - mon.x) / mon.w if mon.w else 0.5
        ref = min(max(ref, 0.0), 1.0)

        best = None
        best_key = None
        for i, z in enumerate(layout.zones):
            if direction == "right":
                primary, perp = z.x, abs(z.y + z.h / 2 - ref)
            elif direction == "left":
                primary, perp = -(z.x + z.w), abs(z.y + z.h / 2 - ref)
            elif direction == "down":
                primary, perp = z.y, abs(z.x + z.w / 2 - ref)
            elif direction == "up":
                primary, perp = -(z.y + z.h), abs(z.x + z.w / 2 - ref)
            else:
                return None
            key = (primary, perp, i)
            if best_key is None or key < best_key:
                best_key, best = key, i
        return best

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
            # Super+Arrow zone navigation — independent of drag state and of
            # modifier snap.  Arrow keys are never modifier keys, so this never
            # overlaps the mod-snap handling below.
            if self._kbd_move and kc in self._arrow_keycodes:
                # Reject auto-repeat: a repeat KeyPress shares the timestamp of
                # the KeyRelease that immediately preceded it.  One move per
                # physical press; holding the key does not repeat.
                if (event.state & self._super_mask) and \
                        _ev_time(event) != self._arrow_last_release_time:
                    self._on_move_key(self._arrow_keycodes[kc])
                return
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
                mon_name = self._current_monitor.name if self._current_monitor else None
                self.ui_queue.put(("show",))
                self.ui_queue.put(("highlight", zone_idx, mon_name))
            return

        elif etype == X.KeyRelease:
            kc = event.detail
            if self._kbd_move and kc in self._arrow_keycodes:
                # Record the release timestamp so the next same-timestamp
                # KeyPress is recognised as auto-repeat and ignored.
                self._arrow_last_release_time = _ev_time(event)
                return
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
                mon_name = self._current_monitor.name if self._current_monitor else None
                self.ui_queue.put(("show",))
                self.ui_queue.put(("highlight", zone_idx, mon_name))

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
                mon_name = self._current_monitor.name if self._current_monitor else None
                if zone_idx != self._last_zone or mon_name != getattr(self, "_last_mon_name", None):
                    self._last_zone = zone_idx
                    self._last_mon_name = mon_name
                    self.ui_queue.put(("highlight", zone_idx, mon_name))

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
            # Skip keyboard events entirely when no keyboard feature is on.
            if raw_type in (X.KeyPress, X.KeyRelease) and \
                    not (self._mod_snap or self._kbd_move):
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

        Keyboard events (KeyPress=2, KeyRelease=3) are requested ONLY when a
        keyboard feature is enabled (modifier snap or Super+Arrow zone move).
        When both are off — the default — the range starts at ButtonPress=4, so
        keystrokes typed in other applications are never delivered to this
        process at all (principle of least privilege: we do not subscribe to a
        global keystroke feed we have no use for).
        """
        first_event = X.KeyPress if (self._mod_snap or self._kbd_move) else X.ButtonPress
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

    def update_monitor_config(
        self,
        monitors: List[MonitorInfo],
        monitor_layouts: dict,
        layouts: dict,
    ) -> None:
        """Thread-safe update of multi-monitor layout mapping."""
        self._monitors        = monitors
        self._monitor_layouts = monitor_layouts
        self._layouts         = layouts
        self._multi           = bool(monitors and len(monitors) > 1)

    def _keyboard_subscribed(self) -> bool:
        """True when the RECORD range currently includes keyboard events."""
        return self._mod_snap or self._kbd_move

    def _rebuild_record_context(self) -> None:
        """Break the blocking record_enable_context so run() rebuilds the range.

        Disabling from a separate display connection (ctrl_dpy) is the documented
        python-xlib pattern; the editor is open and the daemon idle when this
        runs, so there is no concurrent use of ctrl_dpy from the record thread.
        """
        self._reconfigure_requested = True
        ctx = self._ctx
        if ctx is not None:
            try:
                self.ctrl_dpy.record_disable_context(ctx)
                self.ctrl_dpy.flush()
            except Exception as e:
                print(f"[linuxzones] RECORD reconfigure failed: {e}")

    def update_mod_snap(self, enabled: bool, mod_key: str = "shift") -> None:
        """Toggle modifier snap and/or change the modifier key.

        Called from the UI thread after the editor saves.  Re-resolves the
        modifier's keycodes immediately (cheap, no context change required when
        only the key changes, since the RECORD range is identical for any
        modifier).  The RECORD context is rebuilt only when this flip changes
        whether *any* keyboard feature needs the keyboard-event subscription (so
        toggling modifier snap while Super+Arrow move is already on is a no-op
        for the context).
        """
        mod_key = mod_key if mod_key in _MODIFIER_KEYSYMS else "shift"
        self._mod_key = mod_key
        self._mod_keycodes = self._resolve_mod_keycodes(mod_key)

        if enabled == self._mod_snap:
            return
        was_subscribed = self._keyboard_subscribed()
        self._mod_snap = enabled
        if self._keyboard_subscribed() != was_subscribed:
            self._rebuild_record_context()

    @property
    def kbd_move_saved(self) -> dict:
        """Snapshot of WM keybindings we cleared, for persistence/restore."""
        return dict(self._kbd_move_saved)

    def update_kbd_move(self, enabled: bool) -> None:
        """Enable/disable Super+Arrow zone navigation.

        Enabling clears the conflicting WM shortcut (snapshotting the originals
        so they can be restored) and, when needed, rebuilds the RECORD context
        to start observing keyboard events.  Disabling restores the shortcut.
        Safe to call with ``enabled`` equal to the current state at startup —
        it (re)applies the gsettings change without churning the context.
        """
        if enabled:
            self._kbd_move_saved = _free_super_arrows(self._kbd_move_saved)
        else:
            _restore_super_arrows(self._kbd_move_saved)
            self._kbd_move_saved = {}

        if enabled == self._kbd_move:
            return
        was_subscribed = self._keyboard_subscribed()
        self._kbd_move = enabled
        if self._keyboard_subscribed() != was_subscribed:
            self._rebuild_record_context()

    def restore_kbd_bindings(self) -> None:
        """Restore any cleared WM shortcut without changing config/flags.

        Called on app exit so Super+Arrow returns to the window manager while
        LinuxZones is not running to interpret it.  Idempotent.
        """
        if self._kbd_move_saved:
            _restore_super_arrows(self._kbd_move_saved)


# ---------------------------------------------------------------- gsettings (WM shortcut)

def _gsettings(*args: str) -> Optional[str]:
    """Run ``gsettings`` and return stripped stdout, or None on any failure."""
    try:
        r = subprocess.run(["gsettings", *args],
                           capture_output=True, timeout=3, text=True)
        if r.returncode == 0:
            return r.stdout.strip()
        print(f"[linuxzones] gsettings {' '.join(args)} → rc={r.returncode}: "
              f"{r.stderr.strip()}")
    except Exception as e:
        print(f"[linuxzones] gsettings {' '.join(args)} failed: {e}")
    return None


def _parse_accel_list(value: str) -> Optional[list]:
    """Parse a gsettings 'as' value (e.g. ``['<Super>Left']`` or ``@as []``)."""
    if value.startswith("@as"):
        value = value.split(" ", 1)[1] if " " in value else "[]"
    try:
        result = ast.literal_eval(value)
    except Exception:
        return None
    return result if isinstance(result, list) else None


def _free_super_arrows(existing_saved: dict) -> dict:
    """Strip the Super+Arrow accelerators from Cinnamon's WM keybindings.

    Removing them frees the keys so the passive RECORD path can observe
    Super+Arrow without the WM also acting (it would otherwise tile the window).
    Returns the snapshot of original accelerator lists needed to restore them.
    An ``existing_saved`` snapshot (from a prior session / config) is preserved
    verbatim rather than re-recorded, so a crash that left the keys already
    cleared can never overwrite the true originals with empty lists.
    """
    listing = _gsettings("list-keys", _WM_KEYBINDINGS_SCHEMA)
    if listing is None:
        return dict(existing_saved)
    saved = dict(existing_saved)
    for key in listing.split():
        cur = _gsettings("get", _WM_KEYBINDINGS_SCHEMA, key)
        if cur is None:
            continue
        accels = _parse_accel_list(cur)
        if accels is None:
            continue
        remaining = [a for a in accels if a not in _SUPER_ARROW_ACCELS]
        if len(remaining) != len(accels):
            if key not in saved:
                saved[key] = list(accels)
            _gsettings("set", _WM_KEYBINDINGS_SCHEMA, key, str(remaining))
    return saved


def _restore_super_arrows(saved: dict) -> None:
    """Write the snapshotted accelerator lists back; Muffin re-grabs live."""
    for key, accels in saved.items():
        _gsettings("set", _WM_KEYBINDINGS_SCHEMA, key, str(list(accels)))
