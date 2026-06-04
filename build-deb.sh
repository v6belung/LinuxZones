#!/usr/bin/env bash
# Build a .deb package for LinuxZones.
# Usage: bash build-deb.sh [version]
set -euo pipefail

VERSION=${1:-$(python3 -c "
import re, sys
m = re.search(r'__version__ = [\"\'](.*?)[\"\']', open('linuxzones/__init__.py').read())
sys.stdout.write(m.group(1))
")}
PKG="linuxzones_${VERSION}_all"
echo "==> Building ${PKG}.deb"
rm -rf "$PKG"

# ── directory tree ──────────────────────────────────────────────────────────
install -d \
  "$PKG/DEBIAN" \
  "$PKG/usr/lib/linuxzones" \
  "$PKG/usr/bin" \
  "$PKG/usr/share/applications" \
  "$PKG/usr/share/icons/hicolor/128x128/apps" \
  "$PKG/usr/lib/systemd/user" \
  "$PKG/etc/xdg/autostart"

# ── Python package ────────────────────────────────────────────────────────────
cp -r linuxzones "$PKG/usr/lib/linuxzones/"

# ── systemd user service ──────────────────────────────────────────────────────
cp linuxzones.service "$PKG/usr/lib/systemd/user/linuxzones.service"

# ── icon ─────────────────────────────────────────────────────────────────────
ICON_DST="$PKG/usr/share/icons/hicolor/128x128/apps/linuxzones.png"
if [ -f icon.png ]; then
  cp icon.png "$ICON_DST"
else
  python3 - "$ICON_DST" << 'PYEOF'
import sys
try:
    from PIL import Image, ImageDraw
    S = 128
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    try:
        d.rounded_rectangle([0, 0, S-1, S-1], radius=18, fill="#1e2a3a")
    except AttributeError:
        d.rectangle([0, 0, S-1, S-1], fill="#1e2a3a")
    d.rectangle([ 8, 14,  30, S-14], fill="#4a90d9")
    d.rectangle([34, 14, S-34, S-14], fill="#dce8ff")
    d.rectangle([S-30, 14, S-8, S-14], fill="#4a90d9")
    img.save(sys.argv[1])
except ImportError:
    sys.stderr.write("Warning: Pillow not available — icon omitted\n")
PYEOF
fi

# ── launcher ─────────────────────────────────────────────────────────────────
# Sets PYTHONPATH so Python finds the linuxzones package in /usr/lib/linuxzones/,
# then uses -m to invoke it as a module (respects __main__.py entry point).
cat > "$PKG/usr/bin/linuxzones" << 'EOF'
#!/usr/bin/env bash
export PYTHONPATH=/usr/lib/linuxzones${PYTHONPATH:+:$PYTHONPATH}
exec -a linuxzones python3 -m linuxzones "$@"
EOF
chmod 755 "$PKG/usr/bin/linuxzones"

# ── desktop files ─────────────────────────────────────────────────────────────
DESKTOP="[Desktop Entry]
Type=Application
Name=LinuxZones
GenericName=Window Zone Manager
Comment=FancyZones-like window snapping for X11
Exec=linuxzones
Icon=linuxzones
Terminal=false
Categories=Utility;
StartupNotify=false
StartupWMClass=linuxzones"

printf '%s\n' "$DESKTOP" > "$PKG/usr/share/applications/linuxzones.desktop"
printf '%s\n' "$DESKTOP" > "$PKG/etc/xdg/autostart/linuxzones.desktop"

# ── DEBIAN/control ────────────────────────────────────────────────────────────
cat > "$PKG/DEBIAN/control" << EOF
Package: linuxzones
Version: ${VERSION}
Architecture: all
Maintainer: Aleksei Suvorov <aleksei.suvorov@gmail.com>
Depends: python3 (>= 3.10), python3-xlib, python3-tk, python3-pil, wmctrl
Section: utils
Priority: optional
Homepage: https://github.com/v6belung/LinuxZones
Description: FancyZones-like window zone snapping for X11
 Drag a window, hold right-click to show the zone overlay, then release
 to snap. Hover near a zone boundary to span two zones at once. Supports
 an optional keyboard modifier trigger. Requires X11 or XWayland.
EOF

# ── DEBIAN/postinst ───────────────────────────────────────────────────────────
cat > "$PKG/DEBIAN/postinst" << 'EOF'
#!/bin/bash
set -e
gtk-update-icon-cache /usr/share/icons/hicolor --ignore-theme-index -q 2>/dev/null || true
echo ""
echo "LinuxZones installed. To enable crash recovery via systemd:"
echo "  systemctl --user enable linuxzones.service"
echo "  systemctl --user start linuxzones.service"
echo ""
echo "Or start it now from the application menu / desktop shortcut."
EOF
chmod 755 "$PKG/DEBIAN/postinst"

# ── DEBIAN/prerm ──────────────────────────────────────────────────────────────
cat > "$PKG/DEBIAN/prerm" << 'EOF'
#!/bin/bash
pkill -x linuxzones 2>/dev/null || true
systemctl --user stop linuxzones.service 2>/dev/null || true
systemctl --user disable linuxzones.service 2>/dev/null || true
exit 0
EOF
chmod 755 "$PKG/DEBIAN/prerm"

# ── build ─────────────────────────────────────────────────────────────────────
dpkg-deb --build --root-owner-group "$PKG"
rm -rf "$PKG"
echo "==> Done: ${PKG}.deb"
