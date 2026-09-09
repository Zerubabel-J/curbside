"""Block scan — given one address, find the homes around it.

The product hook: a contractor types the address of a job they're already
working on, and we surface every neighbour worth mailing. No list to upload,
no CSV, no setup.

Two strategies:
  1. parcels  — query the county parcel layer for properties whose centroid
                falls within a radius. Exact, gives real addresses.
  2. grid     — fall back to sampling points around the origin and reverse
                geocoding them. Works anywhere, less precise.
"""
import json
import math
import urllib.parse
import urllib.request

from curbside.sources.sales import SoldProperty, _polygon_centroid

WAKE_PARCELS = ("https://maps.wake.gov/arcgis/rest/services/Property/"
                "Parcels/FeatureServer/0/query")

# Statewide Indiana parcels - pairs with Indiana's 3in CC0 imagery, which is
# sharp enough to actually assess a driveway.
INDIANA_PARCELS = ("https://gisdata.in.gov/server/rest/services/Hosted/"
                   "Parcel_Boundaries_of_Indiana_Current/FeatureServer/0/query")


def _meters_to_degrees(meters, lat):
    """Rough conversion, good enough for a few hundred metres."""
    dlat = meters / 111_320.0
    dlon = meters / (111_320.0 * max(math.cos(math.radians(lat)), 0.01))
    return dlat, dlon


def _haversine_m(lat1, lon1, lat2, lon2):
    r = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * r * math.asin(math.sqrt(a))


def scan_wake_parcels(lat, lon, radius_m=250, limit=12, exclude_new_after=None,
                      sold_within_months=None):
    """Homes near a point, from the Wake County parcel layer.

    Optionally narrows to homes sold recently - the original targeting idea,
    on the theory that a new owner still has budget for exterior work.
    """
    import datetime as _date
    dlat, dlon = _meters_to_degrees(radius_m, lat)
    envelope = f"{lon-dlon},{lat-dlat},{lon+dlon},{lat+dlat}"

    where = ["YEAR_BUILT > 0"]
    if exclude_new_after:
        where.append(f"YEAR_BUILT <= {int(exclude_new_after)}")
    if sold_within_months:
        today = _date.date.today()
        cutoff = today - _date.timedelta(days=int(sold_within_months * 30.44))
        # Upper bound matters: assessor data carries typo'd future dates.
        where.append(f"SALE_DATE >= DATE '{cutoff}'")
        where.append(f"SALE_DATE <= DATE '{today}'")

    params = {
        "geometry": envelope,
        "geometryType": "esriGeometryEnvelope",
        "inSR": 4326, "outSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "where": " AND ".join(where),
        "outFields": "SITE_ADDRESS,YEAR_BUILT,TOTSALPRICE,SALE_DATE",
        "returnGeometry": "true",
        "resultRecordCount": limit * 4,     # over-fetch, then rank by distance
        "f": "json",
    }
    url = f"{WAKE_PARCELS}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        d = json.load(r)
    if "error" in d:
        raise RuntimeError(f"block scan: {d['error'].get('message')}")

    seen, out = set(), []
    for f in d.get("features", []):
        a = f.get("attributes", {})
        addr = (a.get("SITE_ADDRESS") or "").strip()
        if not addr or addr in seen:
            continue
        plat, plon = _polygon_centroid(f.get("geometry"))
        if plat is None:
            continue
        dist = _haversine_m(lat, lon, plat, plon)
        if dist > radius_m:
            continue
        seen.add(addr)
        out.append((dist, SoldProperty(
            address=addr, town="", state="NC",
            sale_date="", sale_price=a.get("TOTSALPRICE"),
            lat=plat, lon=plon,
            property_type=f"built {a.get('YEAR_BUILT')}" if a.get("YEAR_BUILT") else None,
            source="block_scan",
        )))

    out.sort(key=lambda t: t[0])
    return [p for _, p in out[:limit]]


#: Sources whose parcel layer carries a sale date. Indiana's does not, so a
#: recency filter there would silently return everything.
SUPPORTS_SALE_DATE = {"NC"}


def scan_indiana_parcels(lat, lon, radius_m=250, limit=12, **_):
    """Homes near a point, from the Indiana statewide parcel layer.

    No sale date in this layer - recency filtering is unavailable.
    """
    dlat, dlon = _meters_to_degrees(radius_m, lat)
    params = {
        "geometry": f"{lon-dlon},{lat-dlat},{lon+dlon},{lat+dlat}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": 4326, "outSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "where": "1=1",
        "outFields": "prop_add,prop_city,prop_zip,latitude,longitude",
        "returnGeometry": "false",
        "resultRecordCount": limit * 5,
        "f": "json",
    }
    url = f"{INDIANA_PARCELS}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        d = json.load(r)
    if "error" in d:
        raise RuntimeError(f"block scan: {d['error'].get('message')}")

    seen, out = set(), []
    for f in d.get("features", []):
        a = f.get("attributes", {})
        addr = (a.get("prop_add") or "").strip()
        plat, plon = a.get("latitude"), a.get("longitude")
        if not addr or plat is None or plon is None or addr in seen:
            continue
        dist = _haversine_m(lat, lon, float(plat), float(plon))
        if dist > radius_m:
            continue
        seen.add(addr)
        out.append((dist, SoldProperty(
            address=addr, town=(a.get("prop_city") or "").strip(), state="IN",
            sale_date="", sale_price=None,
            lat=float(plat), lon=float(plon),
            source="block_scan",
        )))
    out.sort(key=lambda t: t[0])
    return [p for _, p in out[:limit]]


SCANNERS = {"NC": scan_wake_parcels, "IN": scan_indiana_parcels}


def scan(address_or_point, radius_m=250, limit=12, exclude_new_after=None,
         state=None, sold_within_months=None):
    """Entry point. Accepts an address string or a (lat, lon) tuple.

    Returns (origin_label, [SoldProperty, ...]).
    """
    if isinstance(address_or_point, (tuple, list)):
        lat, lon = address_or_point
        label = f"{lat:.5f}, {lon:.5f}"
    else:
        from curbside.sources.geocode import geocode
        lat, lon, prec, err = geocode(address_or_point)
        if lat is None:
            raise RuntimeError(f"could not locate {address_or_point!r}: {err}")
        label = address_or_point

    # Pick the parcel layer that matches the configured imagery source.
    if state is None:
        from curbside.config import settings
        state = (settings.imagery().states or ("IN",))[0]
    state = state.upper()
    scanner = SCANNERS.get(state, scan_indiana_parcels)
    kw = {"radius_m": radius_m, "limit": limit,
          "exclude_new_after": exclude_new_after}
    if sold_within_months and state in SUPPORTS_SALE_DATE:
        kw["sold_within_months"] = sold_within_months
    return label, scanner(lat, lon, **kw)
