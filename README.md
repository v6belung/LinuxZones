# LinuxZones

**Current version: 0.1.10**

Window zone snapping for Linux — the core FancyZones workflow from Windows PowerToys, for X11. Drag a window, hold right-click over a zone to snap it, or hover near a zone boundary to span two zones at once. An optional keyboard modifier trigger (Shift / Alt / Ctrl) is available as an alternative to right-click.

---

## Requirements

- Linux with an **X11 or XWayland** session (XWayland is on by default in GNOME and KDE)
- Python 3.10+
- `wmctrl`, `python-xlib`, `Pillow`, `tkinter`

---

## Installation

**Debian / Ubuntu / Linux Mint** — download the latest `.deb` from [GitHub Releases](https://github.com/v6belung/LinuxZones/releases) and install:

```bash
sudo apt install ./linuxzones_*.deb
```

**All other distros** — clone and run the installer:

```bash
git clone https://github.com/v6belung/LinuxZones.git
cd LinuxZones
bash install.sh
```

After installation, **double-click the LinuxZones icon** on your desktop to start. The app runs silently in the background and starts automatically on every login.

> **Cinnamon:** if the desktop icon shows an "Allow Launching?" prompt, right-click → **Allow Launching**, then double-click. One-time only.

---

## How snapping works

### Right-click trigger (default)

1. Left-click and drag a window by its title bar.
2. **Hold right-click** — the zone overlay appears.
3. Move into the zone you want — it highlights white.
4. **Release right-click** — the window snaps to that zone.

Release the left button at any point to cancel. A quick right-click (tap, not hold) while dragging works the same way.

### Spanning two zones

Move the cursor within **10 px of the boundary** between two adjacent zones — a white strip appears there instead of highlighting either zone. Releasing snaps the window to the **combined bounding box** of both zones.

### Keyboard modifier trigger (optional)

Enable it in the Layout Editor under **Settings**, then choose **Shift**, **Alt**, or **Ctrl**.

1. Left-click and drag a window.
2. **Hold the modifier** — overlay appears.
3. Move to the desired zone.
4. **Release the modifier** — window snaps.

Right-click always works regardless of modifier settings.

> **Privacy:** the modifier trigger makes LinuxZones monitor key-press events globally (in addition to mouse events) so it can detect the modifier during a drag. Keystrokes are checked in-process for the chosen modifier only and are never recorded, stored, or transmitted. Leave this off to limit monitoring to the mouse.

---

## Configuring zones

**Double-click the desktop icon** while LinuxZones is running to open the **Layout Editor**, or run `linuxzones editor`.

### Drawing zones

1. **Click and drag** on the canvas to draw a zone rectangle.
2. Zones snap to a 5% grid automatically.
3. **Left-click** a zone to select it; **right-click** (or **Delete Zone**) to remove it.
4. Zones can overlap — the zone the cursor is inside when you release wins.

Zone coordinates are stored as fractions of the work area (0.0–1.0), so layouts are resolution-independent. The editor shows pixel dimensions for your current resolution. Configuration is saved to `~/.config/linuxzones/config.json`.

### Managing layouts

| Action | How |
|---|---|
| Switch active layout | Click a name in the Layouts list |
| Create / duplicate | **New** / **Duplicate** |
| Rename | **Rename** or double-click the name |
| Delete | **Delete** (at least one layout must remain) |
| Apply a preset | Click any preset name — replaces the current layout's zones |

### Built-in presets

| Preset | Description |
|---|---|
| `ultrawide-8-16-8` | **Default.** 32:9 split: side panels 25%, centre 50% |
| `halves` | Left / right halves |
| `thirds` | Three equal columns |
| `quad` | Four equal quadrants |
| `primary-sidebar` | 65% main + two sidebar slots |

---

## Logs

When launched from the desktop icon, output goes to:

```
~/.local/share/linuxzones/linuxzones.log
```

Each session appends a timestamped block.

---

## Multiple monitors

Zones are defined relative to the full X11 screen, which spans all monitors. Keep zones within a monitor by constraining their coordinates to that monitor's fraction.

**Two 1920×1080 monitors side by side (total 3840×1080):**

| Zone | x | y | w | h |
|---|---|---|---|---|
| Left monitor — left half  | 0.00 | 0.00 | 0.25 | 1.00 |
| Left monitor — right half | 0.25 | 0.00 | 0.25 | 1.00 |
| Right monitor — left half | 0.50 | 0.00 | 0.25 | 1.00 |
| Right monitor — right half | 0.75 | 0.00 | 0.25 | 1.00 |

---

## Updating

**From a .deb install:** download the new `.deb` from [GitHub Releases](https://github.com/v6belung/LinuxZones/releases) and re-run `sudo apt install ./linuxzones_*.deb`.

**From git:**

```bash
git pull
bash install.sh
```

The installer stops any running instance automatically. Log out and back in, or run `linuxzones` to start the updated version immediately.

---

## Command-line

```bash
linuxzones                       # start
linuxzones editor                # open layout editor
linuxzones list                  # list saved layouts
linuxzones run --layout thirds   # start with a specific layout
linuxzones --version
```

---

## Troubleshooting

**"Allow Launching?" dialog on every launch**
Right-click the icon → **Allow Launching**. If it persists, re-run `bash install.sh`.

**Overlay doesn't appear**
- Check you're in an X11 session: `echo $DISPLAY` should print `:0` or similar.
- Check the RECORD extension: `xdpyinfo | grep RECORD`. If absent, the X server needs it enabled.

**Window snaps to wrong position**
- Some windows resist external repositioning (fullscreen games, some Electron apps) — this is per-app, not a bug.
- If snapping feels unreliable, increase `SNAP_DELAY` in `daemon.py` from `0.10` to `0.15`.

**Right-click does nothing**
Hold the left button down while right-clicking — both must be held simultaneously.

**`No module named 'Xlib'`**
```bash
sudo apt install python3-xlib        # Debian / Ubuntu / Mint
sudo dnf install python3-xlib        # Fedora
sudo pacman -S python-xlib           # Arch
```

**`_tkinter.TclError` on start**
```bash
sudo apt install python3-tk
```

---

## Config file reference

`~/.config/linuxzones/config.json`

```json
{
  "active_layout": "ultrawide-8-16-8",
  "overlay_opacity": 0.5,
  "modifier_snap": false,
  "modifier_key": "shift",
  "layouts": {
    "ultrawide-8-16-8": {
      "name": "ultrawide-8-16-8",
      "zones": [
        {"x": 0.0,  "y": 0.0, "w": 0.25, "h": 1.0, "name": "left"},
        {"x": 0.25, "y": 0.0, "w": 0.50, "h": 1.0, "name": "center"},
        {"x": 0.75, "y": 0.0, "w": 0.25, "h": 1.0, "name": "right"}
      ]
    }
  }
}
```

Zone values are fractions (0.0–1.0). The `name` field is optional and used as the zone label in the overlay.

---

## License

MIT
