#!/usr/bin/env python3
"""Masked compositing + QC.

The render model is asked to change only the driveway, but nothing enforces it.
We enforce it here:

  1. derive_mask()  - find which pixels actually changed between before/after,
                      clean the region up, and keep only the driveway blob
  2. composite()    - rebuild the output as: original everywhere, rendered only
                      inside the mask. Pixels outside the mask are copied from
                      the original, so they are byte-identical by construction.
  3. qc()           - measure what the model tried to change outside the mask.
                      High drift = the model rewrote the house; reject it.
"""
import os
import numpy as np
from PIL import Image, ImageFilter


def _arr(src):
    """Accept a path (str/bytes/PathLike) or a PIL Image."""
    if isinstance(src, Image.Image):
        im = src.convert("RGB")
    else:
        im = Image.open(os.fspath(src)).convert("RGB")
    return np.asarray(im).astype(np.int16), im.size


def derive_mask(before_path, after_path, threshold=26, min_blob_frac=0.004,
                feather=2):
    """Boolean mask of the region the model meaningfully changed.

    threshold      per-pixel colour distance counted as 'changed'
    min_blob_frac  discard connected regions smaller than this fraction of frame
    feather        px of blur on the mask edge so the composite seam is soft
    """
    a, size = _arr(before_path)
    b, _    = _arr(after_path)
    if a.shape != b.shape:
        b_img = Image.open(os.fspath(after_path)).convert("RGB").resize(size, Image.LANCZOS)
        b = np.asarray(b_img).astype(np.int16)

    diff = np.abs(a - b).max(axis=2)
    raw  = diff > threshold

    # Morphological clean-up via PIL: close small gaps, drop speckle.
    m = Image.fromarray((raw * 255).astype(np.uint8))
    m = m.filter(ImageFilter.MaxFilter(7))   # dilate - close interior gaps
    m = m.filter(ImageFilter.MinFilter(9))   # erode  - remove thin noise
    m = m.filter(ImageFilter.MaxFilter(5))   # restore size
    cleaned = np.asarray(m) > 127

    kept = _largest_blobs(cleaned, min_frac=min_blob_frac)

    if feather:
        mi = Image.fromarray((kept * 255).astype(np.uint8))
        mi = mi.filter(ImageFilter.GaussianBlur(feather))
        return np.asarray(mi).astype(np.float32) / 255.0
    return kept.astype(np.float32)


def _largest_blobs(mask, min_frac=0.004):
    """Keep connected components above min_frac of the frame. Iterative flood
    fill - no scipy dependency."""
    h, w = mask.shape
    min_px = int(h * w * min_frac)
    seen = np.zeros_like(mask, dtype=bool)
    out = np.zeros_like(mask, dtype=bool)

    for sy in range(0, h, 4):
        for sx in range(0, w, 4):
            if not mask[sy, sx] or seen[sy, sx]:
                continue
            stack, comp = [(sy, sx)], []
            seen[sy, sx] = True
            while stack:
                y, x = stack.pop()
                comp.append((y, x))
                for dy, dx in ((1,0), (-1,0), (0,1), (0,-1)):
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
            if len(comp) >= min_px:
                ys, xs = zip(*comp)
                out[np.array(ys), np.array(xs)] = True
    return out


def composite(before_path, after_path, mask, out_path):
    """Original everywhere; rendered only inside the mask.

    Guarantees pixels outside the mask are unchanged - not by asking the model,
    but by never copying them from its output.
    """
    a, size = _arr(before_path)
    b, _    = _arr(after_path)
    if a.shape != b.shape:
        b = np.asarray(Image.open(os.fspath(after_path)).convert("RGB")
                       .resize(size, Image.LANCZOS)).astype(np.int16)

    m = mask[..., None]                       # (H,W,1) float 0..1
    blended = (a * (1.0 - m) + b * m).clip(0, 255).astype(np.uint8)
    Image.fromarray(blended).save(out_path, "JPEG", quality=95)
    return out_path


def qc(before_path, after_path, mask, drift_threshold=18, max_outside_frac=0.06):
    """Did the model respect the boundary?

    Returns a dict; `passed` is False when the model rewrote too much of the
    scene outside the driveway region.
    """
    a, size = _arr(before_path)
    b, _    = _arr(after_path)
    if a.shape != b.shape:
        b = np.asarray(Image.open(os.fspath(after_path)).convert("RGB")
                       .resize(size, Image.LANCZOS)).astype(np.int16)

    diff    = np.abs(a - b).max(axis=2)
    inside  = mask > 0.5
    outside = ~inside

    total_px      = diff.size
    mask_frac     = float(inside.sum()) / total_px
    drifted       = (diff > drift_threshold) & outside
    outside_frac  = float(drifted.sum()) / max(int(outside.sum()), 1)
    mean_outside  = float(diff[outside].mean()) if outside.any() else 0.0
    mean_inside   = float(diff[inside].mean()) if inside.any() else 0.0

    passed = (mask_frac >= 0.008 and mask_frac <= 0.45
              and outside_frac <= max_outside_frac)

    reasons = []
    if mask_frac < 0.008:
        reasons.append(f"changed region too small ({mask_frac:.1%})")
    if mask_frac > 0.45:
        reasons.append(f"changed region too large ({mask_frac:.1%})")
    if outside_frac > max_outside_frac:
        reasons.append(f"drift outside mask ({outside_frac:.1%})")

    return {
        "passed": passed,
        "mask_frac": round(mask_frac, 4),
        "outside_drift_frac": round(outside_frac, 4),
        "mean_diff_inside": round(mean_inside, 2),
        "mean_diff_outside": round(mean_outside, 2),
        "reasons": reasons,
    }


def verify_region(before_path, mask_preview_path, key, model=None):
    """Semantic QC: did the model edit the DRIVEWAY, or something else?

    Boundary QC proves the edit stayed inside the mask. It cannot tell whether
    the mask was on the right object - a roof, a road or a neighbour's lot can
    all pass a drift check.

    The failure this catches most often is the public street: it is paved,
    sits right beside the driveway, and is frequently the largest paved area
    in frame, so the model reaches for it. The prompt asks for the specific
    tells rather than just "is this a driveway".
    """
    from curbside.vision import gemini
    SCHEMA = {
        "type": "object",
        "properties": {
            "highlighted_object": {"type": "string",
                "enum": ["driveway", "road", "roof", "sidewalk", "lawn",
                         "parking_lot", "building", "other"]},
            "spans_full_width": {"type": "boolean",
                "description": "does the red region run edge to edge across the frame"},
            "touches_house": {"type": "boolean",
                "description": "does it connect to the house or garage"},
            "cars_on_it": {"type": "integer",
                "description": "how many vehicles sit on the red region"},
            "is_driveway": {"type": "boolean"},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "note": {"type": "string"},
        },
        "required": ["highlighted_object", "spans_full_width", "touches_house",
                     "cars_on_it", "is_driveway", "confidence", "note"],
    }
    PROMPT = (
        "A red translucent overlay marks one region of this top-down aerial "
        "photograph of a residential property. Identify what real-world "
        "surface lies underneath it.\n\n"
        "A DRIVEWAY is a private paved strip that CONNECTS THE HOUSE OR GARAGE "
        "to the street. It is short, sits on the property, and usually widens "
        "near the garage.\n\n"
        "A ROAD is the public street. Tells: it runs edge to edge across the "
        "frame, it is roughly parallel to the front of the house, it does NOT "
        "touch the house, and cars are often parked along it. If the red "
        "region looks like this, answer 'road', not 'driveway'.\n\n"
        "Answer the structural questions honestly first, then decide."
    )
    result, err, cost = gemini.ask_json(mask_preview_path, PROMPT, SCHEMA,
                                        key, model=model)
    if err or not result:
        return result, err, cost

    # Override an optimistic 'driveway' using structural answers. Asked to
    # confirm a label the model agrees; asked to count cars it reports
    # accurately - and a residential driveway does not hold three vehicles.
    if result.get("is_driveway"):
        cars = result.get("cars_on_it") or 0
        reasons = []
        if cars >= 2:
            reasons.append(f"{cars} vehicles on it")
        if result.get("spans_full_width") and not result.get("touches_house"):
            reasons.append("spans the frame without touching the house")
        if reasons:
            result["is_driveway"] = False
            result["highlighted_object"] = "road"
            result["note"] = ("looks like the public street: "
                              + ", ".join(reasons))
    return result, err, cost


def save_mask_preview(before_path, mask, out_path):
    """Diagnostic: original with the mask region tinted, for eyeballing."""
    a, _ = _arr(before_path)
    tint = np.zeros_like(a)
    tint[..., 0] = 255
    m = (mask[..., None] * 0.45)
    blended = (a * (1 - m) + tint * m).clip(0, 255).astype(np.uint8)
    Image.fromarray(blended).save(out_path, "JPEG", quality=88)
    return out_path
