#!/usr/bin/env bash
#
# Install VexTrace for the current user on CachyOS / Arch.
#
#   - system packages via pacman (python, python-pyqt6, potrace)
#   - pip packages into a venv at ~/.local/share/vextrace/venv
#     (created with --system-site-packages so the pacman-installed PyQt6 is visible)
#   - launcher at ~/.local/bin/vextrace
#   - .desktop entry so it shows up in the Plasma menu and Dolphin's "Open With"
#
# CachyOS marks the system Python as externally managed, so pip must never touch
# it. This script only ever installs into the venv; --break-system-packages is
# deliberately not used anywhere.

set -euo pipefail

APP_ID="vextrace"
APP_NAME="VexTrace"
SRC_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}"
APP_DIR="$DATA_DIR/$APP_ID"
VENV_DIR="$APP_DIR/venv"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="$DATA_DIR/applications"
ICON_DIR="$DATA_DIR/icons/hicolor/scalable/apps"

PACMAN_PACKAGES=(python python-pyqt6 potrace)

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m==>\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m==>\033[0m %s\n' "$*" >&2; exit 1; }

[[ -f "$SRC_DIR/vextrace.py" ]] || die "vextrace.py not found next to install.sh"
[[ $EUID -ne 0 ]] || die "Run this as your normal user, not with sudo."

# ---------------------------------------------------------------- system packages
missing=()
for pkg in "${PACMAN_PACKAGES[@]}"; do
    pacman -Qq "$pkg" &>/dev/null || missing+=("$pkg")
done

if (( ${#missing[@]} )); then
    say "Installing system packages: ${missing[*]}"
    sudo pacman -S --needed "${missing[@]}"
else
    say "System packages already present: ${PACMAN_PACKAGES[*]}"
fi

# ---------------------------------------------------------------- venv
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    say "Creating venv at $VENV_DIR"
    mkdir -p "$APP_DIR"
    python3 -m venv --system-site-packages "$VENV_DIR"
else
    say "Reusing existing venv at $VENV_DIR"
fi

say "Installing Python packages into the venv"
"$VENV_DIR/bin/python" -m pip install --upgrade --quiet pip
"$VENV_DIR/bin/python" -m pip install --upgrade --quiet vtracer pillow

# PyQt6 comes from pacman and is visible through --system-site-packages.
if ! "$VENV_DIR/bin/python" -c 'import PyQt6.QtSvgWidgets' &>/dev/null; then
    warn "PyQt6 (with QtSvg) is not importable from the venv."
    warn "Install it with: sudo pacman -S python-pyqt6"
fi

# ---------------------------------------------------------------- app files
say "Installing application to $APP_DIR"
install -Dm644 "$SRC_DIR/vextrace.py" "$APP_DIR/vextrace.py"
install -Dm644 "$SRC_DIR/assets/vextrace.svg" "$ICON_DIR/$APP_ID.svg"
install -Dm644 "$SRC_DIR/assets/vextrace.svg" "$APP_DIR/assets/vextrace.svg"
for sample in "$SRC_DIR"/assets/samples/*; do
    install -Dm644 "$sample" "$APP_DIR/assets/samples/$(basename "$sample")"
done

# ---------------------------------------------------------------- launcher
say "Installing launcher to $BIN_DIR/$APP_ID"
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/$APP_ID" <<LAUNCHER
#!/bin/sh
# Launcher for $APP_NAME, written by install.sh
exec "$VENV_DIR/bin/python" "$APP_DIR/vextrace.py" "\$@"
LAUNCHER
chmod 755 "$BIN_DIR/$APP_ID"

# ---------------------------------------------------------------- desktop entry
say "Installing desktop entry to $DESKTOP_DIR"
mkdir -p "$DESKTOP_DIR"
cat > "$DESKTOP_DIR/$APP_ID.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Version=1.0
Name=$APP_NAME
GenericName=Image Vectorizer
Comment=Convert raster images into clean SVG vector graphics
Exec=$BIN_DIR/$APP_ID %f
Icon=$APP_ID
Terminal=false
Categories=Graphics;2DGraphics;RasterGraphics;VectorGraphics;Qt;
MimeType=image/png;image/jpeg;image/bmp;image/webp;image/gif;image/tiff;
Keywords=svg;vector;trace;vextrace;potrace;vtracer;
StartupNotify=true
StartupWMClass=vextrace
DESKTOP
chmod 644 "$DESKTOP_DIR/$APP_ID.desktop"

# Refresh the caches Plasma and Dolphin read.
command -v update-desktop-database &>/dev/null && update-desktop-database "$DESKTOP_DIR" || true
command -v gtk-update-icon-cache   &>/dev/null && gtk-update-icon-cache -qtf "$DATA_DIR/icons/hicolor" 2>/dev/null || true
command -v kbuildsycoca6           &>/dev/null && kbuildsycoca6 --noincremental &>/dev/null || true

# ---------------------------------------------------------------- smoke test
say "Verifying the install"
"$VENV_DIR/bin/python" "$APP_DIR/vextrace.py" --version

echo
say "$APP_NAME installed."
echo "  GUI : run 'vextrace', or find it in the Plasma menu under Graphics"
echo "  CLI : vextrace input.png -o out.svg --mode color --preset logo"
echo "  Dolphin: right-click an image -> Open With -> $APP_NAME"

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo
       warn "$BIN_DIR is not on your PATH."
       warn "For fish, add it with:  fish_add_path $BIN_DIR" ;;
esac
