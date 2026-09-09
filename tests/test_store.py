import pytest
from curbside.store import Store, address_key


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def test_address_key_normalizes_punctuation_and_case():
    assert address_key("123 Main St., Indianapolis, IN") == \
           address_key("123 MAIN ST  Indianapolis IN")


def test_duplicate_address_does_not_create_second_lead(store):
    a, created_a = store.add_lead("123 Main St, Indianapolis, IN 46201")
    b, created_b = store.add_lead("123 Main St., Indianapolis, IN 46201")
    assert a == b
    assert created_a is True and created_b is False


def test_advance_records_state_and_event(store):
    lead_id, _ = store.add_lead("1 A St, Indianapolis, IN 46201")
    store.advance(lead_id, "imaged", lat=39.0, lon=-86.0)
    row = store.get(lead_id)
    assert row["state"] == "imaged"
    assert row["lat"] == 39.0
    events = store.db.execute(
        "SELECT * FROM events WHERE lead_id=?", (lead_id,)).fetchall()
    assert events[-1]["to_state"] == "imaged"


def test_ready_for_only_returns_matching_state(store):
    a, _ = store.add_lead("1 A St, Indianapolis, IN 46201")
    b, _ = store.add_lead("2 B St, Indianapolis, IN 46201")
    store.advance(a, "imaged")
    assert [r["id"] for r in store.ready_for("imaged")] == [a]
    assert [r["id"] for r in store.ready_for("discovered")] == [b]


def test_costs_accumulate_and_group_by_stage(store):
    lead_id, _ = store.add_lead("1 A St, Indianapolis, IN 46201")
    store.add_cost(lead_id, "qualify", 0.0009)
    store.add_cost(lead_id, "render", 0.067)
    assert store.total_spend() == pytest.approx(0.0679)
    assert store.spend_by_stage()["render"] == pytest.approx(0.067)


def test_fail_increments_attempts_and_retry_rewinds(store):
    lead_id, _ = store.add_lead("1 A St, Indianapolis, IN 46201")
    store.advance(lead_id, "qualified")
    store.fail(lead_id, "render", "boom")
    assert store.get(lead_id)["state"] == "failed"
    assert store.get(lead_id)["attempts"] == 1
    assert store.retry_failed(max_attempts=3) == 1
    assert store.get(lead_id)["state"] == "qualified"


def test_retry_respects_max_attempts(store):
    lead_id, _ = store.add_lead("1 A St, Indianapolis, IN 46201")
    store.advance(lead_id, "qualified")
    for _ in range(3):
        store.fail(lead_id, "render", "boom")
        store.retry_failed(max_attempts=3)
    store.fail(lead_id, "render", "boom")
    assert store.retry_failed(max_attempts=3) == 0
