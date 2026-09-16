"""Parcel lookup: exact property coordinates from county land records."""
import pytest

from curbside.sources import parcels
from curbside.sources.parcels import _centroid, _normalise, _haversine_m, SOURCES


def test_unknown_source_rejected():
    with pytest.raises(ValueError, match="unknown parcel source"):
        parcels.get_source("atlantis")


def test_sources_cover_the_target_market():
    """Palm Beach and Miami-Dade are two of John's three target counties.
    Broward is excluded - its imagery terms require written permission."""
    assert set(SOURCES) == {"miami_dade", "palm_beach"}
    assert all(s.states == ("FL",) for s in SOURCES.values())


def test_centroid_averages_ring_vertices():
    geom = {"rings": [[[-80.5, 25.7], [-80.4, 25.7], [-80.4, 25.8], [-80.5, 25.8]]]}
    lat, lon = _centroid(geom)
    assert round(lat, 3) == 25.75 and round(lon, 3) == -80.45


def test_centroid_tolerates_missing_geometry():
    assert _centroid(None) == (None, None)
    assert _centroid({}) == (None, None)
    assert _centroid({"rings": []}) == (None, None)


def test_normalise_strips_city_and_postcode():
    """County records store the street line only, uppercased."""
    assert _normalise("1731 SW 103 Ave, Miami, FL 33165") == "1731 SW 103 AVE"
    assert _normalise("  1009 7th St.  ") == "1009 7TH ST"


def _fake_features(coords_list, addr="1801 BRANDYWINE RD"):
    return {"features": [
        {"attributes": {"SITE_ADDR_STR": addr},
         "geometry": {"rings": [[[lon, lat], [lon+0.0001, lat],
                                 [lon+0.0001, lat+0.0001], [lon, lat+0.0001]]]}}
        for lat, lon in coords_list]}


def test_single_match_resolves(monkeypatch):
    monkeypatch.setattr(parcels, "_query",
                        lambda *a, **k: _fake_features([(26.70, -80.06)]))
    lat, lon, detail, err = parcels.locate("1801 Brandywine Rd", "palm_beach")
    assert err is None
    assert detail["precision"] == "PARCEL_CENTROID"
    assert detail["candidates"] == 1


def test_ambiguous_address_refused_without_a_hint(monkeypatch):
    """Condos put the same street line on every unit. Measured: one Palm Beach
    address returns twelve parcels, one of them 1.3 km from the rest. Guessing
    would aim the camera at a stranger's house."""
    monkeypatch.setattr(parcels, "_query", lambda *a, **k: _fake_features(
        [(26.70, -80.06), (26.71, -80.07), (26.72, -80.08)]))
    lat, lon, detail, err = parcels.locate("1801 Brandywine Rd", "palm_beach")
    assert lat is None
    assert "3 parcels share" in err


def test_hint_picks_the_nearest_of_several(monkeypatch):
    monkeypatch.setattr(parcels, "_query", lambda *a, **k: _fake_features(
        [(26.80, -80.20), (26.70, -80.06), (26.90, -80.30)]))
    lat, lon, detail, err = parcels.locate(
        "1801 Brandywine Rd", "palm_beach", hint=(26.7001, -80.0601))
    assert err is None
    assert round(lat, 2) == 26.70
    assert detail["candidates"] == 3


def test_hint_too_far_is_refused(monkeypatch):
    """If every candidate is far from the hint, the address did not really
    match - better to skip the lead than photograph the wrong building."""
    monkeypatch.setattr(parcels, "_query", lambda *a, **k: _fake_features(
        [(26.90, -80.40), (26.95, -80.45)]))
    lat, lon, detail, err = parcels.locate(
        "1801 Brandywine Rd", "palm_beach", hint=(26.70, -80.06), max_hint_m=400)
    assert lat is None
    assert "from the hint" in err


def test_no_match_returns_an_error_not_an_exception(monkeypatch):
    monkeypatch.setattr(parcels, "_query", lambda *a, **k: {"features": []})
    lat, lon, detail, err = parcels.locate("9999 Nowhere Rd", "miami_dade")
    assert lat is None and "no parcel matching" in err


def test_service_failure_returns_an_error(monkeypatch):
    def boom(*a, **k): raise RuntimeError("service down")
    monkeypatch.setattr(parcels, "_query", boom)
    lat, lon, detail, err = parcels.locate("1 A St", "miami_dade")
    assert lat is None and "parcel lookup failed" in err


@pytest.mark.network
def test_miami_dade_returns_a_real_centroid():
    lat, lon, detail, err = parcels.locate("1731 SW 103 AVE", "miami_dade")
    assert err is None
    assert 25.0 < lat < 26.5 and -81.0 < lon < -80.0
    assert detail["precision"] == "PARCEL_CENTROID"
