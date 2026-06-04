#!/usr/bin/env bash
# LinuxZones installer — run once after cloning / after an update
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Refuse to continue if the checkout path contains shell metacharacters that
# would be re-evaluated when the generated .desktop Exec= lines are parsed.
case "$SCRIPT_DIR" in
    *'`'*|*'$'*|*'"'*|*'\'*|*"'"*)
        echo "ERROR: LinuxZones path contains unsafe characters:" >&2
        echo "         $SCRIPT_DIR" >&2
        echo "       Move the project to a safe path and re-run." >&2
        exit 1
        ;;
esac

VERSION="$(python3 -c "import re; print(re.search(r\"__version__ = [\\\"'](.*?)[\\\"']\", open('$SCRIPT_DIR/linuxzones/__init__.py').read()).group(1))")"

echo "=== LinuxZones $VERSION ==="
echo ""

LZ_BIN="$HOME/.local/bin/linuxzones"
SERVICE_DIR="$HOME/.config/systemd/user"
APP_DESKTOP="$HOME/.local/share/applications/linuxzones.desktop"
DESK_SHORTCUT="$HOME/Desktop/LinuxZones.desktop"
AUTOSTART="$HOME/.config/autostart/linuxzones.desktop"

# ------------------------------------------------------------------ stop running instance

echo "==> Stopping any running LinuxZones instance..."
if pkill -x linuxzones 2>/dev/null; then
    sleep 0.5
    echo "  Stopped."
else
    echo "  Not running — nothing to stop."
fi

# ------------------------------------------------------------------ packages

echo "==> Installing system packages..."
if command -v apt &>/dev/null; then
    sudo apt install -y \
        python3-pip python3-xlib python3-tk python3-pil wmctrl \
        python3-gi gir1.2-gtk-3.0
elif command -v dnf &>/dev/null; then
    sudo dnf install -y python3-pip python3-xlib python3-tkinter \
        python3-pillow wmctrl python3-gobject gtk3
elif command -v pacman &>/dev/null; then
    sudo pacman -S --noconfirm python-pip python-xlib tk python-pillow \
        wmctrl python-gobject gtk3
else
    echo "WARNING: Unknown package manager."
    echo "         Install python3-pip, python3-xlib, python3-tk, python3-pil,"
    echo "         wmctrl manually, then re-run."
fi

# ------------------------------------------------------------------ install Python package

echo "==> Installing LinuxZones..."
pip install --user --break-system-packages -q "$SCRIPT_DIR"
echo "  → $LZ_BIN"

# ------------------------------------------------------------------ icon

echo "==> Generating icon..."
python3 - "$SCRIPT_DIR/icon.png" <<'PYEOF'
import sys
out = sys.argv[1]
try:
    from PIL import Image, ImageDraw
    S = 128
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    try:
        d.rounded_rectangle([0, 0, S-1, S-1], radius=18, fill="#1e2a3a")
    except AttributeError:                       # Pillow < 8.2
        d.rectangle([0, 0, S-1, S-1], fill="#1e2a3a")
    d.rectangle([ 8, 14,  30, S-14], fill="#4a90d9")   # left
    d.rectangle([34, 14, S-34, S-14], fill="#dce8ff")  # centre
    d.rectangle([S-30, 14, S-8, S-14], fill="#4a90d9") # right
    img.save(out)
    print(f"  Saved {out}")
except ImportError:
    print("  python3-pil not available — will use system icon instead")
PYEOF

echo "==> Installing icon to user icon theme..."
HICOLOR_DIR="$HOME/.local/share/icons/hicolor"
mkdir -p "$HICOLOR_DIR/128x128/apps"
if [ -f "$SCRIPT_DIR/icon.png" ]; then
    cp "$SCRIPT_DIR/icon.png" "$HICOLOR_DIR/128x128/apps/linuxzones.png"
    gtk-update-icon-cache "$HICOLOR_DIR" --ignore-theme-index -q 2>/dev/null || true
    echo "  Icon installed as 'linuxzones' in hicolor theme."
fi

# ------------------------------------------------------------------ .desktop helper

make_desktop_content() {
    local exec_line="$1"
    cat <<EOF
[Desktop Entry]
Type=Application
Name=LinuxZones
GenericName=Window Zone Manager
Comment=FancyZones-like window snapping for X11
Exec=$exec_line
Icon=linuxzones
Terminal=false
Categories=Utility;
StartupNotify=false
StartupWMClass=linuxzones
EOF
}

# ------------------------------------------------------------------ app-menu entry

echo "==> Creating application menu entry..."
mkdir -p "$(dirname "$APP_DESKTOP")"
make_desktop_content "$LZ_BIN" > "$APP_DESKTOP"

# ------------------------------------------------------------------ desktop shortcut

echo "==> Creating desktop shortcut..."
mkdir -p "$HOME/Desktop"
make_desktop_content "$LZ_BIN" > "$DESK_SHORTCUT"
chmod +x "$DESK_SHORTCUT"

gio set "$DESK_SHORTCUT" metadata::trusted true 2>/dev/null \
    || xattr -w com.apple.metadata 'trusted' "$DESK_SHORTCUT" 2>/dev/null \
    || true   # silently ignore if neither tool is available

# ------------------------------------------------------------------ systemd user service (preferred) or autostart .desktop (fallback)

echo "==> Setting up autostart..."
mkdir -p "$SERVICE_DIR"
cp "$SCRIPT_DIR/linuxzones.service" "$SERVICE_DIR/linuxzones.service"

if systemctl --user daemon-reload 2>/dev/null && \
   systemctl --user enable linuxzones.service 2>/dev/null; then
    echo "  → systemd service enabled (starts automatically at next login)"
    echo "     Manage it with: systemctl --user {start|stop|restart|status} linuxzones"
    # Remove the old .desktop autostart — the service takes over.
    # Having both would open the editor on every login (second invocation sends SIGUSR1).
    rm -f "$AUTOSTART"
else
    echo "  → systemd user session unavailable; using .desktop autostart as fallback"
    mkdir -p "$(dirname "$AUTOSTART")"
    make_desktop_content "$LZ_BIN" > "$AUTOSTART"
fi

# ------------------------------------------------------------------ start now (no re-login required)

echo "==> Starting LinuxZones..."
setsid "$LZ_BIN" > /dev/null 2>&1 &
disown
sleep 0.6
if pgrep -x linuxzones > /dev/null 2>&1; then
    echo "  → running (PID $(pgrep -x linuxzones | head -1))"
else
    echo "  → not running yet; will start automatically at next login"
    echo "     Or run manually: $LZ_BIN"
fi

# ------------------------------------------------------------------ done

echo ""
echo "======================================================"
echo " LinuxZones $VERSION installed successfully!"
echo "======================================================"
echo ""
echo " Double-click 'LinuxZones' on your desktop to open"
echo " the layout editor (while running) or start it."
echo ""
echo " Logs (desktop / service launches):"
echo "   ~/.local/share/linuxzones/linuxzones.log"
echo "   journalctl --user -u linuxzones -f"
echo ""
echo " Command-line:"
echo "   linuxzones               start"
echo "   linuxzones editor        open layout editor"
echo "   linuxzones list          list saved layouts"
echo "   pkill linuxzones         stop"
echo "======================================================"
