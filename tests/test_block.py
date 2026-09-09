"""Block scan: given one address, find the neighbours."""
import pytest

from curbside.sources.block import (_haversine_m, _meters_to_degrees,
                                    scan_wake_parcels, scan_indiana_parcels,
                                    SCANNERS)


def test_haversine_matches_known_distance():
    # Two points ~111m apart in latitude
    d = _haversine_m(39.9000, -86.0000, 39.9010, -86.0000)
    assert 105 < d < 120


def test_haversine_is_zero_for_identical_points():
    assert _haversine_m(39.9, -86.0, 39.9, -86.0) == pytest.approx(0, abs=1e-6)


def test_meters_to_degrees_widens_longitude_near_the_poles():
    """A degree of longitude covers less ground the further from the equator,
    so the same radius needs a wider degree span."""
    _, dlon_equator = _meters_to_degrees(1000, 0.0)
    _, dlon_north = _meters_to_degrees(1000, 60.0)
    assert dlon_north > dlon_equator * 1.9


def test_scanners_registered_for_supported_states():
    assert set(SCANNERS) == {"NC", "IN"}


@pytest.mark.network
def test_indiana_scan_returns_nearby_addresses():
    homes = scan_indiana_parcels(39.90286, -85.99147, radius_m=200, limit=5)
    assert homes
    assert all(h.state == "IN" and h.address for h in homes)
    assert all(h.lat and h.lon for h in homes)


@pytest.mark.network
def test_scan_respects_the_radius():
    homes = scan_indiana_parcels(39.90286, -85.99147, radius_m=120, limit=20)
    for h in homes:
        assert _haversine_m(39.90286, -85.99147, h.lat, h.lon) <= 120


@pytest.mark.network
def test_wake_scan_returns_nearby_addresses():
    homes = scan_wake_parcels(35.78648, -78.65098, radius_m=200, limit=5)
    assert homes and all(h.state == "NC" for h in homes)


# --------------------------------------------------- recently-sold filter

def test_wake_where_clause_bounds_the_sale_window(monkeypatch):
    """Assessor data carries typo'd future dates, so both ends need bounding."""
    import urllib.request
    from urllib.parse import unquote_plus
    import curbside.sources.block as blk

    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url if hasattr(req, "full_url") else str(req)
        raise RuntimeError("stop here")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError):
        blk.scan_wake_parcels(35.78, -78.65, sold_within_months=12)

    where = unquote_plus(captured["url"])
    assert "SALE_DATE >=" in where
    assert "SALE_DATE <=" in where


def test_wake_omits_sale_clause_when_no_window_given(monkeypatch):
    import urllib.request
    from urllib.parse import unquote_plus
    import curbside.sources.block as blk

    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url if hasattr(req, "full_url") else str(req)
        raise RuntimeError("stop here")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError):
        blk.scan_wake_parcels(35.78, -78.65)

    assert "SALE_DATE >=" not in unquote_plus(captured["url"])


def test_sale_filter_is_only_offered_where_the_layer_supports_it():
    """Indiana's parcel layer has no sale date - applying the filter there
    would silently return everything, which is worse than not offering it."""
    from curbside.sources.block import SUPPORTS_SALE_DATE
    assert "NC" in SUPPORTS_SALE_DATE
    assert "IN" not in SUPPORTS_SALE_DATE


@pytest.mark.network
def test_sale_filter_narrows_the_result_set():
    from curbside.sources.block import scan_wake_parcels
    everyone = scan_wake_parcels(35.78648, -78.65098, radius_m=400, limit=20)
    recent = scan_wake_parcels(35.78648, -78.65098, radius_m=400, limit=20,
                               sold_within_months=12)
    assert len(recent) < len(everyone)
    assert all(h.sale_date for h in recent)
