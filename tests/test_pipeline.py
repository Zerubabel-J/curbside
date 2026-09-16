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

    from curbside.mail.providers import COST_PER_PIECE
    assert store.counts()["mailed"] == 3            # modelled, none billed
    assert store.total_spend() == pytest.approx(3 * COST_PER_PIECE)
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
    assert set(settings.renderable_sources) == {
        "indiana", "connecticut", "miami_dade", "palm_beach"}
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


# ------------------------------------------------- South East Florida

def test_florida_counties_are_configured_and_renderable():
    """John's target market. Miami-Dade at 3in, Palm Beach at 6in - both
    inside the 6in bar for resolving a driveway edge."""
    from curbside.config import SOURCES
    for key, max_in in (("miami_dade", 3.0), ("palm_beach", 6.0)):
        src = SOURCES[key]
        assert src.states == ("FL",)
        assert src.resolution_in == max_in
        assert src.renderable is True


def test_broward_is_deliberately_absent():
    """Broward's terms require prior written permission and its endpoint
    blocks automated access (HTTP 403). Excluded on purpose, not overlooked."""
    from curbside.config import SOURCES
    assert "broward" not in SOURCES


def test_miami_dade_carries_the_raw_raster_layer_parameter():
    """Without layers=show:29 the export burns street labels, road centrelines
    and parcel lines into the image - unusable for a postcard."""
    from curbside.config import SOURCES
    assert SOURCES["miami_dade"].extra_params.get("layers") == "show:29"


def test_image_server_sources_send_no_extra_params():
    """ImageServer endpoints reject the layer parameter a MapServer needs."""
    from curbside.config import SOURCES
    assert SOURCES["indiana"].extra_params == {}
    assert SOURCES["palm_beach"].extra_params == {}


def test_florida_sources_record_their_licence_honestly():
    """Neither county grants commercial use; both publish disclaimers only.
    The wording must not imply a grant that was never made."""
    from curbside.config import SOURCES
    for key in ("miami_dade", "palm_beach"):
        lic = SOURCES[key].license.lower()
        assert "no stated restriction" in lic
        assert "cc0" not in lic and "public domain" not in lic


def test_street_level_uses_a_higher_mask_threshold_than_aerial():
    """From the road the model redraws foliage across the frame, so an aerial
    threshold captures the garden along with the driveway."""
    from curbside.config import settings
    assert settings.mask_threshold_street > settings.mask_threshold


# ------------------------------------------------- view mode (aerial/street)
#
# Every stage below reads settings.view and picks different behaviour. A branch
# that silently takes the aerial path at street level is invisible until a
# postcard ships with the wrong driveway on it, so each one is pinned here.

@pytest.fixture
def street(monkeypatch):
    """Switch the pipeline to street level for one test."""
    from curbside.config import settings
    monkeypatch.setattr(settings, "view", "street")
    monkeypatch.setattr(settings, "source", "miami_dade")
    return settings


def test_street_view_is_off_unless_asked_for():
    """Aerial is the licensed path. Street level must never be the default."""
    from curbside.config import settings
    assert settings.view == "aerial"
    assert settings.street_view is False


def test_parcel_source_only_offered_where_a_layer_exists(monkeypatch):
    """Aiming falls back to Google's own geocode where no county layer is
    wired, rather than querying a service that does not exist."""
    from curbside.config import settings
    monkeypatch.setattr(settings, "source", "miami_dade")
    assert settings.parcel_source == "miami_dade"
    monkeypatch.setattr(settings, "source", "connecticut")
    assert settings.parcel_source is None


def test_image_stage_routes_to_street_view(store, street, monkeypatch, tmp_path):
    """Street level resolves its own position from the address, so the aerial
    fetch - which needs coordinates - must not be called at all."""
    monkeypatch.setattr(street, "images_dir", tmp_path)

    def boom(*a, **kw):                         # aerial path, must stay unused
        raise AssertionError("aerial fetch called in street mode")
    monkeypatch.setattr("curbside.pipeline.fetch", boom)

    seen = {}

    def fake_street(address, path, **kw):
        seen.update(kw, address=address)
        path.write_bytes(b"x" * 30000)
        return True, {"lat": 25.7, "lon": -80.3, "geocode_precision": "PARCEL_CENTROID",
                      "camera_distance_m": 18.4, "captured": "2022-03"}
    monkeypatch.setattr("curbside.sources.streetview.fetch", fake_street)

    lid, _ = store.add_lead("1 SW 1st St, Miami, FL 33130")
    store.advance(lid, "discovered")             # no coordinates, on purpose

    res = pipeline.image(store, log=lambda *_: None)

    assert res == {"imaged": 1, "failed": 0}
    row = store.get(lid)
    assert row["state"] == "imaged"
    assert row["precision"] == "PARCEL_CENTROID"
    assert seen["parcel_source"] == "miami_dade", "camera must aim at the parcel"


def test_street_imaging_failure_fails_only_that_lead(store, street, monkeypatch, tmp_path):
    """Street View has no coverage on every road. One gap is acceptable."""
    monkeypatch.setattr(street, "images_dir", tmp_path)

    def selective(address, path, **kw):
        if "000002" in str(path):
            return False, "no imagery within 50m"
        path.write_bytes(b"x" * 30000)
        return True, {"lat": 25.7, "lon": -80.3, "geocode_precision": "PARCEL_CENTROID",
                      "camera_distance_m": 18.4}
    monkeypatch.setattr("curbside.sources.streetview.fetch", selective)

    good, _ = store.add_lead("1 Good St, Miami, FL 33130")
    bad, _ = store.add_lead("2 Bad St, Miami, FL 33130")
    for lid in (good, bad):
        store.advance(lid, "discovered")

    res = pipeline.image(store, log=lambda *_: None)
    assert res == {"imaged": 1, "failed": 1}
    assert store.get(good)["state"] == "imaged"
    assert store.get(bad)["state"] == "failed"


def test_street_qualify_uses_the_street_prompt(store, street, monkeypatch, tmp_path):
    """The aerial prompt asks what a driveway looks like from above. Asked
    from the kerb it describes the road."""
    from curbside.vision import gemini
    monkeypatch.setattr(street, "images_dir", tmp_path)

    seen = {}

    def fake_qualify(path, key, model=None, prompt=None):
        seen["prompt"] = prompt
        return {"qualified": False, "surface": "none", "condition_score": 10,
                "reason": "no driveway", "obstruction": "none"}, None, 0.0009
    monkeypatch.setattr("curbside.pipeline.gemini.qualify", fake_qualify)

    lid, _ = store.add_lead("1 SW 1st St, Miami, FL 33130")
    store.advance(lid, "imaged", before_path=str(tmp_path / "b.jpg"))

    pipeline.qualify(store, "k", pipeline.Budget(store, cap=1.0), log=lambda *_: None)
    assert seen["prompt"] is gemini.STREET_QUALIFY_PROMPT


def test_aerial_qualify_leaves_the_prompt_at_its_default(store, monkeypatch, tmp_path):
    from curbside.config import settings
    monkeypatch.setattr(settings, "images_dir", tmp_path)

    seen = {}

    def fake_qualify(path, key, model=None, prompt=None):
        seen["prompt"] = prompt
        return {"qualified": False, "surface": "none", "condition_score": 10,
                "reason": "no driveway", "obstruction": "none"}, None, 0.0009
    monkeypatch.setattr("curbside.pipeline.gemini.qualify", fake_qualify)

    lid, _ = store.add_lead("1 A St, Indianapolis, IN 46201")
    store.advance(lid, "imaged", before_path=str(tmp_path / "b.jpg"))

    pipeline.qualify(store, "k", pipeline.Budget(store, cap=1.0), log=lambda *_: None)
    assert seen["prompt"] is None


def test_street_level_masks_at_a_higher_threshold():
    """Foliage and lighting shift across a street-level frame, so the aerial
    threshold pulls the garden into the mask alongside the driveway."""
    from curbside.config import settings
    assert settings.mask_threshold_street > settings.mask_threshold


def test_street_semantic_check_inverts_the_aerial_heuristics():
    """From above a driveway spans the frame and holds cars. From the kerb it
    recedes toward the house, so the aerial cues mean the opposite."""
    from curbside.render.compositing import verify_region, verify_region_street
    assert verify_region_street is not verify_region
