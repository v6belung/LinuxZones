"""Tests for overlapping-zone hit-testing.

When zones overlap, the smallest-area zone containing the point wins —
both for Layout.zone_at (daemon hover/snap) and the editor's
_zone_at_canvas (click-to-select). This keeps a small zone nested inside
a larger one reachable, regardless of which was created first.
"""

import pytest
import Xlib.X as X

from linuxzones.zones import Layout, Zone, label_anchor
from linuxzones.daemon import _State
from conftest import make_event, drain

SCREEN = 1000


@pytest.fixture
def nested_layout():
    """A full-screen zone with a small zone nested in its centre."""
    return Layout("nested", [
        Zone(0.0, 0.0, 1.0, 1.0, "big"),
        Zone(0.4, 0.4, 0.2, 0.2, "small"),
    ])


@pytest.fixture
def nested_layout_reverse_order():
    """Same geometry as nested_layout, but the small zone is created first."""
    return Layout("nested", [
        Zone(0.4, 0.4, 0.2, 0.2, "small"),
        Zone(0.0, 0.0, 1.0, 1.0, "big"),
    ])


# ─── Layout.zone_at ────────────────────────────────────────────────────────

def test_zone_at_picks_smallest_zone_inside_overlap(nested_layout):
    # Point inside the nested small zone, which is also inside the big zone.
    assert nested_layout.zone_at(500, 500, SCREEN, SCREEN) == 1


def test_zone_at_smallest_wins_regardless_of_creation_order(nested_layout_reverse_order):
    # Same point, but the small zone is now index 0 — still wins.
    assert nested_layout_reverse_order.zone_at(500, 500, SCREEN, SCREEN) == 0


def test_zone_at_outside_small_zone_returns_big_zone(nested_layout):
    # Point inside the big zone only.
    assert nested_layout.zone_at(10, 10, SCREEN, SCREEN) == 0


def test_zone_at_equal_area_ties_fall_back_to_list_order():
    layout = Layout("tie", [
        Zone(0.0, 0.0, 0.5, 0.5, "a"),
        Zone(0.0, 0.0, 0.5, 0.5, "b"),
    ])
    assert layout.zone_at(100, 100, SCREEN, SCREEN) == 0


def test_zone_at_outside_all_zones_returns_none(nested_layout):
    layout = Layout("partial", [Zone(0.0, 0.0, 0.5, 0.5, "tl")])
    assert layout.zone_at(900, 900, SCREEN, SCREEN) is None


# ─── daemon hover highlight via _zone_at ───────────────────────────────────

# ─── label_anchor ───────────────────────────────────────────────────────────

def test_label_anchor_no_overlap_is_center():
    zones = [Zone(0.0, 0.0, 0.5, 1.0, "left")]
    assert label_anchor(zones[0], zones) == (0.25, 0.5)


def test_label_anchor_avoids_smaller_zone_spanning_bottom_half():
    # "right large" spans the full right column; "right low" overlaps its
    # bottom half and fully spans its width — label should move to the
    # remaining top half.
    big   = Zone(0.75, 0.0, 0.25, 1.0, "right large")
    small = Zone(0.75, 0.5, 0.25, 0.5, "right low")
    zones = [small, big]
    assert label_anchor(big, zones) == (0.875, 0.25)
    # The smaller zone itself is unaffected (no smaller zone overlaps it).
    assert label_anchor(small, zones) == (0.875, 0.75)


def test_label_anchor_avoids_smaller_zone_spanning_right_half():
    big   = Zone(0.0, 0.0, 1.0, 0.5, "top")
    small = Zone(0.5, 0.0, 0.5, 0.5, "top right")
    zones = [small, big]
    assert label_anchor(big, zones) == (0.25, 0.25)


def test_label_anchor_falls_back_to_center_when_fully_covered():
    big   = Zone(0.0, 0.0, 1.0, 1.0, "big")
    small = Zone(0.0, 0.0, 1.0, 1.0, "small-duplicate-size")
    # Equal area -> not considered "smaller", so big keeps its own center.
    assert label_anchor(big, [big, small]) == (0.5, 0.5)


def test_daemon_highlight_uses_smallest_overlapping_zone(make_daemon, nested_layout):
    d = make_daemon(state=_State.OVERLAY_ACTIVE, layout=nested_layout)
    d._last_zone = 0  # currently highlighting "big"

    d._handle(make_event(X.MotionNotify, root_x=500, root_y=500))  # inside "small" too

    assert d._last_zone == 1
    assert ("highlight", 1, None) in drain(d.ui_queue)
