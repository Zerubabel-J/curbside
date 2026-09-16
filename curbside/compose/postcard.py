#!/usr/bin/env python3
"""Print-ready 4x6in postcard @ 300 DPI, built to Lob's template.

Size is a pricing decision, not a design one. USPS charges letter-rate postage
for a card within 4.25x6 and flat-rate above it, and Lob's per-piece price
follows: 4x6 is $0.905 on the developer tier against $1.026 for 6x9. Every
piece over the line costs ~13% more to deliver the same message, so the
artwork is built to the smaller spec and `mail.providers` sends `size="4x6"`
to match. The two must agree - artwork at one size and postage at another is
rejected by the printer.

Geometry, from Lob's 4x6 template:

    bleed  4.25 x 6.25 in   what we submit; trimmed away
    trim   4    x 6    in   the finished card
    safe   3.875 x 5.875 in   text must not leave this

Landscape, so the card is 6.25 wide x 4.25 tall. The front is fully ours;
Lob prints the address block, barcode and postage onto the back itself.

Design intent: the recipient must see the difference in under a second.
- the panels are stacked and equal, with the change ringed on the AFTER panel
- a chevron between them pushes the eye down, before -> after
- the right rail carries the address, the offer and the phone number

At this size there is roughly a third of the area of the old 9x6 card, so the
layout is two columns rather than three and every type size is set against the
safe zone rather than scaled down from the larger card.
"""
import pathlib
from PIL import Image, ImageDraw, ImageFont, ImageFilter

DPI = 300

# Lob 4x6 landscape. BLEED is the canvas; TRIM and SAFE are measured in from it.
BLEED_W_IN, BLEED_H_IN = 6.25, 4.25
TRIM_W_IN,  TRIM_H_IN  = 6.00, 4.00
SAFE_W_IN,  SAFE_H_IN  = 5.875, 3.875

W, H = int(BLEED_W_IN * DPI), int(BLEED_H_IN * DPI)      # 1875 x 1275

# Inset from the bleed edge to each guide. Bleed is trimmed off, so anything
# inside BLEED_IN of an edge may not survive the cut.
BLEED_IN = (BLEED_W_IN - TRIM_W_IN) / 2                  # 0.125 in
SAFE_IN  = (BLEED_W_IN - SAFE_W_IN) / 2                  # 0.1875 in

# Text stays inside the safe zone with a little breathing room beyond it.
MARGIN = int((SAFE_IN + 0.055) * DPI)

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


def _fit(d, text, width, max_size, bold=False, min_size=None):
    """Largest size at or below `max_size` that keeps `text` on one line.

    Addresses and company names vary in length by a factor of three. Fixing the
    type size instead pushes the long ones outside the safe zone, where the
    trimmer removes them.
    """
    min_size = min_size or max(10, int(max_size * 0.55))
    size = max_size
    while size > min_size:
        f = _font(size, bold)
        if d.textlength(text, font=f) <= width:
            return f
        size -= 1
    return _font(min_size, bold)


def _crop(img, w, h, zoom=1.0, focus=None):
    """Crop to a w:h window with optional zoom, centred on `focus` (fx, fy).

    Framing on the driveway rather than the image centre keeps the changed
    region in view - otherwise a large roof can dominate the panel.

    Returns the cropped panel and where the focus point landed inside it. The
    window is clamped at the image edges, so on a home near the frame edge the
    driveway is off-centre in the panel and anything pointing at it - the
    highlight ring - has to follow it there.
    """
    iw, ih = img.size
    want = w / h
    cw, ch = (iw, int(iw / want)) if iw / ih > want else (int(ih * want), ih)
    cw, ch = int(cw / zoom), int(ch / zoom)
    cw, ch = max(1, min(cw, iw)), max(1, min(ch, ih))
    if focus:
        cx, cy = int(focus[0] * iw), int(focus[1] * ih)
    else:
        cx, cy = iw // 2, ih // 2
    x = max(0, min(cx - cw // 2, iw - cw))
    y = max(0, min(cy - ch // 2, ih - ch))
    panel = img.crop((x, y, x + cw, y + ch)).resize((w, h), Image.LANCZOS)
    at = (int((cx - x) * w / cw), int((cy - y) * h / ch))
    return panel, at


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
          points=("Adds lasting value to your home",
                  "Stays level for decades",
                  "Installed in about two days"),
          zoom=1.25, focus=None,
          attribution=None):
    from curbside.compliance.policy import assert_disclosure, required_disclosure
    from curbside.config import settings

    # Attribution follows the imagery actually used - hardcoding it credits
    # the wrong agency the moment the source changes. At street level the
    # photograph is Google's, not the county's, and Google's terms require the
    # credit to be visible on the piece.
    if attribution is None:
        if settings.street_view:
            from curbside.sources.streetview import ATTRIBUTION
            attribution = ATTRIBUTION
        else:
            attribution = settings.imagery().attribution

    # Compliance is enforced here, at the point of composition - a piece that
    # cannot carry its disclosure is never produced in the first place.
    disclosure = disclosure or required_disclosure()
    assert_disclosure(disclosure)
    if not return_address:
        raise ValueError("return_address is required on a mailable piece")
    opt_out = opt_out or "To stop receiving these, write to the address above."

    card = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(card)

    # ---------- compliance footer, measured first ----------
    # The disclosure is mandatory and the longest text on the card, so it
    # claims its space before the layout does. Sizing the art first and giving
    # the footer the remainder is how legal text ends up trimmed off.
    f_fine = _font(int(0.062 * DPI))
    f_tiny = _font(int(0.055 * DPI))
    fine_lh = int(0.079 * DPI)
    tiny_lh = int(0.072 * DPI)
    body_w = W - 2 * MARGIN

    disc_lines = _wrap(d, disclosure, f_fine, body_w)
    legal_lines = _wrap(d, f"{return_address}  |  {opt_out}", f_tiny, body_w)
    footer_h = (len(disc_lines) * fine_lh + len(legal_lines) * tiny_lh
                + tiny_lh + int(0.05 * DPI))
    footer_top = H - MARGIN - footer_h

    # ---------- headline band ----------
    f_head = _fit(d, headline, body_w, int(0.175 * DPI), True)
    y = MARGIN - int(0.01 * DPI)
    d.text((MARGIN, y), headline, font=f_head, fill=INK)
    y += int(0.195 * DPI)
    f_sub = _fit(d, subhead, body_w, int(0.092 * DPI))
    d.text((MARGIN, y), subhead, font=f_sub, fill=MUTED)

    # ---------- geometry: stacked panels left, rail right ----------
    top = MARGIN + int(0.345 * DPI)
    art_h = footer_top - top - int(0.06 * DPI)
    gap = int(0.055 * DPI)
    panel_h = (art_h - gap) // 2
    panel_w = int(2.95 * DPI)
    rail_x = MARGIN + panel_w + int(0.18 * DPI)
    rail_w = W - rail_x - MARGIN

    for i, (path, label, colour) in enumerate((
            (before_path, "TODAY", (52, 58, 70)),
            (after_path,  "AFTER", ACCENT))):
        py = top + i * (panel_h + gap)
        im, at = _crop(Image.open(path).convert("RGB"), panel_w, panel_h,
                       zoom=zoom, focus=focus)
        card.paste(im, (MARGIN, py))

        # highlight ring on the AFTER panel - draws the eye to the change.
        # Centred on the driveway, not the panel: a ring around the middle of
        # the frame points at whatever happens to be there, usually the lawn.
        if i == 1:
            ring = Image.new("RGBA", (panel_w, panel_h), (0, 0, 0, 0))
            rd = ImageDraw.Draw(ring)
            rw, rh = int(panel_w * 0.46), int(panel_h * 0.46)
            rcx, rcy = at
            rcx = max(rw // 2, min(rcx, panel_w - rw // 2))
            rcy = max(rh // 2, min(rcy, panel_h - rh // 2))
            rd.rounded_rectangle([rcx - rw // 2, rcy - rh // 2,
                                  rcx + rw // 2, rcy + rh // 2],
                                 radius=int(panel_h * 0.07),
                                 outline=GOLD + (235,), width=int(0.028 * DPI))
            ring = ring.filter(ImageFilter.GaussianBlur(1.2))
            card.paste(ring, (MARGIN, py), ring)

        d.rectangle([MARGIN, py, MARGIN + panel_w - 1, py + panel_h - 1],
                    outline=(206, 202, 196), width=2)

        # label chip
        cw, ch = int(0.62 * DPI), int(0.17 * DPI)
        d.rectangle([MARGIN, py, MARGIN + cw, py + ch], fill=colour)
        d.text((MARGIN + int(0.075 * DPI), py + int(0.026 * DPI)),
               label, font=_font(int(0.088 * DPI), True), fill=(255, 255, 255))

    # ---------- chevron between panels, pointing down ----------
    cx = MARGIN + panel_w // 2
    cy = top + panel_h + gap // 2
    r = int(0.105 * DPI)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=ACCENT)
    a = int(0.042 * DPI)
    d.polygon([(cx - a, cy - a // 2), (cx, cy + a), (cx + a, cy - a // 2)],
              fill=(255, 255, 255))

    # ---------- right rail ----------
    ry = top
    d.text((rail_x, ry), "THIS PROPERTY",
           font=_font(int(0.072 * DPI), True), fill=ACCENT)
    ry += int(0.135 * DPI)

    # The street line carries the recognition; the city line only confirms it.
    street, _, rest = address.partition(",")
    f_addr = _fit(d, street.strip(), rail_w, int(0.105 * DPI), True)
    d.text((rail_x, ry), street.strip(), font=f_addr, fill=INK)
    ry += int(0.125 * DPI)
    if rest.strip():
        f_city = _fit(d, rest.strip(), rail_w, int(0.075 * DPI))
        d.text((rail_x, ry), rest.strip(), font=f_city, fill=MUTED)
        ry += int(0.105 * DPI)

    ry += int(0.05 * DPI)
    d.line([rail_x, ry, rail_x + rail_w, ry], fill=(224, 221, 216), width=2)
    ry += int(0.10 * DPI)

    cta_h_reserved = int(0.62 * DPI)

    f_body = _font(int(0.068 * DPI))
    body_lh = int(0.098 * DPI)
    lines = []
    for point in points:
        lines.extend(_wrap(d, point, f_body, rail_w - int(0.11 * DPI)))

    # The rail is taller than its copy. Open the leading up to a bound so the
    # list breathes, and let what is left sit as one gap above the call to
    # action - centring the list instead splits it into two smaller gaps that
    # read as an accident rather than a margin.
    cta_top = top + 2 * panel_h + gap - cta_h_reserved
    span = cta_top - int(0.10 * DPI) - ry
    lead = max(body_lh, min(int(0.165 * DPI), span // max(len(lines), 1)))

    bullet_r = int(0.017 * DPI)
    for i, line in enumerate(lines):
        # Mark the first line of each point; wrapped continuations align under.
        if line == _wrap(d, points[min(i, len(points) - 1)], f_body,
                         rail_w - int(0.11 * DPI))[0]:
            d.ellipse([rail_x, ry + int(0.028 * DPI),
                       rail_x + bullet_r * 2, ry + int(0.028 * DPI) + bullet_r * 2],
                      fill=ACCENT)
        d.text((rail_x + int(0.11 * DPI), ry), line, font=f_body, fill=MUTED)
        ry += lead

    # ---------- call to action ----------
    # Fixed height, bottom-aligned with the art. Stretching it to fill the rail
    # turns the phone number into a small mark on a large dark slab; the copy
    # above absorbs the slack instead.
    art_bottom = top + 2 * panel_h + gap
    cta_h = cta_h_reserved
    by = art_bottom - cta_h
    d.rectangle([rail_x, by, rail_x + rail_w, by + cta_h], fill=INK)
    pad = int(0.085 * DPI)
    oy = by + pad
    f_off = _fit(d, offer, rail_w - 2 * pad, int(0.068 * DPI), True)
    d.text((rail_x + pad, oy), offer, font=f_off, fill=(214, 210, 202))
    oy += int(0.105 * DPI)
    f_ph = _fit(d, phone, rail_w - 2 * pad, int(0.125 * DPI), True)
    d.text((rail_x + pad, oy), phone, font=f_ph, fill=(255, 255, 255))
    oy += int(0.175 * DPI)
    f_co = _fit(d, company, rail_w - 2 * pad, int(0.062 * DPI))
    d.text((rail_x + pad, oy), company, font=f_co, fill=(160, 166, 176))

    # ---------- compliance footer, into the space reserved above ----------
    fy = footer_top
    d.line([MARGIN, fy - int(0.05 * DPI), W - MARGIN, fy - int(0.05 * DPI)],
           fill=(226, 223, 218), width=2)
    for line in disc_lines:
        d.text((MARGIN, fy), line, font=f_fine, fill=(120, 124, 132))
        fy += fine_lh
    for line in legal_lines:
        d.text((MARGIN, fy), line, font=f_tiny, fill=(158, 158, 158))
        fy += tiny_lh
    d.text((MARGIN, fy), attribution, font=f_tiny, fill=(178, 178, 178))

    card.save(out_path, "JPEG", quality=95, dpi=(DPI, DPI))
    return out_path
