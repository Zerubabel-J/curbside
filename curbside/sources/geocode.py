#!/usr/bin/env python3
"""Address -> lat/lon using free, keyless geocoders.

OSM/Nominatim first: often returns a rooftop or parcel centroid, which is what
we need to centre an aerial crop on the house.
US Census second: always available, but places addresses on the STREET
CENTRELINE, so a crop centred there shows road, not roof.
"""
import json, time, urllib.parse, urllib.request

_last_osm = [0.0]  # Nominatim asks for <=1 req/sec

def _osm(address, timeout=45):
    wait = 1.1 - (time.time() - _last_osm[0])
    if wait > 0:
        time.sleep(wait)
    _last_osm[0] = time.time()
    q = urllib.parse.urlencode({"q": address, "format": "json", "limit": 1})
    req = urllib.request.Request(
        f"https://nominatim.openstreetmap.org/search?{q}",
        headers={"User-Agent": "driveway-demo/1.0 (portfolio testing)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.load(r)
    except Exception as e:
        return None, None, None, f"osm error: {e}"
    if not d:
        return None, None, None, "osm: no match"
    hit = d[0]
    return float(hit["lat"]), float(hit["lon"]), hit.get("type", "?"), None

def _census(address, timeout=45):
    q = urllib.parse.urlencode({
        "address": address, "benchmark": "Public_AR_Current", "format": "json"})
    try:
        with urllib.request.urlopen(
                f"https://geocoding.geo.census.gov/geocoder/locations/onelineaddress?{q}",
                timeout=timeout) as r:
            d = json.load(r)
    except Exception as e:
        return None, None, None, f"census error: {e}"
    m = (d.get("result") or {}).get("addressMatches") or []
    if not m:
        return None, None, None, "census: no match"
    c = m[0]["coordinates"]
    return c["y"], c["x"], "street-centreline", None

def geocode(address):
    """Return (lat, lon, precision, error). precision names the source quality."""
    lat, lon, kind, err = _osm(address)
    if lat is not None:
        # 'house'/'building' are rooftop-grade; anything else is coarser.
        precision = "rooftop" if kind in ("house", "building") else f"osm:{kind}"
        return lat, lon, precision, None
    lat, lon, kind, err2 = _census(address)
    if lat is not None:
        return lat, lon, kind, None
    return None, None, None, f"{err}; {err2}"

if __name__ == "__main__":
    import sys
    for a in sys.argv[1:]:
        print(a, "->", geocode(a))
