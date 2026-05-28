"""Zone layout editor — draw zones by click-dragging on a scaled screen preview."""

import copy
import tkinter as tk
from tkinter import simpledialog, messagebox
from typing import Dict, Optional, Tuple

from zones import Zone, Layout, DEFAULT_LAYOUTS

GRID = 0.05        # snap to 5% grid
COLORS = ["#4a90d9", "#7b68ee", "#48c774", "#ff9f43", "#ff6b35", "#e84393"]
PANEL_BG   = "#2b2b2b"
CANVAS_BG  = "#1a1a2e"
BTN_BG     = "#3d3d3d"
BTN_ACTIVE = "#4a90d9"


def _snap(v: float) -> float:
    return round(v / GRID) * GRID


class ZoneEditor:
    """Zone layout editor dialog.

    Parameters
    ----------
    layouts, active_layout, screen_w, screen_h : as usual
    opacity : current overlay opacity (0.0–1.0), shown in the slider
    master  : parent tk.Tk when launched from the tray app, None for standalone.
              When None a new tk.Tk() root is created and mainloop() is used.
              When a master is given, a Toplevel is created and wait_window() is used.

    run() returns (layouts, active_layout, opacity) on save, or None on cancel.
    """

    def __init__(
        self,
        layouts: Dict[str, Layout],
        active_layout: str,
        screen_w: int,
        screen_h: int,
        opacity: float = 0.5,
        master: Optional[tk.Tk] = None,
    ):
        self.layouts = copy.deepcopy(layouts)
        self.active_layout = active_layout
        self.screen_w = screen_w
        self.screen_h = screen_h
        self.result = None

        # Scale preview to fit ~800 px wide while preserving aspect ratio
        self.pw = 800
        self.ph = int(800 * screen_h / screen_w)

        self._drawing   = False
        self._draw_start: Optional[Tuple[int, int]] = None
        self._selected:   Optional[int]             = None

        # Create root window
        if master is None:
            self.root = tk.Tk()
            self._toplevel = False
        else:
            self.root = tk.Toplevel(master)
            self._toplevel = True

        self.root.title("LinuxZones — Layout Editor")
        self.root.configure(bg=PANEL_BG)
        self.root.resizable(False, False)

        # Opacity slider value (integer percent, 10–90)
        self.opacity_var = tk.IntVar(value=max(10, min(90, int(opacity * 100))))

        self._build()
        self._refresh_list()
        self._redraw()

    # ------------------------------------------------------------------ UI build

    def _build(self):
        # ---- Left sidebar ----
        sidebar = tk.Frame(self.root, bg=PANEL_BG, width=210)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        tk.Label(sidebar, text="Layouts", bg=PANEL_BG, fg="white",
                 font=("sans-serif", 11, "bold")).pack(pady=(12, 2))

        self.lb = tk.Listbox(
            sidebar, bg="#3a3a3a", fg="white",
            selectbackground=BTN_ACTIVE, selectforeground="white",
            relief="flat", height=9, font=("sans-serif", 10),
        )
        self.lb.pack(fill="x", padx=8)
        self.lb.bind("<<ListboxSelect>>", self._on_layout_select)

        for text, cmd in [
            ("New",       self._new_layout),
            ("Duplicate", self._dup_layout),
            ("Rename",    self._rename_layout),
            ("Delete",    self._delete_layout),
        ]:
            tk.Button(sidebar, text=text, command=cmd,
                      bg=BTN_BG, fg="white", relief="flat",
                      activebackground=BTN_ACTIVE).pack(fill="x", padx=8, pady=1)

        self._divider(sidebar)
        tk.Label(sidebar, text="Presets", bg=PANEL_BG, fg="#aaa",
                 font=("sans-serif", 10)).pack()
        for preset in DEFAULT_LAYOUTS:
            tk.Button(sidebar, text=preset,
                      command=lambda p=preset: self._apply_preset(p),
                      bg=BTN_BG, fg="white", relief="flat",
                      activebackground=BTN_ACTIVE).pack(fill="x", padx=8, pady=1)

        self._divider(sidebar)
        self.zone_var = tk.StringVar(value="Click a zone to select")
        tk.Label(sidebar, textvariable=self.zone_var, bg=PANEL_BG, fg="#ccc",
                 wraplength=190, justify="left", font=("sans-serif", 9)).pack(padx=8)
        tk.Button(sidebar, text="Delete Zone", command=self._delete_zone,
                  bg="#7f1d1d", fg="white", relief="flat",
                  activebackground="#c0392b").pack(fill="x", padx=8, pady=(6, 2))

        # ---- Opacity slider ----
        self._divider(sidebar)
        tk.Label(sidebar, text="Overlay opacity", bg=PANEL_BG, fg="#aaa",
                 font=("sans-serif", 10)).pack()

        slider_row = tk.Frame(sidebar, bg=PANEL_BG)
        slider_row.pack(fill="x", padx=8, pady=(2, 0))

        self._opacity_label = tk.Label(
            slider_row,
            text=f"{self.opacity_var.get()}%",
            bg=PANEL_BG, fg="white",
            font=("sans-serif", 10, "bold"),
            width=4,
        )
        self._opacity_label.pack(side="right")

        tk.Scale(
            slider_row,
            from_=10, to=90,
            orient="horizontal",
            variable=self.opacity_var,
            command=self._on_opacity_change,
            bg=PANEL_BG, fg="white",
            activebackground=BTN_ACTIVE,
            troughcolor="#3a3a3a",
            highlightthickness=0,
            showvalue=False,
        ).pack(side="left", fill="x", expand=True)

        tk.Label(sidebar, text="how visible the overlay is",
                 bg=PANEL_BG, fg="#555", font=("sans-serif", 8)).pack()

        # ---- Canvas area ----
        right = tk.Frame(self.root, bg=PANEL_BG)
        right.pack(side="right", fill="both", expand=True)

        tk.Label(right,
                 text="Draw zones: click-drag  |  Left-click to select  |  Right-click to delete",
                 bg=PANEL_BG, fg="#666", font=("sans-serif", 8)).pack(pady=(6, 0))

        self.canvas = tk.Canvas(
            right,
            width=self.pw, height=self.ph,
            bg=CANVAS_BG,
            highlightthickness=2, highlightbackground="#444",
            cursor="crosshair",
        )
        self.canvas.pack(padx=16, pady=8)
        self.canvas.bind("<ButtonPress-1>",  self._on_press)
        self.canvas.bind("<B1-Motion>",      self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<ButtonPress-3>",  self._on_right_click)

        # ---- Bottom bar ----
        bar = tk.Frame(self.root, bg="#1e1e1e")
        bar.pack(fill="x", side="bottom")
        tk.Button(bar, text="Save & Close", command=self._save,
                  bg="#166534", fg="white", relief="flat",
                  font=("sans-serif", 10, "bold"),
                  padx=16, pady=6).pack(side="right", padx=12, pady=8)
        tk.Button(bar, text="Cancel", command=self.root.destroy,
                  bg=BTN_BG, fg="white", relief="flat",
                  padx=12, pady=6).pack(side="right", pady=8)

    def _divider(self, parent):
        tk.Label(parent, text="──────────────", bg=PANEL_BG, fg="#555").pack(pady=3)

    # ------------------------------------------------------------------ opacity

    def _on_opacity_change(self, _=None):
        self._opacity_label.config(text=f"{self.opacity_var.get()}%")

    # ------------------------------------------------------------------ layout list

    def _refresh_list(self):
        self.lb.delete(0, "end")
        for name in self.layouts:
            self.lb.insert("end", name)
        names = list(self.layouts.keys())
        if self.active_layout in names:
            idx = names.index(self.active_layout)
            self.lb.selection_set(idx)
            self.lb.see(idx)

    @property
    def _layout(self) -> Layout:
        return self.layouts[self.active_layout]

    def _on_layout_select(self, _=None):
        sel = self.lb.curselection()
        if sel:
            self.active_layout = self.lb.get(sel[0])
            self._selected = None
            self._redraw()

    # ------------------------------------------------------------------ canvas interactions

    def _frac(self, cx: int, cy: int) -> Tuple[float, float]:
        return (_snap(max(0.0, min(1.0, cx / self.pw))),
                _snap(max(0.0, min(1.0, cy / self.ph))))

    def _zone_at_canvas(self, cx: int, cy: int) -> Optional[int]:
        fx, fy = cx / self.pw, cy / self.ph
        for i, z in enumerate(self._layout.zones):
            if z.contains(fx, fy):
                return i
        return None

    def _on_press(self, e):
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

    def _on_drag(self, e):
        if self._drawing and self._draw_start:
            self._redraw()
            x0, y0 = self._draw_start
            self.canvas.create_rectangle(
                min(x0, e.x), min(y0, e.y),
                max(x0, e.x), max(y0, e.y),
                outline="#ff6b35", width=2, dash=(4, 2),
            )

    def _on_release(self, e):
        if not self._drawing or not self._draw_start:
            return
        self._drawing = False
        x0, y0 = self._draw_start
        self._draw_start = None
        if abs(e.x - x0) < 8 or abs(e.y - y0) < 8:
            return
        fx0, fy0 = self._frac(min(x0, e.x), min(y0, e.y))
        fx1, fy1 = self._frac(max(x0, e.x), max(y0, e.y))
        zone = Zone(fx0, fy0, fx1 - fx0, fy1 - fy0, "")
        self._layout.zones.append(zone)
        self._selected = len(self._layout.zones) - 1
        self._update_info()
        self._redraw()

    def _on_right_click(self, e):
        idx = self._zone_at_canvas(e.x, e.y)
        if idx is not None:
            self._layout.zones.pop(idx)
            self._selected = None
            self._update_info()
            self._redraw()

    # ------------------------------------------------------------------ drawing

    def _redraw(self):
        self.canvas.delete("all")
        self.canvas.create_rectangle(1, 1, self.pw - 1, self.ph - 1,
                                     outline="#444", width=1)
        for i, z in enumerate(self._layout.zones):
            x, y = int(z.x * self.pw), int(z.y * self.ph)
            w, h = int(z.w * self.pw), int(z.h * self.ph)
            sel   = (i == self._selected)
            color = COLORS[i % len(COLORS)]
            self.canvas.create_rectangle(
                x + 3, y + 3, x + w - 3, y + h - 3,
                fill=color,
                outline="white" if sel else "#aaa",
                width=3 if sel else 1,
                stipple="gray50",
            )
            self.canvas.create_text(
                x + w // 2, y + h // 2,
                text=z.name or str(i + 1),
                fill="white",
                font=("sans-serif", 10, "bold"),
            )

    def _update_info(self):
        if self._selected is None or self._selected >= len(self._layout.zones):
            self.zone_var.set("Click a zone to select")
            return
        z = self._layout.zones[self._selected]
        self.zone_var.set(
            f"Zone {self._selected + 1}  ({z.name or 'unnamed'})\n"
            f"x={z.x:.2f}  y={z.y:.2f}\n"
            f"w={z.w:.2f}  h={z.h:.2f}\n"
            f"({int(z.w * self.screen_w)}×{int(z.h * self.screen_h)} px)"
        )

    # ------------------------------------------------------------------ layout actions

    def _new_layout(self):
        name = simpledialog.askstring("New Layout", "Name:", parent=self.root)
        if not name:
            return
        if name in self.layouts:
            messagebox.showerror("Exists", f"Layout '{name}' already exists.")
            return
        self.layouts[name] = Layout(name, [])
        self.active_layout = name
        self._refresh_list()
        self._redraw()

    def _dup_layout(self):
        name = simpledialog.askstring("Duplicate", "New name:", parent=self.root,
                                      initialvalue=self.active_layout + "_copy")
        if not name or name == self.active_layout:
            return
        if name in self.layouts:
            messagebox.showerror("Exists", f"Layout '{name}' already exists.")
            return
        self.layouts[name] = copy.deepcopy(self._layout)
        self.layouts[name].name = name
        self.active_layout = name
        self._refresh_list()
        self._redraw()

    def _rename_layout(self):
        name = simpledialog.askstring("Rename", "New name:", parent=self.root,
                                      initialvalue=self.active_layout)
        if not name or name == self.active_layout:
            return
        if name in self.layouts:
            messagebox.showerror("Exists", f"Layout '{name}' already exists.")
            return
        self.layouts[name] = self.layouts.pop(self.active_layout)
        self.layouts[name].name = name
        self.active_layout = name
        self._refresh_list()

    def _delete_layout(self):
        if len(self.layouts) <= 1:
            messagebox.showwarning("Cannot Delete", "At least one layout must remain.")
            return
        if not messagebox.askyesno("Delete", f"Delete layout '{self.active_layout}'?"):
            return
        del self.layouts[self.active_layout]
        self.active_layout = next(iter(self.layouts))
        self._selected = None
        self._refresh_list()
        self._redraw()

    def _delete_zone(self):
        if self._selected is not None and self._selected < len(self._layout.zones):
            self._layout.zones.pop(self._selected)
        self._selected = None
        self._update_info()
        self._redraw()

    def _apply_preset(self, preset: str):
        if preset in DEFAULT_LAYOUTS:
            self._layout.zones = copy.deepcopy(DEFAULT_LAYOUTS[preset].zones)
        self._selected = None
        self._update_info()
        self._redraw()

    # ------------------------------------------------------------------ save / run

    def _save(self):
        self.result = (self.layouts, self.active_layout, self.opacity_var.get() / 100)
        self.root.destroy()

    def run(self):
        """Block until the editor is closed; return (layouts, active, opacity) or None."""
        if self._toplevel:
            self.root.wait_window()   # integrated: yields back to parent mainloop
        else:
            self.root.mainloop()      # standalone: owns the event loop
        return self.result
