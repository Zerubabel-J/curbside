"""Sold-lead search. Network tests are marked and deselected by default."""
import datetime as dt

import pytest

from curbside.sources import sold


def test_rejects_anything_that_is_not_a_zip():
    # A ZIP+4 is truncated to its first five digits, which is correct; these
    # are inputs with no valid five-digit ZIP at all.
    for bad in ("", "abc", "331", "3315x", "zip33156"):
        with pytest.raises(sold.SoldError, match="5-digit ZIP"):
            sold.search(bad)


def test_rejects_an_unknown_county():
    with pytest.raises(sold.SoldError, match="unknown county"):
        sold.search("33156", county="nassau")


def test_price_floor_screens_out_non_market_transfers(monkeypatch):
    """Quitclaims and family transfers record as $10 sales. Mailing them
    wastes postage on someone who did not buy anything."""
    seen = {}

    def fake(zip_code, min_price, since, limit):
        seen["min_price"] = min_price
        return []

    monkeypatch.setitem(sold.COUNTIES, "miami_dade", ("test", fake))
    sold.search("33156", county="miami_dade", min_price=0)
    assert seen["min_price"] >= sold.MIN_ARMS_LENGTH_PRICE


def test_money_parses_both_county_formats():
    """Palm Beach stores '7992550', Broward stores '$1,100,000'."""
    assert sold._money("7992550") == 7992550.0
    assert sold._money("$1,100,000") == 1100000.0
    assert sold._money(2400000.0) == 2400000.0
    assert sold._money(None) == 0.0
    assert sold._money("") == 0.0


def test_since_walks_back_across_a_year_boundary():
    d = sold._since(6)
    assert isinstance(d, dt.date)
    assert (dt.date.today() - d).days > 150


def test_address_line_is_mailable():
    lead = sold.SoldLead(address="900 DIPLOMAT PKWY", city="Hollywood",
                         zip_code="33019", price=1_638_000,
                         sold_on=dt.date(2026, 9, 10),
                         property_use="SINGLE FAMILY", county="broward")
    assert lead.as_address() == "900 DIPLOMAT PKWY, Hollywood, FL 33019"


def test_address_line_survives_a_missing_city():
    """Broward stores the city as a two-letter internal code, so it is left
    out rather than printing 'Hw, FL' on a postcard."""
    lead = sold.SoldLead(address="332 BALBOA ST", city="", zip_code="33019",
                         price=2_300_000, sold_on=dt.date(2026, 8, 24),
                         property_use="SINGLE FAMILY", county="broward")
    assert lead.as_address() == "332 BALBOA ST, FL 33019"


def test_palm_beach_never_filters_on_the_owners_mailing_zip():
    """PBC's sales layer has no property ZIP - ZIP1 is where the *owner*
    collects post. 15395 Whispering Willow Dr is in Wellington but carries
    ZIP1 33480 because its owner uses a suite in Palm Beach. Filtering on it
    selects homes by their owner's mailbox and mails the wrong town."""
    import inspect
    src = inspect.getsource(sold._palm_beach)
    assert "ZIP1" not in src, "ZIP1 is the owner's mailing ZIP, not the site's"
    assert "MUNICIPALITY IN" in src


def test_palm_beach_refuses_an_unmapped_zip():
    """Better to return nothing than to guess a municipality."""
    with pytest.raises(sold.SoldError, match="not mapped"):
        sold._palm_beach("90210", 700000, sold._since(6), 10)


def test_palm_beach_map_covers_every_spelling_variant():
    """The county spells Boynton Beach two ways and Greenacres three. A map
    listing one spelling silently drops the others' sales."""
    boynton = sold._PB_ZIP_TO_MUNI["33435"]
    assert "BOYNTON BEACH" in boynton and "BOYTON BEACH" in boynton


def test_broward_fetches_every_parcel_not_just_the_lead_limit():
    """The parcel set is the join key for the sales layer, so truncating it to
    the caller's limit discards sales instead of returning fewer leads. That
    bug returned 2 leads where 71 existed."""
    import inspect
    src = inspect.getsource(sold._broward)
    assert "limit * 40" not in src
    assert "25000" in src


def test_broward_assembles_its_seven_address_columns():
    parcel = {"SITUS_STREET_NUMBER": "900", "SITUS_STREET_DIRECTION": "",
              "SITUS_STREET_NAME": "DIPLOMAT", "SITUS_STREET_TYPE": "PKWY",
              "SITUS_STREET_POST_DIR": "", "SITUS_UNIT_NUMBER": ""}
    assert sold._broward_address(parcel) == "900 DIPLOMAT PKWY"
    parcel["SITUS_UNIT_NUMBER"] = "4B"
    assert sold._broward_address(parcel) == "900 DIPLOMAT PKWY #4B"


def test_one_county_failing_does_not_lose_the_others(monkeypatch):
    good = sold.SoldLead("1 A St", "Miami", "33156", 900000,
                         dt.date(2026, 9, 1), "SINGLE FAMILY", "miami_dade")

    monkeypatch.setitem(sold.COUNTIES, "miami_dade",
                        ("md", lambda *a: [good]))
    monkeypatch.setitem(sold.COUNTIES, "palm_beach",
                        ("pb", lambda *a: (_ for _ in ()).throw(sold.SoldError("down"))))
    monkeypatch.setitem(sold.COUNTIES, "broward",
                        ("bc", lambda *a: (_ for _ in ()).throw(sold.SoldError("down"))))

    leads, errors = sold.search("33156")
    assert leads == [good]
    assert len(errors) == 2


def test_all_counties_failing_raises(monkeypatch):
    for k in list(sold.COUNTIES):
        monkeypatch.setitem(sold.COUNTIES, k,
                            ("x", lambda *a: (_ for _ in ()).throw(sold.SoldError("down"))))
    with pytest.raises(sold.SoldError):
        sold.search("33156")


def test_results_are_newest_first(monkeypatch):
    mk = lambda d: sold.SoldLead("1 A St", "Miami", "33156", 900000, d,
                                 "SINGLE FAMILY", "miami_dade")
    rows = [mk(dt.date(2026, 5, 1)), mk(dt.date(2026, 9, 1)),
            mk(dt.date(2026, 7, 1))]
    monkeypatch.setitem(sold.COUNTIES, "miami_dade", ("md", lambda *a: rows))
    leads, _ = sold.search("33156", county="miami_dade")
    assert [l.sold_on for l in leads] == [
        dt.date(2026, 9, 1), dt.date(2026, 7, 1), dt.date(2026, 5, 1)]


# ----------------------------------------------------------- network

@pytest.mark.network
def test_miami_dade_returns_mailable_leads():
    leads, errors = sold.search("33156", county="miami_dade", limit=5)
    assert leads and not errors
    for l in leads:
        assert l.address and l.zip_code.startswith("331")
        assert l.price > 700_000
        assert "SINGLE FAMILY" in l.property_use.upper()


@pytest.mark.network
def test_palm_beach_returns_only_that_municipality():
    leads, errors = sold.search("33480", county="palm_beach", limit=10)
    assert leads and not errors
    assert {l.city for l in leads} == {"Palm Beach"}


@pytest.mark.network
def test_broward_join_finds_the_whole_zip():
    leads, errors = sold.search("33019", county="broward", limit=200)
    assert not errors
    assert len(leads) > 20, "the folio join must not truncate the parcel set"
