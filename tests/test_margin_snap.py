"""Tests for zone-margin detection, spanning, and the end-to-end snap flow.

MARGIN_PX (10) is the half-width of the merge strip either side of a shared
zone boundary.  The conftest daemon fixture uses a 1000×1000 work area so
1 px == 0.001 in fractional space — easy arithmetic.
"""

import pytest
import Xlib.X as X

from zones import Layout, Zone, MARGIN_PX
from daemon import _State
from conftest import make_event, drain

SCREEN = 1000  # matches the make_daemon fixture's screen_w / screen_h default


# ─── fixtures / shared layouts ────────────────────────────────────────────────

@pytest.fixture
def halves():
    return Layout("halves", [
        Zone(0.0, 0.0, 0.5, 1.0, "left"),
        Zone(0.5, 0.0, 0.5, 1.0, "right"),
    ])


@pytest.fixture
def quad():
    return Layout("quad", [
        Zone(0.0, 0.0, 0.5, 0.5, "tl"),
        Zone(0.5, 0.0, 0.5, 0.5, "tr"),
        Zone(0.0, 0.5, 0.5, 0.5, "bl"),
        Zone(0.5, 0.5, 0.5, 0.5, "br"),
    ])


# ─── margin_at — basic detection ──────────────────────────────────────────────

def test_margin_at_exact_boundary(halves):
    assert halves.margin_at(500, 500, SCREEN, SCREEN) == (0, 1)


def test_margin_at_one_px_left_of_boundary(halves):
    assert halves.margin_at(499, 500, SCREEN, SCREEN) == (0, 1)


def test_margin_at_one_px_right_of_boundary(halves):
    assert halves.margin_at(501, 500, SCREEN, SCREEN) == (0, 1)


def test_margin_at_full_margin_left(halves):
    """MARGIN_PX - 1 inside the left zone is still within the strip."""
    assert halves.margin_at(500 - MARGIN_PX + 1, 500, SCREEN, SCREEN) == (0, 1)


def test_margin_at_full_margin_right(halves):
    assert halves.margin_at(500 + MARGIN_PX - 1, 500, SCREEN, SCREEN) == (0, 1)


def test_margin_at_just_outside_left(halves):
    """One pixel past the margin → no longer in the strip."""
    assert halves.margin_at(500 - MARGIN_PX - 1, 500, SCREEN, SCREEN) is None


def test_margin_at_just_outside_right(halves):
    assert halves.margin_at(500 + MARGIN_PX + 1, 500, SCREEN, SCREEN) is None


def test_margin_at_deep_interior_returns_none(halves):
    assert halves.margin_at(100, 500, SCREEN, SCREEN) is None
    assert halves.margin_at(800, 500, SCREEN, SCREEN) is None


# ─── margin_at — horizontal boundary ──────────────────────────────────────────

def test_margin_at_horizontal_boundary(quad):
    """Cursor near the top/bottom boundary of the left column → zones (0, 2)."""
    assert quad.margin_at(200, 500, SCREEN, SCREEN) == (0, 2)


def test_margin_at_horizontal_boundary_right_column(quad):
    """Cursor near the top/bottom boundary of the right column → zones (1, 3)."""
    assert quad.margin_at(700, 500, SCREEN, SCREEN) == (1, 3)


def test_margin_at_vertical_boundary_top_row(quad):
    """Cursor near the left/right boundary in the top row → zones (0, 1)."""
    assert quad.margin_at(500, 200, SCREEN, SCREEN) == (0, 1)


def test_margin_at_vertical_boundary_bottom_row(quad):
    """Cursor near the left/right boundary in the bottom row → zones (2, 3)."""
    assert quad.margin_at(500, 700, SCREEN, SCREEN) == (2, 3)


# ─── spanning_zone ────────────────────────────────────────────────────────────

def test_spanning_halves_is_full_screen(halves):
    z = halves.spanning_zone(0, 1)
    assert (z.x, z.y, z.w, z.h) == pytest.approx((0.0, 0.0, 1.0, 1.0))


def test_spanning_top_row_is_top_half(quad):
    z = quad.spanning_zone(0, 1)   # tl + tr
    assert (z.x, z.y, z.w, z.h) == pytest.approx((0.0, 0.0, 1.0, 0.5))


def test_spanning_left_column_is_left_half(quad):
    z = quad.spanning_zone(0, 2)   # tl + bl
    assert (z.x, z.y, z.w, z.h) == pytest.approx((0.0, 0.0, 0.5, 1.0))


def test_spanning_is_commutative(halves):
    assert halves.spanning_zone(0, 1).x == halves.spanning_zone(1, 0).x
    assert halves.spanning_zone(0, 1).w == halves.spanning_zone(1, 0).w


# ─── _zone_at integration (daemon level) ──────────────────────────────────────

def test_zone_at_interior_left_returns_index(make_daemon):
    d = make_daemon()
    assert d._zone_at(100, 500) == 0


def test_zone_at_interior_right_returns_index(make_daemon):
    d = make_daemon()
    assert d._zone_at(800, 500) == 1


def test_zone_at_boundary_returns_margin_pair(make_daemon):
    d = make_daemon()
    assert d._zone_at(500, 500) == (0, 1)


def test_zone_at_margin_inside_left_returns_pair(make_daemon):
    d = make_daemon()
    assert d._zone_at(500 - MARGIN_PX + 1, 500) == (0, 1)


def test_zone_at_outside_margin_returns_zone(make_daemon):
    d = make_daemon()
    assert d._zone_at(500 - MARGIN_PX - 1, 500) == 0


# ─── end-to-end snap via _handle ──────────────────────────────────────────────

def test_b3_release_at_margin_passes_tuple_to_snap(make_daemon):
    """B3 released at the margin boundary snaps to the zone pair, not a single zone."""
    d = make_daemon(state=_State.OVERLAY_ACTIVE)
    d._overlay_by_mod = False
    d._b1_held = True
    # x=495 is 5 px from the halves boundary → inside the margin strip.
    d._handle(make_event(X.ButtonRelease, detail=3, root_x=495, root_y=500))
    assert d._snap_calls == [(0, 1)]


def test_motion_into_margin_highlights_pair(make_daemon):
    """Moving into the margin strip changes the highlight to a zone-index tuple."""
    d = make_daemon(state=_State.OVERLAY_ACTIVE)
    d._last_zone = 0
    d._handle(make_event(X.MotionNotify, root_x=495, root_y=500))
    assert d._last_zone == (0, 1)
    assert ("highlight", (0, 1)) in drain(d.ui_queue)


def test_motion_out_of_margin_highlights_zone(make_daemon):
    """Moving from the margin into the interior of a zone highlights that zone."""
    d = make_daemon(state=_State.OVERLAY_ACTIVE)
    d._last_zone = (0, 1)
    d._handle(make_event(X.MotionNotify, root_x=800, root_y=500))
    assert d._last_zone == 1
    assert ("highlight", 1) in drain(d.ui_queue)


def test_modifier_release_at_margin_snaps_to_pair(make_daemon):
    """Modifier released at the margin snaps to the zone pair."""
    d = make_daemon(state=_State.OVERLAY_ACTIVE, mod_snap=True)
    d._overlay_by_mod = True
    d._b1_held = True
    d._handle(make_event(X.KeyRelease, detail=50, root_x=495, root_y=500))
    assert d._snap_calls == [(0, 1)]


def test_highlighted_margin_used_when_release_coords_ambiguous(make_daemon):
    """_last_zone = (0, 1) is used when release coordinates resolve to nothing."""
    d = make_daemon(state=_State.OVERLAY_ACTIVE)
    d._overlay_by_mod = False
    d._b1_held = True
    d._last_zone = (0, 1)
    d._handle(make_event(X.ButtonRelease, detail=3, root_x=99999, root_y=99999))
    assert d._snap_calls == [(0, 1)]
