import numpy as np
import pytest
from PIL import Image

from curbside.compose.postcard import build, focus_from_mask
from curbside.compliance.policy import ComplianceError

RA = "Heartland Driveway Co., 1 Main St, Indianapolis, IN 46201"


@pytest.fixture
def images(tmp_path):
    a = np.full((256, 256, 3), 120, dtype=np.uint8)
    b = a.copy(); b[100:200, 100:200] = 220
    p1, p2 = tmp_path / "b.jpg", tmp_path / "a.jpg"
    Image.fromarray(a).save(p1); Image.fromarray(b).save(p2)
    return p1, p2


def test_postcard_requires_return_address(images, tmp_path):
    b, a = images
    with pytest.raises(ValueError, match="return_address"):
        build(b, a, "1 A St, Indianapolis, IN 46201", tmp_path / "o.jpg")


def test_postcard_rejects_weak_disclosure(images, tmp_path):
    b, a = images
    with pytest.raises(ComplianceError):
        build(b, a, "1 A St, Indianapolis, IN 46201", tmp_path / "o.jpg",
              return_address=RA, disclosure="Call now!")


def test_postcard_is_print_resolution(images, tmp_path):
    b, a = images
    out = tmp_path / "o.jpg"
    build(b, a, "1 A St, Indianapolis, IN 46201", out, return_address=RA)
    im = Image.open(out)
    assert im.size == (2700, 1800)          # 9x6in @ 300 DPI
    assert im.info.get("dpi") == (300, 300)


def test_focus_from_mask_finds_tinted_centroid(tmp_path):
    base = np.full((200, 200, 3), 100, dtype=np.uint8)
    tinted = base.copy()
    tinted[20:60, 20:60, 0] = 255           # red blob, upper-left
    p1, p2 = tmp_path / "b.jpg", tmp_path / "m.jpg"
    Image.fromarray(base).save(p1, quality=99)
    Image.fromarray(tinted).save(p2, quality=99)
    fx, fy = focus_from_mask(p2, p1)
    assert 0.1 < fx < 0.35 and 0.1 < fy < 0.35


def test_focus_returns_none_without_a_mask(tmp_path):
    base = np.full((100, 100, 3), 100, dtype=np.uint8)
    p1, p2 = tmp_path / "b.jpg", tmp_path / "m.jpg"
    Image.fromarray(base).save(p1); Image.fromarray(base).save(p2)
    assert focus_from_mask(p2, p1) is None
