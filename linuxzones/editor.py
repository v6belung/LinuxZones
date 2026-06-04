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

from .zones import Zone, Layout, ZonesConfig, MonitorInfo, DEFAULT_LAYOUTS, VALID_MODIFIERS, _coerce_modifier

GRID = 0.05   # snap-to-grid step (5 % of screen)

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
        # Main frame splits into sidebar (left) + canvas area (right)
        main = ttk.Frame(self.root, padding=8)
        main.pack(fill="both", expand=True)

        # ── Left sidebar ──────────────────────────────────────────────────
        # Do NOT use pack_propagate(False) + fill="y" here.  Those two together
        # clamp the sidebar to the window height (which is driven by the canvas),
        # clipping the bottom sections on wide-aspect-ratio screens where the
        # canvas is shorter than the sidebar's content.  Instead, let the sidebar
        # report its natural height; the window will grow to the taller of
        # sidebar vs canvas.
        sidebar = ttk.Frame(main)
        sidebar.pack(side="left", anchor="nw", padx=(0, 10))

        # LAYOUTS ---------------------------------------------------------
        lf_layouts = ttk.LabelFrame(sidebar, text="Layouts")
        lf_layouts.pack(fill="x", pady=(0, 6))

        lb_wrap = ttk.Frame(lf_layouts)
        lb_wrap.pack(fill="x", padx=4, pady=(4, 0))

        scrollbar = ttk.Scrollbar(lb_wrap, orient="vertical")
        self.lb = tk.Listbox(
            lb_wrap,
            height=5,
            yscrollcommand=scrollbar.set,
            exportselection=False,
            activestyle="none",
        )
        scrollbar.config(command=self.lb.yview)
        scrollbar.pack(side="right", fill="y")
        self.lb.pack(side="left", fill="both", expand=True)
        self.lb.bind("<<ListboxSelect>>", self._on_layout_select)
        self.lb.bind("<Double-Button-1>", lambda _: self._rename_layout())

        r1 = ttk.Frame(lf_layouts)
        r1.pack(fill="x", padx=4, pady=(4, 0))
        ttk.Button(r1, text="New",    command=self._new_layout   ).pack(side="left", expand=True, fill="x", padx=(0, 2))
        ttk.Button(r1, text="Rename", command=self._rename_layout).pack(side="left", expand=True, fill="x")

        r2 = ttk.Frame(lf_layouts)
        r2.pack(fill="x", padx=4, pady=(2, 4))
        ttk.Button(r2, text="Duplicate", command=self._dup_layout   ).pack(side="left", expand=True, fill="x", padx=(0, 2))
        ttk.Button(r2, text="Delete",    command=self._delete_layout).pack(side="left", expand=True, fill="x")

        # PRESETS ---------------------------------------------------------
        lf_presets = ttk.LabelFrame(sidebar, text="Presets")
        lf_presets.pack(fill="x", pady=(0, 6))

        for preset in DEFAULT_LAYOUTS:
            ttk.Button(
                lf_presets, text=preset,
                command=lambda p=preset: self._apply_preset(p),
            ).pack(fill="x", padx=4, pady=1)
        ttk.Frame(lf_presets).pack(pady=2)   # bottom breathing room

        # SELECTED ZONE ---------------------------------------------------
        lf_zone = ttk.LabelFrame(sidebar, text="Selected Zone")
        lf_zone.pack(fill="x", pady=(0, 6))

        self.zone_var = tk.StringVar(value="Click a zone to select it")
        ttk.Label(
            lf_zone, textvariable=self.zone_var,
            wraplength=220, justify="left",
            font="TkFixedFont",
        ).pack(fill="x", padx=6, pady=4)

        zb = ttk.Frame(lf_zone)
        zb.pack(fill="x", padx=4, pady=(0, 4))
        ttk.Button(zb, text="Rename Zone", command=self._rename_zone).pack(side="left", expand=True, fill="x", padx=(0, 2))
        ttk.Button(zb, text="Delete Zone", command=self._delete_zone).pack(side="left", expand=True, fill="x")

        # SETTINGS --------------------------------------------------------
        lf_settings = ttk.LabelFrame(sidebar, text="Settings")
        lf_settings.pack(fill="x", pady=(0, 6))

        ttk.Checkbutton(
            lf_settings,
            text="Keyboard modifier snap",
            variable=self.mod_snap_var,
            command=self._on_mod_toggle,
        ).pack(anchor="w", padx=6, pady=(6, 2))
        ttk.Label(
            lf_settings,
            text="Hold the chosen key while dragging to\nsnap (alternative to right-click)",
            justify="left",
        ).pack(anchor="w", padx=24, pady=(0, 2))

        mod_row = ttk.Frame(lf_settings)
        mod_row.pack(anchor="w", fill="x", padx=24, pady=(0, 2))
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
            lf_settings,
            text="Privacy: enabling this makes LinuxZones\n"
                 "monitor key presses globally so it can\n"
                 "detect the modifier. Keystrokes are never\n"
                 "stored or sent anywhere. Leave off to\n"
                 "monitor mouse buttons only.",
            justify="left",
            foreground="#888888",
        ).pack(anchor="w", padx=24, pady=(0, 6))

        # Reflect the initial enabled/disabled state of the dropdown.
        self._on_mod_toggle()

        # OVERLAY OPACITY -------------------------------------------------
        lf_opacity = ttk.LabelFrame(sidebar, text="Overlay Opacity")
        lf_opacity.pack(fill="x")

        op_row = ttk.Frame(lf_opacity)
        op_row.pack(fill="x", padx=4, pady=4)

        self._opacity_lbl = ttk.Label(
            op_row, text=f"{self.opacity_var.get()}%", width=4, anchor="e",
        )
        self._opacity_lbl.pack(side="right")

        ttk.Scale(
            op_row,
            from_=10, to=90,
            orient="horizontal",
            variable=self.opacity_var,
            command=self._on_opacity,
        ).pack(side="left", fill="x", expand=True, padx=(0, 4))

        ttk.Label(
            lf_opacity,
            text="how visible the zone overlay is during snapping",
            justify="left",
        ).pack(anchor="w", padx=6, pady=(0, 4))

        # ── Right: canvas area ────────────────────────────────────────────
        right = ttk.Frame(main)
        right.pack(side="right", fill="both", expand=True)

        ttk.Label(
            right,
            text="Draw: click-drag  ·  Select: left-click  ·  Delete: right-click",
        ).pack(pady=(0, 4))

        # Monitor selector (only shown when multiple monitors are detected)
        if self._multi:
            mon_row = ttk.Frame(right)
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

        self.canvas = tk.Canvas(
            right, width=self.pw, height=self.ph,
            bg=_CANVAS_BG,
            highlightthickness=1, highlightbackground="gray",
            cursor="crosshair",
        )
        self.canvas.pack()
        self.canvas.bind("<ButtonPress-1>",   self._on_press)
        self.canvas.bind("<B1-Motion>",       self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<ButtonPress-3>",   self._on_right_click)

        # ── Bottom action bar ─────────────────────────────────────────────
        ttk.Separator(self.root, orient="horizontal").pack(fill="x")

        bar = ttk.Frame(self.root, padding=(8, 6))
        bar.pack(fill="x")

        # Save & Close on the right, Cancel to its left
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
        fx, fy = cx / self.pw, cy / self.ph
        for i, z in enumerate(self._layout.zones):
            if z.contains(fx, fy):
                return i
        return None

    def _on_press(self, e) -> None:
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
        if self._drawing and self._draw_start:
            self._redraw()
            x0, y0 = self._draw_start
            self.canvas.create_rectangle(
                min(x0, e.x), min(y0, e.y),
                max(x0, e.x), max(y0, e.y),
                outline="#ff6b35", width=2, dash=(4, 2),
            )

    def _on_release(self, e) -> None:
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
        for i, z in enumerate(self._layout.zones):
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
            mid_x = x + w // 2
            mid_y = y + h // 2
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
