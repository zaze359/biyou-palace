#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mark_spec.py - Turn an abstract logo motif into a production-ready specification kit.

Renders a geometric motif (defined as a small JSON primitive DSL) into:
  mark.svg              full-color master (vector)
  mark-mono.svg         single-color version (printing, foil, engraving)
  mark-reverse.svg      reversed-out white version (dark backgrounds)
  safe-zone.svg         clear-space diagram
  scale-test.png        16/24/32/48/64 px legibility test, real size + 4x zoom
  application-preview.png   six-scenario application board
  spec.md               written specification (colors, sizes, clear space, misuse)
  spec.html             one-page browser-viewable brand sheet

Dependencies:
  - Python 3.9+ standard library only, for SVG / Markdown / HTML output.
  - Pillow (optional) for the PNG outputs. Installed? -> PNGs are generated.
    Missing? -> PNGs are skipped with a notice; everything else still works.

Usage:
  python mark_spec.py --config mark.json --outdir ./dist
  python mark_spec.py --config mark.json --outdir ./dist --no-png
  python mark_spec.py --config mark.json --outdir ./dist --check-mono
  python mark_spec.py --init-template ./mark-template.json
"""

import argparse
import base64
import json
import math
import os
import sys
from pathlib import Path

# ----------------------------------------------------------------------------
# Optional Pillow
# ----------------------------------------------------------------------------
try:
    from PIL import Image, ImageDraw, ImageFont
    HAS_PIL = True
except ImportError:  # pragma: no cover
    HAS_PIL = False


DEFAULT_CANVAS = 100.0
PANEL_IVORY = "#F3F0E8"

TEMPLATE_CONFIG = {
    "brand": {
        "name_zh": "示例品牌",
        "name_en": "Stellar Vision",
        "tagline": "",
    },
    "palette": {
        "primary": "#4A6FA5",
        "structural": "#1B2A41",
        "accent": "#E8C47A",
        "panel": PANEL_IVORY,
    },
    "canvas": DEFAULT_CANVAS,
    "motif": [
        {"type": "arc", "cx": 50, "cy": 52, "rx": 28, "ry": 28,
         "start": 35, "end": 145, "fill": "none", "stroke": "structural", "width": 9},
        {"type": "rect", "x": 44, "y": 18, "w": 12, "h": 52, "r": 4, "fill": "primary"},
        {"type": "circle", "cx": 50, "cy": 30, "r": 5, "fill": "accent"},
        {"type": "line", "x1": 26, "y1": 74, "x2": 74, "y2": 74,
         "stroke": "structural", "width": 3},
    ],
    "min_size_px": 24,
    "notes": "",
}


# ----------------------------------------------------------------------------
# Color utilities
# ----------------------------------------------------------------------------
def hex_to_rgb(value):
    v = value.strip().lstrip("#")
    if len(v) == 3:
        v = "".join(ch * 2 for ch in v)
    if len(v) != 6:
        raise ValueError("invalid hex color: %s" % value)
    return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))


def rgb_to_hex(rgb):
    return "#%02X%02X%02X" % tuple(max(0, min(255, int(round(c)))) for c in rgb)


def rgb_to_cmyk(rgb):
    r, g, b = [c / 255.0 for c in rgb]
    k = 1.0 - max(r, g, b)
    if k >= 1.0:
        return 0, 0, 0, 100
    c = (1.0 - r - k) / (1.0 - k)
    m = (1.0 - g - k) / (1.0 - k)
    y = (1.0 - b - k) / (1.0 - k)
    return (int(round(c * 100)), int(round(m * 100)),
            int(round(y * 100)), int(round(k * 100)))


def rgb_to_hsl(rgb):
    r, g, b = [c / 255.0 for c in rgb]
    mx, mn = max(r, g, b), min(r, g, b)
    l = (mx + mn) / 2.0
    if mx == mn:
        return 0, 0, l * 100
    d = mx - mn
    s = d / (2.0 - mx - mn) if l > 0.5 else d / (mx + mn)
    if mx == r:
        h = ((g - b) / d) % 6
    elif mx == g:
        h = (b - r) / d + 2
    else:
        h = (r - g) / d + 4
    return h * 60, s * 100, l * 100


def rel_luminance(rgb):
    def lin(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = [lin(c) for c in rgb]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(hex_a, hex_b):
    la, lb = rel_luminance(hex_to_rgb(hex_a)), rel_luminance(hex_to_rgb(hex_b))
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def resolve_color(name, palette, mode="color"):
    """Resolve a color role name or literal hex into a concrete hex for a mode."""
    if name is None or name == "none":
        return None
    base = palette.get(name, name)
    try:
        hex_to_rgb(base)
    except ValueError:
        base = palette.get("structural", "#1B2A41")
    if mode == "mono":
        if name == "panel":
            return base
        return palette.get("structural", "#1B2A41")
    if mode == "reverse":
        if name == "panel":
            return base
        return "#FFFFFF"
    return base


# ----------------------------------------------------------------------------
# Motif helpers
# ----------------------------------------------------------------------------
def iter_colors(motif):
    for item in motif:
        for key in ("fill", "stroke"):
            v = item.get(key)
            if v and v != "none":
                yield v, item


def compute_min_element_width(motif):
    """Smallest horizontal extent among all primitives -> clear-space unit."""
    widths = []
    for it in motif:
        t = it.get("type")
        if t == "rect":
            widths.append(it.get("w", 0))
        elif t == "circle":
            widths.append(2 * it.get("r", 0))
        elif t == "ellipse":
            widths.append(it.get("width", 4) if it.get("fill") in (None, "none")
                          else 2 * it.get("rx", 0))
        elif t == "line":
            widths.append(it.get("width", 2))
        elif t == "arc":
            widths.append(it.get("width", 2))
        elif t == "polygon":
            xs = [p[0] for p in it.get("points", [])]
            widths.append((max(xs) - min(xs)) if xs else 0)
    widths = [w for w in widths if w > 0]
    return round(min(widths), 2) if widths else 8.0


def motif_bbox(motif):
    xs, ys = [], []

    def add(x, y):
        xs.append(x)
        ys.append(y)

    for it in motif:
        t = it.get("type")
        if t == "rect":
            add(it["x"], it["y"])
            add(it["x"] + it["w"], it["y"] + it["h"])
        elif t == "circle":
            add(it["cx"] - it["r"], it["cy"] - it["r"])
            add(it["cx"] + it["r"], it["cy"] + it["r"])
        elif t == "ellipse":
            add(it["cx"] - it["rx"], it["cy"] - it["ry"])
            add(it["cx"] + it["rx"], it["cy"] + it["ry"])
        elif t == "line":
            add(it["x1"], it["y1"])
            add(it["x2"], it["y2"])
        elif t == "arc":
            add(it["cx"] - it["rx"], it["cy"] - it["ry"])
            add(it["cx"] + it["rx"], it["cy"] + it["ry"])
        elif t == "polygon":
            for p in it.get("points", []):
                add(p[0], p[1])
    if not xs:
        return 0, 0, 100, 100
    return min(xs), min(ys), max(xs), max(ys)


# ----------------------------------------------------------------------------
# SVG rendering
# ----------------------------------------------------------------------------
def _arc_path(it):
    cx, cy = it["cx"], it["cy"]
    rx, ry = it["rx"], it.get("ry", it["rx"])
    start = math.radians(it.get("start", 0))
    end = math.radians(it.get("end", 180))
    x1, y1 = cx + rx * math.cos(start), cy + ry * math.sin(start)
    x2, y2 = cx + rx * math.cos(end), cy + ry * math.sin(end)
    sweep = (end - start) % (2 * math.pi)
    large = 1 if sweep > math.pi else 0
    return ("M %.3f %.3f A %.3f %.3f 0 %d 1 %.3f %.3f"
            % (x1, y1, rx, ry, large, x2, y2))


def primitive_to_svg(it, palette, mode):
    fill = resolve_color(it.get("fill"), palette, mode) if it.get("fill") else None
    stroke = resolve_color(it.get("stroke"), palette, mode) if it.get("stroke") else None
    width = it.get("width", 2)
    rot = it.get("rotate")
    transform = ""
    if rot:
        pivot = it.get("pivot", [it.get("cx", 50), it.get("cy", 50)])
        transform = ' transform="rotate(%s %.3f %.3f)"' % (rot, pivot[0], pivot[1])
    common = ' fill="%s" stroke="%s" stroke-width="%s"%s' % (
        fill or "none", stroke or "none", width, transform)
    t = it.get("type")
    if t == "rect":
        return '<rect x="%.3f" y="%.3f" width="%.3f" height="%.3f" rx="%s"%s/>' % (
            it["x"], it["y"], it["w"], it["h"], it.get("r", 0), common)
    if t == "circle":
        return '<circle cx="%.3f" cy="%.3f" r="%.3f"%s/>' % (
            it["cx"], it["cy"], it["r"], common)
    if t == "ellipse":
        return '<ellipse cx="%.3f" cy="%.3f" rx="%.3f" ry="%.3f"%s/>' % (
            it["cx"], it["cy"], it["rx"], it.get("ry", it["rx"]), common)
    if t == "line":
        return ('<line x1="%.3f" y1="%.3f" x2="%.3f" y2="%.3f" '
                'fill="none" stroke="%s" stroke-width="%s" stroke-linecap="%s"%s/>') % (
            it["x1"], it["y1"], it["x2"], it["y2"], stroke or "#000",
            width, it.get("cap", "butt"),
            transform if transform else "")
    if t == "arc":
        return ('<path d="%s" fill="none" stroke="%s" stroke-width="%s" '
                'stroke-linecap="%s"%s/>') % (
            _arc_path(it), stroke or "#000", width, it.get("cap", "butt"),
            transform if transform else "")
    if t == "polygon":
        pts = " ".join("%.3f,%.3f" % (p[0], p[1]) for p in it["points"])
        return '<polygon points="%s"%s/>' % (pts, common)
    if t == "path":
        return '<path d="%s"%s/>' % (it["d"], common)
    return "<!-- unknown primitive: %s -->" % t


def build_svg(motif, palette, canvas=DEFAULT_CANVAS, mode="color",
              background=None, padding=0.0):
    vb = [-padding, -padding, canvas + 2 * padding, canvas + 2 * padding]
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="%.2f %.2f %.2f %.2f" '
        'width="%.0f" height="%.0f" role="img">' % (
            vb[0], vb[1], vb[2], vb[3], vb[2], vb[3])
    ]
    if background and background != "none":
        parts.append('<rect x="%.2f" y="%.2f" width="%.2f" height="%.2f" fill="%s"/>'
                     % (vb[0], vb[1], vb[2], vb[3], background))
    for it in motif:
        parts.append(primitive_to_svg(it, palette, mode))
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def build_safe_zone_svg(motif, palette, canvas, clear):
    pad = clear
    vb = [-pad, -pad, canvas + 2 * pad, canvas + 2 * pad]
    size = canvas + 2 * pad
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="%.2f %.2f %.2f %.2f" '
        'width="%.0f" height="%.0f" role="img">' % (vb[0], vb[1], vb[2], vb[3], size, size),
        '<rect x="%.2f" y="%.2f" width="%.2f" height="%.2f" fill="%s"/>'
        % (vb[0], vb[1], vb[2], vb[3], palette.get("panel", PANEL_IVORY)),
        '<rect x="%.2f" y="%.2f" width="%.2f" height="%.2f" fill="none" '
        'stroke="%s" stroke-width="0.6" stroke-dasharray="3 2.5"/>'
        % (-pad, -pad, size, size, palette.get("structural", "#1B2A41")),
    ]
    for it in motif:
        parts.append(primitive_to_svg(it, palette, "color"))
    parts.append(
        '<text x="%.2f" y="%.2f" font-family="Helvetica,Arial,sans-serif" '
        'font-size="4" fill="%s" text-anchor="middle">clear space = X</text>'
        % (canvas / 2.0, -pad * 0.35, palette.get("structural", "#1B2A41")))
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


# ----------------------------------------------------------------------------
# Pillow rendering
# ----------------------------------------------------------------------------
def _find_font(size, bold=False, cjk=False):
    if not HAS_PIL:
        return None
    names = []
    if cjk:
        names += ["msyhbd.ttc" if bold else "msyh.ttc",
                  "msyh.ttc", "simhei.ttf", "simsun.ttc",
                  "/System/Library/Fonts/PingFang.ttc"]
    names += ["seguisb.ttf" if bold else "segoeui.ttf", "segoeui.ttf",
              "arialbd.ttf" if bold else "arial.ttf", "arial.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    for name in names:
        p = name if os.path.isabs(name) else os.path.join("C:/Windows/Fonts", name)
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _scale_val(v, s, minimum=1):
    return max(minimum, int(round(v * s)))


def draw_motif_pil(draw, motif, palette, scale, mode="color", offset=(0, 0)):
    ox, oy = offset
    for it in motif:
        t = it.get("type")
        fill = resolve_color(it.get("fill"), palette, mode) if it.get("fill") else None
        stroke = resolve_color(it.get("stroke"), palette, mode) if it.get("stroke") else None
        w = _scale_val(it.get("width", 2), scale)
        if t == "rect":
            box = [ox + it["x"] * scale, oy + it["y"] * scale,
                   ox + (it["x"] + it["w"]) * scale, oy + (it["y"] + it["h"]) * scale]
            r = it.get("r", 0) * scale
            if r > 0.5:
                draw.rounded_rectangle(box, radius=r, fill=fill, outline=stroke, width=w if stroke else 0)
            else:
                draw.rectangle(box, fill=fill, outline=stroke, width=w if stroke else 0)
        elif t == "circle":
            r = it["r"] * scale
            box = [ox + (it["cx"] - it["r"]) * scale, oy + (it["cy"] - it["r"]) * scale,
                   ox + (it["cx"] + it["r"]) * scale, oy + (it["cy"] + it["r"]) * scale]
            draw.ellipse(box, fill=fill,
                         outline=stroke, width=w if stroke else 0)
        elif t == "ellipse":
            box = [ox + (it["cx"] - it["rx"]) * scale, oy + (it["cy"] - it["ry"]) * scale,
                   ox + (it["cx"] + it["rx"]) * scale, oy + (it["cy"] + it["ry"]) * scale]
            draw.ellipse(box, fill=fill, outline=stroke, width=w if stroke else 0)
        elif t == "line":
            draw.line([ox + it["x1"] * scale, oy + it["y1"] * scale,
                       ox + it["x2"] * scale, oy + it["y2"] * scale],
                      fill=stroke or "#000", width=w)
        elif t == "arc":
            box = [ox + (it["cx"] - it["rx"]) * scale, oy + (it["cy"] - it["ry"]) * scale,
                   ox + (it["cx"] + it["rx"]) * scale, oy + (it["cy"] + it["ry"]) * scale]
            draw.arc(box, it.get("start", 0), it.get("end", 180),
                     fill=stroke or "#000", width=w)
        elif t == "polygon":
            pts = [(ox + p[0] * scale, oy + p[1] * scale) for p in it["points"]]
            draw.polygon(pts, fill=fill, outline=stroke)


def render_motif_image(motif, palette, size, canvas=DEFAULT_CANVAS,
                       mode="color", background=None, supersample=4):
    """Render the motif to a PIL Image. `background=None` -> transparent."""
    big = size * supersample
    mode_str = "RGBA"
    img = Image.new(mode_str, (big, big), background or (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    scale = big / canvas
    draw_motif_pil(draw, motif, palette, scale, mode)
    if supersample > 1:
        img = img.resize((size, size), Image.LANCZOS)
    return img


# ----------------------------------------------------------------------------
# PNG boards
# ----------------------------------------------------------------------------
def build_scale_test(motif, palette, canvas, out_path, sizes=(16, 24, 32, 48, 64), zoom=4):
    margin, label_h, row_gap = 48, 34, 46
    widths = [s * zoom for s in sizes]
    row1 = sum(sizes) + margin * (len(sizes) + 1)
    row2 = sum(widths) + margin * (len(widths) + 1)
    W = max(row1, row2, 420)
    H = margin + 26 + max(sizes) + label_h + row_gap + 26 + max(widths) + label_h + margin
    img = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)
    f_title = _find_font(19, bold=True)
    f_lbl = _find_font(15)
    f_small = _find_font(13)

    y = margin
    d.text((margin, y), "Actual size (px)", fill="#111111", font=f_title)
    y += 30
    x = margin
    for s in sizes:
        tile = render_motif_image(motif, palette, s, canvas, mode="mono")
        img.paste(tile, (x, y), tile)
        d.text((x, y + s + 7), "%dpx" % s, fill="#555555", font=f_lbl)
        x += s + margin

    y += max(sizes) + label_h + row_gap
    d.text((margin, y), "Zoomed %dx (same rendering, enlarged for inspection)" % zoom,
           fill="#111111", font=f_title)
    y += 30
    x = margin
    for s, w in zip(sizes, widths):
        tile = render_motif_image(motif, palette, w, canvas, mode="mono")
        img.paste(tile, (x, y), tile)
        d.text((x, y + w + 7), "%dpx" % s, fill="#555555", font=f_lbl)
        x += w + margin

    d.text((margin, H - margin - 4),
           "Monochrome legibility test - if the 16px or 24px tile is unreadable, "
           "the motif must be simplified.", fill="#888888", font=f_small)
    img.save(out_path)
    return out_path


def build_application_preview(motif, palette, canvas, out_path, brand_en):
    cell_w, cell_h, cols, rows = 520, 400, 3, 2
    pad = 34
    W = cols * cell_w + pad * (cols + 1)
    H = rows * cell_h + pad * (rows + 1)
    img = Image.new("RGB", (W, H), (238, 238, 238))
    d = ImageDraw.Draw(img)
    f_cap = _find_font(17, bold=True)
    f_txt = _find_font(15)
    f_cjk = _find_font(15, cjk=True)

    def cell(idx, title, drawer):
        cx = pad + (idx % cols) * (cell_w + pad)
        cy = pad + (idx // cols) * (cell_h + pad)
        d.rectangle([cx, cy, cx + cell_w, cy + cell_h], fill=(255, 255, 255))
        drawer(cx, cy)
        d.rectangle([cx, cy, cx + cell_w, cy + cell_h], outline=(215, 215, 215), width=1)
        d.text((cx + 14, cy + cell_h - 30), title, fill="#333333", font=f_cap)
        return cx, cy

    ivory = palette.get("panel", PANEL_IVORY)
    deep = palette.get("structural", "#1B2A41")

    def scene_light(cx, cy):
        box = (cx + 40, cy + 30, cx + cell_w - 40, cy + cell_h - 70)
        d.rectangle(box, fill=hex_to_rgb(ivory))
        m = render_motif_image(motif, palette, 180, canvas, mode="color")
        img.paste(m, (cx + (cell_w - 180) // 2, cy + 55), m)

    def scene_dark(cx, cy):
        box = (cx + 40, cy + 30, cx + cell_w - 40, cy + cell_h - 70)
        d.rectangle(box, fill=hex_to_rgb(deep))
        m = render_motif_image(motif, palette, 180, canvas, mode="reverse")
        img.paste(m, (cx + (cell_w - 180) // 2, cy + 55), m)

    def scene_favicon(cx, cy):
        box = (cx + 40, cy + 30, cx + cell_w - 40, cy + cell_h - 70)
        d.rectangle(box, fill=hex_to_rgb(ivory))
        for i, s in enumerate((16, 32, 64)):
            m = render_motif_image(motif, palette, s, canvas, mode="mono")
            img.paste(m, (cx + 90 + i * 120, cy + 90 - s // 2), m)
            d.text((cx + 90 + i * 120 - 4, cy + 150), "%dpx" % s,
                   fill="#666666", font=f_txt)

    def scene_card(cx, cy):
        cw, ch = 380, 220
        bx, by = cx + (cell_w - cw) // 2, cy + 60
        d.rectangle([bx, by, bx + cw, by + ch], fill=(255, 255, 255),
                    outline=(220, 220, 220), width=1)
        m = render_motif_image(motif, palette, 68, canvas, mode="color")
        img.paste(m, (bx + 30, by + 34), m)

    def scene_foil(cx, cy):
        box = (cx + 40, cy + 30, cx + cell_w - 40, cy + cell_h - 70)
        d.rectangle(box, fill=hex_to_rgb(ivory))
        m = render_motif_image(motif, palette, 150, canvas, mode="mono")
        img.paste(m, (cx + 70, cy + 90), m)
        d.text((cx + 250, cy + 130), "Single color", fill="#555555", font=f_txt)
        d.text((cx + 250, cy + 156), "foil / engraving", fill="#555555", font=f_txt)

    def scene_outdoor(cx, cy):
        box = (cx + 40, cy + 30, cx + cell_w - 40, cy + cell_h - 70)
        d.rectangle(box, fill=hex_to_rgb(deep))
        m = render_motif_image(motif, palette, 130, canvas, mode="reverse")
        img.paste(m, (cx + 70, cy + 100), m)
        d.text((cx + 230, cy + 152), "Large format", fill=(190, 200, 215), font=f_txt)

    scenes = [
        ("1. Light background", scene_light),
        ("2. Dark background (reversed)", scene_dark),
        ("3. Favicon / app icon", scene_favicon),
        ("4. Business card", scene_card),
        ("5. Single color / foil", scene_foil),
        ("6. Large format / outdoor", scene_outdoor),
    ]
    for i, (title, fn) in enumerate(scenes):
        cell(i, title, fn)
    del f_cjk
    img.save(out_path)
    return out_path


# ----------------------------------------------------------------------------
# Reports
# ----------------------------------------------------------------------------
def color_table_rows(palette):
    rows = []
    roles = [
        ("primary", "Primary", "主色", "主标记族的填充，占面积最大"),
        ("structural", "Structural", "深色结构色", "轮廓、骨架、单色版与文字"),
        ("accent", "Accent", "点缀色", "焦点强调，面积不超过 8%"),
        ("panel", "Panel", "象牙底", "编辑作品抽象面板背景"),
    ]
    for key, en, zh, usage in roles:
        hx = palette.get(key)
        if not hx:
            continue
        rgb = hex_to_rgb(hx)
        c, m, y, k = rgb_to_cmyk(rgb)
        hu, sa, li = rgb_to_hsl(rgb)
        rows.append({
            "key": key, "en": en, "zh": zh, "hex": hx.upper(),
            "rgb": "RGB(%d, %d, %d)" % rgb,
            "cmyk": "C%d M%d Y%d K%d" % (c, m, y, k),
            "hsl": "H%.0f S%.0f%% L%.0f%%" % (hu, sa, li),
            "contrast_panel": "%.2f:1" % contrast_ratio(hx, palette.get("panel", PANEL_IVORY)),
            "contrast_white": "%.2f:1" % contrast_ratio(hx, "#FFFFFF"),
            "usage": usage,
        })
    return rows


def mono_diagnosis(motif, palette):
    """F4 check: does the motif survive monochrome? Report luminance spread."""
    lums = []
    for name, item in iter_colors(motif):
        hx = resolve_color(name, palette, "mono")
        lums.append((name, item.get("type"), hx, rel_luminance(hex_to_rgb(hx))))
    if not lums:
        return []
    spread = max(l[3] for l in lums) - min(l[3] for l in lums)
    filled = [l for l in lums if l[0] != "panel"]
    distinct = len({l[2] for l in filled})
    return {
        "entries": lums,
        "luminance_spread": round(spread, 4),
        "distinct_mono_colors": distinct,
        "verdict": ("PASS - shape carries the hierarchy"
                    if distinct <= 1 else
                    "REVIEW - monochrome still has %d tones; verify hierarchy "
                    "comes from shape, not tone" % distinct),
    }


def build_spec_md(cfg, palette, canvas, clear, outdir, files, diag):
    b = cfg.get("brand", {})
    rows = color_table_rows(palette)
    L = []
    A = L.append
    A("# %s — Brand Mark Specification" % b.get("name_en", "Brand"))
    A("")
    if b.get("name_zh"):
        A("**%s** / %s%s" % (b.get("name_zh"), b.get("name_en", ""),
                             (" — " + b["tagline"]) if b.get("tagline") else ""))
        A("")
    A("Generated by `mark-to-spec`. All values are derived from the approved "
      "abstract motif; do not hand-edit without updating the source config.")
    A("")
    A("## 1. Deliverables")
    A("")
    A("| File | Purpose |")
    A("|------|---------|")
    for f, purpose in files:
        A("| `%s` | %s |" % (f, purpose))
    A("")
    A("## 2. Color specification")
    A("")
    A("| Role | 角色 | HEX | RGB | CMYK | HSL | vs Panel | vs White | Usage |")
    A("|------|------|-----|-----|------|-----|----------|----------|-------|")
    for r in rows:
        A("| %s | %s | `%s` | %s | %s | %s | %s | %s | %s |" % (
            r["en"], r["zh"], r["hex"], r["rgb"], r["cmyk"], r["hsl"],
            r["contrast_panel"], r["contrast_white"], r["usage"]))
    A("")
    A("**Color discipline**: 1 primary + 1 dark structural + at most 1 accent, "
      "all extracted from the reference visual. No new hue may be introduced.")
    A("")
    A("## 3. Clear space and minimum size")
    A("")
    A("- **Clear space (X)** = %.2f units on a %.0f-unit canvas (= %.1f%% of the "
      "mark width). Keep this area free of any other element." % (
          clear, canvas, clear / canvas * 100))
    A("- **Minimum size**: %d px (screen) / 12 mm (print). Below this, use the "
      "simplified variant." % cfg.get("min_size_px", 24))
    A("- **Safe zone diagram**: `safe-zone.svg`")
    A("")
    A("## 4. Variants")
    A("")
    A("| Variant | File | When to use |")
    A("|---------|------|-------------|")
    A("| Full color | `mark.svg` | Default, on light backgrounds |")
    A("| Single color | `mark-mono.svg` | Foil stamping, engraving, "
      "screen printing, fax, one-color print |")
    A("| Reversed | `mark-reverse.svg` | Dark or colored backgrounds |")
    A("")
    A("## 5. Monochrome legibility diagnosis")
    A("")
    if diag:
        A("- Distinct tones after monochrome conversion: **%d**" % diag["distinct_mono_colors"])
        A("- Luminance spread: %.4f" % diag["luminance_spread"])
        A("- Verdict: **%s**" % diag["verdict"])
    A("")
    A("## 6. Misuse (prohibited)")
    A("")
    for item in [
        "Do not recolor the mark with colors outside the specified palette.",
        "Do not stretch, condense, rotate, or apply perspective distortion.",
        "Do not add gradients, drop shadows, glows, or outlines.",
        "Do not place the mark on a busy background without the reversed variant "
        "or a solid panel behind it.",
        "Do not reduce below the minimum size or inside the clear space.",
        "Do not reassemble the primitives into a new composition.",
    ]:
        A("- %s" % item)
    A("")
    A("## 7. Motif source (primitive list)")
    A("")
    A("```json")
    A(json.dumps(cfg.get("motif", []), indent=2, ensure_ascii=False))
    A("```")
    A("")
    if cfg.get("notes"):
        A("## 8. Notes")
        A("")
        A(cfg["notes"])
        A("")
    return "\n".join(L)


def build_spec_html(cfg, palette, canvas, clear, files, diag, svg_map):
    b = cfg.get("brand", {})
    rows = color_table_rows(palette)
    name_en = b.get("name_en", "Brand")
    name_zh = b.get("name_zh", "")
    ivory = palette.get("panel", PANEL_IVORY)
    deep = palette.get("structural", "#1B2A41")

    swatches = "".join(
        '<div class="sw"><div class="chip" style="background:%s"></div>'
        '<div class="meta"><b>%s</b><span>%s</span><code>%s</code>'
        '<code>%s</code><code>%s</code></div></div>' % (
            r["hex"], "%s / %s" % (r["en"], r["zh"]), r["usage"],
            r["hex"], r["rgb"], r["cmyk"])
        for r in rows)

    variants = "".join(
        '<div class="var"><div class="stage %s">%s</div><span>%s</span></div>' % (
            cls, svg, label)
        for cls, svg, label in [
            ("stage-light", svg_map["color"], "Full color"),
            ("stage-panel", svg_map["mono"], "Single color"),
            ("stage-dark", svg_map["reverse"], "Reversed"),
            ("stage-light", svg_map["safe_zone"], "Clear space"),
        ])

    file_rows = "".join("<tr><td><code>%s</code></td><td>%s</td></tr>" % (f, p)
                        for f, p in files)

    diag_html = ""
    if diag:
        diag_html = (
            '<div class="diag"><b>Monochrome check:</b> %d distinct tone(s) after '
            'conversion &middot; luminance spread %.4f &middot; <i>%s</i></div>' % (
                diag["distinct_mono_colors"], diag["luminance_spread"], diag["verdict"]))

    return """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%(name_en)s — Brand Mark Specification</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#faf9f6;color:#2b2b2b;
 font-family:-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,
 "PingFang SC","Microsoft YaHei",sans-serif;line-height:1.6}
header{background:%(deep)s;color:#fff;padding:38px 44px}
header h1{margin:0;font-size:27px;font-weight:600;letter-spacing:.3px}
header p{margin:6px 0 0;color:#c7d2e0;font-size:14px}
main{max-width:1080px;margin:0 auto;padding:36px 24px 64px}
h2{font-size:17px;margin:38px 0 14px;padding-bottom:8px;
 border-bottom:1px solid #e3e0d8;letter-spacing:.4px}
.grid{display:flex;flex-wrap:wrap;gap:16px}
.sw{display:flex;gap:12px;background:#fff;border:1px solid #e8e5dd;
 border-radius:8px;padding:14px;min-width:250px;flex:1}
.chip{width:64px;height:64px;border-radius:6px;flex:0 0 64px;
 border:1px solid rgba(0,0,0,.08)}
.meta{display:flex;flex-direction:column;gap:2px;font-size:12.5px}
.meta b{font-size:14px}
.meta span{color:#7a7468}
.meta code{font-size:11.5px;color:#4a4a4a;font-family:ui-monospace,Menlo,Consolas,monospace}
.vars{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:16px}
.var{text-align:center}
.stage{border-radius:8px;padding:22px;display:flex;align-items:center;
 justify-content:center;margin-bottom:8px}
.stage svg{width:120px;height:120px}
.stage-light{background:#fff;border:1px solid #e8e5dd}
.stage-panel{background:%(ivory)s;border:1px solid #e8e5dd}
.stage-dark{background:%(deep)s}
.var span{font-size:12.5px;color:#6f6a60}
table{width:100%%;border-collapse:collapse;font-size:13.5px;background:#fff}
th,td{text-align:left;padding:10px 12px;border-bottom:1px solid #ece9e1}
th{background:#f4f2ec;font-weight:600}
code{font-family:ui-monospace,Menlo,Consolas,monospace}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:14px}
.kv div{background:#fff;border:1px solid #e8e5dd;border-radius:8px;padding:14px 16px}
.kv b{display:block;font-size:12px;color:#8a8375;font-weight:500;
 text-transform:uppercase;letter-spacing:.6px}
.kv span{font-size:19px;color:%(deep)s}
.diag{background:#fff8e8;border:1px solid #eddfbc;border-radius:8px;
 padding:12px 16px;font-size:13.5px;margin-top:12px}
ul{margin:0;padding-left:20px}li{margin:5px 0;font-size:14px}
footer{margin-top:48px;padding-top:18px;border-top:1px solid #e3e0d8;
 font-size:12px;color:#999}
</style></head><body>
<header><h1>%(name_en)s%(zh_part)s</h1>
<p>Brand Mark Specification &middot; generated by mark-to-spec</p></header>
<main>
<h2>Mark variants</h2><div class="vars">%(variants)s</div>
%(diag_html)s
<h2>Color specification</h2><div class="grid">%(swatches)s</div>
<h2>Metrics</h2><div class="kv">
<div><b>Clear space (X)</b><span>%(clear).2f / %(canvas).0f</span></div>
<div><b>Minimum size</b><span>%(minsize)d px</span></div>
<div><b>Motif footprint</b><span>%(footprint)s</span></div>
<div><b>Primitives</b><span>%(pcount)d</span></div></div>
<h2>Deliverables</h2><table>
<tr><th>File</th><th>Purpose</th></tr>%(file_rows)s</table>
<h2>Misuse — prohibited</h2><ul>
<li>Do not recolor outside the specified palette.</li>
<li>Do not stretch, condense, rotate, or apply perspective.</li>
<li>Do not add gradients, shadows, glows, or outlines.</li>
<li>Do not place on busy backgrounds without the reversed variant.</li>
<li>Do not reduce below the minimum size or place content inside the clear space.</li>
<li>Do not reassemble the primitives into a new composition.</li></ul>
<footer>Values derived from the approved abstract motif. Regenerate from the
source config rather than editing by hand.</footer>
</main></body></html>
""" % {
        "name_en": name_en,
        "zh_part": (" &middot; " + name_zh) if name_zh else "",
        "deep": deep, "ivory": ivory,
        "variants": variants, "diag_html": diag_html, "swatches": swatches,
        "clear": clear, "canvas": canvas,
        "minsize": cfg.get("min_size_px", 24),
        "footprint": "%s" % _footprint_text(cfg.get("motif", []), canvas),
        "pcount": len(cfg.get("motif", [])),
        "file_rows": file_rows,
    }


def _footprint_text(motif, canvas):
    x0, y0, x1, y1 = motif_bbox(motif)
    return "%.0f%% x %.0f%%" % ((x1 - x0) / canvas * 100, (y1 - y0) / canvas * 100)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("canvas", DEFAULT_CANVAS)
    cfg.setdefault("brand", {})
    cfg.setdefault("min_size_px", 24)
    palette = dict(TEMPLATE_CONFIG["palette"])
    palette.update(cfg.get("palette", {}))
    cfg["palette"] = palette
    if "motif" not in cfg or not cfg["motif"]:
        raise ValueError("config must contain a non-empty 'motif' array")
    return cfg


def generate(cfg, outdir, want_png=True, check_mono=False):
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    motif = cfg["motif"]
    palette = cfg["palette"]
    canvas = float(cfg.get("canvas", DEFAULT_CANVAS))
    clear = compute_min_element_width(motif)
    files = []

    svg_color = build_svg(motif, palette, canvas, "color")
    svg_mono = build_svg(motif, palette, canvas, "mono")
    svg_rev = build_svg(motif, palette, canvas, "reverse")
    svg_safe = build_safe_zone_svg(motif, palette, canvas, clear)

    for name, content in [
        ("mark.svg", svg_color),
        ("mark-mono.svg", svg_mono),
        ("mark-reverse.svg", svg_rev),
        ("safe-zone.svg", svg_safe),
    ]:
        (outdir / name).write_text(content, encoding="utf-8")
    files += [
        ("mark.svg", "Full-color master vector"),
        ("mark-mono.svg", "Single-color version for foil, engraving, one-color print"),
        ("mark-reverse.svg", "Reversed white version for dark backgrounds"),
        ("safe-zone.svg", "Clear-space diagram"),
    ]
    print("  [ok] mark.svg, mark-mono.svg, mark-reverse.svg, safe-zone.svg")

    if want_png and HAS_PIL:
        p1 = build_scale_test(motif, palette, canvas, str(outdir / "scale-test.png"))
        p2 = build_application_preview(motif, palette, canvas,
                                       str(outdir / "application-preview.png"),
                                       cfg.get("brand", {}).get("name_en", "Brand"))
        files += [("scale-test.png", "16/24/32/48/64 px monochrome legibility test"),
                  ("application-preview.png", "Six-scenario application board")]
        print("  [ok] scale-test.png, application-preview.png")
    elif want_png:
        print("  [skip] PNG outputs - Pillow not installed "
              "(pip install Pillow to enable)")

    diag = mono_diagnosis(motif, palette) if (check_mono or True) else None
    (outdir / "spec.md").write_text(
        build_spec_md(cfg, palette, canvas, clear, outdir, files, diag),
        encoding="utf-8")
    files.append(("spec.md", "Written specification"))
    (outdir / "spec.html").write_text(
        build_spec_html(cfg, palette, canvas, clear, files, diag, {
            "color": svg_color, "mono": svg_mono,
            "reverse": svg_rev, "safe_zone": svg_safe,
        }), encoding="utf-8")
    files.append(("spec.html", "One-page browser-viewable brand sheet"))
    print("  [ok] spec.md, spec.html")

    if check_mono and diag:
        print("\nMonochrome diagnosis")
        print("  distinct tones : %d" % diag["distinct_mono_colors"])
        print("  luminance spread: %.4f" % diag["luminance_spread"])
        print("  verdict        : %s" % diag["verdict"])

    # Guardrail reports
    n = len(motif)
    if n > 6:
        print("\n[warn] %d primitives - above the recommended maximum of 6; "
              "the mark will likely fail the 24px test." % n)
    fp = motif_bbox(motif)
    fw = (fp[2] - fp[0]) / canvas * 100
    fh = (fp[3] - fp[1]) / canvas * 100
    print("\nMetrics")
    print("  footprint      : %.0f%% W x %.0f%% H" % (fw, fh))
    print("  clear space X  : %.2f units (%.1f%% of canvas)" % (clear, clear / canvas * 100))
    print("  primitives     : %d" % n)
    return outdir


def main():
    ap = argparse.ArgumentParser(
        description="Turn an abstract logo motif into a production-ready spec kit.")
    ap.add_argument("--config", help="path to motif config JSON")
    ap.add_argument("--outdir", default="./dist", help="output directory")
    ap.add_argument("--no-png", action="store_true", help="skip PNG outputs")
    ap.add_argument("--check-mono", action="store_true",
                    help="print monochrome legibility diagnosis")
    ap.add_argument("--init-template", metavar="PATH",
                    help="write a starter config template and exit")
    args = ap.parse_args()

    if args.init_template:
        Path(args.init_template).write_text(
            json.dumps(TEMPLATE_CONFIG, indent=2, ensure_ascii=False),
            encoding="utf-8")
        print("Template written to %s" % args.init_template)
        return 0

    if not args.config:
        ap.error("--config is required (or use --init-template)")

    try:
        cfg = load_config(args.config)
    except Exception as e:
        print("ERROR: cannot load config: %s" % e, file=sys.stderr)
        return 1

    print("Generating spec kit -> %s" % args.outdir)
    try:
        out = generate(cfg, args.outdir, want_png=not args.no_png,
                       check_mono=args.check_mono)
    except Exception as e:
        print("ERROR: generation failed: %s" % e, file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1
    print("\nDone: %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
