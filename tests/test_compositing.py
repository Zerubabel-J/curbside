import numpy as np
import pytest
from PIL import Image

from curbside.render.compositing import composite, qc, derive_mask
from curbside.render.segmentation import box_to_mask, consensus_mask


@pytest.fixture
def scene(tmp_path):
    """A synthetic 'property': grey house block, dark driveway strip, green lawn."""
    a = np.zeros((256, 256, 3), dtype=np.uint8)
    a[:, :] = (60, 140, 60)              # lawn
    a[40:150, 40:150] = (150, 150, 155)  # house
    a[60:200, 170:210] = (70, 70, 72)    # driveway
    before = tmp_path / "before.jpg"
    Image.fromarray(a).save(before, quality=98)
    return tmp_path, a


def test_composite_leaves_pixels_outside_mask_untouched(scene, tmp_path):
    d, arr = scene
    changed = arr.copy()
    changed[60:200, 170:210] = (215, 210, 200)   # repaved driveway
    changed[40:150, 40:150] = (255, 0, 0)        # model also wrecked the house
    after = d / "after.jpg"
    Image.fromarray(changed).save(after, quality=98)

    mask = np.zeros((256, 256), dtype=np.float32)
    mask[60:200, 170:210] = 1.0                  # driveway only

    out = d / "out.jpg"
    composite(d / "before.jpg", after, mask, out)
    result = np.asarray(Image.open(out).convert("RGB")).astype(np.int16)
    original = np.asarray(Image.open(d / "before.jpg").convert("RGB")).astype(np.int16)

    house_diff = np.abs(result[50:140, 50:140] - original[50:140, 50:140]).max()
    assert house_diff < 12, "house must survive even when the model rewrote it"

    drive_diff = np.abs(result[80:180, 180:200] - original[80:180, 180:200]).mean()
    assert drive_diff > 50, "driveway should actually change"


def test_qc_flags_large_drift_outside_mask(scene, tmp_path):
    d, arr = scene
    wrecked = arr.copy()
    wrecked[:, :] = (200, 30, 30)               # model rewrote everything
    after = d / "after.jpg"
    Image.fromarray(wrecked).save(after, quality=98)

    mask = np.zeros((256, 256), dtype=np.float32)
    mask[60:200, 170:210] = 1.0
    report = qc(d / "before.jpg", after, mask)
    assert not report["passed"]
    assert any("drift" in r for r in report["reasons"])


def test_qc_rejects_mask_covering_almost_everything(scene):
    d, arr = scene
    after = d / "after.jpg"
    Image.fromarray(arr).save(after, quality=98)
    mask = np.ones((256, 256), dtype=np.float32)
    report = qc(d / "before.jpg", after, mask)
    assert not report["passed"]
    assert any("too large" in r for r in report["reasons"])


def test_box_to_mask_accepts_pixel_coordinates():
    """Vision models return pixel coords as often as normalized ones."""
    px = box_to_mask({"x0": 100, "y0": 100, "x1": 200, "y1": 200}, (400, 400), feather=0)
    norm = box_to_mask({"x0": 0.25, "y0": 0.25, "x1": 0.5, "y1": 0.5}, (400, 400), feather=0)
    assert abs(px.mean() - norm.mean()) < 0.02


def test_box_to_mask_handles_degenerate_box():
    m = box_to_mask({"x0": 0.5, "y0": 0.5, "x1": 0.1, "y1": 0.1}, (100, 100))
    assert m.sum() == 0


def test_consensus_falls_back_to_diff_when_prior_is_useless(scene):
    d, arr = scene
    changed = arr.copy()
    changed[60:200, 170:210] = (215, 210, 200)
    after = d / "after.jpg"
    Image.fromarray(changed).save(after, quality=98)

    empty = np.zeros((256, 256), dtype=np.float32)
    mask, meta = consensus_mask(d / "before.jpg", after, empty)
    assert meta["mode"] == "diff-only"

    everything = np.ones((256, 256), dtype=np.float32)
    mask, meta = consensus_mask(d / "before.jpg", after, everything)
    assert meta["mode"] == "diff-only"


def test_consensus_intersects_prior_with_diff(scene):
    d, arr = scene
    changed = arr.copy()
    changed[60:200, 170:210] = (215, 210, 200)   # driveway
    changed[40:150, 40:150] = (250, 250, 250)    # plus unwanted house edit
    after = d / "after.jpg"
    Image.fromarray(changed).save(after, quality=98)

    prior = np.zeros((256, 256), dtype=np.float32)
    prior[60:200, 170:210] = 1.0                 # prior knows: driveway only

    mask, meta = consensus_mask(d / "before.jpg", after, prior, dilate_prior=9)
    assert meta["mode"] == "consensus"
    assert mask[100, 190] > 0.5, "driveway kept"
    assert mask[90, 90] < 0.2, "house edit excluded by the prior"


# --------------------------------------------- semantic QC: the street trap

def _fake_verify(monkeypatch, payload):
    """verify_region calls the model then applies structural overrides. Stub
    the model so we test the override logic, not the model."""
    from curbside.vision import gemini
    monkeypatch.setattr(gemini, "ask_json",
                        lambda *a, **k: (dict(payload), None, 0.0))
    from curbside.render.compositing import verify_region
    return verify_region("b.jpg", "m.jpg", "key")


BASE = {"highlighted_object": "driveway", "spans_full_width": False,
        "touches_house": True, "cars_on_it": 0, "is_driveway": True,
        "confidence": "high", "note": "looks like a driveway"}


def test_genuine_driveway_passes(monkeypatch):
    r, err, _ = _fake_verify(monkeypatch, BASE)
    assert err is None and r["is_driveway"] is True


def test_one_parked_car_is_normal_for_a_driveway(monkeypatch):
    r, _, _ = _fake_verify(monkeypatch, {**BASE, "cars_on_it": 1})
    assert r["is_driveway"] is True


def test_multiple_cars_means_it_masked_the_street(monkeypatch):
    """The model will happily call a street a driveway, but it counts cars
    accurately - and a residential driveway does not hold two parked cars in
    a row along its length."""
    r, _, _ = _fake_verify(monkeypatch, {**BASE, "cars_on_it": 2})
    assert r["is_driveway"] is False
    assert r["highlighted_object"] == "road"
    assert "2 vehicles" in r["note"]


def test_full_width_band_not_touching_house_is_the_street(monkeypatch):
    r, _, _ = _fake_verify(monkeypatch, {**BASE, "spans_full_width": True,
                                         "touches_house": False})
    assert r["is_driveway"] is False
    assert "spans the frame" in r["note"]


def test_full_width_but_touching_house_is_still_a_driveway(monkeypatch):
    """A wide apron in front of a garage legitimately spans much of the frame."""
    r, _, _ = _fake_verify(monkeypatch, {**BASE, "spans_full_width": True,
                                         "touches_house": True})
    assert r["is_driveway"] is True


def test_a_negative_verdict_is_never_overridden_upward(monkeypatch):
    r, _, _ = _fake_verify(monkeypatch, {**BASE, "is_driveway": False,
                                         "highlighted_object": "roof"})
    assert r["is_driveway"] is False and r["highlighted_object"] == "roof"


def test_model_error_passes_through(monkeypatch):
    from curbside.vision import gemini
    monkeypatch.setattr(gemini, "ask_json", lambda *a, **k: (None, "boom", 0.0))
    from curbside.render.compositing import verify_region
    r, err, _ = verify_region("b.jpg", "m.jpg", "key")
    assert err == "boom" and r is None


# ------------------------------------------- street-level framing and refusal

def test_frame_fov_narrows_with_distance():
    """A fixed angle photographs a fixed slice of the world, so from the far
    kerb it takes in the neighbours. The angle has to follow the distance."""
    from curbside.sources.streetview import frame_fov
    near, far = frame_fov(14), frame_fov(45)
    assert near > far, "closer houses need a wider angle, not narrower"
    assert 40 <= far <= 90 and 40 <= near <= 90


def test_frame_fov_survives_a_missing_distance():
    from curbside.sources.streetview import frame_fov
    assert frame_fov(None, default=80) == 80
    assert frame_fov(0, default=80) == 80


def test_neighbours_driveway_is_rejected_by_position():
    """The camera is aimed at the subject property, so its driveway is near
    the centre. One hard against the frame edge belongs to somebody else, and
    rendering it mails a homeowner a picture of next door."""
    import numpy as np
    from curbside.render.segmentation import centred_enough

    def strip(x0, x1, w=100, h=100):
        m = np.zeros((h, w), dtype=np.float32)
        m[60:, x0:x1] = 1.0
        return m

    ok, _ = centred_enough(strip(38, 62))
    assert ok, "a centred driveway must pass"

    for x0, x1 in ((82, 100), (0, 18)):
        ok, detail = centred_enough(strip(x0, x1))
        assert not ok and "centre" in detail["reason"]

    ok, detail = centred_enough(strip(0, 100))
    assert not ok and "full frame" in detail["reason"]


def test_street_masking_refuses_without_a_driveway_prior():
    """Diff-only masking knows what changed but not what a driveway is, so it
    accepts a paved lawn. At street level that must refuse, not guess."""
    import numpy as np
    from PIL import Image
    from curbside.render.segmentation import consensus_mask

    import tempfile, pathlib
    d = pathlib.Path(tempfile.mkdtemp())
    a = np.full((64, 64, 3), 120, dtype=np.uint8)
    b = a.copy(); b[40:60, 10:50] = 220
    pa, pb = d / "a.jpg", d / "b.jpg"
    Image.fromarray(a).save(pa); Image.fromarray(b).save(pb)

    lenient, meta = consensus_mask(pa, pb, None, require_prior=False)
    assert lenient is not None and meta["mode"] == "diff-only"

    strict, meta = consensus_mask(pa, pb, None, require_prior=True)
    assert strict is None and meta["mode"] == "refused"


# ------------------------------------------------------- render variation

def test_materials_vary_between_neighbouring_leads():
    """A block of postcards that all show the same terracotta herringbone
    reads as one postcard printed five times."""
    from curbside.vision.gemini import material_for
    keys = {material_for(i)[0] for i in range(1, 6)}
    assert len(keys) == 5, "consecutive leads must not share a surface"


def test_material_choice_is_stable_for_a_lead():
    """A retry must not change the offer - a homeowner who receives two
    mailings should not see two different driveways."""
    from curbside.vision.gemini import material_for
    assert material_for(7) == material_for(7)


def test_every_material_keeps_the_boundary_language():
    """Varying the finish must not weaken the instruction that nothing outside
    the driveway may change - that guarantee is what makes the piece honest."""
    from curbside.vision.gemini import MATERIALS, street_render_ladder
    for m in MATERIALS:
        ladder = street_render_ladder(m)
        assert len(ladder) >= 2
        for _, prompt in ladder:
            low = prompt.lower()
            assert "driveway" in low
            assert "lawn" in low, f"{m[0]} lost the lawn exclusion"
        bold = ladder[0][1].lower()
        assert "do not touch anything else" in bold
        assert m[1].split(",")[0].lower() in bold, "material must reach the prompt"


def test_box_coordinates_survive_all_three_conventions():
    """The model answers in normalized 0-1, raw pixels, or Gemini's 0-1000
    grid, and the box does not say which. Reading a grid value as a pixel puts
    it outside the frame, which empties the box, drops the driveway prior, and
    - with require_prior on - rejects a lead whose driveway was found
    correctly. Three leads failed this way in production."""
    from curbside.render.segmentation import box_to_mask
    size = (640, 640)
    normalized = box_to_mask({"x0": 0.0, "y0": 0.75, "x1": 0.77, "y1": 1.0}, size)
    grid = box_to_mask({"x0": 0.0, "y0": 749, "x1": 767, "y1": 997}, size)
    pixels = box_to_mask({"x0": 0, "y0": 480, "x1": 491, "y1": 638}, size)

    for name, m in (("grid", grid), ("pixels", pixels)):
        assert m.mean() > 0.1, f"{name} box collapsed to nothing"
        assert abs(m.mean() - normalized.mean()) < 0.02, \
            f"{name} disagrees with the normalized reading"


def test_a_box_mixing_conventions_still_resolves():
    """'x1': 1.0 beside 'y0': 743 is a real response."""
    from curbside.render.segmentation import box_to_mask
    m = box_to_mask({"x0": 0.0, "y0": 743, "x1": 1.0, "y1": 1.0}, (640, 640))
    assert 0.15 < m.mean() < 0.40
