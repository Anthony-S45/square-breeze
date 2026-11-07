#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Anthony Schuler
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Make the downloadable archive for a GitHub release / the KDE Store:
#   tools/release.sh            ->  dist/square-breeze-<VERSION>.tar.gz
# The archive has a single top-level "square-breeze/" folder, so it also works with
# System Settings > Plasma Style > "Install from File..." and kpackagetool6.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."

VERSION="$(cat VERSION)"
grep -q "\"Version\": \"$VERSION\"" package/metadata.json ||
    { echo "package/ is not built for $VERSION - run tools/build.py first"; exit 1; }

mkdir -p dist
STAGE="$(mktemp -d)"
cp -r package "$STAGE/square-breeze"
cp LICENSE "$STAGE/square-breeze/LICENSE"
tar --sort=name --owner=0 --group=0 --numeric-owner --mtime='2026-01-01' \
    -C "$STAGE" -czf "dist/square-breeze-$VERSION.tar.gz" square-breeze
rm -rf "$STAGE"
echo "dist/square-breeze-$VERSION.tar.gz"
