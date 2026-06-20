"""Tests for zone sanitisation and config persistence (security fixes P2#5, P1#3)."""

import json
import math

import pytest

import linuxzones.zones as zones
from linuxzones.zones import Zone, Layout, ZonesConfig, _sanitize_zones, load_config, save_config


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

    cfg = load_config()
    # The Infinity zone is dropped; the valid one survives.
    assert [z.name for z in cfg.layouts["mine"].zones] == ["ok"]
    assert cfg.active == "mine"


def test_load_config_missing_file_returns_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(zones, "CONFIG_FILE", str(tmp_path / "nope.json"))
    cfg = load_config()
    assert cfg.active == "ultrawide-8-16-8"
    assert cfg.opacity == 0.5
    assert cfg.mod_snap is False
    assert cfg.mod_key == "shift"


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
    save_config(ZonesConfig(layouts, "halves", opacity=0.7, mod_snap=True, mod_key="alt"))

    assert cfg_file.exists()
    cfg2 = load_config()
    assert cfg2.active == "halves"
    assert cfg2.opacity == pytest.approx(0.7)
    assert cfg2.mod_snap is True
    assert cfg2.mod_key == "alt"
    assert [z.name for z in cfg2.layouts["halves"].zones] == ["left", "right"]


def test_kbd_move_round_trip(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "linuxzones"
    cfg_file = cfg_dir / "config.json"
    monkeypatch.setattr(zones, "CONFIG_DIR", str(cfg_dir))
    monkeypatch.setattr(zones, "CONFIG_FILE", str(cfg_file))

    saved = {"push-tile-left": ["<Super>Left"], "push-tile-right": ["<Super>Right"]}
    save_config(ZonesConfig({"halves": Layout("halves", [])}, "halves",
                            kbd_move=True, kbd_move_saved_bindings=saved))
    cfg2 = load_config()
    assert cfg2.kbd_move is True
    assert cfg2.kbd_move_saved_bindings == saved


def test_kbd_move_defaults_off_when_absent(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch, {})
    cfg = load_config()
    assert cfg.kbd_move is False
    assert cfg.kbd_move_saved_bindings == {}


def test_save_leaves_no_temp_files_behind(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "linuxzones"
    cfg_file = cfg_dir / "config.json"
    monkeypatch.setattr(zones, "CONFIG_DIR", str(cfg_dir))
    monkeypatch.setattr(zones, "CONFIG_FILE", str(cfg_file))

    save_config(ZonesConfig({"halves": Layout("halves", [])}, "halves"))

    leftovers = [p.name for p in cfg_dir.iterdir() if p.name != "config.json"]
    assert leftovers == []


# --------------------------------------------------------------- modifier key / snap

def _write_cfg(tmp_path, monkeypatch, data):
    cfg_dir = tmp_path / "linuxzones"
    cfg_dir.mkdir()
    cfg_file = cfg_dir / "config.json"
    monkeypatch.setattr(zones, "CONFIG_DIR", str(cfg_dir))
    monkeypatch.setattr(zones, "CONFIG_FILE", str(cfg_file))
    base = {"active_layout": "halves",
            "layouts": {"halves": {"name": "halves", "zones": []}}}
    base.update(data)
    cfg_file.write_text(json.dumps(base))
    return cfg_file


def test_coerce_modifier_accepts_valid_and_rejects_garbage():
    from linuxzones.zones import _coerce_modifier
    assert _coerce_modifier("alt") == "alt"
    assert _coerce_modifier("CTRL") == "ctrl"        # case-insensitive
    assert _coerce_modifier("super") == "shift"      # unknown → default
    assert _coerce_modifier(None) == "shift"
    assert _coerce_modifier(42) == "shift"


def test_legacy_shift_snap_true_maps_to_modifier_shift(tmp_path, monkeypatch):
    """Old configs only had a boolean shift_snap; honour it as modifier=shift."""
    _write_cfg(tmp_path, monkeypatch, {"shift_snap": True})
    cfg = load_config()
    assert cfg.mod_snap is True
    assert cfg.mod_key == "shift"


def test_legacy_shift_snap_false_maps_to_disabled(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch, {"shift_snap": False})
    cfg = load_config()
    assert cfg.mod_snap is False
    assert cfg.mod_key == "shift"


def test_new_keys_take_precedence_over_legacy(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch,
               {"shift_snap": True, "modifier_snap": True, "modifier_key": "ctrl"})
    cfg = load_config()
    assert cfg.mod_snap is True
    assert cfg.mod_key == "ctrl"


def test_invalid_modifier_key_in_file_falls_back_to_shift(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch,
               {"modifier_snap": True, "modifier_key": "hyper"})
    cfg = load_config()
    assert cfg.mod_snap is True
    assert cfg.mod_key == "shift"


def test_save_writes_legacy_shift_only_for_shift_modifier(tmp_path, monkeypatch):
    """Downgrade safety: shift_snap mirrors modifier_snap iff modifier is shift."""
    cfg_dir = tmp_path / "linuxzones"
    cfg_file = cfg_dir / "config.json"
    monkeypatch.setattr(zones, "CONFIG_DIR", str(cfg_dir))
    monkeypatch.setattr(zones, "CONFIG_FILE", str(cfg_file))

    save_config(ZonesConfig({"halves": Layout("halves", [])}, "halves",
                             mod_snap=True, mod_key="alt"))
    written = json.loads(cfg_file.read_text())
    assert written["modifier_snap"] is True
    assert written["modifier_key"] == "alt"
    assert written["shift_snap"] is False     # an old binary won't snap on Shift

    save_config(ZonesConfig({"halves": Layout("halves", [])}, "halves",
                             mod_snap=True, mod_key="shift"))
    written = json.loads(cfg_file.read_text())
    assert written["shift_snap"] is True      # old binary still gets Shift snap
