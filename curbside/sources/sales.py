"""Recently-sold property records — free, keyless, public-record sources.

Property transfers are public record in the US, which is why this data is
publishable at all. These adapters read the government's own open-data
endpoints rather than a commercial aggregator.

USE NOTE. These sources are wired up for development and verification — they
prove the pipeline works end to end without a paid subscription. Only
Connecticut carries an explicit public-domain grant; the county portals
publish openly but state no licence, which means "no restriction found", not
"commercial redistribution granted". Before running a real campaign, either
review the county's terms or move to a licensed commercial source. Each
adapter reports `license` and `commercial_use` so that distinction stays
visible rather than being assumed.
"""
import dataclasses
import datetime as _dt
import json
import urllib.parse
import urllib.request


def _epoch_ms_to_date(ms):
    """ArcGIS returns dates as epoch milliseconds."""
    if not ms:
        return None
    return _dt.datetime.fromtimestamp(ms / 1000, _dt.UTC).date().isoformat()


def _polygon_centroid(geometry):
    """Average vertex of a parcel ring - close enough to sit on the property.

    GeoJSON-style {"rings": [[[x, y], ...]]} from the ArcGIS JSON format.
    """
    if not geometry:
        return None, None
    rings = geometry.get("rings") or []
    if not rings or not rings[0]:
        return None, None
    pts = rings[0]
    xs = [p[0] for p in pts if len(p) >= 2]
    ys = [p[1] for p in pts if len(p) >= 2]
    if not xs or not ys:
        return None, None
    return sum(ys) / len(ys), sum(xs) / len(xs)


@dataclasses.dataclass(frozen=True)
class SoldProperty:
    address: str
    town: str
    state: str
    sale_date: str
    sale_price: float | None = None
    lat: float | None = None
    lon: float | None = None
    property_type: str | None = None
    source: str = ""

    def full_address(self, zipcode=None):
        parts = [self.address]
        if self.town:
            parts.append(self.town)
        parts.append(self.state)
        base = ", ".join(parts)
        return f"{base} {zipcode}" if zipcode else base


class SalesSource:
    key = "base"
    name = "base"
    license = "unknown"
    #: "granted" only where the publisher explicitly permits it. "unstated"
    #: means the data is public but the terms are silent - fine for
    #: development, needs review before a commercial campaign.
    commercial_use = "unstated"
    states = ()
    requires_key = False

    def latest_sale_date(self):
        """Newest record in the dataset - portals publish on a lag."""
        raise NotImplementedError

    def recent_sales(self, months=18, limit=200, **filters):
        raise NotImplementedError


class ConnecticutSales(SalesSource):
    """CT statewide Real Estate Sales, via the Socrata Open Data API.

    Public Domain. No API key. Includes address, sale date, sale amount and
    point geometry, so leads arrive pre-geocoded — which also sidesteps the
    1 req/sec limit on the OSM geocoder.

    Note the state publishes on a lag; `latest_sale_date()` reports how current
    the dataset actually is rather than assuming.
    """
    key = "connecticut"
    name = "CT Real Estate Sales (data.ct.gov)"
    license = "Public Domain"
    commercial_use = "granted"
    states = ("CT",)
    ENDPOINT = "https://data.ct.gov/resource/5mzw-sjtu.json"

    def _get(self, params, timeout=60):
        url = f"{self.ENDPOINT}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)

    def latest_sale_date(self):
        rows = self._get({"$select": "max(daterecorded) as newest"})
        return (rows[0].get("newest") or "")[:10] if rows else None

    def recent_sales(self, months=18, limit=200, town=None,
                     residential_type="Single Family",
                     min_price=None, max_price=None, **_):
        """Sales within `months` of the dataset's most recent record.

        Anchoring to the data rather than to today matters: the state publishes
        on a lag, so "last 18 months from now" can return nothing.
        """
        newest = self.latest_sale_date()
        if not newest:
            return []
        anchor = _dt.date.fromisoformat(newest)
        cutoff = anchor - _dt.timedelta(days=int(months * 30.44))

        where = [f"daterecorded > '{cutoff.isoformat()}'"]
        if residential_type:
            where.append(f"residentialtype='{residential_type}'")
        if town:
            where.append(f"upper(town)='{town.upper()}'")
        if min_price:
            where.append(f"saleamount >= {float(min_price)}")
        if max_price:
            where.append(f"saleamount <= {float(max_price)}")

        rows = self._get({
            "$select": ("daterecorded,town,address,saleamount,"
                        "residentialtype,geo_coordinates"),
            "$where": " AND ".join(where),
            "$order": "daterecorded DESC",
            "$limit": int(limit),
        })

        out = []
        for r in rows:
            addr = (r.get("address") or "").strip()
            town_name = (r.get("town") or "").strip()
            if not addr or not town_name:
                continue
            lat = lon = None
            geo = r.get("geo_coordinates")
            if isinstance(geo, dict) and geo.get("coordinates"):
                lon, lat = geo["coordinates"][0], geo["coordinates"][1]
            price = r.get("saleamount")
            out.append(SoldProperty(
                address=addr, town=town_name, state="CT",
                sale_date=(r.get("daterecorded") or "")[:10],
                sale_price=float(price) if price else None,
                lat=lat, lon=lon,
                property_type=r.get("residentialtype"),
                source=self.key,
            ))
        return out


class WakeCountySales(SalesSource):
    """Wake County, NC (Raleigh) parcels — ArcGIS FeatureServer, no API key.

    The freshest source wired up: records land within about two weeks of the
    deed. Carries both sale date and sale price, and returns parcel polygons,
    so leads arrive pre-located and skip geocoding.

    Licence is unstated. The county publishes openly with a disclaimer rather
    than a grant, so this is suitable for development and verification; a
    commercial campaign needs the terms reviewed or a licensed source.
    """
    key = "wake_nc"
    name = "Wake County NC Parcels (maps.wake.gov)"
    license = "unstated (public records, published openly)"
    commercial_use = "unstated - review before commercial use"
    states = ("NC",)
    ENDPOINT = ("https://maps.wake.gov/arcgis/rest/services/Property/"
                "Parcels/FeatureServer/0/query")
    PAGE = 1000                      # server maxRecordCount is 2000

    def _query(self, params, timeout=90):
        params = {"f": "json", **params}
        url = f"{self.ENDPOINT}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.load(r)
        if "error" in d:
            raise RuntimeError(f"wake: {d['error'].get('message')}")
        return d

    def latest_sale_date(self):
        today = _dt.date.today()
        d = self._query({
            "where": f"SALE_DATE <= DATE '{today}'",
            "outFields": "SALE_DATE",
            "orderByFields": "SALE_DATE DESC",
            "resultRecordCount": 1, "returnGeometry": "false",
        })
        feats = d.get("features") or []
        if not feats:
            return None
        ms = feats[0]["attributes"].get("SALE_DATE")
        return _epoch_ms_to_date(ms)

    def count(self, months=6, min_price=None, **filters):
        d = self._query({"where": self._where(months, min_price=min_price, **filters),
                         "returnCountOnly": "true", "returnGeometry": "false"})
        return d.get("count", 0)

    def _where(self, months, min_price=None, max_price=None,
               min_year_built=None, max_year_built=None, **_):
        today = _dt.date.today()
        cutoff = today - _dt.timedelta(days=int(months * 30.44))
        # An upper bound matters: assessor data carries typo'd future dates.
        clauses = [f"SALE_DATE >= DATE '{cutoff}'",
                   f"SALE_DATE <= DATE '{today}'"]
        clauses.append(f"TOTSALPRICE > {float(min_price)}" if min_price
                       else "TOTSALPRICE > 0")
        if max_price:
            clauses.append(f"TOTSALPRICE <= {float(max_price)}")
        # YEAR_BUILT > 0 excludes vacant land, which has no driveway.
        clauses.append(f"YEAR_BUILT >= {int(min_year_built)}" if min_year_built
                       else "YEAR_BUILT > 0")
        # Recent sales skew heavily to new construction, whose driveways are
        # brand new. Capping the build year targets homes that may need work.
        if max_year_built:
            clauses.append(f"YEAR_BUILT <= {int(max_year_built)}")
        return " AND ".join(clauses)

    def recent_sales(self, months=6, limit=200, min_price=None,
                     max_price=None, min_year_built=None,
                     max_year_built=None, **_):
        where = self._where(months, min_price, max_price,
                            min_year_built, max_year_built)
        out, offset = [], 0
        while len(out) < limit:
            page = min(self.PAGE, limit - len(out))
            d = self._query({
                "where": where,
                "outFields": "SITE_ADDRESS,SALE_DATE,TOTSALPRICE,YEAR_BUILT",
                "orderByFields": "SALE_DATE DESC",
                "resultOffset": offset, "resultRecordCount": page,
                "returnGeometry": "true", "outSR": 4326,
            })
            feats = d.get("features") or []
            if not feats:
                break
            for f in feats:
                a = f.get("attributes", {})
                addr = (a.get("SITE_ADDRESS") or "").strip()
                if not addr:
                    continue
                lat, lon = _polygon_centroid(f.get("geometry"))
                price = a.get("TOTSALPRICE")
                out.append(SoldProperty(
                    address=addr, town="", state="NC",
                    sale_date=_epoch_ms_to_date(a.get("SALE_DATE")) or "",
                    sale_price=float(price) if price else None,
                    lat=lat, lon=lon,
                    property_type=f"built {a.get('YEAR_BUILT')}"
                                  if a.get("YEAR_BUILT") else None,
                    source=self.key,
                ))
            if not d.get("exceededTransferLimit") and len(feats) < page:
                break
            offset += len(feats)
        return out[:limit]


SOURCES = {s.key: s for s in (WakeCountySales, ConnecticutSales)}


def get_source(key):
    if key not in SOURCES:
        raise ValueError(f"unknown sales source {key!r}; have {sorted(SOURCES)}")
    return SOURCES[key]()
