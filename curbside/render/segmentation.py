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


STREET_POLYGON_PROMPT = """This is a street-level photograph of a US
residential property, taken from the road looking toward the house.

Locate the DRIVEWAY: the paved strip running from the kerb in the foreground
back toward the garage or house, including any parking apron. In this view it
is widest at the bottom of the frame and narrows as it recedes.

Do NOT locate: the lawn on either side of it, the public road across the
foreground, the walkway to the front door, the house, or a neighbour's
driveway. The lawn is the most common mistake - the box must not extend
sideways into grass.

Return the driveway's tight bounding box in normalized coordinates, where
x0,y0 is the top-left corner and x1,y1 the bottom-right, with x=0 at the left
edge, x=1 at the right edge, y=0 at the top, y=1 at the bottom. Make the box
as tight around the paved surface as possible.

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

    # Three conventions turn up in practice and the box does not say which it
    # is using: normalized 0-1, raw pixels, and Gemini's documented 0-1000
    # grid. The grid is the common one and the easiest to misread - on a 640px
    # image "y1": 997 looks like a pixel value 357px outside the frame, which
    # collapses the box to nothing, drops the prior, and takes the mask with
    # it. Values above the image's own extent can only be grid coordinates.
    grid = max(vals) > 1.5 and max(vals) <= 1000 and (
        max(vals[0], vals[2]) > w or max(vals[1], vals[3]) > h
        or max(vals) > max(w, h))

    def norm(v, extent):
        if v <= 1.5:
            return v                      # already normalized
        return v / 1000.0 if grid else v / extent

    nx = lambda v: norm(v, w)
    ny = lambda v: norm(v, h)
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


def segment(image_path, key=None, model=None, strategy="grounded", prompt=None):
    """Return (mask, meta). Falls back to spectral if the model is unavailable.

    `prompt` selects the view. Asking for a top-down driveway in a photograph
    taken from the kerb gets an honest "not found", and the mask then falls
    back to raw pixel difference - which knows what changed but not what a
    driveway is, so it will happily accept a paved lawn.
    """
    _, size = _to_array(image_path)

    if strategy == "grounded" and key:
        from curbside.vision import gemini
        from curbside.config import settings
        if prompt is None:
            prompt = (STREET_POLYGON_PROMPT if settings.street_view
                      else POLYGON_PROMPT)
        result, err, cost = gemini.ask_json(
            image_path, prompt, POLYGON_SCHEMA, key, model=model)
        if not err and result and result.get("found") and result.get("box"):
            raw = box_to_mask(result["box"], size)
            # An empty mask from a box the model called "found" means the box
            # was malformed - out of frame, or inverted. Treating that as a
            # legitimate prior would drop it and fall through to pixel diff
            # without saying so.
            if raw.mean() <= 0.002:
                mask = spectral_mask(image_path)
                return mask, {"strategy": "spectral-fallback",
                              "reason": f"unusable box {result['box']}",
                              "coverage": round(float(mask.mean()), 4),
                              "cost": cost}
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


def centred_enough(prior_mask, max_offset=0.30, min_edge_gap=0.02):
    """Is the segmented driveway the subject property's, or a neighbour's?

    Street View frames a point on the road, not a parcel, so a shot of one
    house routinely contains the frontage of two others. The camera is aimed
    at the subject, so its driveway sits near the horizontal centre; a
    driveway hard against the frame edge belongs to somebody else.

    Returns (ok, detail). Vertical position is ignored - a driveway correctly
    runs from the bottom edge toward the house.
    """
    import numpy as _np
    if prior_mask is None or prior_mask.mean() < 0.002:
        return False, {"reason": "no driveway region to place"}

    cols = prior_mask.sum(axis=0)
    total = cols.sum()
    if total <= 0:
        return False, {"reason": "empty driveway region"}

    centre = float((cols * _np.arange(cols.size)).sum() / total) / cols.size
    offset = abs(centre - 0.5)

    present = _np.nonzero(cols > cols.max() * 0.05)[0]
    left_gap = float(present[0]) / cols.size
    right_gap = 1.0 - float(present[-1] + 1) / cols.size

    detail = {"centre_x": round(centre, 3), "offset": round(offset, 3),
              "left_gap": round(left_gap, 3), "right_gap": round(right_gap, 3)}

    if offset > max_offset:
        detail["reason"] = (f"driveway sits at x={centre:.2f}, too far from the "
                            "centre to be this property's")
        return False, detail
    # Touching both edges means the region spans the whole frontage, which is
    # a road or a terrace of driveways rather than one lot's.
    if left_gap < min_edge_gap and right_gap < min_edge_gap:
        detail["reason"] = "region spans the full frame width"
        return False, detail
    return True, detail


def consensus_mask(before_path, after_path, prior_mask, threshold=26,
                   min_blob_frac=0.004, feather=2, dilate_prior=25,
                   require_prior=False):
    """Combine a segmentation prior with the observed render diff.

    Neither signal is reliable alone:
      - the prior knows what a driveway *is* but traces it imprecisely
      - the diff knows exactly which pixels changed but not what they are

    Their intersection is precise AND semantically anchored. The prior is
    dilated first so a slightly-off polygon still admits the true region;
    the diff then supplies the exact boundary.

    Falls back to the diff alone when the prior is unusable, so a bad
    segmentation degrades to the previous behaviour rather than breaking.

    `require_prior` disables that fallback. From the kerb the frame contains
    lawn, walkways and the neighbours' frontage, and a diff-only mask accepts
    whatever the renderer changed - which is how a paved lawn reaches a
    postcard. Refusing is the better failure: it costs a lead, not a mailing
    that shows the wrong surface paved.
    """
    from curbside.render.compositing import derive_mask

    diff_mask = derive_mask(before_path, after_path, threshold=threshold,
                            min_blob_frac=min_blob_frac, feather=feather)

    if prior_mask is None or prior_mask.mean() < 0.002 or prior_mask.mean() > 0.60:
        if require_prior:
            return None, {"mode": "refused", "reason": "no usable driveway prior"}
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
        if require_prior:
            return None, {"mode": "refused",
                          "reason": f"driveway prior kept only {kept:.0%} of "
                                    "the change - the render moved something else",
                          "prior_coverage": round(float(prior_mask.mean()), 4)}
        return diff_mask, {"mode": "diff-only", "reason": f"prior kept only {kept:.0%}",
                           "prior_coverage": round(float(prior_mask.mean()), 4)}

    return consensus, {
        "mode": "consensus",
        "prior_coverage": round(float(prior_mask.mean()), 4),
        "diff_coverage": round(float(diff_mask.mean()), 4),
        "final_coverage": round(float(consensus.mean()), 4),
        "diff_kept": round(float(kept), 3),
    }
