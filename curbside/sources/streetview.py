"""Google Street View Static imagery — front-of-house photographs.

Aerial imagery shows the driveway plainly but not the house a person
recognises. Street View shows the front elevation, which is what makes a
recipient stop at "that's my house".

LICENSING. Google's Geo Guidelines prohibit Street View imagery in print
advertising, and Google grants no exceptions. This module exists for
prototyping and evaluation only. A commercial campaign needs either
commissioned drive-by capture (clean under 17 U.S.C. 120(a), roughly
$30-100/property) or a negotiated licence from a vendor that owns its own
street-level imagery. `LICENSE` below is reported through the pipeline so the
constraint travels with the data rather than living in someone's memory.

Aiming the camera is the hard part. The default view faces along the road, so
a request by address alone usually returns the street rather than the house.
We resolve the house separately, read the camera position from the free
metadata endpoint, and compute the bearing between them.
"""
import json
import math
import os
import pathlib
import urllib.error
import urllib.parse
import urllib.request

METADATA = "https://maps.googleapis.com/maps/api/streetview/metadata"
IMAGE = "https://maps.googleapis.com/maps/api/streetview"
GEOCODE = "https://maps.googleapis.com/maps/api/geocode/json"

LICENSE = "Google Street View - prototype use only, not licensed for print"
ATTRIBUTION = "Imagery © Google"

#: Google's own maximum for the free Static API.
MAX_SIZE = 640


def _key(name="GOOGLE_STREETVIEW_API_KEY"):
    k = os.environ.get(name, "").strip()
    if not k:
        raise RuntimeError(f"{name} not set")
    return k


def _get_json(url, timeout=45):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def bearing(lat1, lon1, lat2, lon2):
    """Compass heading in degrees from one point toward another."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _haversine_m(lat1, lon1, lat2, lon2):
    r = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * r * math.asin(math.sqrt(a))


def locate(address, key=None):
    """Resolve an address to coordinates plus Google's precision claim.

    `location_type` matters: ROOFTOP means Google knows the building, while
    RANGE_INTERPOLATED means it estimated a position along the street. An
    interpolated point can sit tens of metres off, which aims the camera at a
    neighbour.
    """
    key = key or _key("GOOGLE_GEOCODING_API_KEY")
    d = _get_json(f"{GEOCODE}?" + urllib.parse.urlencode(
        {"address": address, "key": key}))
    if d.get("status") != "OK" or not d.get("results"):
        return None, None, None, f"geocode: {d.get('status')}"
    r = d["results"][0]
    loc = r["geometry"]["location"]
    return (loc["lat"], loc["lng"],
            r["geometry"].get("location_type"), None)


def coverage(address=None, latlng=None, key=None, radius=60):
    """Is there a panorama near this address? The metadata call is free and
    quota-exempt, so check before spending on an image."""
    key = key or _key()
    params = {"key": key, "radius": radius}
    if latlng:
        params["location"] = f"{latlng[0]},{latlng[1]}"
    else:
        params["location"] = address
    d = _get_json(f"{METADATA}?" + urllib.parse.urlencode(params))
    if d.get("status") != "OK":
        return None, d.get("status", "UNKNOWN")
    return d, None


def fetch(address, out_path, key=None, size=MAX_SIZE, fov=80, pitch=8,
          timeout=60, attempts=2, parcel_source=None):
    """Front-of-house photograph, camera aimed at the property.

    Returns (ok, message_or_meta). Failures come back as values rather than
    exceptions so one bad address costs one lead, not the batch.

    `parcel_source` names a county parcel layer to resolve the property from
    land records instead of the geocoder. That matters here: aiming is only as
    good as the target coordinate, and Google answers RANGE_INTERPOLATED for
    roughly half of addresses - an estimate along the street that can sit tens
    of metres from the house. The geocoder is still called first and passed to
    the parcel lookup as a hint, since one street line can map to many parcels.
    """
    key = key or _key()
    size = min(int(size), MAX_SIZE)

    lat, lon, precision, err = locate(address, key=key)

    if parcel_source:
        from curbside.sources import parcels
        hint = (lat, lon) if lat is not None else None
        plat, plon, detail, perr = parcels.locate(address, parcel_source, hint=hint)
        if plat is not None:
            lat, lon, precision = plat, plon, detail["precision"]
        elif lat is None:
            return False, f"{err or 'geocode failed'}; parcel: {perr}"

    if lat is None:
        return False, err

    meta, merr = coverage(latlng=(lat, lon), key=key)
    if meta is None:
        return False, f"no street view coverage ({merr})"

    cam = meta["location"]
    head = bearing(cam["lat"], cam["lng"], lat, lon)
    dist = _haversine_m(cam["lat"], cam["lng"], lat, lon)

    q = urllib.parse.urlencode({
        "size": f"{size}x{size}", "pano": meta["pano_id"],
        "heading": round(head, 1), "fov": fov, "pitch": pitch, "key": key,
    })

    last = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(f"{IMAGE}?{q}", timeout=timeout) as r:
                data = r.read()
        except urllib.error.HTTPError as e:
            last = f"street view HTTP {e.code}"
        except Exception as e:
            last = f"street view fetch failed: {type(e).__name__}"
        else:
            # Google returns a small grey "no imagery" tile rather than an
            # error when a pano exists but the view is empty.
            if len(data) < 15000:
                return False, f"blank street view tile ({len(data)} bytes)"
            pathlib.Path(out_path).write_bytes(data)
            return True, {
                "bytes": len(data),
                "lat": lat, "lon": lon,
                "heading": round(head, 1),
                "camera_distance_m": round(dist, 1),
                "geocode_precision": precision,
                "captured": meta.get("date"),
                "pano_id": meta.get("pano_id"),
            }
    return False, last
