from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Union
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

    def margin_at(self, screen_x: int, screen_y: int,
                  screen_w: int, screen_h: int) -> Optional[Tuple[int, int]]:
        """Return (i, j) when the cursor is within MARGIN_PX of the shared
        boundary between zones i and j.  Margins take priority over zone
        interiors so the strip always activates near a boundary."""
        fx = screen_x / screen_w
        fy = screen_y / screen_h
        mfx = MARGIN_PX / screen_w
        mfy = MARGIN_PX / screen_h
        for i in range(len(self.zones)):
            for j in range(i + 1, len(self.zones)):
                a, b = self.zones[i], self.zones[j]
                for za, zb in ((a, b), (b, a)):
                    # Vertical shared edge (za right meets zb left)
                    if abs((za.x + za.w) - zb.x) < _BOUNDARY_TOL:
                        vy0 = max(za.y, zb.y)
                        vy1 = min(za.y + za.h, zb.y + zb.h)
                        bx  = (za.x + za.w + zb.x) / 2
                        if vy0 < vy1 and vy0 <= fy <= vy1 and abs(fx - bx) <= mfx:
                            return (i, j)
                    # Horizontal shared edge (za bottom meets zb top)
                    if abs((za.y + za.h) - zb.y) < _BOUNDARY_TOL:
                        hx0 = max(za.x, zb.x)
                        hx1 = min(za.x + za.w, zb.x + zb.w)
                        by  = (za.y + za.h + zb.y) / 2
                        if hx0 < hx1 and hx0 <= fx <= hx1 and abs(fy - by) <= mfy:
                            return (i, j)
        return None

    def spanning_zone(self, i: int, j: int) -> Zone:
        """Return a Zone whose rect is the bounding box of zones i and j."""
        a, b = self.zones[i], self.zones[j]
        x = min(a.x, b.x)
        y = min(a.y, b.y)
        return Zone(x, y, max(a.x + a.w, b.x + b.w) - x,
                        max(a.y + a.h, b.y + b.h) - y)


MARGIN_PX = 10        # half-width of the between-zone snap strip (pixels)
_BOUNDARY_TOL = 0.002  # fractional tolerance for zone-edge adjacency detection

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


@dataclass
class ZonesConfig:
    """Runtime configuration: layouts + active selection + display settings."""
    layouts:  Dict[str, Layout]
    active:   str
    opacity:  float = 0.5
    mod_snap: bool  = False
    mod_key:  str   = DEFAULT_MODIFIER


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


def load_config() -> ZonesConfig:
    """Load config from disk and return a ZonesConfig.  Falls back to defaults.

    Backward compatibility: configs written before the generic-modifier feature
    only have a boolean ``shift_snap`` key.  When the newer ``modifier_snap`` /
    ``modifier_key`` keys are absent, a truthy legacy ``shift_snap`` is mapped
    to (mod_snap=True, mod_key="shift").
    """
    if not os.path.exists(CONFIG_FILE):
        return ZonesConfig(dict(DEFAULT_LAYOUTS), "ultrawide-8-16-8")
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
        return ZonesConfig(layouts, active, opacity, modifier_snap, modifier_key)
    except Exception as e:
        print(f"[linuxzones] Config load error: {e}. Using defaults.")
        return ZonesConfig(dict(DEFAULT_LAYOUTS), "ultrawide-8-16-8")


def save_config(cfg: ZonesConfig) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    payload = {
        "active_layout": cfg.active,
        "overlay_opacity": round(cfg.opacity, 2),
        "modifier_snap": bool(cfg.mod_snap),
        "modifier_key": _coerce_modifier(cfg.mod_key),
        # Keep writing the legacy key so a config saved by a new version can
        # still be read by an older binary (graceful downgrade).
        "shift_snap": bool(cfg.mod_snap) and _coerce_modifier(cfg.mod_key) == "shift",
        "layouts": {name: l.to_dict() for name, l in cfg.layouts.items()},
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
