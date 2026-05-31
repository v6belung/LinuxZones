import tkinter as tk
from typing import List, Optional, Tuple, Union

from zones import Zone

ZONE_COLORS = ["#4a90d9", "#7b68ee", "#48c774", "#ff9f43", "#ff6b35", "#e84393"]
ACTIVE_COLOR = "#ffffff"
BORDER = 4
LABEL_FONT = ("sans-serif", 13, "bold")


class ZoneOverlay:
    """Full-screen transparent Toplevel that draws zone rectangles.

    Must be created with a tk.Tk master — it is a Toplevel, not a root window.
    The parent application owns the mainloop and calls show/hide/highlight
    directly; there is no internal queue or pump here.
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
    ):
        self._opacity = max(0.1, min(0.9, opacity))
        self.zones = zones
        self.screen_w = screen_w
        self.screen_h = screen_h
        self.work_x = work_x
        self.work_y = work_y
        self.work_w = work_w if work_w > 0 else screen_w
        self.work_h = work_h if work_h > 0 else screen_h
        self.active_zone: Optional[Union[int, Tuple[int, int]]] = None
        self._visible = False

        self.root = tk.Toplevel(master)
        self.root.withdraw()
        self._build()

    def _build(self):
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.geometry(f"{self.work_w}x{self.work_h}+{self.work_x}+{self.work_y}")

        # Do NOT set wm_attributes("-type", ...) — on Cinnamon/Muffin those
        # types tell the compositor to skip compositing, preventing transparency.

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
        for i, zone in enumerate(self.zones):
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
            self.canvas.create_text(
                x + w // 2, y + h // 2,
                text=label,
                fill="black" if is_active else "white",
                font=LABEL_FONT,
            )
        if isinstance(self.active_zone, tuple):
            self._draw_margin_strip(*self.active_zone)

    def _draw_margin_strip(self, i: int, j: int) -> None:
        """Draw the 20-px merge strip centred on the boundary between zones i and j."""
        a, b = self.zones[i], self.zones[j]
        tol = 0.002
        M   = 10  # half-width / half-height of the strip in pixels
        for za, zb in ((a, b), (b, a)):
            if abs((za.x + za.w) - zb.x) < tol:        # vertical boundary
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
            if abs((za.y + za.h) - zb.y) < tol:        # horizontal boundary
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

    # ------------------------------------------------------------------ public API

    def show(self):
        if not self._visible:
            self._visible = True
            self.root.deiconify()
            # Alpha must be applied AFTER the window becomes visible —
            # setting it on a withdrawn window is ignored by most compositors.
            self.root.update_idletasks()
            self.root.attributes("-alpha", self._opacity)
            self.root.lift()
            self._draw()

    def hide(self):
        if self._visible:
            self._visible = False
            self.root.withdraw()

    def highlight(self, zone_idx: Optional[Union[int, Tuple[int, int]]]):
        if zone_idx != self.active_zone:
            self.active_zone = zone_idx
            if self._visible:
                self._draw()

    def update_zones(self, zones: List[Zone]):
        self.zones = zones
        self.active_zone = None
        if self._visible:
            self._draw()

    def set_opacity(self, opacity: float):
        self._opacity = max(0.1, min(0.9, opacity))
        if self._visible:
            self.root.attributes("-alpha", self._opacity)
