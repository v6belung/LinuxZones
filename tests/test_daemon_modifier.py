"""Tests for the generic keyboard-modifier snap (Shift / Alt / Ctrl).

Covers keycode resolution per modifier, runtime toggling/switching via
update_mod_snap, and the KeyPress/KeyRelease half of the state machine — the
modifier-triggered overlay-and-snap path, which mirrors the right-click path.
"""

import Xlib.X as X
from Xlib import XK

from daemon import _State, _MODIFIER_KEYSYMS
from conftest import make_event, drain


# --------------------------------------------------------------- keycode resolution

def test_resolve_keycodes_distinct_per_modifier(make_daemon):
    d = make_daemon()
    shift = d._resolve_mod_keycodes("shift")
    alt   = d._resolve_mod_keycodes("alt")
    ctrl  = d._resolve_mod_keycodes("ctrl")

    # The stub keysym_to_keycode is the identity, so each set is the keysyms.
    assert shift == frozenset({XK.XK_Shift_L, XK.XK_Shift_R})
    assert alt   == frozenset({XK.XK_Alt_L, XK.XK_Alt_R})
    assert ctrl  == frozenset({XK.XK_Control_L, XK.XK_Control_R})
    assert shift != alt != ctrl


def test_resolve_keycodes_unknown_falls_back_to_shift(make_daemon):
    d = make_daemon()
    assert d._resolve_mod_keycodes("hyper") == d._resolve_mod_keycodes("shift")


def test_every_valid_modifier_resolves_non_empty(make_daemon):
    d = make_daemon()
    for name in _MODIFIER_KEYSYMS:
        assert d._resolve_mod_keycodes(name)


# --------------------------------------------------------------- update_mod_snap

def test_enabling_requests_reconfigure_and_sets_key(make_daemon):
    d = make_daemon(mod_snap=False)
    d.update_mod_snap(True, "ctrl")
    assert d._mod_snap is True
    assert d._mod_key == "ctrl"
    assert d._reconfigure_requested is True
    assert d._mod_keycodes == frozenset({XK.XK_Control_L, XK.XK_Control_R})


def test_disabling_requests_reconfigure(make_daemon):
    d = make_daemon(mod_snap=True)
    d.update_mod_snap(False, "shift")
    assert d._mod_snap is False
    assert d._reconfigure_requested is True


def test_changing_key_while_enabled_skips_reconfigure(make_daemon):
    """Switching modifier needs no context rebuild — the RECORD range is same."""
    d = make_daemon(mod_snap=True, mod_key="shift")
    d._reconfigure_requested = False
    d.update_mod_snap(True, "alt")          # still enabled, only the key changed
    assert d._mod_key == "alt"
    assert d._mod_keycodes == frozenset({XK.XK_Alt_L, XK.XK_Alt_R})
    assert d._reconfigure_requested is False


def test_update_invalid_key_coerced_to_shift(make_daemon):
    d = make_daemon(mod_snap=False)
    d.update_mod_snap(True, "bogus")
    assert d._mod_key == "shift"


# --------------------------------------------------------------- key press → overlay

def test_modifier_press_while_dragging_shows_overlay(make_daemon):
    d = make_daemon(state=_State.DRAGGING, mod_snap=True)
    d._handle(make_event(X.KeyPress, detail=50, root_x=100, root_y=500))

    assert d._state == _State.OVERLAY_ACTIVE
    assert d._overlay_by_mod is True
    msgs = drain(d.ui_queue)
    assert ("show",) in msgs
    assert ("highlight", 0) in msgs


def test_modifier_release_snaps_and_returns_to_dragging(make_daemon):
    d = make_daemon(state=_State.OVERLAY_ACTIVE, mod_snap=True)
    d._overlay_by_mod = True
    d._b1_held = True

    d._handle(make_event(X.KeyRelease, detail=50, root_x=800, root_y=500))

    assert d._snap_calls == [1]              # released over the right zone
    assert ("hide",) in drain(d.ui_queue)
    assert d._state == _State.DRAGGING
    assert d._overlay_by_mod is False


def test_modifier_release_goes_idle_when_b1_released(make_daemon):
    d = make_daemon(state=_State.OVERLAY_ACTIVE, mod_snap=True)
    d._overlay_by_mod = True
    d._b1_held = False

    d._handle(make_event(X.KeyRelease, detail=50, root_x=200, root_y=500))

    assert d._snap_calls == [0]
    assert d._state == _State.IDLE


def test_modifier_press_ignored_when_disabled(make_daemon):
    d = make_daemon(state=_State.DRAGGING, mod_snap=False)
    d._handle(make_event(X.KeyPress, detail=50, root_x=100, root_y=500))
    assert d._state == _State.DRAGGING
    assert drain(d.ui_queue) == []


def test_modifier_press_ignored_when_not_dragging(make_daemon):
    d = make_daemon(state=_State.BUTTON1_DOWN, mod_snap=True)
    d._handle(make_event(X.KeyPress, detail=50, root_x=100, root_y=500))
    # _mod_held is set, but no overlay until a drag is in progress.
    assert d._state == _State.BUTTON1_DOWN
    assert drain(d.ui_queue) == []


def test_non_modifier_key_ignored(make_daemon):
    d = make_daemon(state=_State.DRAGGING, mod_snap=True)
    d._handle(make_event(X.KeyPress, detail=99, root_x=100, root_y=500))   # not 50
    assert d._state == _State.DRAGGING
    assert drain(d.ui_queue) == []


# --------------------------------------------------- full press→release sequences
# (the cases the original tests omitted: they pre-set OVERLAY_ACTIVE and only
#  fired the release, so a broken *press* path was invisible.)

def test_quick_tap_snaps_like_right_click(make_daemon):
    """REGRESSION: tap (press then release) while dragging must snap.

    Mirrors the quick right-click flow.  The press and release carry distinct
    server timestamps (a genuine tap, not auto-repeat).
    """
    d = make_daemon(state=_State.DRAGGING, mod_snap=True)
    d._b1_held = True
    d._handle(make_event(X.KeyPress,   detail=50, root_x=800, root_y=500, time=100))
    assert d._state == _State.OVERLAY_ACTIVE      # press opened the overlay
    d._handle(make_event(X.KeyRelease, detail=50, root_x=800, root_y=500, time=140))
    assert d._snap_calls == [1]
    assert d._state == _State.DRAGGING


def test_tap_works_even_just_after_a_previous_release(make_daemon):
    """A real press right after an earlier release (different timestamp) must
    still register — the old wall-clock guard wrongly dropped these."""
    d = make_daemon(state=_State.DRAGGING, mod_snap=True)
    d._b1_held = True
    d._mod_last_release_time = 1000          # an earlier modifier release
    # New genuine press a few ms later → different timestamp, not auto-repeat.
    d._handle(make_event(X.KeyPress,   detail=50, root_x=200, root_y=500, time=1003))
    assert d._state == _State.OVERLAY_ACTIVE
    d._handle(make_event(X.KeyRelease, detail=50, root_x=200, root_y=500, time=1050))
    assert d._snap_calls == [0]


def test_hold_then_release_snaps(make_daemon):
    d = make_daemon(state=_State.DRAGGING, mod_snap=True)
    d._b1_held = True
    d._handle(make_event(X.KeyPress, detail=50, root_x=800, root_y=500, time=200))
    # ... held for a while; some pointer motion arrives ...
    d._handle(make_event(X.MotionNotify, root_x=820, root_y=500))
    d._handle(make_event(X.KeyRelease, detail=50, root_x=800, root_y=500, time=900))
    assert d._snap_calls == [1]


def test_autorepeat_press_is_ignored_same_timestamp(make_daemon):
    """X auto-repeat = KeyRelease then KeyPress sharing one timestamp.

    The release snaps; the same-timestamp press must NOT re-open the overlay.
    """
    d = make_daemon(state=_State.OVERLAY_ACTIVE, mod_snap=True)
    d._overlay_by_mod = True
    d._b1_held = True
    d._mod_held = True

    # Auto-repeat release at t=500 → snaps, returns to DRAGGING.
    d._handle(make_event(X.KeyRelease, detail=50, root_x=800, root_y=500, time=500))
    assert d._snap_calls == [1]
    assert d._state == _State.DRAGGING
    # Auto-repeat press carries the SAME timestamp → ignored, overlay stays down.
    d._handle(make_event(X.KeyPress, detail=50, root_x=800, root_y=500, time=500))
    assert d._state == _State.DRAGGING


def test_repeat_press_while_held_does_not_reopen(make_daemon):
    """Even with detectable auto-repeat (repeated presses, no synthetic release),
    a press while already held is a no-op."""
    d = make_daemon(state=_State.OVERLAY_ACTIVE, mod_snap=True)
    d._overlay_by_mod = True
    d._mod_held = True
    before = drain(d.ui_queue)                # clear queue
    d._handle(make_event(X.KeyPress, detail=50, root_x=800, root_y=500, time=600))
    assert d._state == _State.OVERLAY_ACTIVE
    assert drain(d.ui_queue) == []            # no extra show/highlight emitted


def test_tap_release_with_unresolved_coords_snaps_to_highlighted_zone(make_daemon):
    """REGRESSION: a quick tap whose release coordinates resolve to no zone must
    still snap to the zone the overlay is highlighting (the flash-but-no-snap bug)."""
    d = make_daemon(state=_State.OVERLAY_ACTIVE, mod_snap=True)
    d._overlay_by_mod = True
    d._b1_held = True
    d._last_zone = 1                          # overlay highlighting the right zone
    # Release reports an off-screen fraction → zone_at() would return None.
    d._handle(make_event(X.KeyRelease, detail=50, root_x=99999, root_y=99999, time=10))
    assert d._snap_calls == [1]               # snapped to highlighted zone, not None


def test_full_tap_snaps_via_highlighted_zone_even_if_release_coords_bad(make_daemon):
    d = make_daemon(state=_State.DRAGGING, mod_snap=True)
    d._b1_held = True
    # Press over the right zone → overlay highlights zone 1.
    d._handle(make_event(X.KeyPress, detail=50, root_x=800, root_y=500, time=10))
    assert d._last_zone == 1
    # Release with coordinates that don't resolve → must still snap to zone 1.
    d._handle(make_event(X.KeyRelease, detail=50, root_x=99999, root_y=99999, time=40))
    assert d._snap_calls == [1]


def test_full_flow_press_drag_tap_modifier_snaps(make_daemon):
    """End-to-end through _handle: B1 press → drag → modifier tap → snap."""
    d = make_daemon(state=_State.IDLE, mod_snap=True)

    d._handle(make_event(X.ButtonPress, detail=1, root_x=100, root_y=100))
    assert d._state == _State.BUTTON1_DOWN
    d._handle(make_event(X.MotionNotify, root_x=300, root_y=100))
    assert d._state == _State.DRAGGING
    # Tap the modifier (press + release, distinct timestamps) over the right zone.
    d._handle(make_event(X.KeyPress,   detail=50, root_x=900, root_y=100, time=10))
    d._handle(make_event(X.KeyRelease, detail=50, root_x=900, root_y=100, time=40))
    assert d._snap_calls == [1]
