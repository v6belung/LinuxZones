# LinuxZones — CLAUDE.md

## What this is
FancyZones-like window snapping for X11. While dragging a window, hold
right-click to show a zone overlay, then release over a zone to snap.
Requires XWayland or native X11; does not support pure Wayland.

## Related project
**LinuxAudioSwitcher (LAS)** lives at `~/LinuxAudioSwitcher`. It is a
separate app by the same author. Comes up when comparing autostart
behaviour or desktop integration patterns.

## Architecture
| File | Role |
|---|---|
| `__main__.py` | CLI entry point, app lifecycle, Tk root, queue pump |
| `zones.py` | Data model (`Zone`, `Layout`, `ZonesConfig`), config I/O, monitor detection |
| `editor.py` | Tkinter layout editor — canvas + three-column panel below |
| `overlay.py` | Full-screen transparent Toplevel, draws zone rectangles |
| `daemon.py` | X11 RECORD event loop, drag detection, window snapping |

## Key decisions

**Autostart — XDG `.desktop` only, no systemd service.**
`graphical-session.target` is not activated by most desktop environments,
so the systemd service would silently stay inactive. The `.desktop` file
in `~/.config/autostart/` is what LAS uses and is universally reliable.
`install.sh` disables any previously-installed service on re-run.

**Zone coordinates are monitor-relative fractions (0.0–1.0).**
In single-monitor mode they are fractions of the work area. In
multi-monitor mode each monitor's layout zones are fractions of *that
monitor's* own dimensions, not the full virtual screen.

**Monitor detection uses `Xlib.ext.randr.get_monitors()`.**
The struct fields are `width_in_pixels` / `height_in_pixels` — not
`width` / `height` (which don't exist and will raise `AttributeError`).
Falls back to a single pseudo-monitor covering the full screen if RandR
is unavailable.

**Daemon uses two X display connections.**
`ctrl_dpy` for window queries and manipulation; `record_dpy` exclusively
for the blocking RECORD event loop. Never cross them.

**Window snapping priority:** wmctrl → `_NET_MOVERESIZE_WINDOW` (EWMH)
→ direct `XConfigureWindow`. wmctrl is most reliable on Cinnamon/Muffin.

**Highlight queue message is a 3-tuple:** `("highlight", zone_idx, monitor_name)`
where `monitor_name` is `None` in single-monitor mode.

## Testing
Tests run without an X server. `tests/conftest.py` builds `ZoneDaemon`
via `object.__new__` and sets only the attributes the state-machine code
touches. When adding daemon attributes, also add them to the `_factory`
in `conftest.py` or tests will break with `AttributeError`.

## Versioning

The single source of truth for the version is `linuxzones/__init__.py` (`__version__`).
`pyproject.toml` must always match it exactly. When bumping the version, update **both**
files in the same commit. Never let them drift.

To cut a release: bump both files, commit, then push a `v<version>` tag — the GitHub
Actions release workflow triggers on that tag.

## Config
`~/.config/linuxzones/config.json` — written atomically (temp file +
fsync + `os.replace`). Includes `monitor_layouts` dict mapping RandR
output name (e.g. `"HDMI-1"`) to layout name; absent entries fall back
to `active_layout`.
