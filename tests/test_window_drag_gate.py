"""The overlay only opens for window drags, never for drags inside an app.

Regression: in a game (World of Warcraft) holding left to mouselook and then
pressing right to run passed the old "B1 held + moved past threshold" check,
so the zone overlay popped up mid-game.
"""

from types import SimpleNamespace

import Xlib.X as X

from linuxzones.daemon import _State
from tests.conftest import drain, make_event

WIN = SimpleNamespace(id=0x100)
CLIENT = (100, 100, 800, 600)      # client area, root-relative


def _daemon(make_daemon, *, geom_after=CLIENT, dnd=False, mod_snap=False):
    d = make_daemon(mod_snap=mod_snap)
    d._managed_window_at = lambda: WIN
    calls = {"n": 0}

    def abs_geometry(win):
        calls["n"] += 1
        return CLIENT if calls["n"] == 1 else geom_after   # 1st = at B1 press
    d._abs_geometry = abs_geometry
    d._dnd_active = lambda: dnd
    return d


def _drag(d, x, y):
    d._handle(make_event(X.ButtonPress, detail=1, root_x=x, root_y=y))
    d._handle(make_event(X.MotionNotify, root_x=x + 50, root_y=y + 50))
    assert d._state == _State.DRAGGING


def _shown(d):
    return ("show",) in drain(d.ui_queue)


def test_in_app_drag_then_right_click_shows_no_overlay(make_daemon):
    d = _daemon(make_daemon)
    _drag(d, 400, 400)                       # inside the client: mouselook
    d._handle(make_event(X.ButtonPress, detail=3, root_x=450, root_y=450))
    assert not _shown(d)
    assert d._state == _State.DRAGGING
    d._handle(make_event(X.ButtonRelease, detail=3, root_x=450, root_y=450))
    assert d.__dict__.get("_snap_calls") is None


def test_title_bar_drag_shows_overlay(make_daemon):
    d = _daemon(make_daemon)
    _drag(d, 400, 90)                        # above the client: WM title bar
    d._handle(make_event(X.ButtonPress, detail=3, root_x=450, root_y=140))
    assert _shown(d)
    assert d._state == _State.OVERLAY_ACTIVE


def test_csd_title_bar_drag_shows_overlay_because_window_moved(make_daemon):
    d = _daemon(make_daemon, geom_after=(150, 150, 800, 600))
    _drag(d, 400, 110)                       # header bar inside the client
    d._handle(make_event(X.ButtonPress, detail=3, root_x=450, root_y=160))
    assert _shown(d)


def test_tab_tear_off_shows_overlay_while_dnd_active(make_daemon):
    d = _daemon(make_daemon, dnd=True)
    _drag(d, 400, 110)                       # tab strip, window stays put
    d._handle(make_event(X.ButtonPress, detail=3, root_x=450, root_y=160))
    assert _shown(d)


def test_in_app_drag_then_modifier_shows_no_overlay(make_daemon):
    d = _daemon(make_daemon, mod_snap=True)
    _drag(d, 400, 400)
    d._handle(make_event(X.KeyPress, detail=50, root_x=450, root_y=450, time=1))
    assert not _shown(d)
    assert d._state == _State.DRAGGING


def test_title_bar_drag_then_modifier_shows_overlay(make_daemon):
    d = _daemon(make_daemon, mod_snap=True)
    _drag(d, 400, 90)
    d._handle(make_event(X.KeyPress, detail=50, root_x=450, root_y=140, time=1))
    assert _shown(d)


def test_dnd_active_reads_xdnd_selection_owner(make_daemon):
    d = make_daemon()
    del d._dnd_active
    d._a_xdnd_selection = 7
    owner = {"v": 0}
    d.ctrl_dpy = SimpleNamespace(get_selection_owner=lambda atom: owner["v"])
    assert not d._dnd_active()
    owner["v"] = SimpleNamespace(id=0x4200001)
    assert d._dnd_active()
