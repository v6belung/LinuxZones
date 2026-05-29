"""Shared pytest fixtures for the LinuxZones test suite.

The daemon's real ``__init__`` opens two X11 display connections and interns
atoms, none of which are available (or desirable) in a headless test run.  The
state machine and the RECORD byte-filter, however, are pure Python.  We
therefore build a ``ZoneDaemon`` instance with ``object.__new__`` and populate
only the attributes those code paths touch, so the *actual production methods*
(`_handle`, `_record_callback`) are exercised without an X server.
"""

import os
import queue
import sys
from types import SimpleNamespace

import pytest

# Make the project modules importable when pytest is run from anywhere.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from daemon import ZoneDaemon, _State          # noqa: E402
from zones import Layout, Zone                  # noqa: E402


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
    def _factory(*, state=_State.IDLE, shift_snap=False, layout=None,
                 screen_w=1000, screen_h=1000):
        d = object.__new__(ZoneDaemon)

        d.layout   = layout if layout is not None else halves_layout
        d.ui_queue = queue.Queue()
        d.screen_w = screen_w
        d.screen_h = screen_h

        # Drag state
        d._state    = state
        d._btn1_x   = 0
        d._btn1_y   = 0
        d._drag_win = None
        d._last_zone = None
        d._b1_held            = False
        d._swallow_b1_release = False

        # Shift-snap state
        d._shift_snap         = shift_snap
        d._shift_held         = False
        d._overlay_by_shift   = False
        d._shift_last_release = 0.0
        d._shift_keycodes     = frozenset({50})   # pretend keycode 50 == Shift

        # RECORD context bookkeeping
        d._ctx = None
        d._reconfigure_requested = False

        # Pretend display object for _record_callback's parse call (only its
        # `.display` attribute is read, and only by the binary parser, which
        # tests stub out via monkeypatch).
        d.record_dpy = SimpleNamespace(display=None)

        # Inert stubs for the X11 side effects.  Tests override as needed.
        d._managed_window_at = lambda: SimpleNamespace(id=0xABCDEF)
        d._snap = lambda zone_idx: d.__dict__.setdefault("_snap_calls", []).append(zone_idx)

        return d

    return _factory


def make_event(etype, detail=0, root_x=0, root_y=0):
    """Build a stand-in for an Xlib event object as `_handle` consumes it."""
    return SimpleNamespace(type=etype, detail=detail, root_x=root_x, root_y=root_y)


def drain(q: queue.Queue):
    """Return all currently-queued UI messages as a list."""
    out = []
    try:
        while True:
            out.append(q.get_nowait())
    except queue.Empty:
        pass
    return out
