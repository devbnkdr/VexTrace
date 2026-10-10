#!/usr/bin/env bash
#
# Remove everything install.sh created. System packages installed with pacman
# (python, python-pyqt6, potrace) are left alone - remove those yourself if you
# want them gone, since other software may rely on them.

set -euo pipefail

APP_ID="vextrace"
APP_NAME="VexTrace"

DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}"
APP_DIR="$DATA_DIR/$APP_ID"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="$DATA_DIR/applications"
ICON_DIR="$DATA_DIR/icons/hicolor/scalable/apps"

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

TARGETS=(
    "$APP_DIR"
    "$BIN_DIR/$APP_ID"
    "$DESKTOP_DIR/$APP_ID.desktop"
    "$ICON_DIR/$APP_ID.svg"
)

echo "This will remove:"
for target in "${TARGETS[@]}"; do
    [[ -e "$target" ]] && echo "  $target"
done
echo

read -r -p "Remove $APP_NAME? [y/N] " reply
[[ "$reply" =~ ^[Yy]$ ]] || { echo "Cancelled."; exit 0; }

for target in "${TARGETS[@]}"; do
    if [[ -e "$target" ]]; then
        say "Removing $target"
        rm -rf -- "$target"
    fi
done

command -v update-desktop-database &>/dev/null && update-desktop-database "$DESKTOP_DIR" || true
command -v gtk-update-icon-cache   &>/dev/null && gtk-update-icon-cache -qtf "$DATA_DIR/icons/hicolor" 2>/dev/null || true
command -v kbuildsycoca6           &>/dev/null && kbuildsycoca6 --noincremental &>/dev/null || true

say "$APP_NAME removed."
echo "System packages (python, python-pyqt6, potrace) were left installed."
