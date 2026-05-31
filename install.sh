#!/usr/bin/env bash
# LinuxZones installer — run once after cloning / after an update
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# $SCRIPT_DIR is interpolated into the /usr/local/bin/linuxzones launcher and
# several .desktop Exec= lines below.  If the checkout path contains shell
# metacharacters they would be re-evaluated when the launcher runs (command
# injection) or break the generated files.  Refuse to proceed in that case
# rather than emit a booby-trapped launcher.
case "$SCRIPT_DIR" in
    *'`'*|*'$'*|*'"'*|*'\'*|*"'"*)
        echo "ERROR: LinuxZones path contains unsafe characters:" >&2
        echo "         $SCRIPT_DIR" >&2
        echo "       Move the project to a path without \$ \` \" ' or backslash and re-run." >&2
        exit 1
        ;;
esac

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
    # Three zones: 8 | 16 | 8  (25% | 50% | 25%)
    d.rectangle([ 8, 14,  30, S-14], fill="#4a90d9")   # left
    d.rectangle([34, 14, S-34, S-14], fill="#dce8ff")  # centre
    d.rectangle([S-30, 14, S-8, S-14], fill="#4a90d9") # right
    img.save(out)
    print(f"  Saved {out}")
except ImportError:
    print("  python3-pil not available — will use system icon instead")
PYEOF

# Install the icon into the user's hicolor icon theme so system monitors and
# app launchers can find it by name ("linuxzones") rather than by absolute path.
# Absolute-path Icon= fields are ignored by many system monitors; theme names work.
echo "==> Installing icon to user icon theme..."
HICOLOR_DIR="$HOME/.local/share/icons/hicolor"
mkdir -p "$HICOLOR_DIR/128x128/apps"
if [ -f "$SCRIPT_DIR/icon.png" ]; then
    cp "$SCRIPT_DIR/icon.png" "$HICOLOR_DIR/128x128/apps/linuxzones.png"
    # gtk-update-icon-cache refreshes the theme index so the new icon is found
    gtk-update-icon-cache "$HICOLOR_DIR" --ignore-theme-index -q 2>/dev/null || true
    echo "  Icon installed as 'linuxzones' in hicolor theme."
fi

# ------------------------------------------------------------------ /usr/local/bin launcher
# 'exec -a linuxzones' sets argv[0] so tools like 'ps' show 'linuxzones'.
# prctl inside the Python script sets /proc/PID/comm for system monitors.

echo "==> Creating 'linuxzones' command..."
sudo tee /usr/local/bin/linuxzones > /dev/null <<EOF
#!/usr/bin/env bash
exec -a linuxzones python3 "$SCRIPT_DIR/linuxzones.py" "\$@"
EOF
sudo chmod +x /usr/local/bin/linuxzones

# ------------------------------------------------------------------ .desktop content (shared)

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
make_desktop_content "linuxzones" > "$APP_DESKTOP"

# ------------------------------------------------------------------ desktop shortcut

echo "==> Creating desktop shortcut..."
mkdir -p "$HOME/Desktop"
make_desktop_content "linuxzones" > "$DESK_SHORTCUT"
chmod +x "$DESK_SHORTCUT"

# Mark as trusted so Cinnamon launches it directly without the "Allow?" dialog
gio set "$DESK_SHORTCUT" metadata::trusted true 2>/dev/null \
    || xattr -w com.apple.metadata 'trusted' "$DESK_SHORTCUT" 2>/dev/null \
    || true   # silently ignore if neither tool is available

# ------------------------------------------------------------------ autostart

echo "==> Enabling autostart on login..."
mkdir -p "$(dirname "$AUTOSTART")"
make_desktop_content "linuxzones" > "$AUTOSTART"

# ------------------------------------------------------------------ done

echo ""
echo "======================================================"
echo " LinuxZones installed successfully!"
echo "======================================================"
echo ""
echo " Double-click 'LinuxZones' on your desktop to start."
echo " LinuxZones runs silently in the background."
echo " It will also start automatically on next login."
echo ""
echo " Double-click the icon again while running to open"
echo " the layout editor."
echo ""
echo " Command-line usage (optional):"
echo "   linuxzones              start (same as double-click)"
echo "   linuxzones editor       open layout editor standalone"
echo "   linuxzones list         list saved layouts"
echo "   pkill linuxzones        stop the running instance"
echo ""
echo " Logs (when launched from desktop):"
echo "   ~/.local/share/linuxzones/linuxzones.log"
echo "======================================================"
