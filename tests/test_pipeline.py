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
    store.add_cost(lead_id, "render", 0.95, "gemini-3.1-flash-image")
    b.check(0.04)                                  # still inside
    with pytest.raises(pipeline.BudgetExceeded):
        b.check(0.10)


def test_budget_remaining_tracks_spend(store):
    b = pipeline.Budget(store, cap=2.00)
    lead_id, _ = store.add_lead("1 A St, Indianapolis, IN 46201")
    store.add_cost(lead_id, "render", 0.50, "gemini-3.1-flash-image")
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


def test_dry_run_postage_does_not_consume_the_api_budget(store, tmp_path):
    """Modelled print+postage is recorded for reporting but never billed, so it
    must not eat the budget that guards real API spend."""
    for i in range(3):
        lid, _ = store.add_lead(f"{i+1} Budget St, Indianapolis, IN 46201")
        store.advance(lid, "composed", postcard_path=str(tmp_path / "c.jpg"))
        store.advance(lid, "approved")

    pipeline.mail(store, "dryrun", set(), pipeline.Budget(store, 1.00),
                  log=lambda *_: None)

    assert store.counts()["mailed"] == 3            # $2.25 modelled, none billed
    assert store.total_spend() == pytest.approx(2.25)
    assert store.api_spend() == 0.0                 # nothing charged
    assert pipeline.Budget(store, 1.00).remaining() == pytest.approx(1.00)


def test_api_spend_excludes_modelled_costs(store):
    lead_id, _ = store.add_lead("1 A St, Indianapolis, IN 46201")
    store.add_cost(lead_id, "render", 0.067, "gemini-3.1-flash-image")
    store.add_cost(lead_id, "mail", 0.75, "dryrun")
    assert store.total_spend() == pytest.approx(0.817)
    assert store.api_spend() == pytest.approx(0.067)


# ------------------------------------------------- imagery quality guard

def test_render_refuses_imagery_too_coarse_to_resolve_a_driveway(store, monkeypatch):
    """NC OneMap is ~20 in/px. Renders from it drift and QC rejects nearly all
    of them, so fail loudly with the fix rather than burning money."""
    from curbside.config import settings
    monkeypatch.setattr(settings, "source", "north_carolina")
    lid, _ = store.add_lead("1 A St, Raleigh, NC 27601")
    store.advance(lid, "qualified")
    with pytest.raises(RuntimeError, match="too coarse"):
        pipeline.render(store, "key", pipeline.Budget(store, 5.0),
                        log=lambda *_: None)


def test_render_guard_names_a_usable_alternative(store, monkeypatch):
    from curbside.config import settings
    monkeypatch.setattr(settings, "source", "north_carolina")
    lid, _ = store.add_lead("1 A St, Raleigh, NC 27601")
    store.advance(lid, "qualified")
    with pytest.raises(RuntimeError) as e:
        pipeline.render(store, "key", pipeline.Budget(store, 5.0),
                        log=lambda *_: None)
    assert "indiana" in str(e.value)


def test_renderable_sources_exclude_coarse_imagery():
    from curbside.config import settings, SOURCES
    assert set(settings.renderable_sources) == {"indiana", "connecticut"}
    assert SOURCES["north_carolina"].renderable is False


def test_one_unreachable_tile_does_not_abort_the_batch(store, monkeypatch, tmp_path):
    """State GIS services 500 intermittently. Losing one home is acceptable;
    losing the whole scan is not."""
    from curbside.config import settings
    monkeypatch.setattr(settings, "images_dir", tmp_path)

    good, _ = store.add_lead("1 Good St, Indianapolis, IN 46201")
    bad, _ = store.add_lead("2 Bad St, Indianapolis, IN 46201")
    for lid in (good, bad):
        store.advance(lid, "discovered", lat=39.9, lon=-86.1, precision="test")

    def selective(lat, lon, path, **kw):
        if "000002" in str(path):
            raise RuntimeError("HTTP Error 500")
        path.write_bytes(b"x" * 30000)
        return True, "ok"

    monkeypatch.setattr("curbside.pipeline.fetch", selective)
    monkeypatch.setattr("curbside.pipeline.geocode",
                        lambda a: (39.9, -86.1, "test", None))

    res = pipeline.image(store, log=lambda *_: None)
    assert res["imaged"] == 1 and res["failed"] == 1
    assert store.get(good)["state"] == "imaged"
    assert store.get(bad)["state"] == "failed"


# ------------------------------------------------------- per-scan scoping

def test_stages_can_be_scoped_to_a_lead_set(store, monkeypatch, tmp_path):
    """Batch runs process the whole queue by design. A block scan must report
    on its own homes only, or its progress counts describe someone else's."""
    from curbside.config import settings
    monkeypatch.setattr(settings, "images_dir", tmp_path)
    monkeypatch.setattr("curbside.pipeline.geocode",
                        lambda a: (39.9, -86.1, "test", None))
    monkeypatch.setattr("curbside.pipeline.fetch",
                        lambda lat, lon, path, **kw: (path.write_bytes(b"x"*30000), (True, "ok"))[1])

    mine, theirs = [], []
    for i in range(3):
        lid, _ = store.add_lead(f"{i+1} Mine St, Indianapolis, IN 46201")
        store.advance(lid, "discovered", lat=39.9, lon=-86.1)
        mine.append(lid)
    for i in range(4):
        lid, _ = store.add_lead(f"{i+1} Theirs Rd, Indianapolis, IN 46201")
        store.advance(lid, "discovered", lat=39.9, lon=-86.1)
        theirs.append(lid)

    res = pipeline.image(store, log=lambda *_: None, only=mine)
    assert res["imaged"] == 3, "scoped run must not touch the other leads"
    assert all(store.get(i)["state"] == "imaged" for i in mine)
    assert all(store.get(i)["state"] == "discovered" for i in theirs)


def test_unscoped_stage_still_processes_the_whole_queue(store, monkeypatch, tmp_path):
    from curbside.config import settings
    monkeypatch.setattr(settings, "images_dir", tmp_path)
    monkeypatch.setattr("curbside.pipeline.geocode",
                        lambda a: (39.9, -86.1, "test", None))
    monkeypatch.setattr("curbside.pipeline.fetch",
                        lambda lat, lon, path, **kw: (path.write_bytes(b"x"*30000), (True, "ok"))[1])
    for i in range(3):
        lid, _ = store.add_lead(f"{i+1} Any St, Indianapolis, IN 46201")
        store.advance(lid, "discovered", lat=39.9, lon=-86.1)

    assert pipeline.image(store, log=lambda *_: None)["imaged"] == 3


def test_scoping_respects_limit(store, monkeypatch, tmp_path):
    from curbside.config import settings
    monkeypatch.setattr(settings, "images_dir", tmp_path)
    monkeypatch.setattr("curbside.pipeline.geocode",
                        lambda a: (39.9, -86.1, "test", None))
    monkeypatch.setattr("curbside.pipeline.fetch",
                        lambda lat, lon, path, **kw: (path.write_bytes(b"x"*30000), (True, "ok"))[1])
    ids = []
    for i in range(5):
        lid, _ = store.add_lead(f"{i+1} Cap St, Indianapolis, IN 46201")
        store.advance(lid, "discovered", lat=39.9, lon=-86.1)
        ids.append(lid)

    assert pipeline.image(store, limit=2, log=lambda *_: None, only=ids)["imaged"] == 2
