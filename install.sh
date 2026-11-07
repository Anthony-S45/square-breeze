#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Anthony Schuler
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Install Square Breeze for the current user (nothing outside your home folder is touched).
#
#   ./install.sh              install the prebuilt theme in package/
#   ./install.sh --build      rebuild it from *your* installed Breeze first
#                             (recommended after a Plasma update; needs python3, lxml, pillow)
#   ./install.sh --uninstall  remove it
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/plasma/desktoptheme/square-breeze"
SRC=package

reload() {
    # Plasma caches theme SVGs; drop the cache and re-apply if Square Breeze is active.
    rm -f "${XDG_CACHE_HOME:-$HOME/.cache}"/plasma_theme_square-breeze*.kcache \
          "${XDG_CACHE_HOME:-$HOME/.cache}"/ksvg-elements* 2>/dev/null || true
    if command -v plasma-apply-desktoptheme >/dev/null &&
       grep -qs '^name=square-breeze' "${XDG_CONFIG_HOME:-$HOME/.config}/plasmarc"; then
        plasma-apply-desktoptheme default >/dev/null && plasma-apply-desktoptheme square-breeze >/dev/null
        echo "Reloaded the active Plasma Style."
    fi
}

case "${1:-}" in
    --uninstall)
        rm -rf "$DEST"
        echo "Removed $DEST"
        echo "If it was active, pick another style in System Settings > Colors & Themes > Plasma Style."
        exit 0 ;;
    --build)
        SRC="$(mktemp -d)/square-breeze"
        python3 tools/build.py --source /usr/share/plasma/desktoptheme/default --out "$SRC" ;;
    "") ;;
    *) sed -n '5,10p' "$0"; exit 1 ;;
esac

[ -f "$SRC/metadata.json" ] || { echo "No theme found in $SRC"; exit 1; }
rm -rf "$DEST"
mkdir -p "$DEST"
cp -r "$SRC"/. "$DEST"/
echo "Installed to $DEST"
reload
echo "Select it in System Settings > Colors & Themes > Plasma Style > Square Breeze"
echo "(or run: plasma-apply-desktoptheme square-breeze)"
