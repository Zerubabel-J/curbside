import pytest
from curbside.compliance.policy import (
    check_lead, assert_disclosure, preflight_campaign, state_from_address,
    ComplianceError, REQUIRED_DISCLOSURE, REQUIRED_SUPPRESSION_SOURCES)


def test_state_parsed_from_address():
    assert state_from_address("8102 Talliho Dr, Indianapolis, IN 46256") == "IN"
    assert state_from_address("1 Main St, Los Angeles, CA 90001-1234") == "CA"
    assert state_from_address("no state here") is None


def test_required_disclosure_is_accepted():
    assert assert_disclosure(REQUIRED_DISCLOSURE)


@pytest.mark.parametrize("bad", ["", None, "Call now!", "Limited time offer"])
def test_weak_or_missing_disclosure_rejected(bad):
    with pytest.raises(ComplianceError):
        assert_disclosure(bad)


def test_lead_allowed_when_all_requirements_met():
    d = check_lead("8102 Talliho Dr, Indianapolis, IN 46256",
                   has_disclosure=True, has_return_address=True, has_opt_out=True)
    assert d.allowed and not d.reasons


@pytest.mark.parametrize("field", ["has_disclosure", "has_return_address", "has_opt_out"])
def test_missing_requirement_blocks_lead(field):
    kw = dict(has_disclosure=True, has_return_address=True, has_opt_out=True)
    kw[field] = False
    d = check_lead("8102 Talliho Dr, Indianapolis, IN 46256", **kw)
    assert not d.allowed


def test_review_state_blocked_until_acknowledged():
    addr = "1 Main St, Los Angeles, CA 90001"
    kw = dict(has_disclosure=True, has_return_address=True, has_opt_out=True)
    assert not check_lead(addr, **kw).allowed
    acked = check_lead(addr, acknowledged_states=("CA",), **kw)
    assert acked.allowed and acked.warnings


def test_campaign_preflight_requires_complete_return_address():
    class P: live = False
    with pytest.raises(ComplianceError):
        preflight_campaign(provider=P(), suppression_sources=(),
                           from_address={"name": "X"})


def test_live_campaign_requires_full_suppression_coverage():
    class Live: live = True
    full = {"name": "A", "address_line1": "B", "address_city": "C",
            "address_state": "IN", "address_zip": "46201"}
    with pytest.raises(ComplianceError, match="suppression"):
        preflight_campaign(provider=Live(),
                           suppression_sources=("local do-not-mail list",),
                           from_address=full)
    assert preflight_campaign(provider=Live(),
                              suppression_sources=REQUIRED_SUPPRESSION_SOURCES,
                              from_address=full)


def test_disclosure_describes_the_view_it_was_rendered_from(monkeypatch):
    """The disclosure's whole job is to tell the recipient what they are
    looking at. Calling a street-level photograph "aerial" is a misstatement
    in the one sentence on the piece that exists to be accurate."""
    from curbside.compliance.policy import required_disclosure, assert_disclosure
    from curbside.config import settings

    monkeypatch.setattr(settings, "view", "street")
    street = required_disclosure()
    assert "aerial" not in street.lower()
    assert "front of this property" in street
    assert_disclosure(street)

    monkeypatch.setattr(settings, "view", "aerial")
    aerial = required_disclosure()
    assert "aerial photograph" in aerial
    assert_disclosure(aerial)


def test_street_masks_are_bounded_tighter_than_aerial():
    """When the segmentation prior is discarded the mask falls back to raw
    pixel difference, which can swallow a tree the model re-lit. Such a mask
    drifts nowhere and is well-formed - only its size gives it away."""
    from curbside.config import settings
    assert settings.qc_max_mask_frac_street < 0.45
