#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Anthony Schuler
# SPDX-License-Identifier: LGPL-2.1-or-later
"""Render a before/after preview image:  tools/preview.py --source BREEZE --theme package --out docs/preview.png"""
from __future__ import annotations

import argparse
import base64
import io
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify as V  # noqa: E402
from svgframes import SvgDoc  # noqa: E402

SHOW = [
    ("Popup / dialog", "dialogs/background", ""),
    ("Panel", "widgets/panel-background", ""),
    ("Desktop widget", "widgets/background", ""),
    ("Tooltip", "widgets/tooltip", ""),
    ("Button", "widgets/button", "normal"),
    ("Button pressed", "widgets/button", "pressed"),
    ("Text field", "widgets/lineedit", "base"),
    ("Selected item", "widgets/viewitem", "selected"),
    ("Hovered item", "widgets/viewitem", "hover"),
    ("Slider groove", "widgets/slider", "groove-highlight"),
    ("Scrollbar", "widgets/scrollbar", "slider"),
    ("Popup heading", "widgets/plasmoidheading", "header"),
]
SCALE = 3
BG = (42, 46, 50)


def find(root: Path, rel: str):
    for e in (".svgz", ".svg"):
        if (root / (rel + e)).exists():
            return root / (rel + e)
    return None


def render(page, path: Path, prefix: str) -> Image.Image:
    data = V.load(path)
    doc = SvgDoc(data)
    V.STRETCH = (40, 22)
    job, _ = V.layout(doc, prefix)
    png = page.evaluate(V.JS, dict(svg=data.decode(), jobs=[job], scale=SCALE, w=0, h=0))[0]
    return Image.open(io.BytesIO(base64.b64decode(png.split(",")[1]))).convert("RGBA")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", type=Path, required=True)
    ap.add_argument("--theme", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    from playwright.sync_api import sync_playwright

    cells = []
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        page = b.new_page()
        page.set_content("<html></html>")
        for label, rel, prefix in SHOW:
            old = render(page, find(a.source, rel), prefix)
            new = render(page, find(a.theme, rel) or find(a.source, rel), prefix)
            cells.append((label, old, new))
        b.close()

    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 15)
        head = ImageFont.truetype("DejaVuSans-Bold.ttf", 17)
    except OSError:
        font = head = ImageFont.load_default()
    cols = 3
    cw = max(max(o.width, n.width) for _, o, n in cells) * 2 + 36
    rows = [cells[i:i + cols] for i in range(0, len(cells), cols)]
    rh = [max(max(o.height, n.height) for _, o, n in r) + 40 for r in rows]
    W, H = cols * cw + 24, sum(rh) + 64
    out = Image.new("RGBA", (W, H), BG + (255,))
    d = ImageDraw.Draw(out)
    d.text((16, 14), "Breeze  →  Square Breeze", font=head, fill=(239, 240, 241))
    y = 50
    for r, h in zip(rows, rh):
        x = 12
        for label, o, n in r:
            d.text((x + 4, y), label, font=font, fill=(200, 204, 208))
            half = (cw - 36) // 2
            for i, im in enumerate((o, n)):
                out.alpha_composite(im, (x + 4 + i * (half + 12), y + 24))
            x += cw
        y += h
    a.out.parent.mkdir(parents=True, exist_ok=True)
    out.convert("RGB").save(a.out, optimize=True)
    print(a.out)


if __name__ == "__main__":
    main()
