"""Sales-source adapters. Network tests are marked and skippable."""
import pytest

from curbside.sources.sales import (SoldProperty, ConnecticutSales,
                                    get_source, SOURCES)


def test_get_source_rejects_unknown():
    with pytest.raises(ValueError, match="unknown sales source"):
        get_source("atlantis")


def test_registered_sources_declare_licence_and_states():
    for key, cls in SOURCES.items():
        s = cls()
        assert s.license and s.license != "unknown", f"{key} has no licence"
        assert s.states, f"{key} declares no states"
        assert s.requires_key is False, f"{key} should be keyless"


def test_full_address_formats_for_geocoding():
    p = SoldProperty(address="90 HADDAD RD", town="Waterbury", state="CT",
                     sale_date="2025-09-30")
    assert p.full_address() == "90 HADDAD RD, Waterbury, CT"
    assert p.full_address("06704") == "90 HADDAD RD, Waterbury, CT 06704"


class _FakeCT(ConnecticutSales):
    """Exercises parsing without touching the network."""
    def __init__(self, rows, newest="2025-09-30T00:00:00.000"):
        self._rows, self._newest = rows, newest

    def _get(self, params, timeout=60):
        if "max(daterecorded)" in params.get("$select", ""):
            return [{"newest": self._newest}]
        return self._rows


def test_parses_rows_into_sold_properties():
    src = _FakeCT([{
        "daterecorded": "2025-09-30T00:00:00.000",
        "town": "Waterbury", "address": "90 HADDAD RD",
        "saleamount": "435000", "residentialtype": "Single Family",
        "geo_coordinates": {"coordinates": [-73.07742, 41.5666]},
    }])
    (p,) = src.recent_sales()
    assert p.address == "90 HADDAD RD"
    assert p.sale_date == "2025-09-30"
    assert p.sale_price == 435000.0
    assert (round(p.lat, 4), round(p.lon, 4)) == (41.5666, -73.0774)
    assert p.source == "connecticut"


def test_rows_without_address_or_town_are_dropped():
    src = _FakeCT([
        {"daterecorded": "2025-09-30", "town": "", "address": "1 A St"},
        {"daterecorded": "2025-09-30", "town": "Hartford", "address": "  "},
        {"daterecorded": "2025-09-30", "town": "Hartford", "address": "2 B St"},
    ])
    out = src.recent_sales()
    assert [p.address for p in out] == ["2 B St"]


def test_missing_coordinates_are_tolerated():
    src = _FakeCT([{"daterecorded": "2025-09-30", "town": "Hartford",
                    "address": "1 A St", "saleamount": None}])
    (p,) = src.recent_sales()
    assert p.lat is None and p.sale_price is None


def test_window_is_anchored_to_the_dataset_not_today():
    """State portals publish on a lag, so 'last 12 months from now' can return
    nothing. The window must be relative to the newest available record."""
    captured = {}

    class Capture(_FakeCT):
        def _get(self, params, timeout=60):
            if "max(daterecorded)" in params.get("$select", ""):
                return [{"newest": self._newest}]
            captured["where"] = params["$where"]
            return []

    Capture([], newest="2025-09-30T00:00:00.000").recent_sales(months=12)
    assert "2024-10-0" in captured["where"] or "2024-09-3" in captured["where"]


@pytest.mark.network
def test_connecticut_endpoint_is_live():
    src = ConnecticutSales()
    newest = src.latest_sale_date()
    assert newest and len(newest) == 10
    rows = src.recent_sales(months=12, limit=3)
    assert rows and all(r.state == "CT" for r in rows)


# --------------------------------------------------------------- Wake County

from curbside.sources.sales import WakeCountySales, _epoch_ms_to_date, _polygon_centroid


def test_epoch_ms_converts_to_iso_date():
    assert _epoch_ms_to_date(1788134400000) == "2026-08-31"
    assert _epoch_ms_to_date(1756598400000) == "2025-08-31"
    assert _epoch_ms_to_date(None) is None
    assert _epoch_ms_to_date(0) is None


def test_polygon_centroid_averages_ring_vertices():
    geom = {"rings": [[[-78.5, 35.8], [-78.4, 35.8], [-78.4, 35.9], [-78.5, 35.9]]]}
    lat, lon = _polygon_centroid(geom)
    assert round(lat, 3) == 35.85 and round(lon, 3) == -78.45


def test_polygon_centroid_tolerates_missing_geometry():
    assert _polygon_centroid(None) == (None, None)
    assert _polygon_centroid({}) == (None, None)
    assert _polygon_centroid({"rings": []}) == (None, None)


def test_where_clause_bounds_both_ends_of_the_date_range():
    """Assessor data carries typo'd future dates (2034, 2029 seen in the wild),
    so an upper bound is mandatory, not optional."""
    w = WakeCountySales()._where(months=6)
    assert "SALE_DATE >=" in w and "SALE_DATE <=" in w


def test_where_clause_excludes_vacant_land():
    """YEAR_BUILT > 0 filters parcels with no structure - and no driveway."""
    assert "YEAR_BUILT > 0" in WakeCountySales()._where(months=6)


def test_max_year_built_filters_new_construction():
    """Recent sales skew to new builds whose driveways are already new."""
    w = WakeCountySales()._where(months=6, max_year_built=2005)
    assert "YEAR_BUILT <= 2005" in w


def test_price_bounds_are_applied():
    w = WakeCountySales()._where(months=6, min_price=200000, max_price=700000)
    assert "TOTSALPRICE > 200000" in w and "TOTSALPRICE <= 700000" in w


def test_sources_declare_commercial_use_posture():
    """Only sources with an explicit grant may claim commercial use."""
    from curbside.sources.sales import SOURCES
    for key, cls in SOURCES.items():
        assert cls().commercial_use in ("granted", "unstated",
                                        "unstated - review before commercial use")


@pytest.mark.network
def test_wake_endpoint_is_live_and_recent():
    import datetime as dt
    w = WakeCountySales()
    newest = w.latest_sale_date()
    assert newest
    age = (dt.date.today() - dt.date.fromisoformat(newest)).days
    assert age < 90, f"Wake data unexpectedly stale: {age} days"


@pytest.mark.network
def test_wake_returns_addresses_with_coordinates():
    rows = WakeCountySales().recent_sales(months=6, limit=3, min_price=150000)
    assert rows
    assert all(r.address and r.state == "NC" for r in rows)
    assert any(r.lat and r.lon for r in rows), "expected parcel centroids"
