# -*- coding: utf-8 -*-
"""
Assembly BOM - a self-contained interactive BOM, with no external tools.

InteractiveHtmlBom is a KiCad plugin that needs `pcbnew`, which is what makes it
awkward to run from a script. This module does the same job from first
principles: it reads the .kicad_pcb file directly, works out where every
footprint and pad sits, and writes one standalone HTML file with the board drawn
as SVG next to a clickable BOM.

No pcbnew. No plugin. No pip. Just the board file.

What it draws: the board outline, every pad, and the silkscreen. That is what
you need at a bench to find R47. It is not a pixel-for-pixel copy of
InteractiveHtmlBom - there are no copper pours, no tracks, no 3D.

Entry point:  build(pcb_path, out_path, meta) -> out_path
"""

from __future__ import annotations

import datetime as _dt
import html
import json
import math
import os
import re

__all__ = ["build", "parse_board", "Board", "Footprint"]


# --------------------------------------------------------------------------- #
#  s-expression reader
# --------------------------------------------------------------------------- #

def parse_sexp(text: str) -> list:
    """
    Iterative s-expression reader. Iterative rather than recursive because a
    real board nests deeply enough to blow Python's stack.
    """
    root: list = []
    stack = [root]
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "(":
            node: list = []
            stack[-1].append(node)
            stack.append(node)
            i += 1
        elif c == ")":
            if len(stack) > 1:
                stack.pop()
            i += 1
        elif c == '"':
            j = i + 1
            buf = []
            while j < n:
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j + 1])
                    j += 2
                    continue
                if text[j] == '"':
                    break
                buf.append(text[j])
                j += 1
            stack[-1].append("".join(buf))
            i = j + 1
        elif c.isspace():
            i += 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in "()\"":
                j += 1
            stack[-1].append(text[i:j])
            i = j
    return root


def kids(node, name: str) -> list:
    return [c for c in node if isinstance(c, list) and c and c[0] == name]


def kid(node, name: str):
    for c in node:
        if isinstance(c, list) and c and c[0] == name:
            return c
    return None


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def xy(node, name: str, default=(0.0, 0.0)) -> tuple:
    c = kid(node, name)
    if not c or len(c) < 3:
        return default
    return (_f(c[1]), _f(c[2]))


def at_of(node) -> tuple:
    """(x, y, rotation-degrees)"""
    c = kid(node, "at")
    if not c:
        return (0.0, 0.0, 0.0)
    return (_f(c[1] if len(c) > 1 else 0), _f(c[2] if len(c) > 2 else 0),
            _f(c[3] if len(c) > 3 else 0))


def layer_of(node) -> str:
    c = kid(node, "layer")
    return str(c[1]) if c and len(c) > 1 else ""


def layers_of(node) -> list:
    c = kid(node, "layers")
    return [str(x) for x in c[1:]] if c else []


# --------------------------------------------------------------------------- #
#  board model
# --------------------------------------------------------------------------- #

SILK = ("F.SilkS", "B.SilkS", "F.Silkscreen", "B.Silkscreen")


class Pad:
    __slots__ = ("x", "y", "rot", "w", "h", "shape", "rratio", "front", "back", "number")

    def __init__(self, x, y, rot, w, h, shape, rratio, front, back, number):
        self.x, self.y, self.rot = x, y, rot
        self.w, self.h = w, h
        self.shape, self.rratio = shape, rratio
        self.front, self.back, self.number = front, back, number


class Footprint:
    def __init__(self):
        self.ref = ""
        self.value = ""
        self.fpid = ""
        self.x = self.y = self.rot = 0.0
        self.side = "F"              # F or B
        self.pads: list[Pad] = []
        self.silk: list[tuple] = []  # ("line", x1,y1,x2,y2) / ("circle", cx,cy,r) / ("arc", path)
        self.in_bom = True
        self.dnp = False
        self.smd = False
        self.fields: dict = {}      # every user property on the footprint

    @property
    def key(self) -> tuple:
        return (self.value, self.fpid)


class Board:
    def __init__(self):
        self.outline: list[tuple] = []
        self.footprints: list[Footprint] = []
        self.bbox = (0.0, 0.0, 100.0, 100.0)
        self.layer_count = 2


def _shape_from_pad(pad) -> Pad:
    px, py, prot = at_of(pad)
    w, h = xy(pad, "size", (1.0, 1.0))
    shape = str(pad[3]) if len(pad) > 3 else "rect"
    rr = kid(pad, "roundrect_rratio")
    rratio = _f(rr[1]) if rr and len(rr) > 1 else 0.25
    lay = layers_of(pad)
    front = any(l.startswith("F.") or l == "*.Cu" or l.startswith("*") for l in lay)
    back = any(l.startswith("B.") or l == "*.Cu" or l.startswith("*") for l in lay)
    num = str(pad[1]) if len(pad) > 1 else ""
    return Pad(px, py, prot, abs(w), abs(h), shape, rratio, front, back, num)


def _arc_points(start, mid, end):
    """Approximate a KiCad three-point arc as a polyline - good enough to draw."""
    (x1, y1), (xm, ym), (x2, y2) = start, mid, end
    # circumcentre
    d = 2 * (x1 * (ym - y2) + xm * (y2 - y1) + x2 * (y1 - ym))
    if abs(d) < 1e-9:
        return [start, end]
    ux = ((x1 ** 2 + y1 ** 2) * (ym - y2) + (xm ** 2 + ym ** 2) * (y2 - y1)
          + (x2 ** 2 + y2 ** 2) * (y1 - ym)) / d
    uy = ((x1 ** 2 + y1 ** 2) * (x2 - xm) + (xm ** 2 + ym ** 2) * (x1 - x2)
          + (x2 ** 2 + y2 ** 2) * (xm - x1)) / d
    r = math.hypot(x1 - ux, y1 - uy)
    a1 = math.atan2(y1 - uy, x1 - ux)
    am = math.atan2(ym - uy, xm - ux)
    a2 = math.atan2(y2 - uy, x2 - ux)

    def norm(a):
        while a < 0:
            a += 2 * math.pi
        while a >= 2 * math.pi:
            a -= 2 * math.pi
        return a

    a1n, amn, a2n = norm(a1), norm(am), norm(a2)
    ccw = not ((a1n < amn < a2n) or (amn < a2n < a1n) or (a2n < a1n < amn))
    sweep = a2 - a1
    if ccw and sweep > 0:
        sweep -= 2 * math.pi
    if not ccw and sweep < 0:
        sweep += 2 * math.pi
    steps = max(6, int(abs(sweep) / 0.2))
    return [(ux + r * math.cos(a1 + sweep * k / steps),
             uy + r * math.sin(a1 + sweep * k / steps)) for k in range(steps + 1)]


def _graphic_segments(node, tag: str) -> list:
    """Turn one gr_*/fp_* graphic into a list of ("poly", [(x,y)...]) or ("circle",...)."""
    out = []
    if tag in ("gr_line", "fp_line"):
        out.append(("poly", [xy(node, "start"), xy(node, "end")]))
    elif tag in ("gr_rect", "fp_rect"):
        (x1, y1), (x2, y2) = xy(node, "start"), xy(node, "end")
        out.append(("poly", [(x1, y1), (x2, y1), (x2, y2), (x1, y2), (x1, y1)]))
    elif tag in ("gr_circle", "fp_circle"):
        c, e = xy(node, "center"), xy(node, "end")
        out.append(("circle", c[0], c[1], math.hypot(e[0] - c[0], e[1] - c[1])))
    elif tag in ("gr_arc", "fp_arc"):
        out.append(("poly", _arc_points(xy(node, "start"), xy(node, "mid"), xy(node, "end"))))
    elif tag in ("gr_poly", "fp_poly"):
        pts = kid(node, "pts")
        if pts:
            p = [(_f(c[1]), _f(c[2])) for c in pts if isinstance(c, list) and c[0] == "xy"]
            if p:
                out.append(("poly", p + [p[0]]))
    elif tag in ("gr_curve", "fp_curve"):
        pts = kid(node, "pts")
        if pts:
            p = [(_f(c[1]), _f(c[2])) for c in pts if isinstance(c, list) and c[0] == "xy"]
            if p:
                out.append(("poly", p))
    return out


def parse_board(pcb_path: str) -> Board:
    with open(pcb_path, encoding="utf-8", errors="ignore") as fh:
        text = fh.read()
    tree = parse_sexp(text)
    root = tree[0] if tree and isinstance(tree[0], list) else tree

    b = Board()
    lay = kid(root, "layers")
    if lay:
        b.layer_count = len([c for c in lay if isinstance(c, list)
                             and len(c) > 1 and str(c[1]).endswith(".Cu")])

    # ---- board outline ------------------------------------------------- #
    for tag in ("gr_line", "gr_arc", "gr_rect", "gr_circle", "gr_poly", "gr_curve"):
        for g in kids(root, tag):
            if layer_of(g) != "Edge.Cuts":
                continue
            b.outline.extend(_graphic_segments(g, tag))

    # ---- footprints ------------------------------------------------------ #
    for fp in kids(root, "footprint") + kids(root, "module"):
        f = Footprint()
        f.fpid = str(fp[1]) if len(fp) > 1 else ""
        f.x, f.y, f.rot = at_of(fp)
        f.side = "B" if layer_of(fp).startswith("B.") else "F"

        attr = kid(fp, "attr")
        if attr:
            toks = [str(t) for t in attr[1:]]
            f.smd = "smd" in toks
            if "exclude_from_bom" in toks:
                f.in_bom = False
            if "dnp" in toks:
                f.dnp = True
        dnp = kid(fp, "dnp")
        if dnp and str(dnp[1] if len(dnp) > 1 else "yes") in ("yes", "true"):
            f.dnp = True

        # KiCad 7+: (property "Reference" "R1" ...)   older: (fp_text reference "R1" ...)
        for p in kids(fp, "property"):
            if len(p) >= 3:
                nm, val = str(p[1]), str(p[2])
                if nm == "Reference":
                    f.ref = val
                elif nm == "Value":
                    f.value = val
                elif nm and not nm.startswith("ki_"):
                    f.fields[nm] = val
        for t in kids(fp, "fp_text"):
            if len(t) >= 3:
                if str(t[1]) == "reference" and not f.ref:
                    f.ref = str(t[2])
                elif str(t[1]) == "value" and not f.value:
                    f.value = str(t[2])

        for pad in kids(fp, "pad"):
            f.pads.append(_shape_from_pad(pad))

        for tag in ("fp_line", "fp_arc", "fp_rect", "fp_circle", "fp_poly", "fp_curve"):
            for g in kids(fp, tag):
                if layer_of(g) in SILK:
                    f.silk.extend(_graphic_segments(g, tag))

        if f.ref or f.pads:
            b.footprints.append(f)

    b.bbox = _bbox(b)
    return b


def _bbox(b: Board) -> tuple:
    xs, ys = [], []
    for seg in b.outline:
        if seg[0] == "poly":
            for (x, y) in seg[1]:
                xs.append(x)
                ys.append(y)
        elif seg[0] == "circle":
            xs += [seg[1] - seg[3], seg[1] + seg[3]]
            ys += [seg[2] - seg[3], seg[2] + seg[3]]
    if not xs:                                   # no Edge.Cuts - fall back to parts
        for f in b.footprints:
            for p in f.pads:
                r = max(p.w, p.h)
                xs += [f.x - r, f.x + r]
                ys += [f.y - r, f.y + r]
    if not xs:
        return (0.0, 0.0, 100.0, 100.0)
    pad = 2.0
    return (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)


# --------------------------------------------------------------------------- #
#  SVG
# --------------------------------------------------------------------------- #

def _svg_pad(p: Pad) -> str:
    """One pad, in the footprint's local (unrotated) frame."""
    w, h = p.w, p.h
    if p.shape == "circle" or (p.shape == "oval" and abs(w - h) < 1e-6):
        return f'<circle cx="{p.x:.4f}" cy="{p.y:.4f}" r="{w/2:.4f}"/>'
    rx = 0.0
    if p.shape == "oval":
        rx = min(w, h) / 2
    elif p.shape == "roundrect":
        rx = min(w, h) * p.rratio
    attrs = (f'x="{p.x - w/2:.4f}" y="{p.y - h/2:.4f}" '
             f'width="{w:.4f}" height="{h:.4f}"')
    if rx:
        attrs += f' rx="{rx:.4f}" ry="{rx:.4f}"'
    return f"<rect {attrs}/>"


def _svg_graphic(seg) -> str:
    if seg[0] == "poly":
        pts = " ".join(f"{x:.4f},{y:.4f}" for (x, y) in seg[1])
        return f'<polyline points="{pts}"/>'
    if seg[0] == "circle":
        return f'<circle cx="{seg[1]:.4f}" cy="{seg[2]:.4f}" r="{seg[3]:.4f}"/>'
    return ""


def _local_bbox(f: Footprint) -> tuple:
    """Extent of a footprint in its own frame, for the click target."""
    xs, ys = [], []
    for p in f.pads:
        xs += [p.x - p.w / 2, p.x + p.w / 2]
        ys += [p.y - p.h / 2, p.y + p.h / 2]
    for seg in f.silk:
        if seg[0] == "poly":
            for (x, y) in seg[1]:
                xs.append(x)
                ys.append(y)
        elif seg[0] == "circle":
            xs += [seg[1] - seg[3], seg[1] + seg[3]]
            ys += [seg[2] - seg[3], seg[2] + seg[3]]
    if not xs:
        return (-0.5, -0.5, 1.0, 1.0)
    m = 0.1
    return (min(xs) - m, min(ys) - m,
            max(xs) - min(xs) + 2 * m, max(ys) - min(ys) + 2 * m)


def board_svg(b: Board, side: str) -> str:
    """
    One board view. KiCad's PCB coordinates are y-down, the same as SVG, so
    positions map straight across; rotation flips sign because KiCad measures
    counter-clockwise. The back view is mirrored about the board's centre.
    """
    x0, y0, x1, y1 = b.bbox
    w, h = x1 - x0, y1 - y0
    parts = [f'<svg viewBox="{x0:.3f} {y0:.3f} {w:.3f} {h:.3f}" '
             f'xmlns="http://www.w3.org/2000/svg" class="board" data-side="{side}" '
             f'preserveAspectRatio="xMidYMid meet">']
    if side == "B":
        cx = (x0 + x1) / 2
        parts.append(f'<g transform="translate({2*cx:.4f},0) scale(-1,1)">')

    parts.append('<g class="outline">')
    for seg in b.outline:
        parts.append(_svg_graphic(seg))
    parts.append("</g>")

    for f in b.footprints:
        near = "near" if f.side == side else "far"
        cls = f"fp {near}" + (" dnp" if f.dnp else "")
        parts.append(f'<g class="{cls}" data-ref="{html.escape(f.ref)}" '
                     f'transform="translate({f.x:.4f},{f.y:.4f}) rotate({-f.rot:.4f})">')
        if f.silk:
            parts.append('<g class="silk">')
            for seg in f.silk:
                parts.append(_svg_graphic(seg))
            parts.append("</g>")
        parts.append('<g class="pads">')
        for p in f.pads:
            local_rot = p.rot - f.rot          # pad angles are stored absolute
            if abs(local_rot) > 1e-6:
                parts.append(f'<g transform="rotate({-local_rot:.4f} '
                             f'{p.x:.4f} {p.y:.4f})">{_svg_pad(p)}</g>')
            else:
                parts.append(_svg_pad(p))
        parts.append("</g>")
        # transparent click target: the middle of a QFP is empty, so without
        # this you can only select a part by hitting one of its pads
        hx, hy, hw, hh = _local_bbox(f)
        parts.append(f'<rect class="hit" x="{hx:.4f}" y="{hy:.4f}" '
                     f'width="{hw:.4f}" height="{hh:.4f}"/>')
        parts.append(f'<title>{html.escape(f.ref)}  {html.escape(f.value)}</title>')
        parts.append("</g>")

    if side == "B":
        parts.append("</g>")
    parts.append("</svg>")
    return "".join(parts)


# --------------------------------------------------------------------------- #
#  grouping
# --------------------------------------------------------------------------- #

def schematic_fields_by_ref(sch_paths) -> dict:
    """
    reference -> {field: value}, read from the schematic.

    Footprints only carry the schematic's fields if "Update PCB from Schematic"
    copied them across, so this is the fallback when the board does not have
    them.
    """
    out: dict = {}
    for path in sch_paths or []:
        if not path or not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                tree = parse_sexp(fh.read())
        except Exception:
            continue
        root = tree[0] if tree and isinstance(tree[0], list) else tree
        stack = [root]
        while stack:
            node = stack.pop()
            for c in node:
                if not isinstance(c, list) or not c:
                    continue
                if c[0] == "symbol":
                    props = {}
                    for p in kids(c, "property"):
                        if len(p) >= 3:
                            props[str(p[1])] = str(p[2])
                    ref = props.get("Reference", "")
                    if ref and not ref.startswith("#"):
                        out.setdefault(ref, {}).update(
                            {k: v for k, v in props.items()
                             if k not in ("Reference",) and not k.startswith("ki_")})
                stack.append(c)
    return out


def _refkey(ref: str):
    m = re.match(r"([A-Za-z_]*)(\d*)", ref or "")
    return (m.group(1), int(m.group(2) or 0)) if m else (ref, 0)


def bom_rows(b: Board, include_dnp: bool = False, extra_fields=None,
             sch_fields: dict | None = None) -> list[dict]:
    extra_fields = list(extra_fields or [])
    sch_fields = sch_fields or {}

    def field_of(f, name):
        v = (f.fields.get(name) or "").strip()
        if v:
            return v
        return (sch_fields.get(f.ref, {}).get(name) or "").strip()

    groups: dict = {}
    for f in b.footprints:
        if not f.in_bom or not f.ref:
            continue
        if f.dnp and not include_dnp:
            continue
        key = (f.value, f.fpid, f.side) + tuple(field_of(f, x) for x in extra_fields)
        groups.setdefault(key, []).append(f)
    rows = []
    for key, fps in groups.items():
        value, fpid, side = key[0], key[1], key[2]
        refs = sorted((x.ref for x in fps), key=_refkey)
        rows.append({
            "value": value, "footprint": fpid.split(":")[-1], "lib": fpid,
            "side": side, "qty": len(fps), "refs": refs,
            "dnp": any(x.dnp for x in fps),
            "mount": "SMD" if fps[0].smd else "THT",
            "extra": {name: field_of(fps[0], name) for name in extra_fields},
        })
    rows.sort(key=lambda r: (r["side"] != "F", _refkey(r["refs"][0])))
    for i, r in enumerate(rows, 1):
        r["n"] = i
    return rows


# --------------------------------------------------------------------------- #
#  the page
# --------------------------------------------------------------------------- #

_CSS = """
*{box-sizing:border-box}
:root{
  --bg:#f6f7f9; --panel:#fff; --ink:#14181d; --muted:#657085; --line:#dfe3ea;
  --accent:#1f7a4d; --accent-soft:#d8efe3; --warn:#b4530a;
  --board:#2c6b4f; --pad:#d8a93c; --silk:#f2f4f7; --edge:#9fb4c7; --hl:#ff4f9a;
}
:root[data-theme=dark]{
  --bg:#12151a; --panel:#1a1f26; --ink:#e8ecf2; --muted:#93a0b4; --line:#2a323d;
  --accent:#49c78b; --accent-soft:#173a2b; --warn:#e0913f;
  --board:#1e4a37; --pad:#c9a13a; --silk:#e8ecf2; --edge:#6f8699; --hl:#ff4f9a;
}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--ink);
  font:14px/1.45 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{display:flex;gap:16px;align-items:baseline;flex-wrap:wrap;
  padding:12px 16px;background:var(--panel);border-bottom:1px solid var(--line)}
header h1{font-size:16px;margin:0;font-weight:650}
header .meta{color:var(--muted);font-size:13px}
header .spacer{flex:1}
.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;
  padding:10px 16px;background:var(--panel);border-bottom:1px solid var(--line)}
input[type=search],select{font:inherit;padding:6px 10px;border:1px solid var(--line);
  border-radius:7px;background:var(--bg);color:var(--ink)}
input[type=search]{min-width:220px}
button{font:inherit;padding:6px 12px;border:1px solid var(--line);border-radius:7px;
  background:var(--bg);color:var(--ink);cursor:pointer}
button:hover{border-color:var(--accent)}
button.on{background:var(--accent-soft);border-color:var(--accent);color:var(--ink)}
.progress{flex:1;min-width:140px;height:8px;border-radius:99px;background:var(--line);
  overflow:hidden}
.progress i{display:block;height:100%;background:var(--accent);width:0;
  transition:width .18s ease}
main{display:grid;grid-template-columns:minmax(340px,1fr) minmax(320px,1.1fr);
  gap:12px;padding:12px;align-items:start}
body.wide main{grid-template-columns:minmax(520px,1.5fr) minmax(280px,1fr)}
@media (max-width:1100px){body.wide main{grid-template-columns:1fr}}
@media (max-width:900px){main{grid-template-columns:1fr}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  overflow:hidden}
.tablewrap{max-height:calc(100vh - 210px);overflow:auto}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{text-align:left;padding:7px 9px;border-bottom:1px solid var(--line);
  vertical-align:top}
th{position:sticky;top:0;background:var(--panel);z-index:2;font-weight:600;
  color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
tbody tr{cursor:pointer}
tbody tr:hover{background:var(--accent-soft)}
tbody tr.sel{background:var(--accent-soft);box-shadow:inset 3px 0 0 var(--accent)}
tbody tr.done td:not(.cb){opacity:.45;text-decoration:line-through}
tbody tr.hidden{display:none}
td.cb{width:30px}
td.qty{text-align:right;width:44px;font-variant-numeric:tabular-nums}
td.refs{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
td.pn{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;
  white-space:nowrap}
.pill{display:inline-block;padding:1px 6px;border-radius:99px;font-size:11px;
  border:1px solid var(--line);color:var(--muted)}
.boards{display:flex;flex-direction:column;gap:10px;padding:10px}
.viewbox{position:relative;background:var(--board);border-radius:8px;overflow:hidden}
.viewbox.hidden{display:none}
.viewbox b{position:absolute;top:6px;left:10px;color:#fff;opacity:.75;
  font-size:11px;letter-spacing:.08em;text-transform:uppercase}
svg.board{display:block;width:100%;height:auto;max-height:44vh}
.outline polyline,.outline circle{fill:none;stroke:var(--edge);stroke-width:.22}
.silk polyline,.silk circle{fill:none;stroke:var(--silk);stroke-width:.13;opacity:.75}
.pads{fill:var(--pad)}
g.fp.near .pads{fill:var(--pad)}
g.fp{cursor:pointer}
rect.hit{fill:transparent;stroke:none;pointer-events:all}
g.fp.far rect.hit{pointer-events:none}
g.fp.hl rect.hit{fill:var(--hl);opacity:.10}
g.fp.far{opacity:.16}
g.fp.dnp .pads{fill:none;stroke:var(--pad);stroke-width:.1}
g.fp.hl .pads{fill:var(--hl)}
g.fp.hl{filter:drop-shadow(0 0 .6px var(--hl)) drop-shadow(0 0 1.4px var(--hl))}
g.fp.hl .silk polyline,g.fp.hl .silk circle{stroke:var(--hl);opacity:1;stroke-width:.2}
body.filtering g.fp:not(.hl){opacity:.28}
body.filtering g.fp.hl{opacity:1}
footer{padding:10px 16px;color:var(--muted);font-size:12px}
kbd{border:1px solid var(--line);border-bottom-width:2px;border-radius:4px;
  padding:0 5px;font-size:11px;font-family:inherit}
"""

_JS = r"""
const $=(s,r=document)=>r.querySelector(s), $$=(s,r=document)=>[...r.querySelectorAll(s)];
const KEY='assembly-bom:'+DATA.project+':'+DATA.rev;
let done=new Set(), sel=null;

try{ const raw=localStorage.getItem(KEY); if(raw) done=new Set(JSON.parse(raw)); }catch(e){}
const save=()=>{ try{ localStorage.setItem(KEY,JSON.stringify([...done])); }catch(e){} };

function rowsAll(){ return $$('#bom tbody tr'); }

function highlight(){
  $$('g.fp').forEach(g=>g.classList.remove('hl'));
  const tr=sel!==null?$('#r'+sel):null;
  if(!tr){ document.body.classList.remove('filtering'); return; }
  JSON.parse(tr.dataset.refs).forEach(ref=>{
    $$('g.fp[data-ref="'+CSS.escape(ref)+'"]').forEach(g=>g.classList.add('hl'));
  });
  document.body.classList.add('filtering');
}

function select(n,scroll){
  sel=n; rowsAll().forEach(tr=>tr.classList.toggle('sel',tr.id==='r'+n));
  const tr=$('#r'+n);
  if(tr&&scroll) tr.scrollIntoView({block:'nearest'});
  highlight();
}

function setDone(n,v){
  const tr=$('#r'+n); if(!tr) return;
  v?done.add(String(n)):done.delete(String(n));
  tr.classList.toggle('done',v); $('#c'+n).checked=v; save(); progress();
}

function progress(){
  const total=rowsAll().length, d=rowsAll().filter(t=>t.classList.contains('done')).length;
  $('#bar i').style.width=(total?100*d/total:0)+'%';
  $('#count').textContent=d+' / '+total+' placed';
}

function applyFilter(){
  const q=$('#q').value.trim().toLowerCase();
  const hideDone=$('#hidedone').classList.contains('on');
  rowsAll().forEach(tr=>{
    const hay=tr.dataset.search;
    let show=!q||hay.includes(q);
    if(show&&hideDone&&tr.classList.contains('done')) show=false;
    tr.classList.toggle('hidden',!show);
  });
}

function side(which){
  $$('#sidebtns button').forEach(b=>b.classList.toggle('on',b.dataset.side===which));
  $$('.viewbox').forEach(v=>v.classList.toggle('hidden',
      which!=='BOTH' && v.dataset.side!==which));
}

document.addEventListener('DOMContentLoaded',()=>{
  rowsAll().forEach(tr=>{
    const n=tr.id.slice(1);
    if(done.has(String(n))){ tr.classList.add('done'); $('#c'+n).checked=true; }
    tr.addEventListener('click',e=>{
      if(e.target.type==='checkbox') return;
      select(n,false);
    });
    $('#c'+n).addEventListener('change',e=>{ setDone(n,e.target.checked); applyFilter(); });
  });

  $$('g.fp').forEach(g=>g.addEventListener('click',()=>{
    const ref=g.dataset.ref;
    const tr=rowsAll().find(t=>JSON.parse(t.dataset.refs).includes(ref));
    if(tr) select(tr.id.slice(1),true);
  }));

  $('#q').addEventListener('input',applyFilter);
  $('#hidedone').addEventListener('click',e=>{
    e.currentTarget.classList.toggle('on'); applyFilter();
  });
  $$('#sidebtns button').forEach(b=>b.addEventListener('click',()=>side(b.dataset.side)));
  $('#theme').addEventListener('click',()=>{
    const cur=document.documentElement.getAttribute('data-theme')==='dark'?'light':'dark';
    document.documentElement.setAttribute('data-theme',cur);
    try{ localStorage.setItem('assembly-bom-theme',cur); }catch(e){}
  });
  $('#clear').addEventListener('click',()=>{
    if(!confirm('Clear all placement ticks?')) return;
    done=new Set(); save();
    rowsAll().forEach(tr=>{ tr.classList.remove('done'); $('#c'+tr.id.slice(1)).checked=false; });
    applyFilter(); progress();
  });

  document.addEventListener('keydown',e=>{
    if(e.target.tagName==='INPUT') return;
    const rows=rowsAll().filter(t=>!t.classList.contains('hidden'));
    if(!rows.length) return;
    let i=rows.findIndex(t=>t.id==='r'+sel);
    if(e.key==='n'||e.key==='ArrowDown'){ e.preventDefault(); i=Math.min(rows.length-1,i+1); select(rows[i].id.slice(1),true); }
    else if(e.key==='p'||e.key==='ArrowUp'){ e.preventDefault(); i=Math.max(0,i-1); select(rows[i].id.slice(1),true); }
    else if(e.key===' '&&sel!==null){ e.preventDefault(); setDone(sel,!done.has(String(sel))); applyFilter(); }
    else if(e.key==='Escape'){ sel=null; rowsAll().forEach(t=>t.classList.remove('sel')); highlight(); }
  });

  try{ const t=localStorage.getItem('assembly-bom-theme');
       if(t) document.documentElement.setAttribute('data-theme',t); }catch(e){}
  side('BOTH'); progress(); applyFilter();
});
"""


def build(pcb_path: str, out_path: str, meta: dict | None = None,
          include_dnp: bool = False, dark: bool = True,
          extra_fields=None, sch_paths=None) -> str:
    """Parse the board and write the standalone HTML. Returns out_path."""
    meta = meta or {}
    b = parse_board(pcb_path)
    extra_fields = list(extra_fields or [])
    sch_fields = schematic_fields_by_ref(sch_paths) if (extra_fields and sch_paths) else {}
    rows = bom_rows(b, include_dnp=include_dnp, extra_fields=extra_fields,
                    sch_fields=sch_fields)

    project = meta.get("project") or os.path.splitext(os.path.basename(pcb_path))[0]
    rev = meta.get("rev", "")
    title = meta.get("title") or project
    generated = meta.get("generated") or _dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    front = sum(1 for f in b.footprints if f.side == "F" and f.in_bom)
    back = sum(1 for f in b.footprints if f.side == "B" and f.in_bom)
    parts_total = sum(r["qty"] for r in rows)

    body = []
    for r in rows:
        n = r["n"]
        vals = [r["extra"].get(f, "") for f in extra_fields]
        search = " ".join([r["value"], r["footprint"], " ".join(r["refs"]),
                           r["mount"], r["side"]] + vals).lower()
        body.append(
            f'<tr id="r{n}" data-refs=\'{json.dumps(r["refs"])}\' '
            f'data-search="{html.escape(search, quote=True)}">'
            f'<td class="cb"><input type="checkbox" id="c{n}"></td>'
            f'<td>{n}</td>'
            f'<td><strong>{html.escape(r["value"])}</strong>'
            + (' <span class="pill">DNP</span>' if r["dnp"] else '') +
            f'</td>'
            f'<td>{html.escape(r["footprint"])}<br>'
            f'<span class="pill">{r["mount"]}</span> '
            f'<span class="pill">{"Top" if r["side"] == "F" else "Bottom"}</span></td>'
            + "".join(f'<td class="pn">{html.escape(v) or "&mdash;"}</td>'
                      for v in vals)
            + f'<td class="qty">{r["qty"]}</td>'
            f'<td class="refs">{html.escape(", ".join(r["refs"]))}</td>'
            f'</tr>')

    data = {"project": project, "rev": rev}

    html_doc = f"""<!DOCTYPE html>
<html lang="en" data-theme="{'dark' if dark else 'light'}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} {html.escape(rev)} - Assembly BOM</title>
<style>{_CSS}</style>
</head>
<body class="{'wide' if extra_fields else ''}">
<header>
  <h1>{html.escape(title)}</h1>
  <span class="meta">Rev {html.escape(rev) or '-'}</span>
  <span class="meta">{len(rows)} line items &middot; {parts_total} parts &middot;
    {front} top / {back} bottom &middot; {b.layer_count} layer</span>
  {'<span class="meta">' + html.escape(" / ".join(extra_fields)) + '</span>'
   if extra_fields else ''}
  <span class="spacer"></span>
  <span class="meta">{html.escape(generated)}</span>
</header>

<div class="toolbar">
  <input type="search" id="q" placeholder="Search value, footprint, designator...">
  <span id="sidebtns">
    <button data-side="BOTH" class="on">Both</button>
    <button data-side="F">Top</button>
    <button data-side="B">Bottom</button>
  </span>
  <button id="hidedone">Hide placed</button>
  <button id="clear">Reset ticks</button>
  <button id="theme">Theme</button>
  <span class="progress" id="bar"><i></i></span>
  <span class="meta" id="count"></span>
</div>

<main>
  <section class="card">
    <div class="tablewrap">
      <table id="bom">
        <thead><tr><th></th><th>#</th><th>Value</th><th>Footprint</th>
          {''.join(f'<th>{html.escape(f)}</th>' for f in extra_fields)}
          <th class="qty">Qty</th><th>Designators</th></tr></thead>
        <tbody>{''.join(body)}</tbody>
      </table>
    </div>
  </section>

  <section class="card">
    <div class="boards">
      <div class="viewbox" data-side="F"><b>Top</b>{board_svg(b, 'F')}</div>
      <div class="viewbox" data-side="B"><b>Bottom</b>{board_svg(b, 'B')}</div>
    </div>
  </section>
</main>

<footer>
  Click a row to highlight it on the board, or click a part to find its row.
  <kbd>n</kbd>/<kbd>p</kbd> step through, <kbd>space</kbd> ticks one off,
  <kbd>Esc</kbd> clears. Ticks are saved in this browser only.
</footer>

<script>const DATA={json.dumps(data)};{_JS}</script>
</body>
</html>
"""
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html_doc)
    return out_path


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: assembly_bom.py <board.kicad_pcb> [out.html]")
        raise SystemExit(2)
    src = sys.argv[1]
    dst = sys.argv[2] if len(sys.argv) > 2 else os.path.splitext(src)[0] + "_AssemblyBOM.html"
    print(build(src, dst))
