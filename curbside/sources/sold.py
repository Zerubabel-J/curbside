#!/usr/bin/env python3
"""Recently-sold homes by ZIP — the lead source.

A contractor does not want to type addresses. They want to name a market and
get a mailing list. The signal that turns a ZIP into a list is a recent sale
at a high price: a new owner with budget, in the first months of ownership
when exterior work actually gets commissioned.

Every county here publishes this as free, unauthenticated public record, which
matters beyond the cost. The commercial alternatives each carry a licence
restriction to negotiate — ATTOM's free tier forbids building a product on it
at all, MLS/IDX rules prohibit using sold data for solicitation, and Zillow's
public API was retired in 2021. Florida public records carry no such
condition, so the lead source raises no question that has to be answered by a
lawyer before a campaign can run.

Three counties, three different schemas, one interface. The awkwardness is
real and lives here rather than leaking into the pipeline:

  miami_dade  one layer, typed columns, no qualification code
  palm_beach  one layer, PRICE is a *string*, so comparisons need CAST
  broward     two layers joined on folio, SALE_AMOUNT is "$1,100,000"

`search()` returns the same `SoldLead` from all three.
"""
import dataclasses
import datetime as _dt
import json
import re
import urllib.error
import urllib.parse
import urllib.request


@dataclasses.dataclass(frozen=True)
class SoldLead:
    address: str
    city: str
    zip_code: str
    price: float
    sold_on: _dt.date
    property_use: str
    county: str

    def as_address(self):
        """One line, as the pipeline and the mail provider expect it."""
        parts = [self.address.strip()]
        if self.city.strip():
            parts.append(self.city.strip())
        tail = f"FL {self.zip_code.strip()}".strip()
        return ", ".join(parts + [tail])


class SoldError(RuntimeError):
    pass


#: Below this, a "sale" is almost always a quitclaim, a family transfer or a
#: recorded correction rather than a market transaction. Palm Beach exposes a
#: qualification code for this; Miami-Dade does not, so a floor is the only
#: screen that works across all three.
MIN_ARMS_LENGTH_PRICE = 30_000

#: Driveway work implies a house with a driveway. Unfiltered county results
#: include condominiums, vacant land and commercial parcels - a $4.2M vacant
#: lot is a real row in this data, and it has nothing to pave.
_SINGLE_FAMILY = {
    "miami_dade": "DOR_DESC LIKE 'RESIDENTIAL - SINGLE FAMILY%'",
    "palm_beach": "PROPERTY_USE = 'SINGLE FAMILY'",
    "broward": "USE_CODE = '01'",
}

_TIMEOUT = 90
_PAGE = 1000


def _get(url, params, timeout=_TIMEOUT):
    q = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{url}?{q}",
                                 headers={"User-Agent": "curbside/0.4"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        raise SoldError(f"HTTP {e.code}: {e.read()[:200].decode('utf8', 'replace')}")
    except Exception as e:
        raise SoldError(f"{type(e).__name__}: {e}")
    if isinstance(d, dict) and d.get("error"):
        raise SoldError(str(d["error"])[:300])
    return d


def _since(months):
    """ArcGIS `DATE 'YYYY-MM-DD'` literal, `months` back from today."""
    today = _dt.date.today()
    month = today.month - months
    year = today.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    day = min(today.day, 28)
    return _dt.date(year, month, day)


def _epoch_to_date(ms):
    if ms is None:
        return None
    return _dt.datetime.fromtimestamp(ms / 1000.0, _dt.timezone.utc).date()


def _money(text):
    """'$1,100,000' or '7992550' -> float. Counties disagree on the format."""
    if text is None:
        return 0.0
    if isinstance(text, (int, float)):
        return float(text)
    cleaned = re.sub(r"[^0-9.]", "", str(text))
    try:
        return float(cleaned) if cleaned else 0.0
    except ValueError:
        return 0.0


# --------------------------------------------------------------------------
# Miami-Dade — the straightforward one
# --------------------------------------------------------------------------

_MD_URL = ("https://services.arcgis.com/8Pc9XBTAsYuxx9Ny/arcgis/rest/"
           "services/PaGISView_gdb/FeatureServer/0/query")


def _miami_dade(zip_code, min_price, since, limit):
    # ZIP is stored ZIP+4 ('33156-0000'), so an equality test finds nothing.
    where = (f"TRUE_SITE_ZIP_CODE LIKE '{zip_code}%' "
             f"AND PRICE_1 > {min_price} "
             f"AND DATEOFSALE_UTC >= DATE '{since:%Y-%m-%d}' "
             f"AND {_SINGLE_FAMILY['miami_dade']}")
    rows = _paged(_MD_URL, where,
                  "TRUE_SITE_ADDR,TRUE_SITE_CITY,TRUE_SITE_ZIP_CODE,"
                  "PRICE_1,DATEOFSALE_UTC,DOR_DESC",
                  "DATEOFSALE_UTC DESC", limit)
    out = []
    for a in rows:
        out.append(SoldLead(
            address=(a.get("TRUE_SITE_ADDR") or "").strip(),
            city=(a.get("TRUE_SITE_CITY") or "").strip(),
            zip_code=(a.get("TRUE_SITE_ZIP_CODE") or "")[:5],
            price=_money(a.get("PRICE_1")),
            sold_on=_epoch_to_date(a.get("DATEOFSALE_UTC")),
            property_use=(a.get("DOR_DESC") or "").strip(),
            county="miami_dade"))
    return out


# --------------------------------------------------------------------------
# Palm Beach — PRICE is a string column
# --------------------------------------------------------------------------

_PB_URL = ("https://gis.pbcgov.org/arcgis/rest/services/Parcels/QSALES/"
           "FeatureServer/0/query")


#: Palm Beach's sales layer carries no ZIP for the *property* - `ZIP1`,
#: `CITYNAME` and `PADDR*` are the owner's mailing address, which for an
#: absentee owner is a different town or state entirely. One verified example:
#: 15395 Whispering Willow Dr in Wellington, whose ZIP1 reads 33480 because
#: the owner collects mail at a suite on Sunrise Ave in Palm Beach. Filtering
#: on ZIP1 therefore selects homes by where the owner reads their post, and
#: every resulting postcard would carry the wrong ZIP.
#:
#: MUNICIPALITY *is* the property's town, so the search runs on that and the
#: caller's ZIP is translated to one. The county spells several of them more
#: than one way, so each entry lists every spelling in the data.
_PB_ZIP_TO_MUNI = {
    "33401": ("WEST PALM BEACH",), "33403": ("LAKE PARK",),
    "33404": ("RIVIERA BEACH",),   "33405": ("WEST PALM BEACH",),
    "33406": ("WEST PALM BEACH",), "33407": ("WEST PALM BEACH",),
    "33408": ("NORTH PALM BEACH", "JUNO BEACH"),
    "33409": ("WEST PALM BEACH",), "33410": ("PALM BEACH GARDENS",),
    "33411": ("ROYAL PALM BEACH",), "33412": ("WEST PALM BEACH",),
    "33413": ("GREENACRES", "GREENACRES CITY", "GREEN ACRES"),
    "33414": ("WELLINGTON",),      "33415": ("WEST PALM BEACH",),
    "33417": ("WEST PALM BEACH",), "33418": ("PALM BEACH GARDENS",),
    "33426": ("BOYNTON BEACH", "BOYTON BEACH"),
    "33428": ("BOCA RATON",),      "33430": ("BELLE GLADE",),
    "33431": ("BOCA RATON",),      "33432": ("BOCA RATON",),
    "33433": ("BOCA RATON",),      "33434": ("BOCA RATON",),
    "33435": ("BOYNTON BEACH", "BOYTON BEACH"),
    "33436": ("BOYNTON BEACH", "BOYTON BEACH"),
    "33437": ("BOYNTON BEACH", "BOYTON BEACH"),
    "33444": ("DELRAY BEACH",),    "33445": ("DELRAY BEACH",),
    "33446": ("DELRAY BEACH",),    "33449": ("LAKE WORTH",),
    "33458": ("JUPITER",),         "33460": ("LAKE WORTH", "LAKE WORTH BEACH"),
    "33461": ("LAKE WORTH", "PALM SPRINGS"),
    "33462": ("LANTANA", "HYPOLUXO"),
    "33463": ("LAKE WORTH", "GREENACRES", "GREENACRES CITY"),
    "33467": ("LAKE WORTH",),      "33469": ("JUPITER",),
    "33470": ("LOXAHATCHEE", "LOXAHATCHEE GROVES"),
    "33477": ("JUPITER",),         "33478": ("JUPITER",),
    "33480": ("PALM BEACH",),      "33483": ("DELRAY BEACH",),
    "33484": ("DELRAY BEACH",),    "33486": ("BOCA RATON",),
    "33487": ("BOCA RATON",),      "33496": ("BOCA RATON",),
}


def _palm_beach(zip_code, min_price, since, limit):
    munis = _PB_ZIP_TO_MUNI.get(zip_code)
    if not munis:
        # Better to return nothing than to filter on the owner's mailing ZIP
        # and mail a stranger's town.
        raise SoldError(f"ZIP {zip_code} is not mapped to a Palm Beach "
                        "municipality; the sales layer has no property ZIP")
    quoted = ", ".join(f"'{m}'" for m in munis)

    # PRICE is stored as text, so a bare numeric comparison is rejected. CAST
    # works server-side, which keeps the filtering off this machine.
    where = (f"MUNICIPALITY IN ({quoted}) "
             f"AND SALE_DATE >= DATE '{since:%Y-%m-%d}' "
             f"AND CAST(PRICE AS FLOAT) > {min_price} "
             f"AND {_SINGLE_FAMILY['palm_beach']}")
    rows = _paged(_PB_URL, where,
                  "SITE_ADDR_STR,MUNICIPALITY,PRICE,SALE_DATE,"
                  "PROPERTY_USE,QUAL_CODE",
                  "SALE_DATE DESC", limit)
    out = []
    for a in rows:
        out.append(SoldLead(
            address=(a.get("SITE_ADDR_STR") or "").strip(),
            city=(a.get("MUNICIPALITY") or "").strip().title(),
            # The layer has no property ZIP, so the one the caller searched on
            # is the only one that describes this house.
            zip_code=zip_code,
            price=_money(a.get("PRICE")),
            sold_on=_epoch_to_date(a.get("SALE_DATE")),
            property_use=(a.get("PROPERTY_USE") or "").strip(),
            county="palm_beach"))
    return out


# --------------------------------------------------------------------------
# Broward — two layers, joined on folio
# --------------------------------------------------------------------------

_BC_BASE = ("https://gisweb-adapters.bcpa.net/arcgis/rest/services/"
            "BCPA_EXTERNAL_JAN26/MapServer")
_BC_INFO, _BC_SALES = f"{_BC_BASE}/36/query", f"{_BC_BASE}/37/query"


def _broward_address(a):
    """Seven situs columns, assembled into one street line."""
    bits = [a.get("SITUS_STREET_NUMBER"), a.get("SITUS_STREET_DIRECTION"),
            a.get("SITUS_STREET_NAME"), a.get("SITUS_STREET_TYPE"),
            a.get("SITUS_STREET_POST_DIR")]
    line = " ".join(b.strip() for b in bits if b and b.strip())
    unit = (a.get("SITUS_UNIT_NUMBER") or "").strip()
    return f"{line} #{unit}" if unit else line


def _broward(zip_code, min_price, since, limit):
    # Parcels first: the ZIP filter lives on the info layer, and it cuts the
    # folio set down before the sales layer is touched.
    parcels = _paged(_BC_INFO,
                     f"SITUS_ZIP_CODE LIKE '{zip_code}%' "
                     f"AND {_SINGLE_FAMILY['broward']}",
                     "FOLIO_NUMBER,SITUS_STREET_NUMBER,SITUS_STREET_DIRECTION,"
                     "SITUS_STREET_NAME,SITUS_STREET_TYPE,SITUS_STREET_POST_DIR,"
                     "SITUS_UNIT_NUMBER,SITUS_CITY,SITUS_ZIP_CODE",
                     # Every parcel in the ZIP, not `limit` of them: this set
                     # is the join key for the sales layer, so truncating it
                     # silently discards sales rather than returning fewer
                     # leads. A dense ZIP holds a few thousand.
                     "FOLIO_NUMBER ASC", 25000)
    by_folio = {p["FOLIO_NUMBER"]: p for p in parcels if p.get("FOLIO_NUMBER")}
    if not by_folio:
        return []

    # SALE_AMOUNT is a formatted string ('$1,100,000'), so no server-side
    # numeric filter is possible on price. Narrowing to this ZIP's folios is
    # what keeps the scan bounded: asking the sales layer for every sale in
    # the county and filtering here pulls tens of thousands of rows to find a
    # few dozen. Folio prefixes are geographic, so the distinct prefixes of
    # the matched parcels bound the query to roughly this area.
    prefixes = sorted({f[:6] for f in by_folio if len(f) >= 6})
    clauses = " OR ".join(f"FOLIO_NUMBER LIKE '{p}%'" for p in prefixes[:40])
    sales = _paged(_BC_SALES,
                   f"SALE_DATE >= DATE '{since:%Y-%m-%d}'"
                   + (f" AND ({clauses})" if clauses else ""),
                   "FOLIO_NUMBER,SALE_DATE,SALE_AMOUNT,SALE_VER",
                   "SALE_DATE DESC", 20000)

    out = []
    for s in sales:
        parcel = by_folio.get(s.get("FOLIO_NUMBER"))
        if not parcel:
            continue
        price = _money(s.get("SALE_AMOUNT"))
        if price <= min_price:
            continue
        out.append(SoldLead(
            address=_broward_address(parcel),
            # SITUS_CITY is a two-letter internal code ('HW', 'DN'), not a
            # place name, and printing it would put "Hw, FL" on the card. The
            # ZIP identifies the town well enough for the carrier, and Lob
            # verifies against it before anything is printed.
            city="",
            zip_code=(parcel.get("SITUS_ZIP_CODE") or "")[:5],
            price=price,
            sold_on=_epoch_to_date(s.get("SALE_DATE")),
            property_use="SINGLE FAMILY",
            county="broward"))
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------

def _paged(url, where, out_fields, order_by, limit):
    """Fetch up to `limit` attribute rows, following ArcGIS paging.

    These layers cap a response at 1000-2000 rows and some carry no object id,
    which makes `orderByFields` mandatory rather than optional - without it the
    service rejects any paged request outright.
    """
    rows, offset = [], 0
    while len(rows) < limit:
        page = _get(url, {
            "where": where, "outFields": out_fields,
            "orderByFields": order_by, "returnGeometry": "false",
            "resultOffset": offset, "resultRecordCount": min(_PAGE, limit - len(rows)),
            "f": "json",
        })
        feats = page.get("features", [])
        rows.extend(f.get("attributes", {}) for f in feats)
        if len(feats) < _PAGE or not page.get("exceededTransferLimit", False):
            break
        offset += len(feats)
    return rows[:limit]


COUNTIES = {
    "miami_dade": ("Miami-Dade County", _miami_dade),
    "palm_beach": ("Palm Beach County", _palm_beach),
    "broward":    ("Broward County", _broward),
}


def validate(zip_code, county=None):
    """Check the market without querying anything. Cheap, so callers can run
    it before touching the environment or spending a request."""
    zip_code = str(zip_code).strip()[:5]
    if not zip_code.isdigit() or len(zip_code) != 5:
        raise SoldError(f"not a 5-digit ZIP: {zip_code!r}")
    if county and county not in COUNTIES:
        raise SoldError(f"unknown county {county!r}; have {sorted(COUNTIES)}")
    return zip_code


def search(zip_code, county=None, min_price=700_000, months=6, limit=200):
    """Homes sold in `zip_code` in the last `months` above `min_price`.

    `county` names the records to query; without it every county is tried and
    the results merged, since a caller with a ZIP rarely knows which appraiser
    holds it. Newest sale first.
    """
    zip_code = str(zip_code).strip()[:5]
    if not zip_code.isdigit() or len(zip_code) != 5:
        raise SoldError(f"not a 5-digit ZIP: {zip_code!r}")
    min_price = max(float(min_price), MIN_ARMS_LENGTH_PRICE)
    since = _since(months)

    targets = [county] if county else list(COUNTIES)
    unknown = [t for t in targets if t not in COUNTIES]
    if unknown:
        raise SoldError(f"unknown county {unknown[0]!r}; have {sorted(COUNTIES)}")

    leads, errors = [], []
    for key in targets:
        _, fn = COUNTIES[key]
        try:
            leads.extend(fn(zip_code, min_price, since, limit))
        except SoldError as e:
            # One county being down must not lose the other two. A ZIP usually
            # belongs to exactly one appraiser anyway.
            errors.append(f"{key}: {e}")

    if not leads and errors and len(errors) == len(targets):
        raise SoldError("; ".join(errors))

    leads = [l for l in leads if l.address and l.sold_on]
    leads.sort(key=lambda l: l.sold_on, reverse=True)
    return leads[:limit], errors
