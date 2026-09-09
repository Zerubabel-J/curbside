#!/usr/bin/env python3
"""Indiana statewide 3-inch orthoimagery. CC0-1.0. No key, no billing, no cost.
Source: Indiana Geographic Information Office (IGIO)."""
import math, pathlib, urllib.parse, urllib.request

from curbside.config import settings

SERVICE = settings.imagery().service
ATTRIBUTION = settings.imagery().attribution

def _mercator(lat, lon):
    x = lon * 20037508.34 / 180.0
    y = math.log(math.tan((90 + lat) * math.pi / 360.0)) / (math.pi / 180.0)
    return x, y * 20037508.34 / 180.0

def fetch(lat, lon, out_path, meters=None, px=None, timeout=90, source=None):
    """Square crop centred on (lat,lon). meters = ground width of the crop.
    45m ~ a typical suburban lot with driveway and street frontage."""
    src = settings.imagery() if source is None else source
    meters = settings.crop_meters if meters is None else meters
    px = settings.image_px if px is None else px
    x, y = _mercator(lat, lon)
    h = meters / 2
    q = urllib.parse.urlencode({
        "bbox": f"{x-h},{y-h},{x+h},{y+h}",
        "bboxSR": 3857, "imageSR": 3857,
        "size": f"{px},{px}", "format": "jpg", "f": "image",
    })
    with urllib.request.urlopen(f"{src.service}?{q}", timeout=timeout) as r:
        data = r.read()
    # A near-empty JPEG means we fell outside the mosaic.
    if len(data) < 20000:
        return False, f"outside coverage or blank ({len(data)} bytes)"
    pathlib.Path(out_path).write_bytes(data)
    return True, f"{len(data):,} bytes"
