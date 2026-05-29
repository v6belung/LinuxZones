"""Layout-editor tests (ttk GUI).

These drive the real ZoneEditor widget tree.  They need a working Tk; if Tk
cannot initialise (truly headless CI with no display libs) the whole module is
skipped rather than failed.  We never call run() — that blocks on the event
loop — instead we construct the editor as a Toplevel of a hidden root and poke
its methods/vars directly, exactly as user actions would.
"""

from types import SimpleNamespace

import pytest

tk = pytest.importorskip("tkinter")
from tkinter import TclError

from zones import Layout, Zone


@pytest.fixture(scope="session")
def tk_root():
    # A single Tk root for the whole session.  Creating/destroying multiple
    # tk.Tk() instances in one process corrupts Tcl's library lookup on Windows
    # ("invalid command name tcl_findLibrary"), so every editor is a Toplevel
    # of this one hidden root instead.
    try:
        root = tk.Tk()
    except TclError as e:                      # pragma: no cover
        pytest.skip(f"Tk unavailable: {e}")
    root.withdraw()
    yield root
    try:
        root.destroy()
    except TclError:
        pass


@pytest.fixture
def make_editor(tk_root):
    from editor import ZoneEditor

    created = []

    def _factory(*, layouts=None, active="halves", **kwargs):
        if layouts is None:
            layouts = {"halves": Layout("halves", [
                Zone(0.0, 0.0, 0.5, 1.0, "left"),
                Zone(0.5, 0.0, 0.5, 1.0, "right"),
            ])}
        ed = ZoneEditor(layouts, active, 1920, 1080,
                        master=tk_root, **kwargs)
        created.append(ed)
        return ed

    yield _factory

    # Tear down each editor's Toplevel (it may already be gone if _save ran).
    for ed in created:
        try:
            ed.root.destroy()
        except TclError:
            pass


def _combo_state(ed) -> str:
    return str(ed.mod_key_combo.cget("state"))


# --------------------------------------------------------------- modifier UI wiring

def test_default_modifier_unchecked_and_dropdown_disabled(make_editor):
    ed = make_editor()
    assert ed.mod_snap_var.get() is False
    assert _combo_state(ed) == "disabled"


def test_enabled_modifier_dropdown_is_active_and_shows_key(make_editor):
    ed = make_editor(modifier_snap=True, modifier_key="alt")
    assert ed.mod_snap_var.get() is True
    assert _combo_state(ed) == "readonly"
    assert ed.mod_key_var.get() == "Alt"


def test_toggling_checkbox_greys_and_ungreys_dropdown(make_editor):
    ed = make_editor()                         # starts disabled
    ed.mod_snap_var.set(True)
    ed._on_mod_toggle()
    assert _combo_state(ed) == "readonly"
    ed.mod_snap_var.set(False)
    ed._on_mod_toggle()
    assert _combo_state(ed) == "disabled"


def test_dropdown_offers_shift_alt_ctrl(make_editor):
    ed = make_editor(modifier_snap=True)
    values = list(ed.mod_key_combo.cget("values"))
    assert values == ["Shift", "Alt", "Ctrl"]


def test_invalid_initial_modifier_key_defaults_to_shift(make_editor):
    ed = make_editor(modifier_snap=True, modifier_key="hyper")
    assert ed.mod_key_var.get() == "Shift"


# --------------------------------------------------------------- save result shape

def test_save_returns_five_tuple_with_canonical_modifier(make_editor):
    ed = make_editor(modifier_snap=False)
    ed.mod_snap_var.set(True)
    ed.mod_key_var.set("Ctrl")
    ed._save()
    assert ed.result is not None
    layouts, active, opacity, mod_snap, mod_key = ed.result
    assert active == "halves"
    assert mod_snap is True
    assert mod_key == "ctrl"                    # label mapped back to canonical
    assert 0.0 < opacity <= 1.0


def test_save_disabled_modifier(make_editor):
    ed = make_editor(modifier_snap=True, modifier_key="alt")
    ed.mod_snap_var.set(False)
    ed._save()
    _, _, _, mod_snap, mod_key = ed.result
    assert mod_snap is False
    assert mod_key == "alt"                     # remembered even while disabled


# --------------------------------------------------------------- core: zones / layouts

def test_draw_zone_appends_to_layout(make_editor):
    ed = make_editor(layouts={"empty": Layout("empty", [])}, active="empty")
    assert len(ed._layout.zones) == 0

    # Drag a rectangle across the canvas (press in empty space → draw mode).
    ed._on_press(SimpleNamespace(x=10, y=10))
    ed._on_release(SimpleNamespace(x=400, y=300))

    assert len(ed._layout.zones) == 1
    z = ed._layout.zones[0]
    assert 0.0 <= z.x < z.x + z.w <= 1.0
    assert z.w > 0 and z.h > 0


def test_tiny_drag_is_ignored(make_editor):
    ed = make_editor(layouts={"empty": Layout("empty", [])}, active="empty")
    ed._on_press(SimpleNamespace(x=10, y=10))
    ed._on_release(SimpleNamespace(x=13, y=12))    # below the 8px threshold
    assert len(ed._layout.zones) == 0


def test_left_click_selects_existing_zone(make_editor):
    ed = make_editor()
    # Left zone spans canvas x[0, pw/2); click inside it.
    ed._on_press(SimpleNamespace(x=int(ed.pw * 0.25), y=int(ed.ph * 0.5)))
    assert ed._selected == 0


def test_right_click_deletes_zone(make_editor):
    ed = make_editor()
    assert len(ed._layout.zones) == 2
    ed._on_right_click(SimpleNamespace(x=int(ed.pw * 0.75), y=int(ed.ph * 0.5)))
    assert len(ed._layout.zones) == 1
    assert ed._layout.zones[0].name == "left"   # right zone removed


def test_apply_preset_replaces_zones(make_editor):
    ed = make_editor(layouts={"empty": Layout("empty", [])}, active="empty")
    ed._apply_preset("quad")
    assert len(ed._layout.zones) == 4


def test_delete_selected_zone(make_editor):
    ed = make_editor()
    ed._selected = 0
    ed._delete_zone()
    assert len(ed._layout.zones) == 1
    assert ed._selected is None


def test_rename_zone_uses_dialog_value(make_editor, monkeypatch):
    import editor as editor_mod
    ed = make_editor()
    ed._selected = 0
    monkeypatch.setattr(editor_mod.simpledialog, "askstring",
                        lambda *a, **k: "renamed")
    ed._rename_zone()
    assert ed._layout.zones[0].name == "renamed"


def test_new_layout_uses_dialog_value(make_editor, monkeypatch):
    import editor as editor_mod
    ed = make_editor()
    monkeypatch.setattr(editor_mod.simpledialog, "askstring",
                        lambda *a, **k: "fresh")
    ed._new_layout()
    assert "fresh" in ed.layouts
    assert ed.active_layout == "fresh"


def test_save_preserves_layouts_and_active(make_editor):
    layouts = {
        "a": Layout("a", [Zone(0.0, 0.0, 1.0, 1.0, "full")]),
        "b": Layout("b", [Zone(0.0, 0.0, 0.5, 1.0, "half")]),
    }
    ed = make_editor(layouts=layouts, active="b")
    ed._save()
    out_layouts, active, *_ = ed.result
    assert active == "b"
    assert set(out_layouts.keys()) == {"a", "b"}
