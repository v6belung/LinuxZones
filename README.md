# LinuxZones

**Current version: 0.1.4**

Window zone snapping for Linux — replicates the core FancyZones workflow from Windows PowerToys.

**Interaction model:** drag a window with the left mouse button, then **hold right-click** to show the zone overlay. Move into the zone you want and **release right-click** to snap and resize the window to fill it. Release the left button at any point to cancel without snapping.

---

## Requirements

- Linux with an **X11 session** (Xorg). Wayland is not supported.
- Python 3.10+
- `wmctrl` (for window repositioning)
- `python-xlib`, `Pillow`, `tkinter`

> **Cinnamon / Linux Mint users:** Cinnamon uses X11 by default. No extra steps needed.  
> **GNOME users:** at the login screen, click the gear icon and select **GNOME on Xorg**.  
> **KDE users:** select **Plasma (X11)** at login.

---

## Installation

```bash
git clone https://github.com/yourname/linuxzones.git
cd linuxzones
bash install.sh
```

The installer will:

1. Install system packages (`python3-xlib`, `python3-tk`, `python3-pil`, `wmctrl`, etc.) via your package manager.
2. Install `pystray` via pip.
3. Generate the tray icon (`icon.png`).
4. Create a **double-clickable desktop shortcut** (`~/Desktop/LinuxZones.desktop`).
5. Add LinuxZones to **autostart** so it runs automatically on every login (`~/.config/autostart/`).
6. Create a `linuxzones` command in `/usr/local/bin` for optional terminal use.

---

## Starting LinuxZones

**Double-click** the `LinuxZones` icon on your desktop.

The app starts silently in the background and places an icon in your **system tray** (notification area). That's it — you can start snapping windows immediately.

> If the desktop icon shows an "Allow Launching?" dialog, right-click it and choose **Allow Launching**, then double-click again. This is a one-time Cinnamon security prompt.

---

## Configuring zones

**Double-click the desktop icon** while LinuxZones is already running to open the **Layout Editor**. Or run `linuxzones editor` from a terminal.

### The editor interface

```
┌─────────────────┬────────────────────────────────────────┐
│  Layouts        │                                        │
│  ─────────────  │   (scaled screen preview)              │
│  ultrawide *    │                                        │
│  halves         │  ┌────────┬──────────────────┬──────┐  │
│  thirds         │  │        │                  │      │  │
│  ─────────────  │  │  left  │     center       │right │  │
│  Presets        │  │  25%   │       50%        │  25% │  │
│  halves         │  └────────┴──────────────────┴──────┘  │
│  thirds         │                                        │
│  quad           │                                        │
│  ─────────────  │                                        │
│  Zone 1 (left)  │                                        │
│  x=0.00 y=0.00  │                                        │
│  w=0.25 h=1.00  │                                        │
│  800×1440 px    │                                        │
│  [Delete Zone]  │                                        │
│  ─────────────  │                                        │
│  Overlay opacity│                                        │
│  [──────●────] 50%                                       │
├─────────────────┴────────────────────────────────────────┤
│                               [Cancel]  [Save & Close]   │
└──────────────────────────────────────────────────────────┘
```

### Drawing zones

1. **Click and drag** on the preview canvas to draw a new zone rectangle.
2. Zones snap to a 5% grid automatically.
3. Zones can overlap — whichever zone the cursor is inside when you release right-click is the one that receives the window.
4. **Left-click** an existing zone to select it and see its pixel dimensions.
5. **Right-click** a zone in the editor (or click **Delete Zone**) to remove it.

### Overlay opacity

The **Overlay opacity** slider in the editor controls how visible the zone overlay is during snapping. Drag to adjust from 10% (nearly invisible) to 90% (nearly solid). Click **Save & Close** to apply.

### Managing layouts

| Action | How |
|---|---|
| Switch active layout | Click a name in the Layouts list |
| Create new layout | Click **New**, enter a name |
| Copy a layout | Click **Duplicate** |
| Rename a layout | Click **Rename** |
| Delete a layout | Click **Delete** (at least one must remain) |
| Apply a preset | Click any preset name — it replaces the current layout's zones |

### Built-in presets

| Preset | Description |
|---|---|
| `ultrawide-8-16-8` | **Default.** 32:9 screen split 8\|16\|8 — side panels at 25%, centre at 50% |
| `halves` | Left half / right half |
| `thirds` | Three equal vertical columns |
| `quad` | Four equal quadrants |
| `primary-sidebar` | 65% main area + two sidebar slots on the right |

### Zone coordinates

Zones are stored as fractions of the screen (0.0–1.0), so layouts are resolution-independent and survive monitor changes. The editor shows the pixel dimensions for your current resolution as a reference.

```
x=0.00, y=0.00, w=0.50, h=1.00  →  left half of screen
x=0.50, y=0.00, w=0.50, h=1.00  →  right half of screen
x=0.00, y=0.00, w=1.00, h=0.50  →  top half of screen
```

Configuration is saved to `~/.config/linuxzones/config.json` and is human-editable.

---

## How snapping works

1. Left-click and hold the title bar of any window and begin dragging.
2. **Press and hold the right mouse button** — the zone overlay appears.
3. Move the cursor over the zone you want — it highlights white.
4. **Release the right mouse button** — the window snaps and resizes to fill that zone exactly.
5. To cancel without snapping, release the **left** mouse button instead.

> **Quick snap:** you don't have to hold right — a fast right-click (press + release)
> while dragging works the same way; the overlay appears briefly and snaps on release.

---

## Logs

When launched from the desktop icon (not a terminal), all output is written to:

```
~/.local/share/linuxzones/linuxzones.log
```

Each launch appends a timestamped block. Check here if something looks wrong.

---

## Multiple monitors

Zones are defined relative to the full X11 screen, which spans all monitors in a multi-monitor setup. To keep zones within a single monitor, set their coordinates to stay within that monitor's portion of the total screen.

**Example — two 1920×1080 monitors side by side (total: 3840×1080):**

| Zone | x | y | w | h | Pixel area |
|---|---|---|---|---|---|
| Left monitor — left half | 0.00 | 0.00 | 0.25 | 1.00 | 0–960 px |
| Left monitor — right half | 0.25 | 0.00 | 0.25 | 1.00 | 960–1920 px |
| Right monitor — left half | 0.50 | 0.00 | 0.25 | 1.00 | 1920–2880 px |
| Right monitor — right half | 0.75 | 0.00 | 0.25 | 1.00 | 2880–3840 px |

---

## Updating LinuxZones

After pulling new files, re-run the installer:

```bash
git pull
bash install.sh
```

The installer is idempotent — safe to run multiple times. To restart after an update:

```bash
pkill linuxzones        # stop the running instance
linuxzones              # start the new version
```

Or just log out and back in — autostart will launch the new version.

---

## Command-line usage (optional)

The installer also creates a `linuxzones` shell command:

```bash
linuxzones                  # start (same as double-clicking the icon)
linuxzones editor           # open layout editor without starting the tray app
linuxzones list             # list saved layouts
linuxzones run --layout thirds   # start with a specific layout override
linuxzones --version
```

---

## Troubleshooting

**Desktop icon shows "Allow Launching?" every time**

Right-click the icon → **Allow Launching**. If it keeps appearing after that, re-run `bash install.sh` which re-applies the trusted metadata.

**Overlay does not appear when dragging**

- Confirm you are in an X11 session: `echo $DISPLAY` should print something like `:0`.
- Check that the RECORD extension is available: `xdpyinfo | grep RECORD`. If absent, your X server may need the extension enabled.

**Window snaps to wrong position or does not snap**

- Some windows resist external repositioning (e.g., fullscreen games, certain Electron apps). This is a per-application restriction, not a bug.
- Try a small delay: if snapping feels unreliable, open `daemon.py` and increase `SNAP_RELEASE_DELAY` from `0.05` to `0.1`.

**Overlay appears but right-click does nothing**

- Make sure you are right-clicking **while still holding the left mouse button**.

**`ModuleNotFoundError: No module named 'Xlib'`**

```bash
sudo apt install python3-xlib   # Debian / Ubuntu / Linux Mint
sudo dnf install python3-xlib   # Fedora
sudo pacman -S python-xlib      # Arch
```

**`_tkinter.TclError` on start**

```bash
sudo apt install python3-tk
```

**Checking the log for errors**

```bash
cat ~/.local/share/linuxzones/linuxzones.log
```

---

## Config file reference

`~/.config/linuxzones/config.json`

```json
{
  "active_layout": "ultrawide-8-16-8",
  "overlay_opacity": 0.5,
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

All zone values are fractions of the screen (0.0–1.0). The `name` field is optional and shown as the zone label in the overlay.

---

## License

MIT
