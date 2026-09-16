"""Parcel lookup — exact property coordinates from county land records.

Why this exists. Aiming a Street View camera needs to know where the house
actually is. Google's geocoder often answers RANGE_INTERPOLATED: it estimates
a position along the street from the house numbers either side. Measured on a
sample of eight addresses, half came back interpolated, one placing the target
42 metres from the camera — far enough to photograph a neighbour.

Aerial imagery tolerates that; a 20 m error still lands on the right block and
you are looking down at a wide area. From the road, 20 m sideways is a
different house entirely.

County parcel records carry the surveyed property boundary. Its centroid is a
true rooftop position, which is exactly what the camera needs. Measured effect:
ROOFTOP-grade coordinates went from 4/8 to 8/8 on the same addresses.
"""
import dataclasses
import json
import math
import urllib.error
import urllib.parse
import urllib.request


@dataclasses.dataclass(frozen=True)
class ParcelSource:
    key: str
    name: str
    query_url: str
    address_field: str
    #: Extra attribute fields worth carrying back with the match.
    detail_fields: tuple = ()
    states: tuple = ()


SOURCES = {
    "miami_dade": ParcelSource(
        key="miami_dade",
        name="Miami-Dade Land Information",
        query_url=("https://gisweb.miamidade.gov/arcgis/rest/services/"
                   "MD_LandInformation/MapServer/26/query"),
        address_field="TRUE_SITE_ADDR",
        detail_fields=("TRUE_SITE_CITY", "TRUE_SITE_ZIP_CODE"),
        states=("FL",),
    ),
    "palm_beach": ParcelSource(
        key="palm_beach",
        name="Palm Beach County Parcels",
        query_url=("https://services1.arcgis.com/ZWOoUZbtaYePLlPw/arcgis/rest/"
                   "services/Parcels_and_Property_Details_WebMercator/"
                   "FeatureServer/0/query"),
        address_field="SITE_ADDR_STR",
        detail_fields=("CITYNAME", "ZIP1", "YRBLT"),
        states=("FL",),
    ),
}


def get_source(key):
    if key not in SOURCES:
        raise ValueError(f"unknown parcel source {key!r}; have {sorted(SOURCES)}")
    return SOURCES[key]


def _query(url, params, timeout=60):
    full = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(full, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    if "error" in d:
        raise RuntimeError(f"parcel query: {d['error'].get('message')}")
    return d


def _centroid(geometry):
    """Average vertex of the parcel's outer ring.

    Adequate for aiming a camera at a suburban lot. A true area-weighted
    centroid would matter for an L-shaped parcel, but the error here is metres
    where the geocoder's is tens of metres.
    """
    rings = (geometry or {}).get("rings") or []
    if not rings or not rings[0]:
        return None, None
    pts = [p for p in rings[0] if len(p) >= 2]
    if not pts:
        return None, None
    return (sum(p[1] for p in pts) / len(pts),
            sum(p[0] for p in pts) / len(pts))


def _haversine_m(lat1, lon1, lat2, lon2):
    r = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * r * math.asin(math.sqrt(a))


def _normalise(addr):
    """County records store the street line only, uppercased, without the
    unit, city or postcode a user would type."""
    line = addr.split(",")[0].strip().upper()
    # Records use "AVE" not "AVENUE", and rarely carry a trailing period.
    return line.rstrip(".")


def locate(address, source_key, timeout=60, hint=None, max_hint_m=400):
    """Address -> (lat, lon, detail, error) from county parcel records.

    Matching is exact on the street line first, then a LIKE prefix, because
    county spelling of suffixes is inconsistent.

    One address can map to many parcels - condos and multi-unit buildings
    carry the same street line on every unit. Measured: "1801 BRANDYWINE RD"
    returns ten parcels, one of them 1.3 km from the rest. Passing `hint`
    (a rough lat/lon, e.g. from the geocoder) picks the nearest match and
    rejects anything further than `max_hint_m`; without a hint, ambiguous
    results are refused rather than guessed at.
    """
    src = get_source(source_key)
    street = _normalise(address)
    if not street:
        return None, None, None, "empty address"

    fields = ",".join((src.address_field,) + src.detail_fields)
    escaped = street.replace("'", "''")

    for where in (f"{src.address_field} = '{escaped}'",
                  f"{src.address_field} LIKE '{escaped}%'"):
        try:
            d = _query(src.query_url, {
                "where": where, "outFields": fields,
                "returnGeometry": "true", "outSR": 4326,
                "resultRecordCount": 12, "f": "json",
            }, timeout=timeout)
        except urllib.error.HTTPError as e:
            return None, None, None, f"parcel service HTTP {e.code}"
        except Exception as e:
            return None, None, None, f"parcel lookup failed: {type(e).__name__}"

        feats = d.get("features") or []
        if not feats:
            continue

        # Resolve each candidate to a centroid before choosing.
        cands = []
        for f in feats:
            lat, lon = _centroid(f.get("geometry"))
            if lat is not None:
                cands.append((lat, lon, f.get("attributes") or {}))
        if not cands:
            continue

        if len(cands) > 1:
            if hint is None:
                return (None, None, None,
                        f"{len(cands)} parcels share {street!r} - pass a hint "
                        "to disambiguate")
            hlat, hlon = hint
            cands.sort(key=lambda c: _haversine_m(hlat, hlon, c[0], c[1]))
            best_m = _haversine_m(hlat, hlon, cands[0][0], cands[0][1])
            if best_m > max_hint_m:
                return (None, None, None,
                        f"nearest of {len(cands)} parcels for {street!r} is "
                        f"{best_m:.0f}m from the hint")

        lat, lon, attrs = cands[0]
        detail = {k: v for k, v in attrs.items() if v not in (None, "")}
        detail["precision"] = "PARCEL_CENTROID"
        detail["source"] = src.key
        detail["candidates"] = len(cands)
        return lat, lon, detail, None

    return None, None, None, f"no parcel matching {street!r}"


def source_for_state(state):
    """Parcel sources covering a state, if any."""
    return [k for k, v in SOURCES.items() if state.upper() in v.states]
