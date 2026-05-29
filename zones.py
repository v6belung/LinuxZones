from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
import json
import math
import os
import tempfile

CONFIG_DIR = os.path.expanduser("~/.config/linuxzones")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

# Keyboard modifiers that can trigger the overlay (canonical lowercase names).
# The first entry is the default used when an unknown value is encountered.
VALID_MODIFIERS: Tuple[str, ...] = ("shift", "alt", "ctrl")
DEFAULT_MODIFIER = "shift"


def _coerce_modifier(value) -> str:
    """Return a valid canonical modifier name, defaulting on anything unknown."""
    if isinstance(value, str) and value.lower() in VALID_MODIFIERS:
        return value.lower()
    return DEFAULT_MODIFIER


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


def _sanitize_zones(zones: List[Zone]) -> List[Zone]:
    """Validate zones loaded from an untrusted / hand-edited config so the
    geometry that reaches wmctrl and the overlay is always on-screen and sane.

    A zone is dropped if any coordinate is non-numeric or non-finite, or if its
    width or height is not positive.  Otherwise the origin is clamped to
    [0.0, 1.0] and the size is clamped so the zone stays within the screen, and
    the name is coerced to a string of at most 64 characters.
    """
    clean: List[Zone] = []
    for z in zones:
        try:
            x, y, w, h = float(z.x), float(z.y), float(z.w), float(z.h)
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(v) for v in (x, y, w, h)):
            continue
        if w <= 0.0 or h <= 0.0:
            continue
        x = min(max(x, 0.0), 1.0)
        y = min(max(y, 0.0), 1.0)
        w = min(w, 1.0 - x)
        h = min(h, 1.0 - y)
        if w <= 0.0 or h <= 0.0:
            continue
        name = z.name if isinstance(z.name, str) else str(z.name)
        clean.append(Zone(x, y, w, h, name[:64]))
    return clean


def load_config() -> Tuple[Dict[str, Layout], str, float, bool, str]:
    """Returns (layouts, active_layout_name, overlay_opacity, modifier_snap,
    modifier_key).  Falls back to defaults.

    Backward compatibility: configs written before the generic-modifier feature
    only have a boolean ``shift_snap`` key.  When the newer ``modifier_snap`` /
    ``modifier_key`` keys are absent, a truthy legacy ``shift_snap`` is mapped
    to (modifier_snap=True, modifier_key="shift").
    """
    if not os.path.exists(CONFIG_FILE):
        return dict(DEFAULT_LAYOUTS), "ultrawide-8-16-8", 0.5, False, DEFAULT_MODIFIER
    try:
        with open(CONFIG_FILE) as f:
            data = json.load(f)
        layouts = {}
        for name, ldict in data.get("layouts", {}).items():
            layout = Layout.from_dict(ldict)
            layout.zones = _sanitize_zones(layout.zones)
            layouts[name] = layout
        if not layouts:
            layouts = dict(DEFAULT_LAYOUTS)
        active = data.get("active_layout", next(iter(layouts)))
        if active not in layouts:
            active = next(iter(layouts))
        opacity = float(data.get("overlay_opacity", 0.5))
        opacity = max(0.1, min(0.9, opacity))

        # New keys take precedence; fall back to the legacy shift_snap boolean.
        legacy_shift = bool(data.get("shift_snap", False))
        modifier_snap = bool(data.get("modifier_snap", legacy_shift))
        modifier_key = _coerce_modifier(data.get("modifier_key", DEFAULT_MODIFIER))
        return layouts, active, opacity, modifier_snap, modifier_key
    except Exception as e:
        print(f"[linuxzones] Config load error: {e}. Using defaults.")
        return dict(DEFAULT_LAYOUTS), "ultrawide-8-16-8", 0.5, False, DEFAULT_MODIFIER


def save_config(
    layouts: Dict[str, Layout],
    active_layout: str,
    opacity: float = 0.5,
    modifier_snap: bool = False,
    modifier_key: str = DEFAULT_MODIFIER,
) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    payload = {
        "active_layout": active_layout,
        "overlay_opacity": round(opacity, 2),
        "modifier_snap": bool(modifier_snap),
        "modifier_key": _coerce_modifier(modifier_key),
        # Keep writing the legacy key so a config saved by a new version can
        # still be read by an older binary (graceful downgrade).
        "shift_snap": bool(modifier_snap) and _coerce_modifier(modifier_key) == "shift",
        "layouts": {name: l.to_dict() for name, l in layouts.items()},
    }
    # Atomic write: serialise to a temp file in the same directory, fsync it,
    # then os.replace() (atomic on POSIX) over the real config.  A crash or
    # power loss mid-write can no longer truncate or corrupt the user's layouts
    # — the old config stays intact until the new one is fully on disk.
    fd, tmp = tempfile.mkstemp(dir=CONFIG_DIR, prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CONFIG_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
