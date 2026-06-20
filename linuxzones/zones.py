from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Union
import json
import math
import os
import tempfile


@dataclass
class MonitorInfo:
    """Geometry of one connected monitor (absolute virtual-screen coordinates)."""
    name: str
    x: int
    y: int
    w: int
    h: int


def get_monitors() -> List[MonitorInfo]:
    """Return connected monitors via RandR, sorted left→right then top→bottom.

    Falls back to a single pseudo-monitor covering the full screen when RandR
    is unavailable or returns nothing.
    """
    import Xlib.display
    dpy = Xlib.display.Display()
    screen = dpy.screen()
    fallback = [MonitorInfo("screen", 0, 0,
                            screen.width_in_pixels, screen.height_in_pixels)]
    try:
        from Xlib.ext import randr
        result = randr.get_monitors(screen.root, True)
        monitors: List[MonitorInfo] = []
        for m in result.monitors:
            try:
                name = dpy.get_atom_name(m.name)
            except Exception:
                name = f"monitor-{len(monitors)}"
            monitors.append(MonitorInfo(
                name, m.x, m.y, m.width_in_pixels, m.height_in_pixels))
        dpy.close()
        if monitors:
            return sorted(monitors, key=lambda mo: (mo.y, mo.x))
    except Exception:
        dpy.close()
    return fallback

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

    def area(self) -> float:
        return self.w * self.h


_LABEL_EPS = 1e-6  # float tolerance for edge-alignment checks in label_anchor


def label_anchor(zone: Zone, zones: List[Zone]) -> Tuple[float, float]:
    """Fractional (x, y) for zone's label, shifted away from any smaller,
    overlapping zone's border.

    Smaller zones are drawn on top (see overlay/editor draw order), so a
    label centred in the full rect can sit under a smaller zone's border.
    If a smaller overlapping zone fully spans this zone's width or height,
    the label is centred in the remaining strip instead.
    """
    rx0, ry0 = zone.x, zone.y
    rx1, ry1 = zone.x + zone.w, zone.y + zone.h
    for other in zones:
        if other is zone or other.area() >= zone.area():
            continue
        ox0, oy0 = other.x, other.y
        ox1, oy1 = other.x + other.w, other.y + other.h
        ix0, iy0 = max(rx0, ox0), max(ry0, oy0)
        ix1, iy1 = min(rx1, ox1), min(ry1, oy1)
        if ix0 >= ix1 or iy0 >= iy1:
            continue  # no overlap
        if ix0 <= rx0 + _LABEL_EPS and ix1 >= rx1 - _LABEL_EPS:
            if iy0 <= ry0 + _LABEL_EPS:
                ry0 = max(ry0, iy1)
            elif iy1 >= ry1 - _LABEL_EPS:
                ry1 = min(ry1, iy0)
        elif iy0 <= ry0 + _LABEL_EPS and iy1 >= ry1 - _LABEL_EPS:
            if ix0 <= rx0 + _LABEL_EPS:
                rx0 = max(rx0, ix1)
            elif ix1 >= rx1 - _LABEL_EPS:
                rx1 = min(rx1, ix0)
    if rx1 <= rx0 or ry1 <= ry0:
        return zone.x + zone.w / 2, zone.y + zone.h / 2
    return (rx0 + rx1) / 2, (ry0 + ry1) / 2


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
        """Return the index of the smallest zone containing the point.

        When zones overlap, the smallest-area zone wins, so a small zone
        nested inside a larger one stays reachable for hover/snap. Ties
        (equal area) fall back to list order.
        """
        fx, fy = screen_x / screen_w, screen_y / screen_h
        best: Optional[int] = None
        for i, zone in enumerate(self.zones):
            if zone.contains(fx, fy):
                if best is None or zone.area() < self.zones[best].area():
                    best = i
        return best

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

    def zone_for_point(self, fx: float, fy: float) -> Optional[int]:
        """Index of the zone best matching fractional point (fx, fy).

        Used for keyboard navigation, where a window's centre is the point.
        Containment uses the project's smallest-area-wins rule (so a nested
        zone stays reachable); if the point is inside no zone, fall back to the
        zone whose centre is closest, so the first Super+Arrow always has a
        starting zone.  Returns None only when the layout has no zones.
        """
        if not self.zones:
            return None
        best: Optional[int] = None
        for i, zone in enumerate(self.zones):
            if zone.contains(fx, fy):
                if best is None or zone.area() < self.zones[best].area():
                    best = i
        if best is not None:
            return best
        # No containing zone — nearest centre wins.
        return min(
            range(len(self.zones)),
            key=lambda i: (self.zones[i].x + self.zones[i].w / 2 - fx) ** 2
                        + (self.zones[i].y + self.zones[i].h / 2 - fy) ** 2,
        )

    def zone_in_direction(self, from_idx: int, direction: str) -> Optional[int]:
        """Index of the next zone from ``from_idx`` in ``direction``.

        ``direction`` is one of "left", "right", "up", "down".  Uses the
        FancyZones "relative position" model: candidates are zones whose centre
        lies strictly in the pressed direction; among them, zones overlapping
        the source on the perpendicular axis are preferred, then the nearest by
        primary-axis gap, then the smallest perpendicular offset, then list
        order (matching the equal-area tie-break in ``zone_at``).  Returns None
        when there is no zone in that direction (the caller may then traverse to
        an adjacent monitor).
        """
        if from_idx < 0 or from_idx >= len(self.zones):
            return None
        src = self.zones[from_idx]
        scx, scy = src.x + src.w / 2, src.y + src.h / 2
        horizontal = direction in ("left", "right")

        best: Optional[int] = None
        best_key: Optional[Tuple[bool, float, float, int]] = None
        for i, z in enumerate(self.zones):
            if i == from_idx:
                continue
            cx, cy = z.x + z.w / 2, z.y + z.h / 2
            if direction == "right":
                if cx <= scx:
                    continue
                primary = cx - scx
            elif direction == "left":
                if cx >= scx:
                    continue
                primary = scx - cx
            elif direction == "down":
                if cy <= scy:
                    continue
                primary = cy - scy
            elif direction == "up":
                if cy >= scy:
                    continue
                primary = scy - cy
            else:
                return None

            if horizontal:
                overlap = src.y < z.y + z.h and z.y < src.y + src.h
                perp = abs(cy - scy)
            else:
                overlap = src.x < z.x + z.w and z.x < src.x + src.w
                perp = abs(cx - scx)

            # not-overlap sorts after overlap; then nearest primary, perp, index.
            key = (not overlap, primary, perp, i)
            if best_key is None or key < best_key:
                best_key, best = key, i
        return best


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
    layouts:         Dict[str, Layout]
    active:          str
    opacity:         float = 0.5
    mod_snap:        bool  = False
    mod_key:         str   = DEFAULT_MODIFIER
    monitor_layouts: Dict[str, str] = field(default_factory=dict)
    # Keyboard zone navigation: Super+Arrow moves the active window between
    # zones.  Enabling it clears the conflicting WM shortcut (snapshotted into
    # kbd_move_saved_bindings) so it can be restored when disabled — see daemon.
    kbd_move:                bool = False
    kbd_move_saved_bindings: Dict[str, list] = field(default_factory=dict)


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
        raw_ml = data.get("monitor_layouts", {})
        monitor_layouts: Dict[str, str] = {
            k: v for k, v in raw_ml.items()
            if isinstance(k, str) and isinstance(v, str) and v in layouts
        }
        kbd_move = bool(data.get("kbd_move", False))
        raw_saved = data.get("kbd_move_saved_bindings", {})
        saved_bindings: Dict[str, list] = {
            k: list(v) for k, v in raw_saved.items()
            if isinstance(k, str) and isinstance(v, list)
        }
        return ZonesConfig(layouts, active, opacity, modifier_snap, modifier_key,
                           monitor_layouts, kbd_move, saved_bindings)
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
        "monitor_layouts": dict(cfg.monitor_layouts),
        "kbd_move": bool(cfg.kbd_move),
        "kbd_move_saved_bindings": {k: list(v) for k, v in cfg.kbd_move_saved_bindings.items()},
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
