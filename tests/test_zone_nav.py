"""Tests for Super+Arrow zone navigation.

Covers the pure layout helpers (``zone_for_point`` / ``zone_in_direction``) and
the daemon's monitor-aware move dispatch (``_on_move_key`` with cross-monitor
traversal), exercised through the headless factory in conftest.
"""

import pytest

import Xlib.X as X

from linuxzones.zones import Zone, Layout, MonitorInfo
from conftest import make_event


def _quad() -> Layout:
    return Layout("quad", [
        Zone(0.0, 0.0, 0.5, 0.5, "tl"),   # 0
        Zone(0.5, 0.0, 0.5, 0.5, "tr"),   # 1
        Zone(0.0, 0.5, 0.5, 0.5, "bl"),   # 2
        Zone(0.5, 0.5, 0.5, 0.5, "br"),   # 3
    ])


def _halves() -> Layout:
    return Layout("halves", [
        Zone(0.0, 0.0, 0.5, 1.0, "left"),    # 0
        Zone(0.5, 0.0, 0.5, 1.0, "right"),   # 1
    ])


# --------------------------------------------------------------- zone_for_point

def test_zone_for_point_smallest_area_wins():
    # A small zone nested inside a bigger one is the match at a shared point.
    layout = Layout("nested", [
        Zone(0.0, 0.0, 1.0, 1.0, "big"),
        Zone(0.4, 0.4, 0.2, 0.2, "small"),
    ])
    assert layout.zone_for_point(0.5, 0.5) == 1


def test_zone_for_point_nearest_centre_fallback():
    # Point outside every zone falls back to the nearest centre.
    layout = Layout("two", [
        Zone(0.0, 0.0, 0.2, 0.2, "a"),   # centre (0.1, 0.1)
        Zone(0.8, 0.8, 0.2, 0.2, "b"),   # centre (0.9, 0.9)
    ])
    assert layout.zone_for_point(0.95, 0.95) == 1
    assert layout.zone_for_point(0.05, 0.05) == 0


def test_zone_for_point_empty_layout_is_none():
    assert Layout("empty", []).zone_for_point(0.5, 0.5) is None


# --------------------------------------------------------------- zone_in_direction

@pytest.mark.parametrize("start,direction,expected", [
    (0, "right", 1),   # tl → tr
    (0, "down",  2),   # tl → bl
    (3, "left",  2),   # br → bl
    (3, "up",    1),   # br → tr
    (1, "left",  0),   # tr → tl
    (2, "up",    0),   # bl → tl
])
def test_zone_in_direction_quad(start, direction, expected):
    assert _quad().zone_in_direction(start, direction) == expected


@pytest.mark.parametrize("start,direction", [
    (0, "left"), (0, "up"), (3, "right"), (3, "down"),
])
def test_zone_in_direction_none_at_edge(start, direction):
    assert _quad().zone_in_direction(start, direction) is None


def test_zone_in_direction_prefers_nearest_primary_gap():
    layout = Layout("row", [
        Zone(0.0,  0.0, 0.34, 1.0, "src"),    # 0
        Zone(0.34, 0.0, 0.33, 1.0, "near"),   # 1
        Zone(0.67, 0.0, 0.33, 1.0, "far"),    # 2
    ])
    assert layout.zone_in_direction(0, "right") == 1


def test_zone_in_direction_prefers_perpendicular_overlap_over_list_order():
    # src is a middle-left strip; two right candidates share the same x gap, but
    # only one overlaps src vertically.  Overlap wins regardless of list order.
    layout = Layout("perp", [
        Zone(0.0, 0.4, 0.4, 0.2, "src"),    # 0  y[0.4,0.6]
        Zone(0.5, 0.0, 0.3, 0.2, "no"),     # 1  y[0.0,0.2] — no overlap, listed first
        Zone(0.5, 0.45, 0.3, 0.1, "yes"),   # 2  y[0.45,0.55] — overlaps src
    ])
    assert layout.zone_in_direction(0, "right") == 2


# --------------------------------------------------------------- daemon dispatch

def _move_daemon(make_daemon, *, multi, monitors, layout, geom):
    """Build a daemon wired for _on_move_key and capture _apply_zone calls."""
    d = make_daemon(layout=layout)
    d._multi = multi
    d._monitors = monitors
    d.layout = layout
    d._layouts = {}
    d._monitor_layouts = {}
    d._active_window = lambda: object()
    d._abs_geometry = lambda win: geom
    d._get_work_area = lambda: (0, 0, 1000, 1000)
    calls = []
    d._apply_zone = lambda win, zone, ox, oy, ow, oh: calls.append(
        {"zone": zone, "ox": ox, "oy": oy, "ow": ow, "oh": oh})
    return d, calls


def test_on_move_key_same_monitor(make_daemon):
    # Window centred in the left half; Super+Right → right half, same origin.
    d, calls = _move_daemon(
        make_daemon, multi=False, monitors=[], layout=_halves(),
        geom=(100, 400, 200, 200))   # centre (200, 500)
    d._on_move_key("right")
    assert len(calls) == 1
    assert calls[0]["zone"].x == pytest.approx(0.5)   # right zone
    assert calls[0]["ox"] == 0


def test_on_move_key_no_move_at_edge_single_monitor(make_daemon):
    # Already in the right half on a single monitor → nothing to the right.
    d, calls = _move_daemon(
        make_daemon, multi=False, monitors=[], layout=_halves(),
        geom=(700, 400, 200, 200))   # centre (800, 500) → right zone
    d._on_move_key("right")
    assert calls == []


def test_on_move_key_crosses_to_adjacent_monitor(make_daemon):
    m0 = MonitorInfo("M0", 0, 0, 1000, 1000)
    m1 = MonitorInfo("M1", 1000, 0, 1000, 1000)
    # Window on M0's right zone (centre x ~800); Super+Right hops to M1's left.
    d, calls = _move_daemon(
        make_daemon, multi=True, monitors=[m0, m1], layout=_halves(),
        geom=(700, 400, 200, 200))   # centre (800, 500) on M0
    d._on_move_key("right")
    assert len(calls) == 1
    assert calls[0]["ox"] == 1000               # M1 origin
    assert calls[0]["zone"].x == pytest.approx(0.0)  # M1 left zone


# --------------------------------------------------------------- _handle routing

def _arrow_daemon(make_daemon):
    d = make_daemon()
    d._kbd_move = True
    d._arrow_keycodes = {111: "up"}
    moves = []
    d._on_move_key = lambda direction: moves.append(direction)
    return d, moves


def test_super_arrow_keypress_triggers_move(make_daemon):
    d, moves = _arrow_daemon(make_daemon)
    ev = make_event(X.KeyPress, detail=111, time=11)
    ev.state = d._super_mask          # Super held
    d._handle(ev)
    assert moves == ["up"]


def test_arrow_without_super_is_ignored(make_daemon):
    d, moves = _arrow_daemon(make_daemon)
    ev = make_event(X.KeyPress, detail=111, time=11)
    ev.state = 0
    d._handle(ev)
    assert moves == []


def test_arrow_autorepeat_rejected_by_timestamp(make_daemon):
    d, moves = _arrow_daemon(make_daemon)
    # Real press.
    p1 = make_event(X.KeyPress, detail=111, time=20); p1.state = d._super_mask
    d._handle(p1)
    # Release records its timestamp.
    d._handle(make_event(X.KeyRelease, detail=111, time=30))
    # Auto-repeat press shares the release timestamp → ignored.
    rep = make_event(X.KeyPress, detail=111, time=30); rep.state = d._super_mask
    d._handle(rep)
    # A fresh press with a new timestamp moves again.
    p2 = make_event(X.KeyPress, detail=111, time=31); p2.state = d._super_mask
    d._handle(p2)
    assert moves == ["up", "up"]
