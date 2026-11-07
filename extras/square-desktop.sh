#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Anthony Schuler
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Square the desktop's drag-selection rectangle and rename box.
#
# These aren't drawn by the Plasma Style: the desktop's QML hard-codes
#   radius: Kirigami.Units.cornerRadius
# This script installs a per-user copy of the desktop containment
# (org.kde.desktopcontainment, also used by the Folder View widget) taken from
# *your installed Plasma*, with only those radius lines changed to 0.
#
# A stale copy could break the desktop after a Plasma update, so the override is
# kept in sync automatically: a systemd drop-in runs "--sync" every time plasmashell
# starts, which re-copies and re-patches whenever the system package changed, and
# removes the override if the patch no longer applies. Nothing outside $HOME is touched.
#
#   extras/square-desktop.sh            install (and restart plasmashell)
#   extras/square-desktop.sh --remove   undo everything
#   extras/square-desktop.sh --sync     what the drop-in runs (quiet, no restart)
set -euo pipefail

ID=org.kde.desktopcontainment
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
USER_PKG="$DATA/plasma/plasmoids/$ID"
STAMP="$USER_PKG/.square-breeze"
SELF_DIR="$DATA/square-breeze"
SELF="$SELF_DIR/square-desktop.sh"
DROPIN_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/plasma-plasmashell.service.d"
DROPIN="$DROPIN_DIR/square-breeze.conf"
MODE="${1:-install}"
QUIET=0; [ "$MODE" = "--sync" ] && QUIET=1

say() { [ "$QUIET" = 1 ] || echo "$@"; }

system_pkg() {
    local IFS=:
    for d in ${XDG_DATA_DIRS:-/usr/local/share:/usr/share}; do
        [ -f "$d/plasma/plasmoids/$ID/metadata.json" ] && { echo "$d/plasma/plasmoids/$ID"; return; }
    done
    return 1
}

hash_dir() { (cd "$1" && find . -type f ! -name .square-breeze -print0 | sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1); }

ours() { [ ! -e "$USER_PKG" ] || [ -f "$STAMP" ]; }

remove_override() {
    if [ -f "$STAMP" ]; then rm -rf "$USER_PKG"; say "Removed the desktop override."; fi
}

restart_shell() {
    if systemctl --user is-active -q plasma-plasmashell.service 2>/dev/null; then
        systemctl --user restart plasma-plasmashell.service
    elif command -v kquitapp6 >/dev/null; then
        kquitapp6 plasmashell >/dev/null 2>&1 || true
        (command -v kstart >/dev/null && kstart plasmashell || setsid plasmashell) >/dev/null 2>&1 &
    else
        say "Log out and back in to apply."; return
    fi
    say "Restarted plasmashell."
}

sync_override() {
    local sys; sys="$(system_pkg)" || { say "Plasma's desktop containment ($ID) wasn't found."; remove_override; return 1; }
    ours || { say "$USER_PKG exists and wasn't made by Square Breeze - leaving it alone."; return 1; }
    local h; h="$(hash_dir "$sys")"
    if [ -f "$STAMP" ] && [ "$(cat "$STAMP")" = "$h" ]; then return 0; fi   # up to date

    local tmp; tmp="$(mktemp -d)"
    cp -r "$sys" "$tmp/$ID"
    local n=0 f c
    for f in "$tmp/$ID/contents/ui/FolderView.qml" "$tmp/$ID/contents/ui/RenameEditor.qml"; do
        [ -f "$f" ] || continue
        c=$(grep -c 'radius: *Kirigami\.Units\.cornerRadius' "$f" || true)
        sed -i 's/radius: *Kirigami\.Units\.cornerRadius/radius: 0 \/\/ Square Breeze/' "$f"
        n=$((n + c))
    done
    if ! grep -q 'radius: 0 // Square Breeze' "$tmp/$ID/contents/ui/FolderView.qml" 2>/dev/null; then
        # Upstream changed how the selection box is drawn: never keep a stale copy.
        rm -rf "$tmp"
        say "This Plasma version draws the selection box differently; the override was not applied."
        remove_override
        return 1
    fi
    echo "$h" > "$tmp/$ID/.square-breeze"
    rm -rf "$USER_PKG"
    mkdir -p "$(dirname "$USER_PKG")"
    mv "$tmp/$ID" "$USER_PKG"
    rmdir "$tmp"
    say "Desktop override installed from $sys ($n corner(s) squared)."
}

case "$MODE" in
    --remove)
        remove_override
        rm -f "$DROPIN"; rmdir "$DROPIN_DIR" 2>/dev/null || true
        rm -f "$SELF"; rmdir "$SELF_DIR" 2>/dev/null || true
        systemctl --user daemon-reload 2>/dev/null || true
        restart_shell ;;
    --sync)
        sync_override || true ;;
    install)
        sync_override
        mkdir -p "$SELF_DIR" "$DROPIN_DIR"
        [ "$(readlink -f "$0")" = "$(readlink -f "$SELF" 2>/dev/null || true)" ] || cp "$0" "$SELF"
        chmod +x "$SELF"
        printf '[Service]\n# Square Breeze: keep the squared desktop selection box in sync with Plasma\nExecStartPre=-%s --sync\n' "$SELF" > "$DROPIN"
        systemctl --user daemon-reload 2>/dev/null || true
        restart_shell ;;
    *)
        sed -n '5,20p' "$0"; exit 1 ;;
esac
