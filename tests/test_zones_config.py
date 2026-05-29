"""Tests for zone sanitisation and config persistence (security fixes P2#5, P1#3)."""

import json
import math

import pytest

import zones
from zones import Zone, Layout, _sanitize_zones, load_config, save_config


# --------------------------------------------------------------- _sanitize_zones

def test_drops_non_finite_coordinates():
    bad = [
        Zone(float("nan"), 0.0, 0.5, 0.5),
        Zone(0.0, float("inf"), 0.5, 0.5),
        Zone(0.0, 0.0, float("-inf"), 0.5),
    ]
    assert _sanitize_zones(bad) == []


def test_drops_non_positive_size():
    assert _sanitize_zones([Zone(0.0, 0.0, 0.0, 0.5)]) == []
    assert _sanitize_zones([Zone(0.0, 0.0, 0.5, -0.2)]) == []


def test_clamps_negative_origin_into_unit_square():
    [z] = _sanitize_zones([Zone(-0.5, -0.5, 0.3, 0.3)])
    assert z.x == 0.0
    assert z.y == 0.0
    assert z.w == pytest.approx(0.3)
    assert z.h == pytest.approx(0.3)


def test_origin_clamped_to_far_edge_loses_size_and_is_dropped():
    # y clamped to 1.0 leaves h = min(0.3, 1.0 - 1.0) = 0.0 → zone dropped.
    assert _sanitize_zones([Zone(0.0, 1.5, 0.3, 0.3)]) == []


def test_zone_overflowing_right_edge_is_clamped_to_fit():
    [z] = _sanitize_zones([Zone(0.8, 0.0, 0.5, 1.0)])
    assert z.x == pytest.approx(0.8)
    assert z.w == pytest.approx(0.2)        # clamped so x + w == 1.0
    assert z.x + z.w <= 1.0 + 1e-9


def test_zone_clamped_to_zero_width_is_dropped():
    # x clamped to 1.0 leaves 1.0 - 1.0 = 0 width → dropped.
    assert _sanitize_zones([Zone(1.5, 0.0, 0.5, 0.5)]) == []


def test_name_coerced_and_truncated():
    [z] = _sanitize_zones([Zone(0.0, 0.0, 0.5, 0.5, "x" * 200)])
    assert isinstance(z.name, str)
    assert len(z.name) == 64


def test_valid_zone_passes_through_unchanged():
    [z] = _sanitize_zones([Zone(0.25, 0.0, 0.5, 1.0, "center")])
    assert (z.x, z.y, z.w, z.h, z.name) == (0.25, 0.0, 0.5, 1.0, "center")


# --------------------------------------------------------------- load_config

def test_load_config_sanitises_hand_edited_file(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "linuxzones"
    cfg_dir.mkdir()
    cfg_file = cfg_dir / "config.json"
    monkeypatch.setattr(zones, "CONFIG_DIR", str(cfg_dir))
    monkeypatch.setattr(zones, "CONFIG_FILE", str(cfg_file))

    cfg_file.write_text(json.dumps({
        "active_layout": "mine",
        "layouts": {
            "mine": {"name": "mine", "zones": [
                {"x": 0.0, "y": 0.0, "w": 0.5, "h": 1.0, "name": "ok"},
                {"x": 0.0, "y": 0.0, "w": float("1e999"), "h": 1.0, "name": "bad"},
            ]},
        },
    }))

    layouts, active, opacity, shift_snap = load_config()
    # The Infinity zone is dropped; the valid one survives.
    assert [z.name for z in layouts["mine"].zones] == ["ok"]
    assert active == "mine"


def test_load_config_missing_file_returns_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(zones, "CONFIG_FILE", str(tmp_path / "nope.json"))
    layouts, active, opacity, shift_snap = load_config()
    assert active == "ultrawide-8-16-8"
    assert opacity == 0.5
    assert shift_snap is False


# --------------------------------------------------------------- save_config (atomic)

def test_save_then_load_round_trip(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "linuxzones"
    cfg_file = cfg_dir / "config.json"
    monkeypatch.setattr(zones, "CONFIG_DIR", str(cfg_dir))
    monkeypatch.setattr(zones, "CONFIG_FILE", str(cfg_file))

    layouts = {"halves": Layout("halves", [
        Zone(0.0, 0.0, 0.5, 1.0, "left"),
        Zone(0.5, 0.0, 0.5, 1.0, "right"),
    ])}
    save_config(layouts, "halves", opacity=0.7, shift_snap=True)

    assert cfg_file.exists()
    layouts2, active, opacity, shift_snap = load_config()
    assert active == "halves"
    assert opacity == pytest.approx(0.7)
    assert shift_snap is True
    assert [z.name for z in layouts2["halves"].zones] == ["left", "right"]


def test_save_leaves_no_temp_files_behind(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "linuxzones"
    cfg_file = cfg_dir / "config.json"
    monkeypatch.setattr(zones, "CONFIG_DIR", str(cfg_dir))
    monkeypatch.setattr(zones, "CONFIG_FILE", str(cfg_file))

    save_config({"halves": Layout("halves", [])}, "halves")

    leftovers = [p.name for p in cfg_dir.iterdir() if p.name != "config.json"]
    assert leftovers == []
