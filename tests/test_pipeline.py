"""Pipeline-level behaviour: budget, gating, resumability. No network."""
import pytest

from curbside import pipeline
from curbside.store import Store
from curbside.mail.providers import load_suppression


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "p.db")
    yield s
    s.close()


def test_budget_refuses_before_exceeding_cap(store):
    b = pipeline.Budget(store, cap=1.00)
    lead_id, _ = store.add_lead("1 A St, Indianapolis, IN 46201")
    store.add_cost(lead_id, "render", 0.95)
    b.check(0.04)                                  # still inside
    with pytest.raises(pipeline.BudgetExceeded):
        b.check(0.10)


def test_budget_remaining_tracks_spend(store):
    b = pipeline.Budget(store, cap=2.00)
    lead_id, _ = store.add_lead("1 A St, Indianapolis, IN 46201")
    store.add_cost(lead_id, "render", 0.50)
    assert b.remaining() == pytest.approx(1.50)


def test_discover_skips_suppressed_addresses(store, tmp_path):
    f = tmp_path / "s.txt"
    f.write_text("2 B St, Indianapolis, IN 46201\n")
    supp = load_suppression(f)
    res = pipeline.discover(store, [
        "1 A St, Indianapolis, IN 46201",
        "2 B St, Indianapolis, IN 46201",
    ], supp)
    assert res == {"added": 1, "suppressed": 1}


def test_discover_is_idempotent(store):
    addrs = ["1 A St, Indianapolis, IN 46201"]
    assert pipeline.discover(store, addrs, set())["added"] == 1
    assert pipeline.discover(store, addrs, set())["added"] == 0


def test_mail_only_touches_approved_leads(store, tmp_path):
    composed, _ = store.add_lead("1 A St, Indianapolis, IN 46201")
    store.advance(composed, "composed", postcard_path=str(tmp_path / "c.jpg"))
    approved, _ = store.add_lead("2 B St, Indianapolis, IN 46201")
    store.advance(approved, "composed", postcard_path=str(tmp_path / "c.jpg"))
    store.advance(approved, "approved")

    res = pipeline.mail(store, "dryrun", set(),
                        pipeline.Budget(store, 10.0), log=lambda *_: None)
    assert res["sent"] == 1
    assert store.get(composed)["state"] == "composed"   # untouched
    assert store.get(approved)["state"] == "mailed"


def test_mail_blocks_review_state_until_acknowledged(store, tmp_path):
    lead_id, _ = store.add_lead("1 Palm Ave, Los Angeles, CA 90001")
    store.advance(lead_id, "composed", postcard_path=str(tmp_path / "c.jpg"))
    store.advance(lead_id, "approved")

    res = pipeline.mail(store, "dryrun", set(),
                        pipeline.Budget(store, 10.0), log=lambda *_: None)
    assert res["sent"] == 0 and res["blocked"] == 1
    assert store.get(lead_id)["state"] == "blocked"

    res = pipeline.mail(store, "dryrun", set(), pipeline.Budget(store, 10.0),
                        acknowledged_states=("CA",), log=lambda *_: None)
    assert res["sent"] == 1
    assert store.get(lead_id)["state"] == "mailed"


def test_mail_respects_daily_cap(store, tmp_path):
    for i in range(5):
        lid, _ = store.add_lead(f"{i+1} Cap St, Indianapolis, IN 46201")
        store.advance(lid, "composed", postcard_path=str(tmp_path / "c.jpg"))
        store.advance(lid, "approved")
    res = pipeline.mail(store, "dryrun", set(), pipeline.Budget(store, 10.0),
                        daily_cap=2, log=lambda *_: None)
    assert res["sent"] == 2
    assert len(store.ready_for("approved")) == 3


def test_mail_honours_suppression_at_send_time(store, tmp_path):
    lead_id, _ = store.add_lead("9 Stop St, Indianapolis, IN 46201")
    store.advance(lead_id, "composed", postcard_path=str(tmp_path / "c.jpg"))
    store.advance(lead_id, "approved")
    f = tmp_path / "s.txt"
    f.write_text("9 Stop St, Indianapolis, IN 46201\n")

    res = pipeline.mail(store, "dryrun", load_suppression(f),
                        pipeline.Budget(store, 10.0), log=lambda *_: None)
    assert res["sent"] == 0
    assert store.get(lead_id)["state"] == "suppressed"


def test_mail_stops_at_budget_cap(store, tmp_path):
    for i in range(3):
        lid, _ = store.add_lead(f"{i+1} Budget St, Indianapolis, IN 46201")
        store.advance(lid, "composed", postcard_path=str(tmp_path / "c.jpg"))
        store.advance(lid, "approved")
    with pytest.raises(pipeline.BudgetExceeded):
        pipeline.mail(store, "dryrun", set(), pipeline.Budget(store, 1.00),
                      log=lambda *_: None)
    assert store.counts().get("mailed", 0) == 1     # one at $0.75, second refused
