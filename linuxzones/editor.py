"""Zone layout editor — draw zones by click-dragging on a scaled screen preview.

Uses ttk widgets throughout so the dialog inherits the desktop's native theme.
The only non-system colours are inside the zone-preview canvas (dark background
and brightly coloured zone rectangles — purely visual, not UI chrome).

run() returns a ZonesConfig on save, or None on cancel.
"""

import copy
import tkinter as tk
from tkinter import ttk, simpledialog, messagebox
from typing import Dict, List, Optional, Tuple

from .zones import Zone, Layout, ZonesConfig, MonitorInfo, DEFAULT_LAYOUTS, VALID_MODIFIERS, _coerce_modifier, label_anchor

GRID     = 0.05   # snap-to-grid step (5 % of screen)
EDGE_TOL = 7      # pixels — hit zone for edge-resize detection

# Canonical modifier name (as stored in config) ↔ human-friendly dropdown label.
_MOD_LABELS = {m: m.capitalize() for m in VALID_MODIFIERS}   # "shift" → "Shift"
_LABEL_TO_MOD = {label: m for m, label in _MOD_LABELS.items()}

# Colours used only inside the zone-preview canvas
_ZONE_COLORS = ["#4a90d9", "#7b68ee", "#48c774", "#ff9f43", "#ff6b35", "#e84393"]
_CANVAS_BG   = "#1a1a2e"


def _snap_val(v: float) -> float:
    return round(v / GRID) * GRID


class ZoneEditor:
    """Zone layout editor dialog.

    Parameters
    ----------
    layouts, active_layout, screen_w/h : as usual
    opacity        : current overlay opacity (0.0–1.0)
    modifier_snap  : whether keyboard-modifier snap is currently enabled
    modifier_key   : which modifier triggers it ("shift", "alt" or "ctrl")
    master         : parent tk.Tk for embedded Toplevel mode;
                     None = standalone (creates its own Tk root)

    run() returns a ZonesConfig on save, or None on cancel.
    """

    def __init__(
        self,
        layouts: Dict[str, Layout],
        active_layout: str,
        screen_w: int,
        screen_h: int,
        opacity: float = 0.5,
        modifier_snap: bool = False,
        modifier_key: str = "shift",
        master: Optional[tk.Tk] = None,
        monitors: Optional[List[MonitorInfo]] = None,
        monitor_layouts: Optional[Dict[str, str]] = None,
    ):
        self.layouts       = copy.deepcopy(layouts)
        self.active_layout = active_layout
        self.screen_w      = screen_w
        self.screen_h      = screen_h
        self.result: Optional[ZonesConfig] = None

        # Multi-monitor
        self._monitors:        List[MonitorInfo] = monitors or []
        self._monitor_layouts: Dict[str, str]    = dict(monitor_layouts or {})
        self._multi = bool(self._monitors and len(self._monitors) > 1)

        # Currently selected monitor in the dropdown ("" = shared / all)
        self._sel_monitor: str = ""

        # Scale preview canvas to ≤800 px wide, preserving aspect ratio
        self.pw = 800
        self.ph = int(800 * screen_h / screen_w)

        self._drawing    = False
        self._draw_start: Optional[Tuple[int, int]] = None
        self._selected:   Optional[int]             = None
        self._layout_names: List[str]               = []

        self._resizing:     bool          = False
        self._resize_zone:  Optional[int] = None
        self._resize_edges: set           = set()

        if master is None:
            self.root      = tk.Tk()
            self._toplevel = False
        else:
            self.root      = tk.Toplevel(master)
            self._toplevel = True

        self.root.title("LinuxZones — Layout Editor")
        self.root.resizable(False, False)

        self.opacity_var  = tk.IntVar(value=max(10, min(90, int(opacity * 100))))
        self.mod_snap_var = tk.BooleanVar(value=bool(modifier_snap))
        self.mod_key_var  = tk.StringVar(
            value=_MOD_LABELS[_coerce_modifier(modifier_key)])

        self._build()
        self._refresh_list()
        self._update_info()
        self._redraw()

    # ── UI construction ───────────────────────────────────────────────────────────

    def _build(self) -> None:
        main = ttk.Frame(self.root, padding=8)
        main.pack(fill="both", expand=True)

        # ── Monitor selector (above canvas, only when 2+ monitors) ───────
        if self._multi:
            mon_row = ttk.Frame(main)
            mon_row.pack(fill="x", pady=(0, 6))
            ttk.Label(mon_row, text="Monitor:").pack(side="left", padx=(0, 6))
            _all_label = "All monitors (shared layout)"
            mon_choices = [_all_label] + [
                f"{m.name}  {m.w}×{m.h}" for m in self._monitors
            ]
            self._mon_var = tk.StringVar(value=_all_label)
            self._mon_combo = ttk.Combobox(
                mon_row,
                textvariable=self._mon_var,
                values=mon_choices,
                state="readonly",
                width=32,
            )
            self._mon_combo.pack(side="left")
            self._mon_combo.bind("<<ComboboxSelected>>", self._on_monitor_select)

        # ── Canvas ────────────────────────────────────────────────────────
        ttk.Label(
            main,
            text="Draw: click-drag  ·  Select: left-click"
                 "  ·  Delete: right-click  ·  Drag edge: resize",
        ).pack(pady=(0, 4))

        self.canvas = tk.Canvas(
            main, width=self.pw, height=self.ph,
            bg=_CANVAS_BG,
            highlightthickness=1, highlightbackground="gray",
            cursor="crosshair",
        )
        self.canvas.pack()
        self.canvas.bind("<ButtonPress-1>",   self._on_press)
        self.canvas.bind("<B1-Motion>",       self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<ButtonPress-3>",   self._on_right_click)
        self.canvas.bind("<Motion>",          self._on_motion)

        # ── Three-column panel below canvas ───────────────────────────────
        ttk.Separator(main, orient="horizontal").pack(fill="x", pady=(8, 0))
        bottom = ttk.Frame(main)
        bottom.pack(fill="both", expand=True, pady=(8, 0))

        # COL 1 — Layouts ─────────────────────────────────────────────────
        col1 = ttk.Frame(bottom)
        col1.pack(side="left", fill="both", expand=True)

        ttk.Label(col1, text="Layouts").pack(anchor="w")
        ttk.Separator(col1, orient="horizontal").pack(fill="x", pady=(2, 4))

        lb_wrap = ttk.Frame(col1)
        lb_wrap.pack(fill="both", expand=True)
        scrollbar = ttk.Scrollbar(lb_wrap, orient="vertical")
        self.lb = tk.Listbox(
            lb_wrap, height=5,
            yscrollcommand=scrollbar.set,
            exportselection=False, activestyle="none",
        )
        scrollbar.config(command=self.lb.yview)
        scrollbar.pack(side="right", fill="y")
        self.lb.pack(side="left", fill="both", expand=True)
        self.lb.bind("<<ListboxSelect>>", self._on_layout_select)
        self.lb.bind("<Double-Button-1>", lambda _: self._rename_layout())

        r1 = ttk.Frame(col1)
        r1.pack(fill="x", pady=(4, 0))
        ttk.Button(r1, text="New",    command=self._new_layout   ).pack(side="left", expand=True, fill="x", padx=(0, 2))
        ttk.Button(r1, text="Rename", command=self._rename_layout).pack(side="left", expand=True, fill="x")

        r2 = ttk.Frame(col1)
        r2.pack(fill="x", pady=(2, 0))
        ttk.Button(r2, text="Duplicate", command=self._dup_layout   ).pack(side="left", expand=True, fill="x", padx=(0, 2))
        ttk.Button(r2, text="Delete",    command=self._delete_layout).pack(side="left", expand=True, fill="x")

        ttk.Separator(bottom, orient="vertical").pack(side="left", fill="y", padx=10)

        # COL 2 — Selected Zone + Presets ─────────────────────────────────
        col2 = ttk.Frame(bottom)
        col2.pack(side="left", fill="both", expand=True)

        ttk.Label(col2, text="Selected Zone").pack(anchor="w")
        ttk.Separator(col2, orient="horizontal").pack(fill="x", pady=(2, 4))

        self.zone_var = tk.StringVar(value="Click a zone to select it")
        ttk.Label(
            col2, textvariable=self.zone_var,
            wraplength=200, justify="left",
            font="TkFixedFont",
        ).pack(anchor="w")

        zb = ttk.Frame(col2)
        zb.pack(fill="x", pady=(4, 0))
        ttk.Button(zb, text="Rename Zone", command=self._rename_zone).pack(side="left", expand=True, fill="x", padx=(0, 2))
        ttk.Button(zb, text="Delete Zone", command=self._delete_zone).pack(side="left", expand=True, fill="x")

        ttk.Separator(col2, orient="horizontal").pack(fill="x", pady=(8, 4))
        ttk.Label(col2, text="Presets").pack(anchor="w")
        ttk.Separator(col2, orient="horizontal").pack(fill="x", pady=(2, 4))

        for preset in DEFAULT_LAYOUTS:
            ttk.Button(
                col2, text=preset,
                command=lambda p=preset: self._apply_preset(p),
            ).pack(fill="x", pady=1)

        ttk.Separator(bottom, orient="vertical").pack(side="left", fill="y", padx=10)

        # COL 3 — Settings ────────────────────────────────────────────────
        col3 = ttk.Frame(bottom)
        col3.pack(side="left", fill="both", expand=True)

        ttk.Label(col3, text="Settings").pack(anchor="w")
        ttk.Separator(col3, orient="horizontal").pack(fill="x", pady=(2, 6))

        ttk.Checkbutton(
            col3,
            text="Keyboard modifier snap",
            variable=self.mod_snap_var,
            command=self._on_mod_toggle,
        ).pack(anchor="w")
        ttk.Label(
            col3,
            text="Hold the chosen key while dragging to\nsnap (alternative to right-click)",
            justify="left",
        ).pack(anchor="w", padx=(20, 0), pady=(0, 2))

        mod_row = ttk.Frame(col3)
        mod_row.pack(anchor="w", padx=(20, 0), pady=(0, 2))
        ttk.Label(mod_row, text="Modifier:").pack(side="left", padx=(0, 4))
        self.mod_key_combo = ttk.Combobox(
            mod_row,
            textvariable=self.mod_key_var,
            values=[_MOD_LABELS[m] for m in VALID_MODIFIERS],
            state="readonly",
            width=8,
        )
        self.mod_key_combo.pack(side="left")

        ttk.Label(
            col3,
            text="Privacy: enabling this makes LinuxZones\n"
                 "monitor key presses globally so it can\n"
                 "detect the modifier. Keystrokes are never\n"
                 "stored or sent anywhere. Leave off to\n"
                 "monitor mouse buttons only.",
            justify="left",
            foreground="#888888",
        ).pack(anchor="w", padx=(20, 0), pady=(0, 6))

        ttk.Separator(col3, orient="horizontal").pack(fill="x", pady=(0, 6))
        ttk.Label(col3, text="Overlay Opacity").pack(anchor="w")

        op_row = ttk.Frame(col3)
        op_row.pack(fill="x", pady=(2, 0))
        self._opacity_lbl = ttk.Label(
            op_row, text=f"{self.opacity_var.get()}%", width=4, anchor="e",
        )
        self._opacity_lbl.pack(side="right")
        ttk.Scale(
            op_row,
            from_=10, to=90, orient="horizontal",
            variable=self.opacity_var,
            command=self._on_opacity,
        ).pack(side="left", fill="x", expand=True, padx=(0, 4))

        ttk.Label(
            col3,
            text="how visible the zone overlay is during snapping",
            justify="left",
        ).pack(anchor="w", pady=(2, 0))

        self._on_mod_toggle()

        # ── Action bar ────────────────────────────────────────────────────
        ttk.Separator(self.root, orient="horizontal").pack(fill="x")
        bar = ttk.Frame(self.root, padding=(8, 6))
        bar.pack(fill="x")
        ttk.Button(bar, text="Save & Close", command=self._save       ).pack(side="right")
        ttk.Button(bar, text="Cancel",       command=self.root.destroy).pack(side="right", padx=(0, 4))

    # ── Opacity ───────────────────────────────────────────────────────────────

    def _on_opacity(self, _=None) -> None:
        # ttk.Scale command callback passes the value as a string; use the
        # IntVar instead for a clean integer display.
        self._opacity_lbl.config(text=f"{self.opacity_var.get()}%")

    # ── Monitor selection ─────────────────────────────────────────────────────

    def _on_monitor_select(self, _=None) -> None:
        label = self._mon_var.get()
        if label.startswith("All"):
            self._sel_monitor = ""
        else:
            # Label is "NAME  WxH" — extract the name (text before the spaces)
            self._sel_monitor = label.split()[0]
            # Resize canvas to this monitor's aspect ratio
            mon = next((m for m in self._monitors if m.name == self._sel_monitor), None)
            if mon:
                self.pw = 800
                self.ph = int(800 * mon.h / mon.w)
                self.canvas.config(width=self.pw, height=self.ph)
        self._selected = None
        self._refresh_list()
        self._update_info()
        self._redraw()

    def _active_layout_for_sel(self) -> str:
        """The layout name that is currently 'active' for the selected monitor."""
        if self._sel_monitor:
            return self._monitor_layouts.get(self._sel_monitor, self.active_layout)
        return self.active_layout

    # ── Modifier snap ───────────────────────────────────────────────────────────

    def _on_mod_toggle(self) -> None:
        """Grey out the modifier dropdown while modifier snap is disabled."""
        # "readonly" = pick-from-list enabled; "disabled" = greyed out.
        self.mod_key_combo.config(
            state="readonly" if self.mod_snap_var.get() else "disabled")

    # ── Layout list ───────────────────────────────────────────────────────────

    def _refresh_list(self) -> None:
        """Repopulate the listbox; mark the layout active for the selected monitor."""
        self.lb.delete(0, "end")
        self._layout_names = list(self.layouts.keys())
        effective = self._active_layout_for_sel()
        for name in self._layout_names:
            prefix = "● " if name == effective else "  "
            self.lb.insert("end", prefix + name)
        # Select (highlight) the effective layout row
        if effective in self._layout_names:
            idx = self._layout_names.index(effective)
            self.lb.selection_set(idx)
            self.lb.see(idx)

    @property
    def _layout(self) -> Layout:
        return self.layouts[self._active_layout_for_sel()]

    def _on_layout_select(self, _=None) -> None:
        sel = self.lb.curselection()
        if not sel:
            return
        idx = sel[0]
        if not (0 <= idx < len(self._layout_names)):
            return
        name = self._layout_names[idx]
        if self._sel_monitor:
            # Assign this layout to the selected monitor
            self._monitor_layouts[self._sel_monitor] = name
        else:
            self.active_layout = name
        self._selected = None
        self._refresh_list()
        self._update_info()
        self._redraw()

    # ── Canvas interactions ───────────────────────────────────────────────────

    def _frac(self, cx: int, cy: int) -> Tuple[float, float]:
        return (
            _snap_val(max(0.0, min(1.0, cx / self.pw))),
            _snap_val(max(0.0, min(1.0, cy / self.ph))),
        )

    def _zone_at_canvas(self, cx: int, cy: int) -> Optional[int]:
        """Return the index of the smallest zone containing the point.

        Mirrors Layout.zone_at: when zones overlap, the smallest-area zone
        wins so a nested zone stays selectable.
        """
        fx, fy = cx / self.pw, cy / self.ph
        best: Optional[int] = None
        for i, z in enumerate(self._layout.zones):
            if z.contains(fx, fy):
                if best is None or z.area() < self._layout.zones[best].area():
                    best = i
        return best

    def _edge_at_canvas(self, cx: int, cy: int) -> Optional[Tuple[int, set]]:
        """Return (zone_idx, edge_set) when cx,cy is within EDGE_TOL px of a zone edge."""
        for i, z in enumerate(self._layout.zones):
            zx = int(z.x * self.pw)
            zy = int(z.y * self.ph)
            zw = int(z.w * self.pw)
            zh = int(z.h * self.ph)
            if not (zx - EDGE_TOL <= cx <= zx + zw + EDGE_TOL and
                    zy - EDGE_TOL <= cy <= zy + zh + EDGE_TOL):
                continue
            edges: set = set()
            if abs(cx - zx)        <= EDGE_TOL: edges.add("left")
            if abs(cx - (zx + zw)) <= EDGE_TOL: edges.add("right")
            if abs(cy - zy)        <= EDGE_TOL: edges.add("top")
            if abs(cy - (zy + zh)) <= EDGE_TOL: edges.add("bottom")
            if edges:
                return i, edges
        return None

    def _cursor_for_edges(self, edges: set) -> str:
        h = "left" in edges or "right" in edges
        v = "top"  in edges or "bottom" in edges
        if h and v:
            tl = ("left" in edges) == ("top" in edges)   # TL or BR corner
            return "top_left_corner" if tl else "top_right_corner"
        return "sb_h_double_arrow" if h else "sb_v_double_arrow"

    def _on_motion(self, e) -> None:
        """Update cursor to a resize arrow when hovering over a zone edge."""
        if self._drawing or self._resizing:
            return
        hit = self._edge_at_canvas(e.x, e.y)
        self.canvas.config(
            cursor=self._cursor_for_edges(hit[1]) if hit else "crosshair"
        )

    def _apply_resize(self, cx: int, cy: int) -> None:
        z  = self._layout.zones[self._resize_zone]
        fx = _snap_val(max(0.0, min(1.0, cx / self.pw)))
        fy = _snap_val(max(0.0, min(1.0, cy / self.ph)))
        if "left" in self._resize_edges:
            old_right = z.x + z.w
            z.x = max(0.0, min(fx, old_right - GRID))
            z.w = old_right - z.x
        if "right" in self._resize_edges:
            z.w = max(GRID, min(fx - z.x, 1.0 - z.x))
        if "top" in self._resize_edges:
            old_bottom = z.y + z.h
            z.y = max(0.0, min(fy, old_bottom - GRID))
            z.h = old_bottom - z.y
        if "bottom" in self._resize_edges:
            z.h = max(GRID, min(fy - z.y, 1.0 - z.y))
        self._update_info()
        self._redraw()

    def _on_press(self, e) -> None:
        # Edge resize takes priority over selection and drawing.
        hit = self._edge_at_canvas(e.x, e.y)
        if hit is not None:
            self._resizing, self._resize_zone, self._resize_edges = True, hit[0], hit[1]
            self._selected = hit[0]
            self._update_info()
            self._redraw()
            return
        idx = self._zone_at_canvas(e.x, e.y)
        if idx is not None:
            self._selected = idx
            self._drawing  = False
            self._update_info()
            self._redraw()
        else:
            self._drawing    = True
            self._draw_start = (e.x, e.y)
            self._selected   = None
            self._update_info()

    def _on_drag(self, e) -> None:
        if self._resizing and self._resize_zone is not None:
            self._apply_resize(e.x, e.y)
            return
        if self._drawing and self._draw_start:
            self._redraw()
            x0, y0 = self._draw_start
            self.canvas.create_rectangle(
                min(x0, e.x), min(y0, e.y),
                max(x0, e.x), max(y0, e.y),
                outline="#ff6b35", width=2, dash=(4, 2),
            )

    def _on_release(self, e) -> None:
        if self._resizing:
            self._resizing = False
            self._resize_zone = None
            self._resize_edges = set()
            self.canvas.config(cursor="crosshair")
            return
        if not self._drawing or not self._draw_start:
            return
        self._drawing = False
        x0, y0 = self._draw_start
        self._draw_start = None
        if abs(e.x - x0) < 8 or abs(e.y - y0) < 8:
            return   # too small — ignore accidental clicks
        fx0, fy0 = self._frac(min(x0, e.x), min(y0, e.y))
        fx1, fy1 = self._frac(max(x0, e.x), max(y0, e.y))
        zone = Zone(fx0, fy0, fx1 - fx0, fy1 - fy0, "")
        self._layout.zones.append(zone)
        self._selected = len(self._layout.zones) - 1
        self._update_info()
        self._redraw()

    def _on_right_click(self, e) -> None:
        idx = self._zone_at_canvas(e.x, e.y)
        if idx is not None:
            self._layout.zones.pop(idx)
            if self._selected == idx:
                self._selected = None
            elif self._selected is not None and self._selected > idx:
                self._selected -= 1
            self._update_info()
            self._redraw()

    # ── Canvas drawing ────────────────────────────────────────────────────────

    def _redraw(self) -> None:
        self.canvas.delete("all")
        self.canvas.create_rectangle(
            1, 1, self.pw - 1, self.ph - 1,
            outline="#3a3a3a", width=1,
        )
        # Largest zones first so smaller, overlapping zones are drawn on top.
        for i, z in sorted(enumerate(self._layout.zones), key=lambda iz: -iz[1].area()):
            x  = int(z.x * self.pw)
            y  = int(z.y * self.ph)
            w  = int(z.w * self.pw)
            h  = int(z.h * self.ph)
            sel   = (i == self._selected)
            color = _ZONE_COLORS[i % len(_ZONE_COLORS)]
            pad   = 2

            self.canvas.create_rectangle(
                x + pad, y + pad, x + w - pad, y + h - pad,
                fill=color,
                outline="#ffffff" if sel else "#aaaaaa",
                width=3 if sel else 1,
                stipple="gray50",
            )
            label = z.name or str(i + 1)
            pct   = f"{z.w * 100:.0f}% × {z.h * 100:.0f}%"
            lfx, lfy = label_anchor(z, self._layout.zones)
            mid_x = int(lfx * self.pw)
            mid_y = int(lfy * self.ph)
            self.canvas.create_text(
                mid_x, mid_y - 9,
                text=label, fill="white",
                font=("sans-serif", 10, "bold"),
            )
            self.canvas.create_text(
                mid_x, mid_y + 9,
                text=pct, fill="#cccccc",
                font=("sans-serif", 8),
            )

    # ── Zone info panel ───────────────────────────────────────────────────────

    def _update_info(self) -> None:
        if self._selected is None or self._selected >= len(self._layout.zones):
            self.zone_var.set("Click a zone to select it")
            return
        z = self._layout.zones[self._selected]
        mon = next((m for m in self._monitors if m.name == self._sel_monitor), None)
        ref_w = mon.w if mon else self.screen_w
        ref_h = mon.h if mon else self.screen_h
        px_w = int(z.w * ref_w)
        px_h = int(z.h * ref_h)
        name_part = f"  ·  {z.name}" if z.name else ""
        self.zone_var.set(
            f"Zone {self._selected + 1}{name_part}\n"
            f"x={z.x * 100:.0f}%  y={z.y * 100:.0f}%\n"
            f"w={z.w * 100:.0f}%  h={z.h * 100:.0f}%\n"
            f"{px_w} × {px_h} px"
        )

    # ── Layout actions ────────────────────────────────────────────────────────

    def _new_layout(self) -> None:
        name = simpledialog.askstring("New Layout", "Layout name:", parent=self.root)
        if not name:
            return
        if name in self.layouts:
            messagebox.showerror("Name taken",
                                 f"A layout named '{name}' already exists.",
                                 parent=self.root)
            return
        self.layouts[name] = Layout(name, [])
        self.active_layout = name
        self._selected = None
        self._refresh_list()
        self._update_info()
        self._redraw()

    def _dup_layout(self) -> None:
        name = simpledialog.askstring(
            "Duplicate Layout", "New layout name:", parent=self.root,
            initialvalue=self.active_layout + "-copy",
        )
        if not name or name == self.active_layout:
            return
        if name in self.layouts:
            messagebox.showerror("Name taken",
                                 f"A layout named '{name}' already exists.",
                                 parent=self.root)
            return
        self.layouts[name] = copy.deepcopy(self._layout)
        self.layouts[name].name = name
        self.active_layout = name
        self._refresh_list()
        self._redraw()

    def _rename_layout(self) -> None:
        name = simpledialog.askstring(
            "Rename Layout", "New name:", parent=self.root,
            initialvalue=self.active_layout,
        )
        if not name or name == self.active_layout:
            return
        if name in self.layouts:
            messagebox.showerror("Name taken",
                                 f"A layout named '{name}' already exists.",
                                 parent=self.root)
            return
        # Rebuild dict preserving insertion order with the key swapped in place
        new_layouts: Dict[str, Layout] = {}
        for k, v in self.layouts.items():
            if k == self.active_layout:
                v.name = name
                new_layouts[name] = v
            else:
                new_layouts[k] = v
        self.layouts = new_layouts
        self.active_layout = name
        self._refresh_list()

    def _delete_layout(self) -> None:
        if len(self.layouts) <= 1:
            messagebox.showwarning("Cannot delete",
                                   "At least one layout must remain.",
                                   parent=self.root)
            return
        if not messagebox.askyesno(
            "Delete layout",
            f"Delete '{self.active_layout}'?\nThis cannot be undone.",
            parent=self.root,
        ):
            return
        del self.layouts[self.active_layout]
        self.active_layout = next(iter(self.layouts))
        self._selected = None
        self._refresh_list()
        self._update_info()
        self._redraw()

    # ── Zone actions ──────────────────────────────────────────────────────────

    def _rename_zone(self) -> None:
        if self._selected is None or self._selected >= len(self._layout.zones):
            return
        z    = self._layout.zones[self._selected]
        name = simpledialog.askstring(
            "Rename Zone", "Zone name:", parent=self.root,
            initialvalue=z.name,
        )
        if name is not None:   # None = user cancelled
            z.name = name
            self._update_info()
            self._redraw()

    def _delete_zone(self) -> None:
        if self._selected is not None and self._selected < len(self._layout.zones):
            self._layout.zones.pop(self._selected)
        self._selected = None
        self._update_info()
        self._redraw()

    def _apply_preset(self, preset: str) -> None:
        if preset in DEFAULT_LAYOUTS:
            self._layout.zones = copy.deepcopy(DEFAULT_LAYOUTS[preset].zones)
        self._selected = None
        self._update_info()
        self._redraw()

    # ── Save / run ────────────────────────────────────────────────────────────

    def _save(self) -> None:
        self.result = ZonesConfig(
            layouts         = self.layouts,
            active          = self.active_layout,
            opacity         = self.opacity_var.get() / 100,
            mod_snap        = self.mod_snap_var.get(),
            mod_key         = _LABEL_TO_MOD.get(self.mod_key_var.get(), "shift"),
            monitor_layouts = dict(self._monitor_layouts),
        )
        self.root.destroy()

    def run(self) -> Optional[ZonesConfig]:
        """Block until the editor closes.

        Returns a ZonesConfig on save, or None if cancelled.
        """
        if self._toplevel:
            self.root.wait_window()   # embedded: yields to parent mainloop
        else:
            self.root.mainloop()      # standalone: owns the event loop
        return self.result
