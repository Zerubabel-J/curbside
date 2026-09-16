"""Street View source: aiming the camera, and failing safely."""
import math
import pytest

from curbside.sources.streetview import bearing, _haversine_m, LICENSE


def test_bearing_cardinal_directions():
    """Due north is 0, due east 90 - the basis for aiming the camera."""
    assert bearing(0, 0, 1, 0) == pytest.approx(0, abs=0.5)
    assert bearing(0, 0, 0, 1) == pytest.approx(90, abs=0.5)
    assert bearing(1, 0, 0, 0) == pytest.approx(180, abs=0.5)
    assert bearing(0, 1, 0, 0) == pytest.approx(270, abs=0.5)


def test_bearing_always_in_compass_range():
    for lat, lon in ((0.5, -0.5), (-0.5, 0.5), (-0.5, -0.5)):
        b = bearing(0, 0, lat, lon)
        assert 0 <= b < 360


def test_bearing_points_camera_at_house():
    """Real values from a verified fetch: camera south-east of the house, so
    the heading should be roughly north-west."""
    b = bearing(39.903046, -85.991302, 39.903111, -85.991561)
    assert 280 < b < 300


def test_haversine_matches_known_distance():
    d = _haversine_m(39.903046, -85.991302, 39.903111, -85.991561)
    assert 20 < d < 30


def test_licence_constraint_is_recorded_not_assumed():
    """Google prohibits Street View in print. The constraint travels with the
    source so it cannot be quietly forgotten downstream."""
    assert "prototype" in LICENSE.lower()
    assert "not licensed for print" in LICENSE.lower()


def test_missing_key_raises_clearly(monkeypatch):
    from curbside.sources import streetview
    monkeypatch.delenv("GOOGLE_STREETVIEW_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GOOGLE_STREETVIEW_API_KEY"):
        streetview._key()


def test_fetch_returns_error_rather_than_raising(monkeypatch, tmp_path):
    """One unreachable address must cost one lead, not the whole scan."""
    from curbside.sources import streetview
    monkeypatch.setenv("GOOGLE_STREETVIEW_API_KEY", "x")
    monkeypatch.setattr(streetview, "locate",
                        lambda a, key=None: (None, None, None, "geocode: ZERO_RESULTS"))
    ok, msg = streetview.fetch("nowhere", tmp_path / "a.jpg")
    assert ok is False and "ZERO_RESULTS" in msg


def test_blank_tile_is_detected(monkeypatch, tmp_path):
    """Google returns a small grey tile rather than an error when a panorama
    exists but shows nothing."""
    import urllib.request
    from curbside.sources import streetview

    class R:
        def read(self): return b"x" * 900
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setenv("GOOGLE_STREETVIEW_API_KEY", "x")
    monkeypatch.setattr(streetview, "locate", lambda a, key=None: (39.9, -86.1, "ROOFTOP", None))
    monkeypatch.setattr(streetview, "coverage",
                        lambda **kw: ({"location": {"lat": 39.9, "lng": -86.1},
                                       "pano_id": "p", "date": "2024-01"}, None))
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: R())
    ok, msg = streetview.fetch("somewhere", tmp_path / "a.jpg")
    assert ok is False and "blank" in msg


# ------------------------------------------- parcel-based camera aiming

def test_parcel_source_overrides_the_geocoder(monkeypatch, tmp_path):
    """Aiming is only as good as the target coordinate. Where parcel records
    resolve the property, they replace the geocoder's estimate."""
    import urllib.request
    from curbside.sources import streetview, parcels

    class R:
        def read(self): return b"x" * 40000
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setenv("GOOGLE_STREETVIEW_API_KEY", "x")
    monkeypatch.setattr(streetview, "locate",
                        lambda a, key=None: (25.0, -80.0, "RANGE_INTERPOLATED", None))
    monkeypatch.setattr(parcels, "locate",
                        lambda a, s, hint=None: (25.5, -80.5,
                                                 {"precision": "PARCEL_CENTROID"}, None))
    monkeypatch.setattr(streetview, "coverage",
                        lambda **kw: ({"location": {"lat": 25.5, "lng": -80.49},
                                       "pano_id": "p", "date": "2024-01"}, None))
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: R())

    ok, meta = streetview.fetch("1 A St", tmp_path / "a.jpg",
                                parcel_source="miami_dade")
    assert ok
    assert meta["geocode_precision"] == "PARCEL_CENTROID"


def test_geocoder_used_when_parcel_lookup_fails(monkeypatch, tmp_path):
    """A missing parcel record should degrade to the geocoder, not lose the
    lead outright."""
    import urllib.request
    from curbside.sources import streetview, parcels

    class R:
        def read(self): return b"x" * 40000
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setenv("GOOGLE_STREETVIEW_API_KEY", "x")
    monkeypatch.setattr(streetview, "locate",
                        lambda a, key=None: (25.0, -80.0, "ROOFTOP", None))
    monkeypatch.setattr(parcels, "locate",
                        lambda a, s, hint=None: (None, None, None, "no parcel matching"))
    monkeypatch.setattr(streetview, "coverage",
                        lambda **kw: ({"location": {"lat": 25.0, "lng": -80.01},
                                       "pano_id": "p", "date": "2024-01"}, None))
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: R())

    ok, meta = streetview.fetch("1 A St", tmp_path / "a.jpg",
                                parcel_source="miami_dade")
    assert ok
    assert meta["geocode_precision"] == "ROOFTOP"


def test_both_lookups_failing_returns_an_error(monkeypatch, tmp_path):
    from curbside.sources import streetview, parcels
    monkeypatch.setenv("GOOGLE_STREETVIEW_API_KEY", "x")
    monkeypatch.setattr(streetview, "locate",
                        lambda a, key=None: (None, None, None, "geocode: ZERO_RESULTS"))
    monkeypatch.setattr(parcels, "locate",
                        lambda a, s, hint=None: (None, None, None, "no parcel matching"))
    ok, msg = streetview.fetch("nowhere", tmp_path / "a.jpg",
                               parcel_source="miami_dade")
    assert ok is False
    assert "ZERO_RESULTS" in msg and "parcel" in msg
