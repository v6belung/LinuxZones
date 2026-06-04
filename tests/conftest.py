"""Shared pytest fixtures for the LinuxZones test suite.

The daemon's real ``__init__`` opens two X11 display connections and interns
atoms, none of which are available (or desirable) in a headless test run.  The
state machine and the RECORD byte-filter, however, are pure Python.  We
therefore build a ``ZoneDaemon`` instance with ``object.__new__`` and populate
only the attributes those code paths touch, so the *actual production methods*
(`_handle`, `_record_callback`) are exercised without an X server.
"""

import queue
import sys
from types import SimpleNamespace

import pytest

from linuxzones.daemon import ZoneDaemon, _State
from linuxzones.zones import Layout, Zone


@pytest.fixture
def halves_layout() -> Layout:
    """Two side-by-side zones: left = x[0.0,0.5), right = x[0.5,1.0)."""
    return Layout("halves", [
        Zone(0.0, 0.0, 0.5, 1.0, "left"),
        Zone(0.5, 0.0, 0.5, 1.0, "right"),
    ])


@pytest.fixture
def make_daemon(halves_layout):
    """Factory that returns a ZoneDaemon with only state-machine attrs set.

    X11-touching helpers (`_managed_window_at`, `_snap`) are replaced with
    inert stubs so `_handle` can run end-to-end; tests that care about them
    override the stub and inspect what was recorded.
    """
    def _factory(*, state=_State.IDLE, mod_snap=False, mod_key="shift",
                 layout=None, screen_w=1000, screen_h=1000):
        d = object.__new__(ZoneDaemon)

        d.layout   = layout if layout is not None else halves_layout
        d.ui_queue = queue.Queue()
        d.screen_w = screen_w
        d.screen_h = screen_h
        d._work_x  = 0
        d._work_y  = 0
        d._work_w  = screen_w
        d._work_h  = screen_h

        # Drag state
        d._state    = state
        d._btn1_x   = 0
        d._btn1_y   = 0
        d._drag_win = None
        d._last_zone = None
        d._b1_held            = False
        d._swallow_b1_release = False

        # Modifier-snap state
        d._mod_snap         = mod_snap
        d._mod_key          = mod_key
        d._mod_held         = False
        d._overlay_by_mod   = False
        d._mod_last_release_time = -1
        d._mod_keycodes     = frozenset({50})   # pretend keycode 50 == the modifier

        # Multi-monitor (disabled in tests — single-monitor path only)
        d._multi           = False
        d._monitors        = []
        d._monitor_layouts = {}
        d._layouts         = {}
        d._current_monitor = None
        d._last_mon_name   = None

        # RECORD context bookkeeping
        d._ctx = None
        d._reconfigure_requested = False

        # Pretend display object for _record_callback's parse call (only its
        # `.display` attribute is read, and only by the binary parser, which
        # tests stub out via monkeypatch).
        d.record_dpy = SimpleNamespace(display=None)

        # Fake control display: keysym_to_keycode is the identity, so distinct
        # keysyms (XK_Shift_L vs XK_Alt_L ...) yield distinct, deterministic
        # "keycodes" for _resolve_mod_keycodes / update_mod_snap tests.  No
        # real X connection is opened.
        d.ctrl_dpy = SimpleNamespace(keysym_to_keycode=lambda ks: ks)

        # Inert stubs for the X11 side effects.  Tests override as needed.
        d._managed_window_at = lambda: SimpleNamespace(id=0xABCDEF)
        d._snap = lambda zone_idx: d.__dict__.setdefault("_snap_calls", []).append(zone_idx)

        return d

    return _factory


def make_event(etype, detail=0, root_x=0, root_y=0, time=0):
    """Build a stand-in for an Xlib event object as `_handle` consumes it.

    ``time`` is the X server timestamp (ms).  Auto-repeat is modelled by giving
    a KeyRelease and its following KeyPress the *same* ``time``.
    """
    return SimpleNamespace(type=etype, detail=detail,
                           root_x=root_x, root_y=root_y, time=time)


def drain(q: queue.Queue):
    """Return all currently-queued UI messages as a list."""
    out = []
    try:
        while True:
            out.append(q.get_nowait())
    except queue.Empty:
        pass
    return out
