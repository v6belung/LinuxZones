"""Zone layout editor — draw zones by click-dragging on a scaled screen preview.

UI layout
---------
Left sidebar (230 px, fixed):
  LAYOUTS        listbox + New / Rename / Duplicate / Delete
  PRESETS        preset quick-apply buttons
  SELECTED ZONE  zone info card + Rename Zone / Delete Zone
  SETTINGS       Shift-key snap checkbox
  OVERLAY        opacity slider

Right area:
  canvas hint + scaled screen preview (click-drag to draw zones)

Bottom bar:
  [Cancel]  [Save & Close]

run() returns (layouts, active_layout, opacity, shift_snap) on save,
or None on cancel.
"""

import copy
import tkinter as tk
from tkinter import simpledialog, messagebox
from typing import Dict, List, Optional, Tuple

from zones import Zone, Layout, DEFAULT_LAYOUTS

GRID = 0.05   # snap-to-grid step (5 % of screen)

# Zone colours for the preview canvas
_ZONE_COLORS = ["#4a90d9", "#7b68ee", "#48c774", "#ff9f43", "#ff6b35", "#e84393"]

# ── Colour palette (dark theme, WCAG AA–compliant) ───────────────────────────
PANEL_BG    = "#252526"   # sidebar / window background
SECTION_BG  = "#1e1e1e"   # section-header strip
CANVAS_BG   = "#1a1a2e"   # preview canvas
INPUT_BG    = "#3a3a3b"   # listbox / input field
BTN_N       = "#3c3c3d"   # normal button background
BTN_N_HV    = "#505051"   # normal button hover
BTN_D       = "#5c1818"   # destructive button background
BTN_D_HV    = "#8b2020"   # destructive button hover
BTN_OK      = "#1a4228"   # primary (save) button background
BTN_OK_HV   = "#215c38"   # primary button hover
ACCENT      = "#4a90d9"   # selection highlight
FG          = "#cccccc"   # primary text
FG_DIM      = "#888888"   # secondary / hint text
FG_HINT     = "#555555"   # placeholder text
HDR_FG      = "#569cd6"   # section-header label
SEL_BG      = "#264f78"   # listbox selection background
FG_DANGER   = "#f98080"   # text on destructive buttons
FG_SUCCESS  = "#6ee7a8"   # text on save button
BAR_BG      = "#1a1a1a"   # bottom action bar


def _snap_val(v: float) -> float:
    return round(v / GRID) * GRID


class ZoneEditor:
    """Zone layout editor dialog.

    Parameters
    ----------
    layouts        : dict mapping name → Layout
    active_layout  : key of the currently active layout
    screen_w/h     : screen dimensions in pixels
    opacity        : current overlay opacity (0.0–1.0)
    shift_snap     : whether Shift-key snap is enabled
    master         : parent tk.Tk for embedded Toplevel mode;
                     None = standalone (creates its own Tk root)

    run() returns (layouts, active_layout, opacity, shift_snap) on save,
    or None on cancel.
    """

    def __init__(
        self,
        layouts: Dict[str, Layout],
        active_layout: str,
        screen_w: int,
        screen_h: int,
        opacity: float = 0.5,
        shift_snap: bool = False,
        master: Optional[tk.Tk] = None,
    ):
        self.layouts       = copy.deepcopy(layouts)
        self.active_layout = active_layout
        self.screen_w      = screen_w
        self.screen_h      = screen_h
        self.result        = None

        # Scale preview canvas to ≤800 px wide, preserving aspect ratio.
        self.pw = 800
        self.ph = int(800 * screen_h / screen_w)

        self._drawing    = False
        self._draw_start: Optional[Tuple[int, int]] = None
        self._selected:   Optional[int]             = None
        self._layout_names: List[str]               = []

        # Create root window
        if master is None:
            self.root      = tk.Tk()
            self._toplevel = False
        else:
            self.root      = tk.Toplevel(master)
            self._toplevel = True

        self.root.title("LinuxZones — Layout Editor")
        self.root.configure(bg=PANEL_BG)
        self.root.resizable(False, False)

        # Tk variables
        self.opacity_var    = tk.IntVar(value=max(10, min(90, int(opacity * 100))))
        self.shift_snap_var = tk.BooleanVar(value=shift_snap)

        self._build()
        self._refresh_list()
        self._update_info()
        self._redraw()

    # ── UI helpers ────────────────────────────────────────────────────────────

    def _section(self, parent: tk.Frame, title: str) -> None:
        """Render a labelled section-header strip with top margin."""
        strip = tk.Frame(parent, bg=SECTION_BG)
        strip.pack(fill="x", pady=(10, 4))
        tk.Label(
            strip, text=title, bg=SECTION_BG, fg=HDR_FG,
            font=("sans-serif", 8, "bold"),
            padx=8, pady=3, anchor="w",
        ).pack(fill="x")

    def _btn(
        self, parent: tk.Frame, text: str, cmd,
        style: str = "normal", **kw
    ) -> tk.Button:
        """Styled button with hover colour transition."""
        palette = {
            "normal":  (BTN_N,  BTN_N_HV,  FG),
            "danger":  (BTN_D,  BTN_D_HV,  FG_DANGER),
            "success": (BTN_OK, BTN_OK_HV, FG_SUCCESS),
        }
        bg, hv, fg = palette.get(style, palette["normal"])
        b = tk.Button(
            parent, text=text, command=cmd,
            bg=bg, fg=fg, activebackground=hv, activeforeground=fg,
            relief="flat", bd=0, cursor="hand2", **kw
        )
        b.bind("<Enter>", lambda _, b=b, h=hv: b.configure(bg=h))
        b.bind("<Leave>", lambda _, b=b, n=bg: b.configure(bg=n))
        return b

    # ── UI construction ───────────────────────────────────────────────────────

    def _build(self) -> None:

        # ── Left sidebar (fixed width) ────────────────────────────────────
        sidebar = tk.Frame(self.root, bg=PANEL_BG, width=230)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        # ── LAYOUTS ───────────────────────────────────────────────────────
        self._section(sidebar, "LAYOUTS")

        self.lb = tk.Listbox(
            sidebar,
            bg=INPUT_BG, fg=FG,
            selectbackground=SEL_BG, selectforeground="#ffffff",
            relief="flat", height=5,
            font=("monospace", 9),
            activestyle="none",
            highlightthickness=0,
        )
        self.lb.pack(fill="x", padx=8, pady=(0, 6))
        self.lb.bind("<<ListboxSelect>>",  self._on_layout_select)
        self.lb.bind("<Double-Button-1>",  lambda _: self._rename_layout())

        # Row 1: New + Rename
        r1 = tk.Frame(sidebar, bg=PANEL_BG)
        r1.pack(fill="x", padx=8, pady=(0, 2))
        self._btn(r1, "New",    self._new_layout,    padx=6, pady=4
                  ).pack(side="left", expand=True, fill="x", padx=(0, 2))
        self._btn(r1, "Rename", self._rename_layout, padx=6, pady=4
                  ).pack(side="left", expand=True, fill="x")

        # Row 2: Duplicate + Delete (destructive)
        r2 = tk.Frame(sidebar, bg=PANEL_BG)
        r2.pack(fill="x", padx=8)
        self._btn(r2, "Duplicate", self._dup_layout,    padx=6, pady=4
                  ).pack(side="left", expand=True, fill="x", padx=(0, 2))
        self._btn(r2, "Delete",    self._delete_layout, style="danger", padx=6, pady=4
                  ).pack(side="left", expand=True, fill="x")

        # ── PRESETS ───────────────────────────────────────────────────────
        self._section(sidebar, "PRESETS")
        for preset in DEFAULT_LAYOUTS:
            self._btn(
                sidebar, preset,
                lambda p=preset: self._apply_preset(p),
                padx=6, pady=3,
            ).pack(fill="x", padx=8, pady=1)

        # ── SELECTED ZONE ─────────────────────────────────────────────────
        self._section(sidebar, "SELECTED ZONE")

        self.zone_var = tk.StringVar(value="Click a zone to select it")
        self._zone_lbl = tk.Label(
            sidebar, textvariable=self.zone_var,
            bg=PANEL_BG, fg=FG_HINT,
            wraplength=210, justify="left",
            font=("sans-serif", 9),
            anchor="nw", padx=8,
        )
        self._zone_lbl.pack(fill="x", pady=(2, 6))

        zb = tk.Frame(sidebar, bg=PANEL_BG)
        zb.pack(fill="x", padx=8)
        self._btn(zb, "Rename Zone", self._rename_zone, padx=6, pady=3
                  ).pack(side="left", expand=True, fill="x", padx=(0, 2))
        self._btn(zb, "Delete Zone", self._delete_zone, style="danger", padx=6, pady=3
                  ).pack(side="left", expand=True, fill="x")

        # ── SETTINGS ─────────────────────────────────────────────────────
        self._section(sidebar, "SETTINGS")

        tk.Checkbutton(
            sidebar,
            text=" Shift key snap",
            variable=self.shift_snap_var,
            bg=PANEL_BG, fg=FG,
            selectcolor=INPUT_BG,
            activebackground=PANEL_BG, activeforeground=FG,
            relief="flat", bd=0,
            font=("sans-serif", 9),
        ).pack(anchor="w", padx=6, pady=(2, 0))
        tk.Label(
            sidebar, text="Hold Shift while dragging to snap\n(alternative to right-click)",
            bg=PANEL_BG, fg=FG_HINT,
            font=("sans-serif", 8), anchor="w", padx=24,
        ).pack(fill="x", pady=(0, 4))

        # ── OVERLAY OPACITY ───────────────────────────────────────────────
        self._section(sidebar, "OVERLAY OPACITY")

        opacity_row = tk.Frame(sidebar, bg=PANEL_BG)
        opacity_row.pack(fill="x", padx=8, pady=(4, 0))

        self._opacity_lbl = tk.Label(
            opacity_row,
            text=f"{self.opacity_var.get()}%",
            bg=PANEL_BG, fg=FG,
            font=("sans-serif", 9, "bold"),
            width=4, anchor="e",
        )
        self._opacity_lbl.pack(side="right")

        tk.Scale(
            opacity_row,
            from_=10, to=90,
            orient="horizontal",
            variable=self.opacity_var,
            command=self._on_opacity,
            bg=PANEL_BG, fg=FG,
            activebackground=ACCENT,
            troughcolor=INPUT_BG,
            highlightthickness=0,
            showvalue=False, bd=0,
        ).pack(side="left", fill="x", expand=True)

        tk.Label(
            sidebar, text="how visible the zone overlay is",
            bg=PANEL_BG, fg=FG_HINT,
            font=("sans-serif", 8), anchor="w", padx=8,
        ).pack(fill="x", pady=(0, 10))

        # ── Right: canvas area ────────────────────────────────────────────
        right = tk.Frame(self.root, bg=PANEL_BG)
        right.pack(side="right", fill="both", expand=True)

        tk.Label(
            right,
            text="Draw: click-drag  ·  Select: click  ·  Delete: right-click",
            bg=PANEL_BG, fg=FG_HINT, font=("sans-serif", 8),
        ).pack(pady=(8, 0))

        self.canvas = tk.Canvas(
            right, width=self.pw, height=self.ph,
            bg=CANVAS_BG,
            highlightthickness=2, highlightbackground="#383838",
            cursor="crosshair",
        )
        self.canvas.pack(padx=16, pady=(6, 8))
        self.canvas.bind("<ButtonPress-1>",   self._on_press)
        self.canvas.bind("<B1-Motion>",       self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<ButtonPress-3>",   self._on_right_click)

        # ── Bottom action bar ─────────────────────────────────────────────
        bar = tk.Frame(self.root, bg=BAR_BG)
        bar.pack(fill="x", side="bottom")

        actions = tk.Frame(bar, bg=BAR_BG)
        actions.pack(side="right", padx=12, pady=8)

        self._btn(actions, "Cancel", self.root.destroy,
                  padx=14, pady=6).pack(side="left", padx=(0, 8))
        self._btn(actions, "Save & Close", self._save, style="success",
                  font=("sans-serif", 10, "bold"), padx=16, pady=6
                  ).pack(side="left")

    # ── Opacity ───────────────────────────────────────────────────────────────

    def _on_opacity(self, _=None) -> None:
        self._opacity_lbl.config(text=f"{self.opacity_var.get()}%")

    # ── Layout list ───────────────────────────────────────────────────────────

    def _refresh_list(self) -> None:
        """Repopulate the listbox; mark the active layout with ●."""
        self.lb.delete(0, "end")
        self._layout_names = list(self.layouts.keys())
        for name in self._layout_names:
            prefix = "● " if name == self.active_layout else "  "
            self.lb.insert("end", prefix + name)
        if self.active_layout in self._layout_names:
            idx = self._layout_names.index(self.active_layout)
            self.lb.selection_set(idx)
            self.lb.see(idx)

    @property
    def _layout(self) -> Layout:
        return self.layouts[self.active_layout]

    def _on_layout_select(self, _=None) -> None:
        sel = self.lb.curselection()
        if not sel:
            return
        idx = sel[0]
        if 0 <= idx < len(self._layout_names):
            self.active_layout = self._layout_names[idx]
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
        # Screen border
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
            # Zone label
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
        """Refresh the SELECTED ZONE info label."""
        if self._selected is None or self._selected >= len(self._layout.zones):
            self.zone_var.set("Click a zone to select it")
            self._zone_lbl.config(fg=FG_HINT, font=("sans-serif", 9, "italic"))
            return

        z    = self._layout.zones[self._selected]
        px_w = int(z.w * self.screen_w)
        px_h = int(z.h * self.screen_h)
        name_part = f"  ·  {z.name}" if z.name else ""
        info = (
            f"Zone {self._selected + 1}{name_part}\n"
            f"x {z.x * 100:5.1f}%   y {z.y * 100:5.1f}%\n"
            f"w {z.w * 100:5.1f}%   h {z.h * 100:5.1f}%\n"
            f"{px_w} × {px_h} px"
        )
        self.zone_var.set(info)
        self._zone_lbl.config(fg=FG, font=("monospace", 9))

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
        # Rebuild dict preserving insertion order, key replaced in-place.
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
        if name is not None:   # None means the user clicked Cancel
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
        self.result = (
            self.layouts,
            self.active_layout,
            self.opacity_var.get() / 100,
            self.shift_snap_var.get(),
        )
        self.root.destroy()

    def run(self):
        """Block until the editor closes.

        Returns (layouts, active_layout, opacity, shift_snap) on save,
        or None if the editor was cancelled.
        """
        if self._toplevel:
            self.root.wait_window()   # integrated: yields to parent mainloop
        else:
            self.root.mainloop()      # standalone: owns the event loop
        return self.result
