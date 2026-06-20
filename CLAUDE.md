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
`install.sh` disables any previously-installed service on re-run. The
`.deb` (`build-deb.sh`) follows the same rule: it ships only
`/etc/xdg/autostart/linuxzones.desktop`, no systemd unit. Don't
reintroduce a packaged systemd service for either install path.

**Zone coordinates are monitor-relative fractions (0.0–1.0).**
In single-monitor mode they are fractions of the work area. In
multi-monitor mode each monitor's layout zones are fractions of *that
monitor's* own dimensions, not the full virtual screen.

**Overlapping zones: smallest area wins.**
`Layout.zone_at()` and the editor's `_zone_at_canvas()` return the
smallest-area zone containing the cursor, not the first match in list
order. This keeps a small zone nested inside a larger one reachable for
hover/snap/select regardless of creation order. Equal-area ties fall back
to list order. Drawing order in `overlay.py` and `editor.py` mirrors this
(largest first, smallest last) so the smaller zone is never visually
covered.

**Zone labels use `label_anchor()`, not the rect center.**
`zones.label_anchor()` shifts a zone's label away from a smaller,
overlapping zone's border (e.g. a label at 50% height isn't hidden by the
top edge of a smaller zone drawn on top). Only handles the case where the
smaller zone fully spans the larger zone's width or height; falls back to
the plain center otherwise.

**Terminals: clear resize increments (and don't restore them), with a
resize-verify-retry loop.**
Terminals (gnome-terminal, xterm, …) advertise a character-cell resize
increment plus base size in `WM_NORMAL_HINTS`, so the WM rounds a
programmatic resize *down* to a whole cell and leaves a sliver of dead
space at the zone's bottom/right edge. OS maximize is exempt from
increments (per EWMH), which is why that fills fully. The fix took three
findings, each of which broke a simpler version:
- Clearing the `PResizeInc` flag (`_suppress_resize_increments()`) once is
  not enough — VTE/GTK re-asserts its own hints asynchronously and can win
  the race. So `_snap()` loops: clear → resize → read geometry back → if
  short (> `GEOM_TOL`) repeat, up to `SNAP_RETRIES`.
- A *maximized* terminal temporarily **drops** its increment hints, so a
  check right after `_unmaximize()` sees none and would skip the mechanism.
  The loop therefore runs unconditionally (not gated on increments being
  present at the start) and re-clears every pass.
- The cleared increments must **not** be restored. Re-applying them makes
  the WM immediately re-validate the window against the cell grid and shrink
  it back, reopening the gap (this was the bug that survived two earlier
  attempts). Leaving them cleared keeps the window filled; the terminal
  re-applies its own increments the next time the user resizes it manually.

The resize chain (wmctrl → EWMH → configure) lives in `_apply_geometry()`,
one attempt per call, returning the method name (the loop logs success).
Non-terminal windows have no increments: they fill on the first pass and
break immediately (one geometry read-back, ~one `SNAP_RETRY_GAP` of added
latency).

The geometry engine (un-maximize + increment retry loop + `_apply_geometry`)
lives in `_apply_zone()`. `_snap()` is the drag-release path: it first fakes a
B1 release to cancel the WM's drag grab, then calls `_apply_zone()`. The
keyboard-move path calls `_apply_zone()` directly (no drag, no fake release).

**Super+Arrow zone move uses passive RECORD + clearing the WM keybinding, not
`XGrabKey`.** `Super+Arrow` is owned by the WM (on Cinnamon,
`org.cinnamon.desktop.keybindings.wm push-tile-*`). X11 passive grabs are
exclusive, so a second `XGrabKey` on it just fails with `BadAccess` — grabbing
a WM-owned key is a dead end. Instead, when `kbd_move` is enabled the daemon
*clears* the conflicting accelerators via `gsettings` (Muffin releases its grab
live) and observes the keys through the **same passive RECORD path used for
modifier snap** — no grab, no extra display connection (still two), no
lock-mask permutations, no `MappingNotify`. The cleared bindings are
snapshotted into `kbd_move_saved_bindings` (persisted in config, so a crash
can't lose the originals) and restored on disable and on every clean exit
(`restore_kbd_bindings`, also via `atexit`), handing `Super+Arrow` back to the
WM whenever LinuxZones isn't running. `_free_super_arrows()` never re-records a
snapshot it already has, so re-freeing on the next startup can't overwrite the
originals with the (already-cleared) empty lists. Caveat: with the key
ungrabbed the focused app also receives `Super+Arrow`, but Super-modified keys
are conventionally WM-reserved and ignored by apps.

Navigation is spatial (`Layout.zone_in_direction`, FancyZones "relative
position": nearest zone in the arrow direction, perpendicular-overlap
preferred). `Layout.zone_for_point` finds the current zone (smallest-area wins,
nearest-centre fallback). At a monitor edge `_cross_monitor_target` /
`_entry_zone` hop to the adjacent monitor. Auto-repeat is rejected with the
same shared-timestamp trick as modifier snap, so one move per physical press.

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
to `active_layout`. `kbd_move` (bool) enables Super+Arrow zone navigation;
`kbd_move_saved_bindings` is the auto-managed snapshot of the WM shortcut(s)
cleared to free `Super+Arrow`, kept so they survive a crash and can be
restored.
