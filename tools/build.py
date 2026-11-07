#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Anthony Schuler
# SPDX-License-Identifier: LGPL-2.1-or-later
"""
Build the Square Breeze Plasma Style from stock Breeze.

    tools/build.py [--source DIR] [--out DIR]

--source  a Breeze Plasma Style: either an installed theme
          (/usr/share/plasma/desktoptheme/default, .svgz files) or libplasma's
          src/desktoptheme/breeze (.svg files). Default: the installed one.
--out     where to write the theme (default: ./package)

Only files that actually change are written; everything else falls back to
Breeze through plasmarc, so the theme follows Breeze updates automatically.
The source directory is only ever read.
"""
from __future__ import annotations

import argparse
import copy
import gzip
import io
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from svgframes import FrameError, SvgDoc, needs_squaring, square_frame  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
VERSION = (ROOT / "VERSION").read_text().strip() if (ROOT / "VERSION").exists() else "0.0.0"

# Frames we deliberately leave alone. Keys are "subdir/file" without extension,
# values are frame prefixes ("*" = whole file).
KEEP_ROUND = {
    # Drop shadows stay soft; only the surface itself is squared.
    "*": {"shadow"},
    # Breeze's active-tab line is deliberately shorter than the tab: a design inset,
    # not a rounded corner.
    "widgets/tabbar": {"north-active-tab", "south-active-tab", "east-active-tab", "west-active-tab"},
    # Legacy assets Plasma 6 doesn't use (embedded bitmaps / Plasma 4-era widgets).
    "widgets/media-delegate": {"picture-selected"},
    "widgets/monitor": {""},
    "widgets/picker": {""},
}
# Slider handles, radio buttons, switch knobs, busy indicators etc. aren't nine-slice
# frames, so they keep their round shapes automatically.

SUBDIRS = ("dialogs", "widgets", "opaque/dialogs", "opaque/widgets",
           "solid/dialogs", "solid/widgets", "translucent/dialogs", "translucent/widgets")


def read_svg(path: Path) -> bytes:
    data = path.read_bytes()
    return gzip.decompress(data) if path.suffix == ".svgz" else data


def write_svgz(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    # mtime=0 -> reproducible output
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9, mtime=0, filename="") as f:
        f.write(data)
    path.write_bytes(buf.getvalue())


def skip(rel: str, prefix: str) -> bool:
    for key in ("*", rel):
        rules = KEEP_ROUND.get(key, set())
        if "*" in rules or prefix in rules or any(r and (prefix.startswith(r + "-") or prefix.endswith("-" + r)) for r in rules):
            return True
    return False


def process(src: Path, rel: str, log):
    doc = SvgDoc(read_svg(src))
    frames = doc.frames()
    changed = []
    for prefix in sorted(frames, key=lambda p: (p != "", p)):
        parts = frames[prefix]
        name = prefix or "(base)"
        if skip(rel, prefix):
            continue
        try:
            # draws the square corners on a copy first: a failure never leaves half a
            # frame, and frames that are already square are left to Breeze
            if not needs_squaring(doc, prefix):
                continue
            square_frame(doc, prefix, parts)
            changed.append(name)
        except FrameError as e:
            log.setdefault("skipped", []).append(f"{rel}: {name}: {e}")
    return doc, changed


def _bytes(doc: SvgDoc) -> bytes:
    from lxml import etree
    # Qt's SVG parser looks attributes up by their literal "xlink:href" name, so the
    # usual prefixes must be declared on the root rather than generated (ns0:...).
    etree.cleanup_namespaces(doc.root, top_nsmap={
        "xlink": "http://www.w3.org/1999/xlink",
        "inkscape": "http://www.inkscape.org/namespaces/inkscape",
        "sodipodi": "http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd",
    })
    return etree.tostring(doc.root, xml_declaration=True, encoding="UTF-8")


def find_source(src: Path, sub: str):
    d = src / sub
    if not d.is_dir():
        return {}
    files = {}
    for p in sorted(d.iterdir()):
        if p.suffix in (".svg", ".svgz") and p.is_file():
            files.setdefault(p.stem, p)
    return files


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", type=Path, default=Path("/usr/share/plasma/desktoptheme/default"))
    ap.add_argument("--out", type=Path, default=ROOT / "package")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    src = a.source.resolve()
    if not (src / "widgets").is_dir():
        sys.exit(f"Not a Plasma Style: {src}")
    out = a.out.resolve()
    if src == out or out in src.parents:
        sys.exit("Refusing to write inside the source theme.")

    staging = out.with_name(out.name + ".building")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    log, summary = {}, {}
    for sub in SUBDIRS:
        for stem, path in find_source(src, sub).items():
            rel = f"{sub}/{stem}"
            doc, changed = process(path, rel, log)
            if changed:
                write_svgz(staging / sub / f"{stem}.svgz", _bytes(doc))
                summary[rel] = changed

    # metadata.json / plasmarc come from theme/ (edit them there)
    meta = (ROOT / "theme" / "metadata.json").read_text().replace("@VERSION@", VERSION)
    json.loads(meta)  # fail early on a typo
    (staging / "metadata.json").write_text(meta)
    shutil.copy2(ROOT / "theme" / "plasmarc", staging / "plasmarc")

    if out.exists():
        shutil.rmtree(out)
    staging.rename(out)

    if not a.quiet:
        for rel, frames in summary.items():
            print(f"squared  {rel:34s} {', '.join(frames)}")
        for line in log.get("skipped", []):
            print(f"SKIPPED  {line}")
        print(f"\n{sum(len(v) for v in summary.values())} frames squared in {len(summary)} files -> {out}")
    return 0 if not log.get("skipped") else 2


if __name__ == "__main__":
    sys.exit(main())
