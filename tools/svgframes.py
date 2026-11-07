# SPDX-FileCopyrightText: 2026 Anthony Schuler
# SPDX-License-Identifier: LGPL-2.1-or-later
"""
Geometry helpers for Plasma (KSvg) nine-slice frame SVGs.

A Plasma frame is a set of elements named  <prefix>-topleft, -top, -topright,
-left, -center, -right, -bottomleft, -bottom, -bottomright  (prefix may be empty).
Plasma stretches the edges and draws the corners as-is, so a rounded look comes
entirely from the four corner elements.

square_frame() rebuilds each corner of a frame from its two neighbouring edges.
Every layer of an edge (fill, border, inner highlight, margin, shadow...) is a
straight strip at some depth from the outside. In a square frame each strip is the
gap between two nested rectangles, so it simply continues around the corner as an
"L". Corners are therefore computed rather than hand-drawn, and keep Breeze's exact
layer styles, colours, opacities and corner size:

  * solid layers become unions of rectangles (exact, no anti-aliasing seams);
  * gradient layers (soft shadows) reuse each edge's own gradient, shifted into the
    corner, and meet on a 45-degree miter;
  * a layer that exists on one edge only (e.g. the panel's top line) runs on to
    where the other edge's visible content starts.

needs_squaring() decides whether a frame needs this at all: its corners are drawn
with curves, or their silhouette (notched / cut-away pixels) differs from the
square version.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from lxml import etree

SVG_NS = "http://www.w3.org/2000/svg"
INKSCAPE_NS = "http://www.inkscape.org/namespaces/inkscape"
SODIPODI_NS = "http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"

CORNERS = ("topleft", "topright", "bottomleft", "bottomright")
EDGES = ("top", "bottom", "left", "right")
DRAWABLE = {"path", "rect", "circle", "ellipse", "line", "polyline", "polygon"}

EPS = 1e-3


class FrameError(Exception):
    """A frame whose structure we can't square safely (left untouched)."""


# --------------------------------------------------------------------------
# Affine transforms  (a c e / b d f)
# --------------------------------------------------------------------------
Matrix = tuple  # (a, b, c, d, e, f)
IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def mul(m: Matrix, n: Matrix) -> Matrix:
    """m * n  (apply n first, then m)."""
    a1, b1, c1, d1, e1, f1 = m
    a2, b2, c2, d2, e2, f2 = n
    return (
        a1 * a2 + c1 * b2,
        b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2,
        b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1,
        b1 * e2 + d1 * f2 + f1,
    )


def inverse(m: Matrix) -> Matrix:
    a, b, c, d, e, f = m
    det = a * d - b * c
    if abs(det) < 1e-12:
        raise FrameError("singular transform")
    return (d / det, -b / det, -c / det, a / det, (c * f - d * e) / det, (b * e - a * f) / det)


def apply(m: Matrix, x: float, y: float):
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"


def parse_transform(s: str | None) -> Matrix:
    m = IDENTITY
    if not s:
        return m
    for name, args in re.findall(r"(\w+)\s*\(([^)]*)\)", s):
        v = [float(x) for x in re.findall(_NUM, args)]
        if name == "matrix":
            t = tuple(v)
        elif name == "translate":
            t = (1, 0, 0, 1, v[0], v[1] if len(v) > 1 else 0)
        elif name == "scale":
            t = (v[0], 0, 0, v[1] if len(v) > 1 else v[0], 0, 0)
        elif name == "rotate":
            r = math.radians(v[0])
            cs, sn = math.cos(r), math.sin(r)
            t = (cs, sn, -sn, cs, 0, 0)
            if len(v) == 3:
                cx, cy = v[1], v[2]
                t = mul(mul((1, 0, 0, 1, cx, cy), t), (1, 0, 0, 1, -cx, -cy))
        elif name == "skewX":
            t = (1, 0, math.tan(math.radians(v[0])), 1, 0, 0)
        elif name == "skewY":
            t = (1, math.tan(math.radians(v[0])), 0, 1, 0, 0)
        else:
            raise FrameError(f"unknown transform {name}")
        m = mul(m, t)
    return m


# --------------------------------------------------------------------------
# Path data -> list of subpaths (lists of points), arcs/curves sampled
# --------------------------------------------------------------------------
_TOKEN = re.compile(r"([MmLlHhVvCcSsQqTtAaZz])|(" + _NUM + r")")


def _tokens(d: str):
    for cmd, num in _TOKEN.findall(d):
        yield cmd if cmd else float(num)


def _arc_points(x1, y1, rx, ry, phi, fa, fs, x2, y2, n=12):
    if rx == 0 or ry == 0:
        return [(x2, y2)]
    rx, ry = abs(rx), abs(ry)
    p = math.radians(phi)
    cp, sp = math.cos(p), math.sin(p)
    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    x1p, y1p = cp * dx + sp * dy, -sp * dx + cp * dy
    lam = (x1p / rx) ** 2 + (y1p / ry) ** 2
    if lam > 1:
        rx, ry = rx * math.sqrt(lam), ry * math.sqrt(lam)
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    co = math.sqrt(max(0.0, num / den)) if den else 0.0
    if fa == fs:
        co = -co
    cxp, cyp = co * rx * y1p / ry, -co * ry * x1p / rx
    cx = cp * cxp - sp * cyp + (x1 + x2) / 2
    cy = sp * cxp + cp * cyp + (y1 + y2) / 2

    def ang(ux, uy, vx, vy):
        a = math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)
        return a

    t1 = ang(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dt = ang((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not fs and dt > 0:
        dt -= 2 * math.pi
    elif fs and dt < 0:
        dt += 2 * math.pi
    pts = []
    for i in range(1, n + 1):
        t = t1 + dt * i / n
        x = cx + rx * math.cos(t) * cp - ry * math.sin(t) * sp
        y = cy + rx * math.cos(t) * sp + ry * math.sin(t) * cp
        pts.append((x, y))
    return pts


def _bez(p0, p1, p2, p3, n=8):
    out = []
    for i in range(1, n + 1):
        t = i / n
        u = 1 - t
        out.append((
            u**3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t**3 * p3[0],
            u**3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t**3 * p3[1],
        ))
    return out


def path_subpaths(d: str):
    """Return (subpaths, has_curves). Each subpath is a list of (x, y)."""
    toks = list(_tokens(d or ""))
    i = 0
    subs, cur = [], []
    x = y = sx = sy = 0.0
    cmd = None
    lc = None  # last control point for S/T
    curved = False

    def num():
        nonlocal i
        v = toks[i]
        if isinstance(v, str):
            raise FrameError("bad path data")
        i += 1
        return v

    def flag():
        # arc flags may be packed ("01"): the tokenizer reads them as one number
        nonlocal i
        v = toks[i]
        i += 1
        return 1 if v else 0

    while i < len(toks):
        if isinstance(toks[i], str):
            cmd = toks[i]
            i += 1
            if cmd in "Zz":
                if cur:
                    subs.append(cur)
                cur = []
                x, y = sx, sy
                lc = None
                continue
        elif cmd is None:
            raise FrameError("path data starts with a number")
        rel = cmd.islower()
        C = cmd.upper()
        ox, oy = (x, y) if rel else (0.0, 0.0)
        if C == "M":
            if cur:
                subs.append(cur)
            x, y = ox + num(), oy + num()
            sx, sy = x, y
            cur = [(x, y)]
            cmd = "l" if rel else "L"
            lc = None
        elif C == "L":
            x, y = ox + num(), oy + num()
            cur.append((x, y)); lc = None
        elif C == "H":
            x = (x if rel else 0) + num()
            cur.append((x, y)); lc = None
        elif C == "V":
            y = (y if rel else 0) + num()
            cur.append((x, y)); lc = None
        elif C in "CS":
            if C == "C":
                p1 = (ox + num(), oy + num())
            else:
                p1 = (2 * x - lc[0], 2 * y - lc[1]) if lc else (x, y)
            p2 = (ox + num(), oy + num())
            p3 = (ox + num(), oy + num())
            cur += _bez((x, y), p1, p2, p3)
            lc = p2
            x, y = p3
            curved = True
        elif C in "QT":
            if C == "Q":
                q = (ox + num(), oy + num())
            else:
                q = (2 * x - lc[0], 2 * y - lc[1]) if lc else (x, y)
            p3 = (ox + num(), oy + num())
            p1 = (x + 2 / 3 * (q[0] - x), y + 2 / 3 * (q[1] - y))
            p2 = (p3[0] + 2 / 3 * (q[0] - p3[0]), p3[1] + 2 / 3 * (q[1] - p3[1]))
            cur += _bez((x, y), p1, p2, p3)
            lc = q
            x, y = p3
            curved = True
        elif C == "A":
            rx, ry, phi = num(), num(), num()
            fa = flag()
            fs = flag()
            nx, ny = ox + num(), oy + num()
            cur += _arc_points(x, y, rx, ry, phi, fa, fs, nx, ny)
            x, y = nx, ny
            lc = None
            curved = True
        else:
            raise FrameError(f"unsupported path command {cmd}")
    if cur:
        subs.append(cur)
    return subs, curved


def local(tag) -> str:
    return etree.QName(tag).localname if isinstance(tag, str) else ""


def shape_subpaths(el):
    """Subpaths (local coordinates) for any drawable element, plus 'is curved'."""
    t = local(el.tag)
    g = lambda k, dflt=0.0: float(re.findall(_NUM, el.get(k, str(dflt)))[0]) if el.get(k) else dflt
    if t == "path":
        return path_subpaths(el.get("d", ""))
    if t == "rect":
        x, y, w, h = g("x"), g("y"), g("width"), g("height")
        curved = g("rx") > 0 or g("ry") > 0
        return [[(x, y), (x + w, y), (x + w, y + h), (x, y + h)]], curved
    if t in ("circle", "ellipse"):
        cx, cy = g("cx"), g("cy")
        rx = g("r") if t == "circle" else g("rx")
        ry = g("r") if t == "circle" else g("ry")
        pts = [(cx + rx * math.cos(a * math.pi / 16), cy + ry * math.sin(a * math.pi / 16)) for a in range(32)]
        return [pts], True
    if t in ("polygon", "polyline"):
        v = [float(n) for n in re.findall(_NUM, el.get("points", ""))]
        return [list(zip(v[::2], v[1::2]))], False
    if t == "line":
        return [[(g("x1"), g("y1")), (g("x2"), g("y2"))]], False
    return [], False


# --------------------------------------------------------------------------
# Styles
# --------------------------------------------------------------------------
def parse_style(s: str | None) -> dict:
    out = {}
    for part in (s or "").split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            out[k.strip()] = v.strip()
    return out


PRESENTATION = ("fill", "fill-opacity", "fill-rule", "opacity", "stroke", "stroke-width",
                "stroke-opacity", "color", "display", "visibility")


def own_props(el) -> dict:
    p = {k: el.get(k) for k in PRESENTATION if el.get(k) is not None}
    p.update(parse_style(el.get("style")))
    return p


# Style keys that don't change how a filled shape renders; ignored when matching layers.
NEUTRAL = {"stroke-width", "stroke-linecap", "stroke-linejoin", "stroke-miterlimit",
           "stroke-dasharray", "stroke-dashoffset", "fill-rule", "paint-order",
           "marker", "enable-background", "shape-rendering"}


INHERITED = ("fill", "fill-opacity", "fill-rule", "color", "visibility")
DEFAULTS = {"fill-opacity": "1", "opacity": "1", "stroke": "none", "display": "inline",
            "visibility": "visible", "stroke-opacity": "1"}


def _norm(k, v):
    v = v.strip()
    if k in ("opacity", "fill-opacity", "stroke-opacity"):
        try:
            return str(round(float(v), 3)).rstrip("0").rstrip(".") if float(v) != 1 else "1"
        except ValueError:
            return v
    if k in ("fill", "stroke") and v.lower() in ("#000", "#000000", "black"):
        return "#000000"
    return v


def effective(leaf, frame_el) -> tuple[dict, str]:
    """Style that actually applies to `leaf`, inherited down from `frame_el`.
    Group-level opacity of frame_el itself is excluded (handled separately)."""
    props = {}
    cls = ""
    n = leaf
    while n is not None:
        own = own_props(n)
        if n is leaf:
            props.update(own)
        else:
            for k in INHERITED:
                if k in own and k not in props:
                    props[k] = own[k]
        if not cls and n.get("class"):
            cls = n.get("class")
        if n is frame_el:
            break
        n = n.getparent()
    props.setdefault("fill", "#000000")
    out = {}
    for k, v in props.items():
        if k in NEUTRAL or k.startswith("-inkscape") or k.startswith("font"):
            continue
        v = _norm(k, v)
        if DEFAULTS.get(k) == v:
            continue
        out[k] = v
    if out.get("stroke", "none") == "none":
        out.pop("stroke-opacity", None)
    return out, cls


def signature(props: dict, cls: str):
    key = {k: ("url(gradient)" if "url(" in v else v) for k, v in props.items()}
    return (tuple(sorted(key.items())), cls)


def group_opacity(el) -> str:
    if local(el.tag) in DRAWABLE:
        return "1"
    return _norm("opacity", own_props(el).get("opacity", "1"))


# --------------------------------------------------------------------------
# Document model
# --------------------------------------------------------------------------
@dataclass
class Layer:
    el: object                      # the drawable leaf
    subpaths: list                  # root-coordinate subpaths
    fill_rule: str
    sig: tuple
    curved: bool
    props: dict = field(default_factory=dict)
    cls: str = ""


@dataclass
class Box:
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def w(self):
        return self.x1 - self.x0

    @property
    def h(self):
        return self.y1 - self.y0


class SvgDoc:
    def __init__(self, data: bytes):
        parser = etree.XMLParser(remove_blank_text=False, huge_tree=True)
        self.root = etree.fromstring(data, parser)
        self.by_id = {}
        for el in self.root.iter():
            if isinstance(el.tag, str) and el.get("id"):
                self.by_id.setdefault(el.get("id"), el)

    def ctm(self, el, include_self=True) -> Matrix:
        chain = []
        n = el if include_self else el.getparent()
        while n is not None and n is not self.root.getparent():
            chain.append(n)
            n = n.getparent()
        m = IDENTITY
        for node in reversed(chain):
            m = mul(m, parse_transform(node.get("transform")))
        return m

    def leaves(self, el):
        """Drawable leaves under el (el itself if drawable), document order."""
        if local(el.tag) in DRAWABLE:
            return [el]
        out = []
        for n in el.iter():
            if n is el or not isinstance(n.tag, str):
                continue
            t = local(n.tag)
            if t == "use":
                raise FrameError(f"<use> inside {el.get('id')}")
            if t in ("defs", "clipPath", "mask", "pattern", "linearGradient", "radialGradient"):
                raise FrameError(f"<{t}> inside {el.get('id')}")
            if t in DRAWABLE:
                out.append(n)
        return out

    def layers(self, el, strict=True):
        out = []
        for leaf in self.leaves(el):
            # styles on groups strictly between el and leaf would be lost: refuse
            n = leaf.getparent()
            while n is not None and n is not el and leaf is not el:
                if own_props(n) or n.get("class"):
                    raise FrameError(f"styled sub-group inside {el.get('id')}")
                n = n.getparent()
            props, cls = effective(leaf, el)
            if strict:
                for k, v in props.items():
                    if "url(" in v:
                        if k != "fill" or self.gradient(v) is None:
                            raise FrameError(f"unsupported paint server in {el.get('id')}")
                if props.get("stroke", "none") != "none":
                    raise FrameError(f"stroked layer in {el.get('id')}")
            if props.get("display") == "none" or props.get("visibility") == "hidden":
                continue
            m = self.ctm(leaf)
            subs, curved = shape_subpaths(leaf)
            subs = [[apply(m, *p) for p in s] for s in subs if len(s) >= 2]
            fr = own_props(leaf).get("fill-rule") or self._inherited(leaf, "fill-rule") or "nonzero"
            out.append(Layer(leaf, subs, fr, signature(props, cls), curved, props, cls))
        return out

    XLINK = "{http://www.w3.org/1999/xlink}href"

    def gradient(self, paint: str):
        """Resolve a url(#id) fill to a linear gradient's effective attributes, or None."""
        m = re.search(r"url\(\s*#([^)\s]+)\s*\)", paint)
        if not m:
            return None
        el = self.by_id.get(m.group(1))
        attrs, stops_el = {}, None
        seen = set()
        while el is not None and id(el) not in seen:
            seen.add(id(el))
            if local(el.tag) != "linearGradient":
                return None
            for k in ("x1", "y1", "x2", "y2", "gradientUnits", "gradientTransform", "spreadMethod"):
                if el.get(k) is not None and k not in attrs:
                    attrs[k] = el.get(k)
            if stops_el is None and any(local(c.tag) == "stop" for c in el):
                stops_el = el
            href = el.get(self.XLINK) or el.get("href")
            el = self.by_id.get(href[1:]) if href and href.startswith("#") else None
        if stops_el is None or not stops_el.get("id"):
            return None
        attrs["stops"] = stops_el.get("id")
        return attrs

    def defs(self):
        for el in self.root:
            if isinstance(el.tag, str) and local(el.tag) == "defs":
                return el
        d = etree.Element(f"{{{SVG_NS}}}defs")
        self.root.insert(0, d)
        return d

    def _inherited(self, el, key):
        n = el.getparent()
        while n is not None:
            v = own_props(n).get(key)
            if v:
                return v
            n = n.getparent()
        return None

    def bbox(self, el) -> Box:
        pts = []
        for leaf in self.leaves(el):
            m = self.ctm(leaf)
            subs, _ = shape_subpaths(leaf)
            for s in subs:
                pts += [apply(m, *p) for p in s]
        if not pts:
            raise FrameError(f"{el.get('id')} has no geometry")
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return Box(min(xs), min(ys), max(xs), max(ys))

    def frames(self):
        """Return {prefix: {part: element}} for every complete nine-slice frame."""
        found = {}
        for i, el in self.by_id.items():
            m = re.fullmatch(r"(?:(.+)-)?(topleft|topright|bottomleft|bottomright)", i)
            if m:
                found.setdefault(m.group(1) or "", {})[m.group(2)] = el
        out = {}
        for prefix, parts in found.items():
            pre = prefix + "-" if prefix else ""
            if all(c in parts for c in CORNERS) and all(pre + e in self.by_id for e in EDGES):
                for e in EDGES:
                    parts[e] = self.by_id[pre + e]
                out[prefix] = parts
        return out


# --------------------------------------------------------------------------
# Corner reconstruction
# --------------------------------------------------------------------------
def _signed_area(pts):
    a = 0.0
    for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
        a += x1 * y2 - x2 * y1
    return a / 2


def coverage(layer: Layer, edge_box: Box, side: str):
    """1-D coverage of a straight edge layer, as depth intervals from the outer side."""
    horizontal = side in ("top", "bottom")
    length = edge_box.w if horizontal else edge_box.h
    events = []
    for s in layer.subpaths:
        xs, ys = [p[0] for p in s], [p[1] for p in s]
        lo_par, hi_par = (min(xs), max(xs)) if horizontal else (min(ys), max(ys))
        if abs(_signed_area(s)) < 1e-9:
            continue
        if (hi_par - lo_par) < 0.98 * length - EPS:
            raise FrameError("edge layer is not a straight strip")
        lo, hi = (min(ys), max(ys)) if horizontal else (min(xs), max(xs))
        if side == "top":
            a, b = lo - edge_box.y0, hi - edge_box.y0
        elif side == "bottom":
            a, b = edge_box.y1 - hi, edge_box.y1 - lo
        elif side == "left":
            a, b = lo - edge_box.x0, hi - edge_box.x0
        else:
            a, b = edge_box.x1 - hi, edge_box.x1 - lo
        w = 1 if _signed_area(s) > 0 else -1
        events.append((a, w))
        events.append((b, -w))
    events.sort()
    out, count, start = [], 0, None
    evenodd = layer.fill_rule == "evenodd"
    parity = 0
    for pos, w in events:
        was = (parity % 2 == 1) if evenodd else (count != 0)
        count += w
        parity += 1
        now = (parity % 2 == 1) if evenodd else (count != 0)
        if now and not was:
            start = pos
        elif was and not now and start is not None:
            if pos - start > EPS:
                out.append((start, pos))
            start = None
    return out


def _snap(v):
    r = round(v)
    return r if abs(v - r) < 2e-3 else round(v, 4)


def _fmt(v):
    v = _snap(v)
    s = f"{v:.4f}".rstrip("0").rstrip(".") if not isinstance(v, int) else str(v)
    return "0" if s in ("-0", "") else s


def align(V, H):
    """Pair the layer stacks of two edges by style, keeping the first edge's z-order.
    Breeze sometimes stacks the same layers in a different order on different edges.
    Layers that exist on only one edge are paired with None."""
    used = set()
    pairs = []
    for v in V:
        j = next((j for j, h in enumerate(H) if j not in used and h.sig == v.sig), None)
        if j is not None:
            used.add(j)
        pairs.append([v, H[j] if j is not None else None, j])
    for j, h in enumerate(H):
        if j in used:
            continue
        # insert after the pair holding the closest earlier H layer
        pos = 0
        for k, (_, _, jj) in enumerate(pairs):
            if jj is not None and jj < j:
                pos = k + 1
        pairs.insert(pos, [None, h, j])
    return [(v, h) for v, h, _ in pairs]


def _fold(layer: Layer, gop: str) -> Layer:
    if gop == "1":
        return layer
    props = dict(layer.props)
    props["opacity"] = _norm("opacity", str(float(props.get("opacity", "1")) * float(gop)))
    if props["opacity"] == "1":
        props.pop("opacity")
    return Layer(layer.el, layer.subpaths, layer.fill_rule, signature(props, layer.cls),
                 layer.curved, props, layer.cls)


def _rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def square_frame(doc: SvgDoc, prefix: str, parts: dict):
    """Replace the four corners of one frame with square versions."""
    plan = []
    for corner in CORNERS:
        vside = "top" if corner.startswith("top") else "bottom"
        hside = "left" if corner.endswith("left") else "right"
        cel = parts[corner]
        K = doc.bbox(cel)
        ve, he = parts[vside], parts[hside]
        VLs, HLs = doc.layers(ve), doc.layers(he)
        gop = group_opacity(ve)
        if gop != group_opacity(he):
            # e.g. Breeze's task-manager highlight: an opaque bar on one edge and a
            # translucent fill on the others. Fold each edge's group opacity into its
            # layers (exact here: layers inside one edge don't overlap).
            VLs = [_fold(l, group_opacity(ve)) for l in VLs]
            HLs = [_fold(l, group_opacity(he)) for l in HLs]
            gop = "1"
        pairs = align(VLs, HLs)
        VB, HB = doc.bbox(ve), doc.bbox(he)

        X = (lambda dep: K.x0 + dep) if hside == "left" else (lambda dep: K.x1 - dep)
        Y = (lambda dep: K.y0 + dep) if vside == "top" else (lambda dep: K.y1 - dep)
        x_in = K.x1 if hside == "left" else K.x0
        y_in = K.y1 if vside == "top" else K.y0
        # shifts that move a corner point onto the matching depth of each edge
        v_shift = (VB.x0 - K.x0, (VB.y0 - K.y0) if vside == "top" else (VB.y1 - K.y1))
        h_shift = ((HB.x0 - K.x0) if hside == "left" else (HB.x1 - K.x1), HB.y0 - K.y0)

        def first_visible(layers, box, side):
            starts = [iv[0][0] for l in layers if _visible(l) for iv in [coverage(l, box, side)] if iv]
            return min(starts) if starts else None
        v_start, h_start = first_visible(VLs, VB, vside), first_visible(HLs, HB, hside)

        items = []   # (style source layer, [polygons in root coords], gradient source or None)
        for vl, hl in pairs:
            tv = coverage(vl, VB, vside) if vl is not None else []
            th = coverage(hl, HB, hside) if hl is not None else []
            graded = any("url(" in l.props.get("fill", "") for l in (vl, hl) if l is not None)
            if vl is not None and hl is not None:
                if len(tv) != len(th):
                    raise FrameError(f"{vside}/{hside} layer profiles don't match")
                if not graded:
                    # Solid layer: each strip is the gap between two nested rectangles of
                    # the square frame, outer at depth (a | c) and inner at depth (b | d).
                    polys = []
                    for (a, b), (c, d) in zip(tv, th):
                        polys.append(_rect(X(c), Y(a), x_in, Y(b)))
                        polys.append(_rect(X(c), Y(a), X(d), y_in))
                    items.append((vl, polys, None))
                else:
                    # Gradient layer (soft shadow): the top arm keeps the top edge's
                    # gradient, the side arm the side edge's; they meet on a 45-degree miter.
                    top, side = [], []
                    for (a, b), (c, d) in zip(tv, th):
                        top.append([(X(c), Y(a)), (x_in, Y(a)), (x_in, Y(b)), (X(d), Y(b))])
                        side.append([(X(c), Y(a)), (X(d), Y(b)), (X(d), y_in), (X(c), y_in)])
                    items.append((vl, top, (vl, v_shift)))
                    items.append((hl, side, (hl, h_shift)))
            else:
                # A layer on one edge only (e.g. Breeze's panel top line) runs on to where
                # the other edge's visible content starts. If the other edge draws nothing
                # at all (e.g. the inset active-tab line), the gap is design, not rounding.
                src = vl or hl
                start = h_start if vl is not None else v_start
                if start is None:
                    raise FrameError("one-sided layer next to an empty edge (intentional inset)")
                polys = [_rect(X(start), Y(a), x_in, Y(b)) for a, b in tv] + \
                        [_rect(X(c), Y(start), X(d), y_in) for c, d in th]
                items.append((src, polys, (src, v_shift if vl is not None else h_shift) if graded else None))
        plan.append((cel, K, gop, items))

    for cel, K, gop, items in plan:
        _write_corner(doc, cel, K, gop, items)


def _clip(poly, K):
    """Clip a convex polygon to box K (Sutherland-Hodgman)."""
    def cut(pts, inside, inter):
        out = []
        for i, cur in enumerate(pts):
            prev = pts[i - 1]
            if inside(cur):
                if not inside(prev):
                    out.append(inter(prev, cur))
                out.append(cur)
            elif inside(prev):
                out.append(inter(prev, cur))
        return out

    def ix(x):
        return lambda p, q: (x, p[1] + (q[1] - p[1]) * (x - p[0]) / (q[0] - p[0]))

    def iy(y):
        return lambda p, q: (p[0] + (q[0] - p[0]) * (y - p[1]) / (q[1] - p[1]), y)

    pts = poly
    for inside, inter in ((lambda p: p[0] >= K.x0 - EPS, ix(K.x0)), (lambda p: p[0] <= K.x1 + EPS, ix(K.x1)),
                          (lambda p: p[1] >= K.y0 - EPS, iy(K.y0)), (lambda p: p[1] <= K.y1 + EPS, iy(K.y1))):
        if not pts:
            break
        pts = cut(pts, inside, inter)
    if len(pts) < 3 or abs(_signed_area(pts)) < EPS * EPS:
        return None
    return pts


def _bbox_matrix(leaf):
    subs, _ = shape_subpaths(leaf)
    pts = [p for s in subs for p in s]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (max(xs) - min(xs), 0.0, 0.0, max(ys) - min(ys), min(xs), min(ys))


def _write_corner(doc, cel, K, gop, items):
    parent = cel.getparent()
    P = doc.ctm(cel, include_self=False)
    inv = inverse(P)
    g = etree.Element(f"{{{SVG_NS}}}g")
    g.set("id", cel.get("id"))
    g.set(f"{{{INKSCAPE_NS}}}label", cel.get("id"))
    if gop != "1":
        g.set("style", f"opacity:{gop}")

    def d_of(polys):
        parts = []
        for pts in polys:
            if _signed_area(pts) < 0:      # same winding for every piece -> nonzero union
                pts = pts[::-1]
            loc = [apply(inv, *q) for q in pts]
            parts.append("M " + " L ".join(f"{_fmt(x)},{_fmt(y)}" for x, y in loc) + " Z")
        return " ".join(parts)

    union = None
    for n, (layer, polys, grad) in enumerate(items):
        polys = [c for c in (_clip(pp, K) for pp in polys) if c]
        if not polys:
            continue
        p = etree.SubElement(g, f"{{{SVG_NS}}}path")
        p.set("id", f"{cel.get('id')}-sq{n}")
        st = dict(layer.props)
        st["fill-rule"] = "nonzero"
        st.setdefault("stroke", "none")
        if grad is not None:
            st["fill"] = f"url(#{_shifted_gradient(doc, cel, n, grad, P)})"
        p.set("style", ";".join(f"{k}:{v}" for k, v in st.items()))
        if layer.cls:
            p.set("class", layer.cls)
        p.set("d", d_of(polys))
        for pts in polys:
            xs, ys = [q[0] for q in pts], [q[1] for q in pts]
            r = (min(xs), min(ys), max(xs), max(ys))
            union = r if union is None else (min(union[0], r[0]), min(union[1], r[1]),
                                             max(union[2], r[2]), max(union[3], r[3]))
    # Plasma sizes corners from their bounding box: keep it identical.
    if union is None or any(abs(a - b) > EPS for a, b in zip(union, (K.x0, K.y0, K.x1, K.y1))):
        h = etree.Element(f"{{{SVG_NS}}}path")
        h.set("id", f"{cel.get('id')}-sqhint")
        h.set("style", "opacity:0.001;fill:#000000;stroke:none")
        h.set("d", d_of([_rect(K.x0, K.y0, K.x1, K.y1)]))
        g.insert(0, h)
    g.tail = cel.tail
    parent.replace(cel, g)
    doc.by_id[g.get("id")] = g


def _shifted_gradient(doc, cel, n, grad, P):
    """Copy an edge's linear gradient so it paints the corner at the same depths."""
    layer, (dx, dy) = grad
    info = doc.gradient(layer.props["fill"])
    GT = parse_transform(info.get("gradientTransform"))
    if info.get("gradientUnits", "objectBoundingBox") == "objectBoundingBox":
        GT = mul(_bbox_matrix(layer.el), GT)
        dflt = {"x1": "0", "y1": "0", "x2": "1", "y2": "0"}
    else:
        dflt = {"x1": "0", "y1": "0", "x2": "100%", "y2": "0"}
    M = doc.ctm(layer.el)
    # corner point q -> edge point q + (dx, dy);  new user space = corner parent's (P)
    newGT = mul(mul(mul(inverse(P), (1, 0, 0, 1, -dx, -dy)), M), GT)
    gid = f"{cel.get('id')}-sqgrad{n}"
    lg = etree.SubElement(doc.defs(), f"{{{SVG_NS}}}linearGradient")
    lg.set("id", gid)
    lg.set(SvgDoc.XLINK, f"#{info['stops']}")
    lg.set("gradientUnits", "userSpaceOnUse")
    lg.set("gradientTransform", "matrix(" + ",".join(f"{v:.6g}" for v in newGT) + ")")
    for k in ("x1", "y1", "x2", "y2"):
        lg.set(k, info.get(k, dflt[k]))
    if info.get("spreadMethod"):
        lg.set("spreadMethod", info["spreadMethod"])
    doc.by_id[gid] = lg
    return gid


def _visible(layer: Layer) -> bool:
    pr = layer.props
    if pr.get("fill", "") == "none":
        return False
    for k in ("opacity", "fill-opacity"):
        try:
            if float(pr.get(k, "1")) < 0.02:
                return False
        except ValueError:
            pass
    return True


def _corner_masks(doc: SvgDoc, el, K: Box, res: int = 8):
    """Rasterise each visible layer of a corner to a coverage mask, keyed by style."""
    from PIL import Image, ImageChops, ImageDraw
    W, H = max(1, round(K.w * res)), max(1, round(K.h * res))
    masks = {}
    for layer in doc.layers(el, strict=False):
        if not _visible(layer):
            continue
        acc = Image.new("1", (W, H), 0)
        for sp in layer.subpaths:
            if len(sp) < 3:
                continue
            m = Image.new("1", (W, H), 0)
            ImageDraw.Draw(m).polygon([((x - K.x0) * res, (y - K.y0) * res) for x, y in sp], fill=1)
            # evenodd: overlapping subpaths cut holes; nonzero (as Breeze draws its
            # corners, and as we write ours): pieces add up
            acc = ImageChops.logical_xor(acc, m) if layer.fill_rule == "evenodd" else ImageChops.logical_or(acc, m)
        key = layer.sig
        masks[key] = ImageChops.logical_or(masks[key], acc) if key in masks else acc
    return masks


def needs_squaring(doc: SvgDoc, prefix: str) -> bool:
    """True when a frame's corners are rounded: drawn with curves, or notched / cut
    away so that their silhouette differs from the square continuation of the edges."""
    from PIL import ImageChops
    parts = doc.frames()[prefix]
    trial = SvgDoc(etree.tostring(doc.root))
    square_frame(trial, prefix, trial.frames()[prefix])   # raises FrameError if impossible
    if corner_is_curved(doc, parts):
        return True
    tparts = trial.frames()[prefix]
    for c in CORNERS:
        K = doc.bbox(parts[c])
        sil = []
        for d, el in ((doc, parts[c]), (trial, tparts[c])):
            masks = list(_corner_masks(d, el, K).values())
            acc = masks[0] if masks else None
            for m in masks[1:]:
                acc = ImageChops.logical_or(acc, m)
            sil.append(acc)
        a, b = sil
        if a is None or b is None:
            if (a or b) is not None and (a or b).getbbox():
                return True
            continue
        diff = ImageChops.logical_xor(a, b)
        if diff.getbbox() and diff.histogram()[-1] > 0.02 * diff.width * diff.height:
            return True
    return False


def corner_is_curved(doc: SvgDoc, parts: dict) -> bool:
    for c in CORNERS:
        for leaf in doc.leaves(parts[c]):
            subs, curved = shape_subpaths(leaf)
            if curved:
                # ignore microscopic curves (Breeze has some 0.001px ones)
                m = doc.ctm(leaf)
                pts = [apply(m, *p) for s in subs for p in s]
                xs, ys = [p[0] for p in pts], [p[1] for p in pts]
                if max(xs) - min(xs) > 0.05 and max(ys) - min(ys) > 0.05:
                    if local(leaf.tag) in ("circle", "ellipse", "rect") or _has_real_curve(leaf):
                        return True
    return False


def _has_real_curve(leaf) -> bool:
    d = leaf.get("d", "")
    # a curve whose control points deviate by < 0.01 from a straight line is not a rounded corner
    for m in re.finditer(r"[CcAaQqSsTt][^MmLlHhVvZz]*", d):
        nums = [abs(float(n)) for n in re.findall(_NUM, m.group(0)[1:])]
        if m.group(0)[0] in "Aa":
            if nums and (nums[0] > 0.05 or nums[1] > 0.05):
                return True
        elif any(n > 0.05 for n in nums):
            return True
    return False
