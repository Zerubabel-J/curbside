"""API contract tests. No network, no API keys."""
import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from curbside.config import settings
from curbside.store import Store


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", tmp_path / "api.db")
    monkeypatch.setattr(settings, "images_dir", tmp_path / "images")
    monkeypatch.setattr(settings, "output_dir", tmp_path / "output")
    from curbside.api.app import app
    return TestClient(app)


@pytest.fixture
def seeded(client, tmp_path):
    s = Store(settings.db_path)
    img = tmp_path / "b.jpg"
    from PIL import Image
    Image.new("RGB", (64, 64), (120, 120, 120)).save(img)
    lid, _ = s.add_lead("8102 Talliho Dr, Indianapolis, IN 46256")
    s.advance(lid, "composed", before_path=str(img), after_path=str(img),
              postcard_path=str(img),
              qualification={"surface": "concrete", "condition_score": 7,
                             "qualified": True, "reason": "worn"},
              qc={"mask_frac": 0.12, "outside_drift_frac": 0.004, "passed": True})
    ca, _ = s.add_lead("1 Palm Ave, Los Angeles, CA 90001")
    s.advance(ca, "composed", before_path=str(img), after_path=str(img),
              postcard_path=str(img))
    s.close()
    return {"in": lid, "ca": ca}


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_config_reports_source_and_checks(client):
    d = client.get("/config").json()
    assert d["source"]["license"] == "CC0-1.0"
    assert "gemini_key" in d["checks"]
    assert "north_carolina" in d["available_sources"]


def test_stats_shape(client, seeded):
    d = client.get("/stats").json()
    assert d["counts"]["composed"] == 2
    assert d["total_spend"] == 0.0
    assert "attribution" in d["source"]


def test_list_leads_filters_by_state(client, seeded):
    assert len(client.get("/leads?state=composed").json()) == 2
    assert client.get("/leads?state=mailed").json() == []


def test_lead_carries_compliance_decision(client, seeded):
    ind = client.get(f"/leads/{seeded['in']}").json()
    assert ind["compliance"]["allowed"] is True
    ca = client.get(f"/leads/{seeded['ca']}").json()
    assert ca["compliance"]["allowed"] is False
    assert "CA" in ca["compliance"]["reasons"][0]


def test_missing_lead_is_404(client):
    assert client.get("/leads/9999").status_code == 404


def test_image_endpoint_serves_jpeg(client, seeded):
    r = client.get(f"/leads/{seeded['in']}/image/before")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"


def test_image_endpoint_rejects_unknown_kind(client, seeded):
    assert client.get(f"/leads/{seeded['in']}/image/roof").status_code == 400


def test_approve_moves_lead_to_approved(client, seeded):
    r = client.post(f"/leads/{seeded['in']}/approve", json={"note": "ok"})
    assert r.status_code == 200 and r.json()["state"] == "approved"
    assert client.get(f"/leads/{seeded['in']}").json()["state"] == "approved"


def test_approve_rejects_lead_in_wrong_state(client, seeded):
    client.post(f"/leads/{seeded['in']}/approve")
    assert client.post(f"/leads/{seeded['in']}/approve").status_code == 409


def test_reject_records_note(client, seeded):
    client.post(f"/leads/{seeded['in']}/reject", json={"note": "bad render"})
    ev = client.get(f"/events/{seeded['in']}").json()
    assert ev[-1]["to_state"] == "rejected"
    assert "bad render" in ev[-1]["note"]


def test_mail_endpoint_blocks_unacknowledged_state(client, seeded):
    client.post(f"/leads/{seeded['ca']}/approve")
    r = client.post("/mail", json={"provider": "dryrun"})
    assert r.status_code == 200
    assert r.json()["sent"] == 0 and r.json()["blocked"] == 1


# ------------------------------------------------------------- block scan

def test_scan_requires_an_address(client):
    assert client.post("/scan", json={}).status_code == 422


def test_scan_returns_a_job_id(client, monkeypatch):
    """The scan endpoint hands back a job immediately; work happens in the
    background so the UI can show progress rather than block."""
    import curbside.api.app as app_mod
    monkeypatch.setattr(app_mod, "_run_scan", lambda *a, **k: None)
    r = client.post("/scan", json={"address": "1 A St, Indianapolis, IN 46201"})
    assert r.status_code == 200
    assert len(r.json()["job_id"]) == 12
    assert r.json()["status"] == "running"


def test_unknown_scan_job_is_404(client):
    assert client.get("/scan/deadbeef1234").status_code == 404


def test_send_one_rejects_lead_in_wrong_state(client, seeded):
    """A rejected lead has no postcard, so it cannot be sent."""
    client.post(f"/leads/{seeded['in']}/reject")
    r = client.post(f"/leads/{seeded['in']}/send")
    assert r.status_code == 409


def test_send_one_approves_and_mails_in_a_single_call(client, seeded):
    r = client.post(f"/leads/{seeded['in']}/send", json={"note": "from card"})
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "mailed"
    assert body["sent"] == 1
    assert body["live"] is False


def test_send_one_blocks_unacknowledged_state(client, seeded):
    """Per-card send honours the same compliance gate as bulk send."""
    r = client.post(f"/leads/{seeded['ca']}/send")
    assert r.status_code == 200
    assert r.json()["sent"] == 0 and r.json()["blocked"] == 1


def test_config_reports_capability_flags(client):
    """The UI hides the sale-date control where the layer cannot support it."""
    d = client.get("/config").json()
    assert "supports_sale_date" in d
    assert "renderable" in d


def test_scan_accepts_a_sale_window(client, monkeypatch):
    import curbside.api.app as app_mod
    monkeypatch.setattr(app_mod, "_run_scan", lambda *a, **k: None)
    r = client.post("/scan", json={"address": "1 A St, Raleigh, NC 27601",
                                   "sold_within_months": 6})
    assert r.status_code == 200
