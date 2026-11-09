"""In-app drags (e.g. tearing a tab off Firefox) move no window.

The window to snap is created by the app when the drop happens, so _snap must
detect the case and snap the *new* window, not the drag source.
"""

from types import SimpleNamespace

import pytest
import Xlib.X as X

import linuxzones.daemon as daemon_mod
from tests.conftest import make_event

SRC = SimpleNamespace(id=0x100)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(daemon_mod.time, "sleep", lambda s: None)


def _in_app_daemon(make_daemon, *, start=(10, 10, 800, 600), now=None):
    d = make_daemon()
    d._drag_win = SRC
    d._drag_geom = start
    geom = {"g": now if now is not None else start}
    d._abs_geometry = lambda win: geom["g"]
    return d, geom


def test_b1_press_records_drag_geometry(make_daemon):
    d = make_daemon()
    d._abs_geometry = lambda win: (1, 2, 3, 4)
    d._handle(make_event(X.ButtonPress, detail=1, root_x=5, root_y=5))
    assert d._drag_geom == (1, 2, 3, 4)


def test_unmoved_window_is_in_app_drag(make_daemon):
    d, _ = _in_app_daemon(make_daemon)
    assert d._is_in_app_drag()


def test_jitter_within_tolerance_is_still_in_app(make_daemon):
    d, _ = _in_app_daemon(make_daemon, now=(12, 9, 800, 600))
    assert d._is_in_app_drag()


def test_moved_window_is_a_window_move(make_daemon):
    d, _ = _in_app_daemon(make_daemon, now=(300, 200, 800, 600))
    assert not d._is_in_app_drag()


def test_unknown_geometry_falls_back_to_window_move(make_daemon):
    d = make_daemon()
    d._drag_win = SRC
    d._drag_geom = None
    assert not d._is_in_app_drag()


def _wire(d, clients, pids):
    d._client_ids = lambda: list(clients)
    d._wm_pid = lambda win: pids.get(win.id)
    d.ctrl_dpy = SimpleNamespace(
        create_resource_object=lambda kind, wid: SimpleNamespace(id=wid))


def test_drop_snaps_new_window_from_same_process(make_daemon):
    d, _ = _in_app_daemon(make_daemon)
    _wire(d, [0x100, 0x300, 0x200], {0x100: 42, 0x200: 42, 0x300: 7})
    win = d._await_drop_window({0x100}, 42, SRC)
    assert win.id == 0x200          # 0x300 is another process — ignored


def test_drop_ignores_unrelated_new_window_and_times_out(make_daemon, monkeypatch):
    d, _ = _in_app_daemon(make_daemon)
    _wire(d, [0x100, 0x300], {0x100: 42, 0x300: 7})
    clock = iter(range(0, 1000))
    monkeypatch.setattr(daemon_mod.time, "monotonic", lambda: next(clock))
    assert d._await_drop_window({0x100}, 42, SRC) is None


def test_drop_that_moves_source_snaps_source(make_daemon):
    """Wireframe WMs move the window only on release."""
    d, geom = _in_app_daemon(make_daemon)
    _wire(d, [0x100], {0x100: 42})
    geom["g"] = (500, 500, 800, 600)
    assert d._await_drop_window({0x100}, 42, SRC) is SRC


def test_snap_on_in_app_drag_targets_new_window(make_daemon, monkeypatch):
    d, _ = _in_app_daemon(make_daemon)
    del d._snap                      # use the real _snap
    d._active_window = lambda: SRC
    d._get_work_area = lambda: (0, 0, 1000, 1000)
    snapped = []
    d._apply_zone = lambda win, zone, *box: snapped.append(win.id)
    clients = [0x100]
    d._client_ids = lambda: list(clients)
    d._wm_pid = lambda win: 42
    d.ctrl_dpy = SimpleNamespace(
        create_resource_object=lambda kind, wid: SimpleNamespace(id=wid),
        sync=lambda: None)
    import Xlib.ext.xtest as xtest

    def fake_input(dpy, etype, detail):
        clients.append(0x200)        # the drop creates the torn-off window
    monkeypatch.setattr(xtest, "fake_input", fake_input)

    d._snap(0)
    assert snapped == [0x200]


def test_snap_on_window_move_targets_active_window(make_daemon, monkeypatch):
    d, _ = _in_app_daemon(make_daemon, now=(400, 300, 800, 600))
    del d._snap
    d._active_window = lambda: SRC
    d._get_work_area = lambda: (0, 0, 1000, 1000)
    snapped = []
    d._apply_zone = lambda win, zone, *box: snapped.append(win.id)
    d.ctrl_dpy = SimpleNamespace(sync=lambda: None)
    import Xlib.ext.xtest as xtest
    monkeypatch.setattr(xtest, "fake_input", lambda *a: None)
    d._snap(0)
    assert snapped == [0x100]
