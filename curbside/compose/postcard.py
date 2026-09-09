#!/usr/bin/env python3
"""Print-ready 9x6in postcard @ 300 DPI, built for glance-readability.

Design intent: the recipient must see the difference in under a second.
- panels are large and equal, with a hard divider between them
- the AFTER panel carries a highlight ring around the changed area
- a centre chevron pushes the eye left -> right
"""
import pathlib
from PIL import Image, ImageDraw, ImageFont, ImageFilter

DPI    = 300
W, H   = 9 * DPI, 6 * DPI
MARGIN = int(0.30 * DPI)

INK    = (23, 28, 38)
MUTED  = (108, 117, 131)
ACCENT = (203, 63, 20)
GOLD   = (214, 158, 46)
PAPER  = (253, 252, 250)

def _font(size, bold=False):
    for p in (f"/usr/share/fonts/truetype/dejavu/DejaVuSans{'-Bold' if bold else ''}.ttf",
              f"/usr/share/fonts/TTF/DejaVuSans{'-Bold' if bold else ''}.ttf"):
        if pathlib.Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()

def _wrap(d, text, font, width):
    lines, line = [], ""
    for w in text.split():
        t = (line + " " + w).strip()
        if d.textlength(t, font=font) > width and line:
            lines.append(line); line = w
        else:
            line = t
    if line:
        lines.append(line)
    return lines

def _crop_square(img, side, zoom=1.0, focus=None):
    """Crop square with optional zoom, centred on `focus` (fx, fy in 0..1).

    Framing on the driveway rather than the image centre keeps the changed
    region in view - otherwise a large roof can dominate the panel.
    """
    w, h = img.size
    s = int(min(w, h) / zoom)
    if focus:
        cx, cy = int(focus[0] * w), int(focus[1] * h)
    else:
        cx, cy = w // 2, h // 2
    x = max(0, min(cx - s // 2, w - s))
    y = max(0, min(cy - s // 2, h - s))
    return img.crop((x, y, x + s, y + s)).resize((side, side), Image.LANCZOS)


def focus_from_mask(mask_preview_path, before_path):
    """Centroid of the changed region, as (fx, fy) fractions - for framing."""
    import numpy as np
    a = np.asarray(Image.open(before_path).convert("RGB")).astype(np.int16)
    b = np.asarray(Image.open(mask_preview_path).convert("RGB")).astype(np.int16)
    if a.shape != b.shape:
        return None
    red = (b[..., 0] - a[..., 0]) > 30
    if red.sum() < 100:
        return None
    ys, xs = np.nonzero(red)
    h, w = red.shape
    return (float(xs.mean()) / w, float(ys.mean()) / h)

def build(before_path, after_path, address, out_path, *,
          disclosure=None, return_address=None, opt_out=None,
          headline="That driveway is costing you curb appeal.",
          subhead="Here is your home with a new paver driveway.",
          offer="Free on-site estimate",
          company="Heartland Driveway Co.",
          phone="(317) 555-0142",
          zoom=1.25, focus=None,
          attribution="Imagery: Indiana Geographic Information Office (IGIO), CC0-1.0"):
    from curbside.compliance.policy import assert_disclosure, REQUIRED_DISCLOSURE

    # Compliance is enforced here, at the point of composition - a piece that
    # cannot carry its disclosure is never produced in the first place.
    disclosure = disclosure or REQUIRED_DISCLOSURE
    assert_disclosure(disclosure)
    if not return_address:
        raise ValueError("return_address is required on a mailable piece")
    opt_out = opt_out or "To stop receiving these, write to the address above."

    card = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(card)

    # ---------- headline band ----------
    f_head = _font(int(0.30 * DPI), True)
    d.text((MARGIN, MARGIN - int(0.06*DPI)), headline, font=f_head, fill=INK)
    f_sub = _font(int(0.145 * DPI))
    d.text((MARGIN, MARGIN + int(0.32*DPI)), subhead, font=f_sub, fill=MUTED)

    # ---------- image pair ----------
    top    = MARGIN + int(0.68 * DPI)
    avail_h = H - top - MARGIN - int(1.55 * DPI)
    panel   = min(int(3.15 * DPI), avail_h)
    gap     = int(0.10 * DPI)
    left    = MARGIN

    for i, (path, label, colour) in enumerate((
            (before_path, "TODAY", (52, 58, 70)),
            (after_path,  "AFTER", ACCENT))):
        x = left + i * (panel + gap)
        im = _crop_square(Image.open(path).convert("RGB"), panel, zoom=zoom, focus=focus)
        card.paste(im, (x, top))

        # highlight ring on the AFTER panel - draws the eye to the change
        if i == 1:
            ring = Image.new("RGBA", (panel, panel), (0, 0, 0, 0))
            rd = ImageDraw.Draw(ring)
            pad = int(panel * 0.17)
            rd.rounded_rectangle([pad, pad, panel-pad, panel-pad],
                                 radius=int(panel*0.05),
                                 outline=GOLD + (235,), width=int(0.045*DPI))
            ring = ring.filter(ImageFilter.GaussianBlur(1.5))
            card.paste(ring, (x, top), ring)

        d.rectangle([x, top, x+panel-1, top+panel-1], outline=(206, 202, 196), width=3)

        # label chip
        cw, ch = int(1.15*DPI), int(0.32*DPI)
        d.rectangle([x, top, x+cw, top+ch], fill=colour)
        d.text((x + int(0.15*DPI), top + int(0.055*DPI)),
               label, font=_font(int(0.16*DPI), True), fill=(255,255,255))

    # ---------- chevron between panels ----------
    cx = left + panel + gap // 2
    cy = top + panel // 2
    r  = int(0.19 * DPI)
    d.ellipse([cx-r, cy-r, cx+r, cy+r], fill=ACCENT)
    a = int(0.075 * DPI)
    d.polygon([(cx-a//2, cy-a), (cx+a, cy), (cx-a//2, cy+a)], fill=(255,255,255))

    # ---------- right rail ----------
    rx = left + 2*panel + gap + int(0.30*DPI)
    rail_w = W - rx - MARGIN
    ry = top

    d.text((rx, ry), "THIS PROPERTY", font=_font(int(0.125*DPI), True), fill=ACCENT)
    ry += int(0.25*DPI)
    f_addr = _font(int(0.155*DPI), True)
    for line in _wrap(d, address, f_addr, rail_w):
        d.text((rx, ry), line, font=f_addr, fill=INK)
        ry += int(0.215*DPI)

    ry += int(0.16*DPI)
    d.line([rx, ry, rx+rail_w, ry], fill=(224, 221, 216), width=3)
    ry += int(0.26*DPI)

    body = ("Pavers add lasting value and stay level for decades. "
            "We install across central Indiana, typically in two days.")
    f_body = _font(int(0.122*DPI))
    for line in _wrap(d, body, f_body, rail_w):
        d.text((rx, ry), line, font=f_body, fill=MUTED)
        ry += int(0.185*DPI)

    # ---------- call to action ----------
    by = min(ry + int(0.30*DPI), H - MARGIN - int(1.02*DPI))
    d.rectangle([rx, by, rx+rail_w, by+int(1.02*DPI)], fill=INK)
    pad = int(0.16*DPI)
    oy = by + pad
    f_off = _font(int(0.105*DPI), True)
    for line in _wrap(d, offer, f_off, rail_w - 2*pad):
        d.text((rx+pad, oy), line, font=f_off, fill=(214, 210, 202))
        oy += int(0.15*DPI)
    oy += int(0.03*DPI)
    f_ph = _font(int(0.165*DPI), True)
    d.text((rx+pad, oy), phone, font=f_ph, fill=(255, 255, 255))
    oy += int(0.27*DPI)
    d.text((rx+pad, oy), company, font=_font(int(0.098*DPI)), fill=(160, 166, 176))

    # ---------- compliance footer ----------
    fy = H - int(0.62 * DPI)
    d.line([MARGIN, fy - int(0.10*DPI), W - MARGIN, fy - int(0.10*DPI)],
           fill=(226, 223, 218), width=2)

    f_fine = _font(int(0.088*DPI))
    footer_w = W - 2*MARGIN
    for line in _wrap(d, disclosure, f_fine, footer_w):
        d.text((MARGIN, fy), line, font=f_fine, fill=(120, 124, 132))
        fy += int(0.125*DPI)

    f_tiny = _font(int(0.078*DPI))
    d.text((MARGIN, fy), f"{return_address}  |  {opt_out}",
           font=f_tiny, fill=(158, 158, 158))
    fy += int(0.115*DPI)
    d.text((MARGIN, fy), attribution, font=f_tiny, fill=(178, 178, 178))

    card.save(out_path, "JPEG", quality=95, dpi=(DPI, DPI))
    return out_path
