from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
import json
import os

CONFIG_DIR = os.path.expanduser("~/.config/linuxzones")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")


@dataclass
class Zone:
    """Position and size as fractions of the screen (0.0–1.0)."""
    x: float
    y: float
    w: float
    h: float
    name: str = ""

    def pixel_rect(self, screen_w: int, screen_h: int) -> Tuple[int, int, int, int]:
        return (
            int(self.x * screen_w),
            int(self.y * screen_h),
            int(self.w * screen_w),
            int(self.h * screen_h),
        )

    def contains(self, fx: float, fy: float) -> bool:
        return self.x <= fx < self.x + self.w and self.y <= fy < self.y + self.h


@dataclass
class Layout:
    name: str
    zones: List[Zone] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "zones": [
                {"x": z.x, "y": z.y, "w": z.w, "h": z.h, "name": z.name}
                for z in self.zones
            ],
        }

    @classmethod
    def from_dict(cls, d: dict) -> Layout:
        return cls(
            name=d["name"],
            zones=[Zone(**z) for z in d.get("zones", [])],
        )

    def zone_at(self, screen_x: int, screen_y: int, screen_w: int, screen_h: int) -> Optional[int]:
        fx, fy = screen_x / screen_w, screen_y / screen_h
        for i, zone in enumerate(self.zones):
            if zone.contains(fx, fy):
                return i
        return None


DEFAULT_LAYOUTS: Dict[str, Layout] = {
    # 32:9 screen split 8|16|8 — side panels at 25%, centre at 50%
    "ultrawide-8-16-8": Layout("ultrawide-8-16-8", [
        Zone(0.0,  0.0, 0.25, 1.0, "left"),
        Zone(0.25, 0.0, 0.50, 1.0, "center"),
        Zone(0.75, 0.0, 0.25, 1.0, "right"),
    ]),
    "halves": Layout("halves", [
        Zone(0.0, 0.0, 0.5, 1.0, "left"),
        Zone(0.5, 0.0, 0.5, 1.0, "right"),
    ]),
    "thirds": Layout("thirds", [
        Zone(0.0,       0.0, 1/3, 1.0, "left"),
        Zone(1/3,       0.0, 1/3, 1.0, "center"),
        Zone(2/3,       0.0, 1/3, 1.0, "right"),
    ]),
    "quad": Layout("quad", [
        Zone(0.0, 0.0, 0.5, 0.5, "top-left"),
        Zone(0.5, 0.0, 0.5, 0.5, "top-right"),
        Zone(0.0, 0.5, 0.5, 0.5, "bottom-left"),
        Zone(0.5, 0.5, 0.5, 0.5, "bottom-right"),
    ]),
    "primary-sidebar": Layout("primary-sidebar", [
        Zone(0.0,  0.0, 0.65, 1.0,  "main"),
        Zone(0.65, 0.0, 0.35, 0.5,  "sidebar-top"),
        Zone(0.65, 0.5, 0.35, 0.5,  "sidebar-bottom"),
    ]),
}


def load_config() -> Tuple[Dict[str, Layout], str, float, bool]:
    """Returns (layouts, active_layout_name, overlay_opacity, shift_snap).
    Falls back to defaults."""
    if not os.path.exists(CONFIG_FILE):
        return dict(DEFAULT_LAYOUTS), "ultrawide-8-16-8", 0.5, False
    try:
        with open(CONFIG_FILE) as f:
            data = json.load(f)
        layouts = {
            name: Layout.from_dict(ldict)
            for name, ldict in data.get("layouts", {}).items()
        }
        if not layouts:
            layouts = dict(DEFAULT_LAYOUTS)
        active = data.get("active_layout", next(iter(layouts)))
        if active not in layouts:
            active = next(iter(layouts))
        opacity = float(data.get("overlay_opacity", 0.5))
        opacity = max(0.1, min(0.9, opacity))
        shift_snap = bool(data.get("shift_snap", False))
        return layouts, active, opacity, shift_snap
    except Exception as e:
        print(f"[linuxzones] Config load error: {e}. Using defaults.")
        return dict(DEFAULT_LAYOUTS), "ultrawide-8-16-8", 0.5, False


def save_config(
    layouts: Dict[str, Layout],
    active_layout: str,
    opacity: float = 0.5,
    shift_snap: bool = False,
) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(
            {
                "active_layout": active_layout,
                "overlay_opacity": round(opacity, 2),
                "shift_snap": shift_snap,
                "layouts": {name: l.to_dict() for name, l in layouts.items()},
            },
            f,
            indent=2,
        )
