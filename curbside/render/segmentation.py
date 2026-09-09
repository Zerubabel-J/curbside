"""Driveway segmentation — find the driveway BEFORE rendering.

The diff-based approach infers the driveway from whatever the render model
changed, which means a bad render produces a bad mask and the two failures
reinforce each other. Segmenting first inverts that: we decide what the
driveway is from the original photo alone, then hold the render to it.

Two strategies, best-first:

  1. `grounded`  - ask a vision model for the driveway's bounding polygon in
                   normalized coordinates, then refine it against local colour
                   and texture statistics.
  2. `spectral`  - pure CV fallback: pavement is low-saturation, mid-value and
                   locally smooth. Seeded from the house-to-street corridor.

Both return a float mask in [0,1] the same size as the image.
"""
import json
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

POLYGON_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "box": {
            "type": "object",
            "description": "Tight bounding box of the driveway, normalized 0-1",
            "properties": {
                "x0": {"type": "number"}, "y0": {"type": "number"},
                "x1": {"type": "number"}, "y1": {"type": "number"},
            },
            "required": ["x0", "y0", "x1", "y1"],
        },
        "surface": {"type": "string",
                    "enum": ["concrete", "asphalt", "gravel", "pavers", "dirt", "unclear"]},
        "note": {"type": "string"},
    },
    "required": ["found", "box", "surface", "note"],
}

POLYGON_PROMPT = """This is a top-down aerial photograph of a US residential
property. Locate the DRIVEWAY: the paved surface connecting the public street
to the house or garage, including any parking apron.

Do NOT locate: the public sidewalk running parallel to the street, the street
itself, the roof, the lawn, or a neighbouring property's driveway.

Return the driveway's tight bounding box in normalized coordinates, where
x0,y0 is the top-left corner and x1,y1 the bottom-right, with x=0 at the left
edge, x=1 at the right edge, y=0 at the top, y=1 at the bottom. Make the box
as tight around the driveway as possible.

If no driveway is visible, set found=false and return zeros."""


def _to_array(path):
    im = Image.open(str(path)).convert("RGB")
    return np.asarray(im).astype(np.float32), im.size


def box_to_mask(box, size, feather=2, pad=0.02):
    """Bounding box -> float mask, with a little padding.

    Accepts either normalized 0-1 coordinates or raw pixel coordinates - models
    return both depending on the day, so detect rather than assume.
    """
    w, h = size
    vals = [float(box.get(k, 0)) for k in ("x0", "y0", "x1", "y1")]
    if max(vals) > 1.5:                       # pixel coords
        nx = lambda v: v / w
        ny = lambda v: v / h
    else:                                      # already normalized
        nx = ny = lambda v: v
    x0 = max(0.0, nx(vals[0]) - pad) * w
    y0 = max(0.0, ny(vals[1]) - pad) * h
    x1 = min(1.0, nx(vals[2]) + pad) * w
    y1 = min(1.0, ny(vals[3]) + pad) * h
    if x1 <= x0 or y1 <= y0:
        return np.zeros((h, w), dtype=np.float32)
    img = Image.new("L", (w, h), 0)
    ImageDraw.Draw(img).rectangle([x0, y0, x1, y1], fill=255)
    if feather:
        img = img.filter(ImageFilter.GaussianBlur(feather))
    return np.asarray(img).astype(np.float32) / 255.0


def refine_by_appearance(image_path, mask, tolerance=2.2, min_keep=0.35):
    """Shrink a coarse polygon to pixels that actually look like its pavement.

    Learns colour statistics from the confident interior of the polygon, then
    keeps pixels within `tolerance` standard deviations. This pulls the mask
    off lawn and roof that a loose polygon may have swallowed.
    """
    arr, (w, h) = _to_array(image_path)
    core = mask > 0.85
    if core.sum() < 200:
        return mask

    # Erode the core so we sample only well-inside pixels.
    ci = Image.fromarray((core * 255).astype(np.uint8)).filter(ImageFilter.MinFilter(7))
    core = np.asarray(ci) > 127
    if core.sum() < 100:
        core = mask > 0.85

    samples = arr[core]
    mu = samples.mean(axis=0)
    sd = samples.std(axis=0) + 6.0          # floor avoids over-tight bounds

    dist = np.abs(arr - mu) / sd
    similar = dist.max(axis=2) < tolerance

    refined = similar & (mask > 0.25)

    # If refinement is too aggressive, the statistics were unreliable.
    if refined.sum() < min_keep * max(core.sum(), 1):
        return mask

    out = Image.fromarray((refined * 255).astype(np.uint8))
    out = out.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.MinFilter(5))
    out = out.filter(ImageFilter.GaussianBlur(2))
    return np.asarray(out).astype(np.float32) / 255.0


def spectral_mask(image_path, sat_max=0.22, val_range=(0.22, 0.82)):
    """CV-only fallback. Pavement: low saturation, mid value, locally smooth."""
    im = Image.open(str(image_path)).convert("RGB")
    hsv = np.asarray(im.convert("HSV")).astype(np.float32) / 255.0
    s, v = hsv[..., 1], hsv[..., 2]

    candidate = (s < sat_max) & (v > val_range[0]) & (v < val_range[1])

    # Texture: pavement is smoother than lawn or tree canopy.
    grey = np.asarray(im.convert("L")).astype(np.float32)
    blur = np.asarray(im.convert("L").filter(ImageFilter.GaussianBlur(3))).astype(np.float32)
    smooth = np.abs(grey - blur) < 12
    candidate &= smooth

    m = Image.fromarray((candidate * 255).astype(np.uint8))
    m = m.filter(ImageFilter.MaxFilter(7)).filter(ImageFilter.MinFilter(9))
    m = m.filter(ImageFilter.MaxFilter(5))
    binary = np.asarray(m) > 127

    # Pavement is everywhere in an aerial frame - street, sidewalk, driveway.
    # Keep only the component nearest the centre, where the subject property is.
    from curbside.render.compositing import _largest_blobs
    blobs = _largest_blobs(binary, min_frac=0.006)
    if blobs.sum() == 0:
        return np.zeros(binary.shape, dtype=np.float32)
    if blobs.mean() > 0.35:
        return np.zeros(binary.shape, dtype=np.float32)  # too broad to trust

    out = Image.fromarray((blobs * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(2))
    return np.asarray(out).astype(np.float32) / 255.0


def segment(image_path, key=None, model=None, strategy="grounded"):
    """Return (mask, meta). Falls back to spectral if the model is unavailable."""
    _, size = _to_array(image_path)

    if strategy == "grounded" and key:
        from curbside.vision import gemini
        result, err, cost = gemini.ask_json(
            image_path, POLYGON_PROMPT, POLYGON_SCHEMA, key, model=model)
        if not err and result and result.get("found") and result.get("box"):
            raw = box_to_mask(result["box"], size)
            if 0.002 < raw.mean() < 0.75:
                refined = refine_by_appearance(image_path, raw)
                return refined, {
                    "strategy": "grounded",
                    "surface": result.get("surface"),
                    "box": result["box"],
                    "coverage_raw": round(float(raw.mean()), 4),
                    "coverage_refined": round(float(refined.mean()), 4),
                    "cost": cost,
                    "note": result.get("note", "")[:160],
                }
        meta_err = err or "no polygon returned"
        mask = spectral_mask(image_path)
        return mask, {"strategy": "spectral-fallback", "reason": meta_err,
                      "coverage": round(float(mask.mean()), 4), "cost": cost}

    mask = spectral_mask(image_path)
    return mask, {"strategy": "spectral",
                  "coverage": round(float(mask.mean()), 4), "cost": 0.0}


def consensus_mask(before_path, after_path, prior_mask, threshold=26,
                   min_blob_frac=0.004, feather=2, dilate_prior=25):
    """Combine a segmentation prior with the observed render diff.

    Neither signal is reliable alone:
      - the prior knows what a driveway *is* but traces it imprecisely
      - the diff knows exactly which pixels changed but not what they are

    Their intersection is precise AND semantically anchored. The prior is
    dilated first so a slightly-off polygon still admits the true region;
    the diff then supplies the exact boundary.

    Falls back to the diff alone when the prior is unusable, so a bad
    segmentation degrades to the previous behaviour rather than breaking.
    """
    from curbside.render.compositing import derive_mask

    diff_mask = derive_mask(before_path, after_path, threshold=threshold,
                            min_blob_frac=min_blob_frac, feather=feather)

    if prior_mask is None or prior_mask.mean() < 0.002 or prior_mask.mean() > 0.60:
        return diff_mask, {"mode": "diff-only", "reason": "prior unusable"}

    # Dilate the prior to tolerate imprecise tracing.
    p = Image.fromarray((np.clip(prior_mask, 0, 1) * 255).astype(np.uint8))
    for _ in range(max(1, dilate_prior // 9)):
        p = p.filter(ImageFilter.MaxFilter(9))
    prior_d = np.asarray(p).astype(np.float32) / 255.0

    consensus = diff_mask * (prior_d > 0.3)

    # If the prior rejects nearly everything the diff found, trust the diff -
    # the polygon was probably in the wrong place entirely.
    kept = consensus.sum() / max(diff_mask.sum(), 1.0)
    if kept < 0.25:
        return diff_mask, {"mode": "diff-only", "reason": f"prior kept only {kept:.0%}",
                           "prior_coverage": round(float(prior_mask.mean()), 4)}

    return consensus, {
        "mode": "consensus",
        "prior_coverage": round(float(prior_mask.mean()), 4),
        "diff_coverage": round(float(diff_mask.mean()), 4),
        "final_coverage": round(float(consensus.mean()), 4),
        "diff_kept": round(float(kept), 3),
    }
