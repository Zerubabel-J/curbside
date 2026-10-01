#!/usr/bin/env python3
"""Lay the best renders out as sheets to send.

The bench produces one contact sheet per house, which is right for iterating
and wrong for showing someone. This builds a small number of grids from the
renders that passed the quality gate, ordered so the strongest is first.

    python bench/present.py --run free-full
    python bench/present.py --run free-full --top 12
"""
import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw, ImageFont

HERE = pathlib.Path(__file__).resolve().parent
HOMES = HERE / "homes"
OUT = HERE / "out"


def _font(size, bold=False):
    for p in (f"/usr/share/fonts/truetype/dejavu/DejaVuSans{'-Bold' if bold else ''}.ttf",
              f"/usr/share/fonts/TTF/DejaVuSans{'-Bold' if bold else ''}.ttf"):
        if pathlib.Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def _postcard_worthy(result, min_brightness=70):
    """Would this actually sell a driveway?

    The render gate measures fidelity - did the model edit the photograph
    rather than repaint it. That is necessary and not sufficient. A render can
    be perfectly faithful and still be useless on a postcard: a photograph
    taken in deep shade, or one aimed at a wall with no house in it, passes
    every fidelity check while showing the recipient nothing they recognise.

    Brightness separates these cleanly on the bench - the dark frames sit near
    50 while every usable one is above 70.
    """
    import numpy as np
    src = HOMES / f"{result['home']}.jpg"
    if not src.exists():
        return False
    a = np.asarray(Image.open(src).convert("L")).astype(float)
    return a.mean() >= min_brightness


def pair(result, cell=460):
    """One before/after pair, captioned with the address."""
    src = HOMES / f"{result['home']}.jpg"
    after = pathlib.Path(result["path"])
    if not (src.exists() and after.exists()):
        return None

    pad, cap = 10, 46
    w = cell * 2 + pad * 3
    h = cell + cap + pad * 2
    tile = Image.new("RGB", (w, h), (255, 255, 255))
    d = ImageDraw.Draw(tile)

    for i, (img, label, colour) in enumerate((
            (Image.open(src).convert("RGB"), "TODAY", (52, 58, 70)),
            (Image.open(after).convert("RGB"), "WITH A NEW DRIVEWAY", (190, 58, 20)))):
        x = pad + i * (cell + pad)
        tile.paste(img.resize((cell, cell), Image.LANCZOS), (x, pad))
        chip_w = int(len(label) * 8.2) + 18
        d.rectangle([x, pad, x + chip_w, pad + 26], fill=colour)
        d.text((x + 9, pad + 5), label, font=_font(13, True), fill=(255, 255, 255))

    d.text((pad, cell + pad + 10), result["address"],
           font=_font(17, True), fill=(20, 24, 32))
    return tile


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--per-sheet", type=int, default=4)
    args = ap.parse_args()

    run_dir = OUT / args.run
    results = json.loads((run_dir / "results.json").read_text())
    good = [r for r in results if r.get("good") and _postcard_worthy(r)]
    # Strongest first: a bigger visible change is a better postcard, provided
    # it passed the fidelity gate - which is what `good` already guarantees.
    good.sort(key=lambda r: -r.get("ground_change", 0))
    good = good[:args.top]
    if not good:
        sys.exit("no renders passed the quality gate in that run")

    dest = run_dir / "present"
    dest.mkdir(exist_ok=True)
    tiles = [t for t in (pair(r) for r in good) if t]

    made = []
    for n in range(0, len(tiles), args.per_sheet):
        chunk = tiles[n:n + args.per_sheet]
        tw, th = chunk[0].size
        pad, header = 16, 58
        sheet = Image.new("RGB", (tw + pad * 2, header + len(chunk) * (th + pad) + pad),
                          (246, 245, 243))
        d = ImageDraw.Draw(sheet)
        d.text((pad, 18), "Curbside - AI driveway renders on real homes",
               font=_font(24, True), fill=(20, 24, 32))
        for i, t in enumerate(chunk):
            sheet.paste(t, (pad, header + i * (th + pad)))
        p = dest / f"sheet_{n // args.per_sheet + 1:02d}.jpg"
        sheet.save(p, "JPEG", quality=92)
        made.append(p)
        print(f"  {p}")

    print(f"\n{len(tiles)} renders across {len(made)} sheets")
    print(f"send: {dest}")


if __name__ == "__main__":
    main()
