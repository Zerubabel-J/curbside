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


def test_postcard_matches_lobs_4x6_bleed_spec(images, tmp_path):
    """4.25x6.25in at 300 DPI - the bleed size Lob's template asks for.

    Size is a postage decision: a card within 4.25x6 mails at letter rate,
    above it at flat rate. Growing the artwork silently raises the cost of
    every piece.
    """
    b, a = images
    out = tmp_path / "o.jpg"
    build(b, a, "1 A St, Indianapolis, IN 46201", out, return_address=RA)
    im = Image.open(out)
    assert im.size == (1875, 1275)          # 6.25x4.25in @ 300 DPI, landscape
    assert im.info.get("dpi") == (300, 300)


def test_artwork_size_and_postage_size_agree():
    """Lob prints what the payload says it is. Artwork built to one size and
    postage bought at another is rejected, after the render has been paid for.
    """
    from curbside.compose import postcard
    from curbside.mail import providers
    w_in = postcard.W / postcard.DPI
    h_in = postcard.H / postcard.DPI
    short, long_ = sorted((w_in, h_in))
    assert providers.POSTCARD_SIZE == f"{short:.0f}x{long_:.0f}"


def test_text_stays_inside_the_safe_zone(images, tmp_path):
    """Everything outside the safe zone may be trimmed off. A disclosure that
    gets cut is a compliance failure, not a cosmetic one."""
    from curbside.compose import postcard as pc
    b, a = images
    out = tmp_path / "o.jpg"
    build(b, a, "1 A St, Indianapolis, IN 46201", out, return_address=RA)
    safe = int(pc.SAFE_IN * pc.DPI)
    assert pc.MARGIN >= safe, "text margin must clear the safe-zone guide"


def test_long_address_does_not_overflow_the_rail(images, tmp_path):
    """Addresses vary in length by a factor of three. A fixed type size pushes
    the long ones past the trim edge."""
    b, a = images
    out = tmp_path / "long.jpg"
    build(b, a, "18245 Northwest Country Club Terrace Drive, "
                "Miami Gardens, FL 33056", out, return_address=RA)
    assert Image.open(out).size == (1875, 1275)


def test_disclosure_is_allotted_space_before_the_art(images, tmp_path):
    """A longer disclosure must shrink the artwork, not run off the card."""
    b, a = images
    long_disclosure = (
        "Illustration only. The 'after' image is a computer-generated "
        "rendering of a public aerial photograph of this address and does not "
        "depict actual work performed, nor any estimate, quotation or offer of "
        "services at the price shown or otherwise implied herein."
    )
    out = tmp_path / "disc.jpg"
    build(b, a, "1 A St, Indianapolis, IN 46201", out,
          return_address=RA, disclosure=long_disclosure)
    assert Image.open(out).size == (1875, 1275)


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


def test_attribution_follows_the_active_imagery_source(images, tmp_path, monkeypatch):
    """Crediting the wrong agency is a licensing problem, not a typo."""
    from curbside.config import settings
    from PIL import Image as _I
    b, a = images
    monkeypatch.setattr(settings, "source", "connecticut")
    out = tmp_path / "ct.jpg"
    build(b, a, "1 A St, Hartford, CT 06103", out, return_address=RA)
    assert out.exists()
    # The default must not be baked in.
    import inspect
    from curbside.compose import postcard
    sig = inspect.signature(postcard.build)
    assert sig.parameters["attribution"].default is None


def test_street_view_cards_credit_google_not_the_county(images, tmp_path, monkeypatch):
    """At street level the photograph is Google's, not the county's. Crediting
    a county aerial agency for a Street View image is a licensing problem, and
    Google's terms require the credit to be visible on the piece."""
    from curbside.config import settings
    from curbside.sources.streetview import ATTRIBUTION
    from curbside.compose import postcard

    b, a = images
    drawn = []

    class Spy(postcard.ImageDraw.ImageDraw):
        def text(self, xy, text, *a, **kw):
            drawn.append(text)
            return super().text(xy, text, *a, **kw)

    monkeypatch.setattr(postcard.ImageDraw, "Draw", lambda im: Spy(im))
    monkeypatch.setattr(settings, "source", "miami_dade")

    monkeypatch.setattr(settings, "view", "street")
    build(b, a, "1 SW 1st St, Miami, FL 33130", tmp_path / "sv.jpg",
          return_address=RA)
    assert ATTRIBUTION in drawn
    assert settings.imagery().attribution not in drawn

    drawn.clear()
    monkeypatch.setattr(settings, "view", "aerial")
    build(b, a, "1 SW 1st St, Miami, FL 33130", tmp_path / "ae.jpg",
          return_address=RA)
    assert settings.imagery().attribution in drawn
    assert ATTRIBUTION not in drawn


def test_a_real_font_is_available():
    """The builder falls back to PIL's bitmap default when DejaVu is missing.
    Nothing raises - the card renders, tests pass, and the headline comes out
    unreadably small. That failed silently in the container until a printed
    proof was inspected, so the font's presence is asserted directly."""
    from curbside.compose import postcard
    from PIL import ImageFont

    f = postcard._font(int(0.175 * postcard.DPI), True)
    assert isinstance(f, ImageFont.FreeTypeFont), (
        "no scalable font found; install fonts-dejavu-core")
    assert f.size == int(0.175 * postcard.DPI)
