import json
import pytest

from curbside.mail.providers import (get_provider, load_suppression, is_suppressed,
                                     MailError, LobProvider, DryRunProvider)


def test_unknown_provider_rejected():
    with pytest.raises(MailError, match="unknown provider"):
        get_provider("carrier-pigeon")


def test_dryrun_writes_receipt_and_sends_nothing(tmp_path):
    p = get_provider("dryrun", outbox=tmp_path)
    lead = {"id": 7, "address": "8102 Talliho Dr, Indianapolis, IN 46256"}
    res = p.send(lead, tmp_path / "card.jpg")
    assert res["live"] is False
    receipt = json.loads((tmp_path / "000007.json").read_text())
    assert receipt["dry_run"] is True
    assert receipt["address"] == lead["address"]


@pytest.mark.parametrize("addr,ok", [
    ("8102 Talliho Dr, Indianapolis, IN 46256", True),
    ("Talliho Dr, Indianapolis, IN 46256", False),   # no house number
    ("8102 Talliho Dr, Indianapolis, IN", False),    # no zip
    ("nowhere", False),
])
def test_dryrun_structural_verification(addr, ok):
    assert DryRunProvider().verify(addr)["deliverable"] is ok


def test_lob_requires_api_key(monkeypatch):
    monkeypatch.delenv("LOB_API_KEY", raising=False)
    with pytest.raises(MailError, match="LOB_API_KEY"):
        get_provider("lob")


def test_lob_test_key_is_not_live(monkeypatch):
    monkeypatch.setenv("LOB_API_KEY", "test_abc123")
    assert get_provider("lob").live is False


def test_lob_live_key_refuses_without_explicit_optin(monkeypatch):
    monkeypatch.setenv("LOB_API_KEY", "live_abc123")
    for k, v in [("LOB_FROM_NAME", "Heartland"), ("LOB_FROM_LINE1", "1 Main St"),
                 ("LOB_FROM_CITY", "Indianapolis"), ("LOB_FROM_STATE", "IN"),
                 ("LOB_FROM_ZIP", "46201")]:
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("CURBSIDE_ALLOW_LIVE_MAIL", raising=False)
    p = get_provider("lob")
    assert p.live is True
    with pytest.raises(MailError, match="CURBSIDE_ALLOW_LIVE_MAIL"):
        p.preflight()


def test_lob_refuses_incomplete_return_address(monkeypatch):
    monkeypatch.setenv("LOB_API_KEY", "test_abc123")
    for k in ("LOB_FROM_NAME", "LOB_FROM_LINE1", "LOB_FROM_CITY",
              "LOB_FROM_STATE", "LOB_FROM_ZIP"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(MailError, match="return address"):
        get_provider("lob").preflight()


def test_suppression_matches_across_punctuation(tmp_path):
    f = tmp_path / "s.txt"
    f.write_text("# comment\n8102 Talliho Dr, Indianapolis, IN 46256\n")
    supp = load_suppression(f)
    assert is_suppressed("8102 TALLIHO DR., Indianapolis, IN 46256", supp)
    assert not is_suppressed("1 Other St, Indianapolis, IN 46256", supp)


def test_missing_suppression_file_is_empty_not_error(tmp_path):
    assert load_suppression(tmp_path / "nope.txt") == set()
