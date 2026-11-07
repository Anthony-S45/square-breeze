#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Anthony Schuler
# SPDX-License-Identifier: LGPL-2.1-or-later
"""
Render-check Square Breeze against stock Breeze.

    tools/verify.py --source BREEZE_DIR --theme package [--sheets DIR]

Each nine-slice frame is laid out the way Plasma does it (corners at their
natural size, edges and centre stretched) and rasterised with Chromium.
A corner passes when every pixel equals the straight edge it continues
(top/bottom edge above the diagonal, left/right edge below it) - i.e. the
corner is a square continuation of its edges. With --sheets, a before/after
picture of every frame is written per file.

Requires: python3-playwright with Chromium, numpy, Pillow, lxml.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import io
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
from svgframes import CORNERS, EDGES, FrameError, SvgDoc  # noqa: E402

SCALE = 4          # device pixels per SVG unit
STRETCH = (28, 18)  # extra width/height given to the stretched edges, in units
BG = (127, 127, 127)

JS = r"""
async ({svg, jobs, scale, w, h}) => {
  const out = [];
  const parser = new DOMParser();
  const base = parser.parseFromString(svg, "image/svg+xml").documentElement;
  const vb = (base.getAttribute("viewBox") || `0 0 ${parseFloat(base.getAttribute("width"))} ${parseFloat(base.getAttribute("height"))}`)
              .trim().split(/[\s,]+/).map(Number);
  base.setAttribute("viewBox", vb.join(" "));
  base.setAttribute("width", vb[2] * scale);
  base.setAttribute("height", vb[3] * scale);
  base.setAttribute("preserveAspectRatio", "none");
  const ser = new XMLSerializer();
  const cache = {};
  async function isolated(id) {
    if (cache[id]) return cache[id];
    const doc = base.cloneNode(true);
    doc.setAttribute("style", (doc.getAttribute("style") || "") + ";visibility:hidden");
    const el = doc.querySelector(`[id="${CSS.escape(id)}"]`);
    if (!el) return null;
    el.setAttribute("style", (el.getAttribute("style") || "") + ";visibility:visible");
    const url = "data:image/svg+xml;base64," + btoa(unescape(encodeURIComponent(ser.serializeToString(doc))));
    const img = new Image();
    img.src = url;
    await img.decode();
    cache[id] = img;
    return img;
  }
  for (const job of jobs) {
    const c = document.createElement("canvas");
    c.width = Math.round(job.W * scale); c.height = Math.round(job.H * scale);
    const g = c.getContext("2d");
    g.imageSmoothingEnabled = true;
    for (const t of job.tiles) {
      const img = await isolated(t.id);
      if (!img) continue;
      g.drawImage(img, (t.sx - vb[0]) * scale, (t.sy - vb[1]) * scale, t.sw * scale, t.sh * scale,
                  t.dx * scale, t.dy * scale, t.dw * scale, t.dh * scale);
    }
    out.push(c.toDataURL("image/png"));
  }
  return out;
}
"""


def load(path: Path) -> bytes:
    d = path.read_bytes()
    return gzip.decompress(d) if path.suffix == ".svgz" else d


def layout(doc: SvgDoc, prefix: str):
    pre = prefix + "-" if prefix else ""
    b = {k: doc.bbox(doc.by_id[pre + k]) for k in CORNERS + EDGES}
    try:
        b["center"] = doc.bbox(doc.by_id[pre + "center"])
    except (KeyError, FrameError):
        b["center"] = None
    tl, tr, bl, br = (b[k] for k in CORNERS)
    L, R, T, B = b["left"].w, b["right"].w, b["top"].h, b["bottom"].h
    W = max(tl.w, bl.w) + STRETCH[0] + max(tr.w, br.w)
    H = max(tl.h, tr.h) + STRETCH[1] + max(bl.h, br.h)
    tiles = []

    def tile(k, dx, dy, dw, dh):
        bb = b[k]
        if bb is None or dw <= 0 or dh <= 0:
            return
        tiles.append(dict(id=pre + k, sx=bb.x0, sy=bb.y0, sw=bb.w, sh=bb.h, dx=dx, dy=dy, dw=dw, dh=dh))

    tile("center", L, T, W - L - R, H - T - B)
    tile("top", tl.w, 0, W - tl.w - tr.w, T)
    tile("bottom", bl.w, H - B, W - bl.w - br.w, B)
    tile("left", 0, tl.h, L, H - tl.h - bl.h)
    tile("right", W - R, tr.h, R, H - tr.h - br.h)
    tile("topleft", 0, 0, tl.w, tl.h)
    tile("topright", W - tr.w, 0, tr.w, tr.h)
    tile("bottomleft", 0, H - bl.h, bl.w, bl.h)
    tile("bottomright", W - br.w, H - br.h, br.w, br.h)
    geo = dict(W=W, H=H, T=T, B=B, L=L, R=R, corners={k: (b[k].w, b[k].h) for k in CORNERS})
    return dict(W=W, H=H, tiles=tiles), geo


def flatten(png: bytes) -> np.ndarray:
    im = Image.open(io.BytesIO(png)).convert("RGBA")
    bg = Image.new("RGBA", im.size, BG + (255,))
    return np.asarray(Image.alpha_composite(bg, im).convert("RGB")).astype(int)


def _settle(profile: np.ndarray, n: int, tol: int) -> int:
    """First index from which profile[:n] stays constant (the edge's interior)."""
    last = profile[n - 1]
    t = n - 1
    while t > 0 and np.abs(profile[t - 1] - last).max() <= tol:
        t -= 1
    return t


def corner_error(img: np.ndarray, geo: dict, tol: int = 8) -> dict:
    """Max colour error of each corner vs. a square continuation of its edges.

    In a square frame, once you are past the side edge's structure (border lines,
    margins...) every row of the corner is just the top edge, and once you are past
    the top edge's structure every column is just the side edge. A rounded corner
    breaks this along its outer rows/columns."""
    s = SCALE
    res = {}
    for corner in CORNERS:
        cw, ch = (int(round(v * s)) for v in geo["corners"][corner])
        if cw < 3 or ch < 3:
            res[corner] = 0
            continue
        a = img
        if corner.endswith("right"):
            a = a[:, ::-1]
        if corner.startswith("bottom"):
            a = a[::-1]
        Hpx2, Wpx2 = a.shape[0] // 2, a.shape[1] // 2
        top_col = a[:Hpx2, cw + 2]        # top edge + interior, sampled just right of the corner
        left_row = a[ch + 2, :Wpx2]       # side edge + interior, sampled just below the corner
        n_y, n_x = ch - 1, cw - 1         # ignore the 1px seam where Plasma joins tiles
        DT = _settle(top_col, len(top_col), tol)   # depth where the frame's interior begins
        DL = _settle(left_row, len(left_row), tol)
        block = a[:n_y, :n_x]
        e1 = np.abs(block[:, DL:] - top_col[:n_y, None, :]).max() if DL < n_x else 0
        e2 = np.abs(block[DT:, :] - left_row[None, :n_x, :]).max() if DT < n_y else 0
        # (inside both structures the corner may legitimately differ, e.g. a 1px margin)
        res[corner] = int(max(e1, e2))
    return res


# Frames that intentionally stay as Breeze draws them (see README "Not squared").
KNOWN_ROUND = {
    "widgets/media-delegate": {"picture-selected"},   # legacy embedded bitmaps
    "widgets/monitor": {"(base)"},                    # legacy, unused by Plasma 6
    "widgets/picker": {"(base)"},                     # legacy, unused by Plasma 6
    # design inset (the line is shorter than the tab), not rounding
    "widgets/tabbar": {"north-active-tab", "south-active-tab", "east-active-tab", "west-active-tab"},
}


def rel_ok(rel: str, name: str) -> bool:
    return name in KNOWN_ROUND.get(rel, set())


def frames_of(doc: SvgDoc):
    try:
        return doc.frames()
    except FrameError:
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", type=Path, required=True)
    ap.add_argument("--theme", type=Path, required=True)
    ap.add_argument("--sheets", type=Path)
    ap.add_argument("--tolerance", type=int, default=24,
                    help="max per-channel colour error (0-255) allowed in a square corner")
    ap.add_argument("--json", type=Path)
    a = ap.parse_args()

    from playwright.sync_api import sync_playwright

    # every stock frame file, checked against the theme's override or (if none) the
    # Breeze file it falls back to - so a rounded frame can't slip through unnoticed
    stock = {}
    for p in sorted(list(a.source.rglob("*.svg")) + list(a.source.rglob("*.svgz"))):
        rel = p.relative_to(a.source).with_suffix("")
        if rel.parts[0] in ("icons",) or "weather" in rel.parts:
            continue
        stock.setdefault(rel, p)
    theme_files = []
    for rel in sorted(stock):
        tp = next((a.theme / rel.with_suffix(e) for e in (".svgz", ".svg") if (a.theme / rel.with_suffix(e)).exists()), None)
        theme_files.append((rel, tp or stock[rel], tp is None))
    results, failures = {}, 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.set_content("<html><body></body></html>")
        for rel, tp, fallback in theme_files:
            sp = stock[rel]
            try:
                new_doc = SvgDoc(load(tp))
            except Exception:
                continue
            if not frames_of(new_doc):
                continue
            old_doc = SvgDoc(load(sp)) if sp else None
            new_svg = load(tp).decode("utf-8")
            old_svg = load(sp).decode("utf-8") if sp else None
            rows = []
            for prefix in sorted(frames_of(new_doc), key=lambda p: (p != "", p)):
                name = prefix or "(base)"
                if "shadow" in prefix:
                    continue
                try:
                    job, geo = layout(new_doc, prefix)
                except FrameError:
                    continue
                png_new = page.evaluate(JS, dict(svg=new_svg, jobs=[job], scale=SCALE, w=0, h=0))[0]
                img_new = flatten(base64.b64decode(png_new.split(",")[1]))
                err_new = corner_error(img_new, geo)
                img_old, err_old = None, None
                if old_doc is not None and prefix in frames_of(old_doc):
                    ojob, ogeo = layout(old_doc, prefix)
                    png_old = page.evaluate(JS, dict(svg=old_svg, jobs=[ojob], scale=SCALE, w=0, h=0))[0]
                    img_old = flatten(base64.b64decode(png_old.split(",")[1]))
                    err_old = corner_error(img_old, ogeo)
                ok = max(err_new.values()) <= a.tolerance
                was_round = err_old is not None and max(err_old.values()) > a.tolerance
                status = "ok" if ok else ("kept" if rel_ok(str(rel), name) else "NOT SQUARE")
                if not ok and not rel_ok(str(rel), name):
                    failures += 1
                results[f"{rel}:{name}"] = dict(status=status, new=err_new, stock=err_old)
                print(f"{status:10s} {str(rel) + ':' + name:52s} max err {max(err_new.values()):3d}"
                      + (f"   (stock {max(err_old.values()):3d}{', was rounded' if was_round else ''})" if err_old else ""))
                rows.append((name, img_old, img_new, ok))
            if a.sheets and rows:
                sheet(a.sheets / f"{str(rel).replace('/', '__')}.png", str(rel), rows)
        browser.close()
    if a.json:
        a.json.write_text(json.dumps(results, indent=1))
    print(f"\n{len(results)} frames checked, {failures} not square"
          "  ('kept' = intentionally left as Breeze draws it)")
    return 1 if failures else 0


def sheet(path: Path, title: str, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    pad, lab = 12, 18
    tiles = []
    for name, old, new, ok in rows:
        imgs = [Image.fromarray(x.astype("uint8")) for x in (old, new) if x is not None]
        tiles.append((name, imgs, ok))
    W = max(sum(i.width for i in t[1]) + pad * (len(t[1]) + 1) for t in tiles)
    W = max(W, 360)
    H = lab + sum(max(i.height for i in t[1]) + lab + pad for t in tiles) + pad
    out = Image.new("RGB", (W, H), (40, 40, 40))
    d = ImageDraw.Draw(out)
    d.text((pad, 3), f"{title}   (left: stock Breeze, right: Square Breeze)", fill=(230, 230, 230))
    y = lab
    for name, imgs, ok in tiles:
        d.text((pad, y + 2), name + ("" if ok else "   <-- NOT SQUARE"), fill=(230, 230, 230) if ok else (255, 90, 90))
        y += lab
        x = pad
        for im in imgs:
            out.paste(im, (x, y))
            x += im.width + pad
        y += max(i.height for i in imgs) + pad
    out.save(path)


if __name__ == "__main__":
    sys.exit(main())
