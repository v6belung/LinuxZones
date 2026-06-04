"""State-machine tests for ZoneDaemon._handle.

These cover the IDLE → BUTTON1_DOWN → DRAGGING → OVERLAY_ACTIVE flow that the
CPU-optimisation regression broke: motion events were filtered before they
could promote BUTTON1_DOWN → DRAGGING, so the overlay never appeared and
snapping silently failed.
"""

import Xlib.X as X

from linuxzones.daemon import _State, DRAG_THRESHOLD
from conftest import make_event, drain


# --------------------------------------------------------------- button 1 down

def test_button1_press_enters_button1_down(make_daemon):
    d = make_daemon(state=_State.IDLE)
    d._handle(make_event(X.ButtonPress, detail=1, root_x=100, root_y=100))

    assert d._state == _State.BUTTON1_DOWN
    assert d._b1_held is True
    assert d._btn1_x == 100 and d._btn1_y == 100
    assert d._drag_win is not None       # _managed_window_at() result stored


# ---------------------------------------------------- THE REGRESSION GUARD

def test_motion_past_threshold_promotes_to_dragging(make_daemon):
    """A drag must be recognised once the pointer moves past the threshold.

    This is the exact transition the byte-filter bug suppressed.
    """
    d = make_daemon(state=_State.BUTTON1_DOWN)
    d._btn1_x, d._btn1_y = 100, 100

    d._handle(make_event(X.MotionNotify,
                          root_x=100 + DRAG_THRESHOLD + 1, root_y=100))

    assert d._state == _State.DRAGGING


def test_motion_below_threshold_stays_button1_down(make_daemon):
    d = make_daemon(state=_State.BUTTON1_DOWN)
    d._btn1_x, d._btn1_y = 100, 100

    d._handle(make_event(X.MotionNotify, root_x=103, root_y=102))

    assert d._state == _State.BUTTON1_DOWN


# --------------------------------------------------------------- right-button overlay

def test_b3_press_while_dragging_shows_overlay(make_daemon):
    d = make_daemon(state=_State.DRAGGING)

    d._handle(make_event(X.ButtonPress, detail=3, root_x=100, root_y=500))

    assert d._state == _State.OVERLAY_ACTIVE
    assert d._overlay_by_mod is False
    msgs = drain(d.ui_queue)
    assert ("show",) in msgs
    assert ("highlight", 0, None) in msgs       # x=100/1000 = 0.1 → left zone


def test_b3_press_ignored_when_not_dragging(make_daemon):
    d = make_daemon(state=_State.BUTTON1_DOWN)

    d._handle(make_event(X.ButtonPress, detail=3, root_x=100, root_y=500))

    assert d._state == _State.BUTTON1_DOWN
    assert drain(d.ui_queue) == []


def test_b3_release_snaps_and_returns_to_dragging(make_daemon):
    d = make_daemon(state=_State.OVERLAY_ACTIVE)
    d._overlay_by_mod = False
    d._b1_held = True

    # Release over the right-hand zone (x = 800/1000 = 0.8 → idx 1).
    d._handle(make_event(X.ButtonRelease, detail=3, root_x=800, root_y=500))

    assert d._snap_calls == [1]
    assert ("hide",) in drain(d.ui_queue)
    assert d._state == _State.DRAGGING    # B1 still held → stay in drag


def test_b3_release_with_unresolved_coords_snaps_to_highlighted_zone(make_daemon):
    """A B3 release whose coords resolve to no zone still snaps to the
    highlighted zone — same robustness the modifier path relies on."""
    d = make_daemon(state=_State.OVERLAY_ACTIVE)
    d._overlay_by_mod = False
    d._b1_held = True
    d._last_zone = 0
    d._handle(make_event(X.ButtonRelease, detail=3, root_x=99999, root_y=99999))
    assert d._snap_calls == [0]


def test_b3_release_goes_idle_when_b1_already_released(make_daemon):
    d = make_daemon(state=_State.OVERLAY_ACTIVE)
    d._overlay_by_mod = False
    d._b1_held = False                    # left button was let go earlier

    d._handle(make_event(X.ButtonRelease, detail=3, root_x=200, root_y=500))

    assert d._snap_calls == [0]
    assert d._state == _State.IDLE
    assert d._drag_win is None


# --------------------------------------------------------------- motion highlight

def test_motion_in_overlay_highlights_new_zone(make_daemon):
    d = make_daemon(state=_State.OVERLAY_ACTIVE)
    d._last_zone = 0

    d._handle(make_event(X.MotionNotify, root_x=800, root_y=500))   # right zone

    assert d._last_zone == 1
    assert ("highlight", 1, None) in drain(d.ui_queue)


def test_motion_in_overlay_same_zone_no_duplicate_highlight(make_daemon):
    d = make_daemon(state=_State.OVERLAY_ACTIVE)
    d._last_zone = 0

    d._handle(make_event(X.MotionNotify, root_x=100, root_y=500))   # still left

    assert drain(d.ui_queue) == []


# --------------------------------------------------------------- cancel paths

def test_b1_release_while_dragging_cancels(make_daemon):
    d = make_daemon(state=_State.DRAGGING)
    d._b1_held = True

    d._handle(make_event(X.ButtonRelease, detail=1, root_x=400, root_y=400))

    assert d._state == _State.IDLE
    assert d._drag_win is None
    assert ("hide",) in drain(d.ui_queue)


def test_b1_release_while_button1_down_goes_idle_no_overlay(make_daemon):
    d = make_daemon(state=_State.BUTTON1_DOWN)
    d._b1_held = True

    d._handle(make_event(X.ButtonRelease, detail=1, root_x=100, root_y=100))

    assert d._state == _State.IDLE
    # Never reached DRAGGING, so the overlay was never shown → no hide needed.
    assert drain(d.ui_queue) == []


def test_fake_b1_release_is_swallowed(make_daemon):
    """The XTest echo during _snap must not clear physical-button state."""
    d = make_daemon(state=_State.OVERLAY_ACTIVE)
    d._b1_held = True
    d._swallow_b1_release = True

    d._handle(make_event(X.ButtonRelease, detail=1, root_x=400, root_y=400))

    assert d._swallow_b1_release is False  # flag consumed
    assert d._b1_held is True              # physical button still considered down
    assert d._state == _State.OVERLAY_ACTIVE


# --------------------------------------------------------------- full happy path

def test_full_right_click_snap_flow(make_daemon):
    """End-to-end: press → drag → B3 hold → move → B3 release snaps."""
    d = make_daemon(state=_State.IDLE)

    # Left press
    d._handle(make_event(X.ButtonPress, detail=1, root_x=100, root_y=100))
    assert d._state == _State.BUTTON1_DOWN

    # Move past threshold → drag recognised (regression-sensitive step)
    d._handle(make_event(X.MotionNotify, root_x=300, root_y=100))
    assert d._state == _State.DRAGGING

    # Hold right button → overlay
    d._handle(make_event(X.ButtonPress, detail=3, root_x=300, root_y=100))
    assert d._state == _State.OVERLAY_ACTIVE

    # Move into the right zone
    d._handle(make_event(X.MotionNotify, root_x=900, root_y=100))
    assert d._last_zone == 1

    # Release right button → snap to right zone
    d._b1_held = True
    d._handle(make_event(X.ButtonRelease, detail=3, root_x=900, root_y=100))
    assert d._snap_calls == [1]
