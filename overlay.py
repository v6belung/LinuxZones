import tkinter as tk
from typing import List, Optional

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
    ):
        self._opacity = max(0.1, min(0.9, opacity))
        self.zones = zones
        self.screen_w = screen_w
        self.screen_h = screen_h
        self.active_zone: Optional[int] = None
        self._visible = False

        self.root = tk.Toplevel(master)
        self.root.withdraw()
        self._build()

    def _build(self):
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.geometry(f"{self.screen_w}x{self.screen_h}+0+0")

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
            x, y, w, h = zone.pixel_rect(self.screen_w, self.screen_h)
            is_active = (i == self.active_zone)
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

    def highlight(self, zone_idx: Optional[int]):
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
