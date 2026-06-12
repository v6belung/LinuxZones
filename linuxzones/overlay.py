import tkinter as tk
from typing import Dict, List, Optional, Tuple, Union

from .zones import Zone, MonitorInfo, Layout, label_anchor

ZONE_COLORS = ["#4a90d9", "#7b68ee", "#48c774", "#ff9f43", "#ff6b35", "#e84393"]
ACTIVE_COLOR = "#ffffff"
BORDER = 4
LABEL_FONT = ("sans-serif", 13, "bold")


class ZoneOverlay:
    """Full-screen transparent Toplevel that draws zone rectangles.

    Must be created with a tk.Tk master — it is a Toplevel, not a root window.
    The parent application owns the mainloop and calls show/hide/highlight
    directly; there is no internal queue or pump here.

    Multi-monitor: when monitors + per-monitor layout info are provided the
    overlay spans the full virtual screen and draws each monitor's zones at
    their correct absolute canvas positions.  In single-monitor mode the
    behaviour is identical to the original implementation.
    """

    def __init__(
        self,
        master: tk.Tk,
        zones: List[Zone],
        screen_w: int,
        screen_h: int,
        opacity: float = 0.5,
        work_x: int = 0,
        work_y: int = 0,
        work_w: int = 0,
        work_h: int = 0,
        monitors: Optional[List[MonitorInfo]] = None,
        monitor_layouts: Optional[Dict[str, str]] = None,
        layouts: Optional[Dict[str, "Layout"]] = None,
    ):
        self._opacity  = max(0.1, min(0.9, opacity))
        self.screen_w  = screen_w
        self.screen_h  = screen_h
        self.work_x    = work_x
        self.work_y    = work_y
        self.work_w    = work_w if work_w > 0 else screen_w
        self.work_h    = work_h if work_h > 0 else screen_h

        # Multi-monitor state
        self._monitors:        List[MonitorInfo]  = monitors or []
        self._monitor_layouts: Dict[str, str]     = monitor_layouts or {}
        self._layouts:         Dict[str, "Layout"] = layouts or {}
        self._multi           = bool(self._monitors and len(self._monitors) > 1)

        # Single-monitor fallback zones (used when _multi is False)
        self.zones = zones

        # Active highlight: (monitor_name, zone_key) in multi mode,
        # or just zone_key (int / tuple / None) in single mode.
        self.active_zone: Optional[Union[int, Tuple[int, int]]] = None
        self._active_monitor: Optional[str] = None
        self._visible = False

        self.root = tk.Toplevel(master)
        self.root.withdraw()
        self._build()

    def _build(self):
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        if self._multi:
            # Span the full virtual screen so all monitors are covered.
            self.root.geometry(
                f"{self.screen_w}x{self.screen_h}+0+0"
            )
        else:
            self.root.geometry(
                f"{self.work_w}x{self.work_h}+{self.work_x}+{self.work_y}"
            )

        self.canvas = tk.Canvas(
            self.root,
            bg="black",
            highlightthickness=0,
            cursor="none",
        )
        self.canvas.pack(fill="both", expand=True)
        self._draw()

    # ------------------------------------------------------------------ drawing

    def _draw(self):
        self.canvas.delete("all")
        if self._multi:
            self._draw_multi()
        else:
            self._draw_single()

    def _draw_single(self):
        # Largest zones first so smaller, overlapping zones are drawn on top.
        for i, zone in sorted(enumerate(self.zones), key=lambda iz: -iz[1].area()):
            x, y, w, h = zone.pixel_rect(self.work_w, self.work_h)
            is_active = (self.active_zone == i)
            fill   = ACTIVE_COLOR if is_active else ZONE_COLORS[i % len(ZONE_COLORS)]
            border = BORDER + 2 if is_active else BORDER
            self.canvas.create_rectangle(
                x + border, y + border,
                x + w - border, y + h - border,
                fill=fill, outline="white", width=border,
            )
            label = zone.name or str(i + 1)
            lfx, lfy = label_anchor(zone, self.zones)
            self.canvas.create_text(
                int(lfx * self.work_w), int(lfy * self.work_h),
                text=label,
                fill="black" if is_active else "white",
                font=LABEL_FONT,
            )
        if isinstance(self.active_zone, tuple):
            self._draw_margin_strip_single(*self.active_zone)

    def _draw_multi(self):
        for mon in self._monitors:
            layout_name = self._monitor_layouts.get(mon.name)
            if layout_name and layout_name in self._layouts:
                layout = self._layouts[layout_name]
            else:
                # No layout assigned — draw nothing for this monitor.
                continue
            zones = layout.zones
            # Largest zones first so smaller, overlapping zones are drawn on top.
            for i, zone in sorted(enumerate(zones), key=lambda iz: -iz[1].area()):
                # Canvas coords = monitor origin + zone fraction * monitor size
                cx = mon.x + int(zone.x * mon.w)
                cy = mon.y + int(zone.y * mon.h)
                cw = int(zone.w * mon.w)
                ch = int(zone.h * mon.h)
                is_active = (
                    self._active_monitor == mon.name
                    and self.active_zone == i
                )
                fill   = ACTIVE_COLOR if is_active else ZONE_COLORS[i % len(ZONE_COLORS)]
                border = BORDER + 2 if is_active else BORDER
                self.canvas.create_rectangle(
                    cx + border, cy + border,
                    cx + cw - border, cy + ch - border,
                    fill=fill, outline="white", width=border,
                )
                label = zone.name or str(i + 1)
                lfx, lfy = label_anchor(zone, zones)
                self.canvas.create_text(
                    mon.x + int(lfx * mon.w), mon.y + int(lfy * mon.h),
                    text=label,
                    fill="black" if is_active else "white",
                    font=LABEL_FONT,
                )
        # Margin strip for multi-monitor
        if isinstance(self.active_zone, tuple) and self._active_monitor:
            layout_name = self._monitor_layouts.get(self._active_monitor)
            if layout_name and layout_name in self._layouts:
                mon = next((m for m in self._monitors
                            if m.name == self._active_monitor), None)
                if mon:
                    self._draw_margin_strip_multi(
                        *self.active_zone,
                        self._layouts[layout_name].zones,
                        mon,
                    )

    def _draw_margin_strip_single(self, i: int, j: int) -> None:
        """Draw the 20-px merge strip centred on the boundary between zones i and j."""
        a, b = self.zones[i], self.zones[j]
        tol = 0.002
        M   = 10
        for za, zb in ((a, b), (b, a)):
            if abs((za.x + za.w) - zb.x) < tol:
                vy0 = max(za.y, zb.y)
                vy1 = min(za.y + za.h, zb.y + zb.h)
                if vy0 < vy1:
                    bx  = int(((za.x + za.w + zb.x) / 2) * self.work_w)
                    py0 = int(vy0 * self.work_h) + BORDER
                    py1 = int(vy1 * self.work_h) - BORDER
                    self.canvas.create_rectangle(
                        bx - M, py0, bx + M, py1,
                        fill=ACTIVE_COLOR, outline="white", width=BORDER,
                    )
                    return
            if abs((za.y + za.h) - zb.y) < tol:
                hx0 = max(za.x, zb.x)
                hx1 = min(za.x + za.w, zb.x + zb.w)
                if hx0 < hx1:
                    by  = int(((za.y + za.h + zb.y) / 2) * self.work_h)
                    px0 = int(hx0 * self.work_w) + BORDER
                    px1 = int(hx1 * self.work_w) - BORDER
                    self.canvas.create_rectangle(
                        px0, by - M, px1, by + M,
                        fill=ACTIVE_COLOR, outline="white", width=BORDER,
                    )
                    return

    def _draw_margin_strip_multi(
        self,
        i: int,
        j: int,
        zones: List[Zone],
        mon: MonitorInfo,
    ) -> None:
        a, b = zones[i], zones[j]
        tol = 0.002
        M   = 10
        for za, zb in ((a, b), (b, a)):
            if abs((za.x + za.w) - zb.x) < tol:
                vy0 = max(za.y, zb.y)
                vy1 = min(za.y + za.h, zb.y + zb.h)
                if vy0 < vy1:
                    bx  = mon.x + int(((za.x + za.w + zb.x) / 2) * mon.w)
                    py0 = mon.y + int(vy0 * mon.h) + BORDER
                    py1 = mon.y + int(vy1 * mon.h) - BORDER
                    self.canvas.create_rectangle(
                        bx - M, py0, bx + M, py1,
                        fill=ACTIVE_COLOR, outline="white", width=BORDER,
                    )
                    return
            if abs((za.y + za.h) - zb.y) < tol:
                hx0 = max(za.x, zb.x)
                hx1 = min(za.x + za.w, zb.x + zb.w)
                if hx0 < hx1:
                    by  = mon.y + int(((za.y + za.h + zb.y) / 2) * mon.h)
                    px0 = mon.x + int(hx0 * mon.w) + BORDER
                    px1 = mon.x + int(hx1 * mon.w) - BORDER
                    self.canvas.create_rectangle(
                        px0, by - M, px1, by + M,
                        fill=ACTIVE_COLOR, outline="white", width=BORDER,
                    )
                    return

    # ------------------------------------------------------------------ public API

    def update_screen_geometry(
        self,
        screen_w: int,
        screen_h: int,
        work_x: int = 0,
        work_y: int = 0,
        work_w: int = 0,
        work_h: int = 0,
    ):
        """Reposition/resize the overlay window after a screen geometry change.

        Called before show() so a stale startup snapshot or a resolution change
        never leaves the overlay at the wrong size.
        """
        self.screen_w = screen_w
        self.screen_h = screen_h
        self.work_x   = work_x
        self.work_y   = work_y
        self.work_w   = work_w if work_w > 0 else screen_w
        self.work_h   = work_h if work_h > 0 else screen_h
        if self._multi:
            self.root.geometry(f"{self.screen_w}x{self.screen_h}+0+0")
        else:
            self.root.geometry(
                f"{self.work_w}x{self.work_h}+{self.work_x}+{self.work_y}"
            )

    def show(self):
        if not self._visible:
            self._visible = True
            self.root.deiconify()
            self.root.update_idletasks()
            self.root.attributes("-alpha", self._opacity)
            self.root.lift()
            self._draw()

    def hide(self):
        if self._visible:
            self._visible = False
            self.root.withdraw()

    def highlight(
        self,
        zone_idx: Optional[Union[int, Tuple[int, int]]],
        monitor_name: Optional[str] = None,
    ):
        if zone_idx != self.active_zone or monitor_name != self._active_monitor:
            self.active_zone      = zone_idx
            self._active_monitor  = monitor_name
            if self._visible:
                self._draw()

    def update_zones(self, zones: List[Zone]):
        """Single-monitor path: replace zones directly."""
        self.zones = zones
        self.active_zone     = None
        self._active_monitor = None
        if self._visible:
            self._draw()

    def update_monitor_config(
        self,
        monitors: List[MonitorInfo],
        monitor_layouts: Dict[str, str],
        layouts: Dict[str, "Layout"],
    ):
        """Multi-monitor path: update monitor/layout mapping and redraw."""
        self._monitors        = monitors
        self._monitor_layouts = monitor_layouts
        self._layouts         = layouts
        self._multi           = bool(monitors and len(monitors) > 1)
        self.active_zone      = None
        self._active_monitor  = None
        if self._visible:
            self._draw()

    def set_opacity(self, opacity: float):
        self._opacity = max(0.1, min(0.9, opacity))
        if self._visible:
            self.root.attributes("-alpha", self._opacity)
