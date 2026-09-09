"""Compliance policy — enforced in code, not left to a document.

Copyright is only one of the legal questions this product raises. CC0 imagery
resolves *whose photograph* it is. It does not resolve:

  - Right of publicity / commercial appropriation: using an identifiable
    person's property in advertising directed at them.
  - Intrusion upon seclusion: some states treat depicting a residence in
    unsolicited commercial mail as an intrusion claim.
  - State UDAP (unfair/deceptive acts and practices): an AI-altered image
    presented without disclosure may read as a deceptive depiction.
  - CAN-SPAM has no direct mail analogue, but state mini-UDAP statutes and
    the FTC's general Section 5 authority both apply to mailed advertising.

This module makes the mitigations mandatory rather than optional:

  1. Every piece must carry an AI-rendering disclosure. `assert_disclosure`
     refuses to pass a postcard spec that lacks one.
  2. Every piece must carry a physical return address and an opt-out route.
  3. Certain states are gated behind explicit acknowledgement.
  4. All of it is recorded per-lead so a compliance posture can be evidenced
     after the fact.

None of this is legal advice. It encodes conservative defaults so that the
open question is *reviewed*, not silently ignored.
"""
import dataclasses
import json
import re
import time

# Disclosure that must appear on the mailed piece. Wording is deliberately
# plain: a reader should not have to infer that the image was altered.
REQUIRED_DISCLOSURE = (
    "Illustration only. The 'after' image is a computer-generated rendering "
    "of a public aerial photograph of this address and does not depict actual "
    "work performed."
)

# States with notably assertive right-of-publicity, privacy, or UDAP regimes
# for advertising. Not a legal conclusion — a prompt to get counsel first.
REVIEW_REQUIRED_STATES = {
    "CA": "strong statutory right of publicity (Civ. Code 3344) + CCPA",
    "IL": "BIPA culture, assertive consumer-fraud act (815 ILCS 505)",
    "NY": "Civil Rights Law 50/51 — written consent for advertising use",
    "MA": "Ch. 93A, treble damages for unfair practices",
    "WA": "Personality Rights Act, broad standing",
    "TX": "DTPA, broad private right of action",
}

# Direct mail is NOT covered by the FTC Do Not Call registry (telephony).
# These are the actual mail-suppression sources a production run must honour.
REQUIRED_SUPPRESSION_SOURCES = (
    "local do-not-mail list",
    "DMAchoice (ANA consumer opt-out)",
    "USPS Deceased Do Not Contact",
    "NCOALink move update",
)


class ComplianceError(Exception):
    pass


@dataclasses.dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reasons: tuple
    warnings: tuple
    checked_at: float

    def to_json(self):
        return json.dumps({
            "allowed": self.allowed,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "checked_at": self.checked_at,
        })


def state_from_address(address):
    m = re.search(r",\s*([A-Z]{2})\s+\d{5}(?:-\d{4})?\s*$", address.strip())
    return m.group(1) if m else None


def assert_disclosure(text):
    """Refuse a postcard whose disclosure is missing or watered down."""
    if not text or not text.strip():
        raise ComplianceError(
            "postcard has no AI-rendering disclosure; refusing to compose")
    lowered = text.lower()
    for token in ("rendering", "illustration", "computer-generated"):
        if token in lowered:
            return True
    raise ComplianceError(
        "disclosure text does not clearly state the image is a rendering; "
        f"got: {text[:80]!r}")


def check_lead(address, *, has_disclosure, has_return_address,
               has_opt_out, acknowledged_states=()):
    """Gate a single lead before it can be mailed."""
    reasons, warnings = [], []

    if not has_disclosure:
        reasons.append("missing AI-rendering disclosure on the piece")
    if not has_return_address:
        reasons.append("missing physical return address")
    if not has_opt_out:
        reasons.append("missing opt-out instructions")

    st = state_from_address(address)
    if st is None:
        warnings.append("could not determine state from address")
    elif st in REVIEW_REQUIRED_STATES:
        if st not in set(acknowledged_states):
            reasons.append(
                f"{st} requires legal sign-off before mailing "
                f"({REVIEW_REQUIRED_STATES[st]}); acknowledge with "
                f"CURBSIDE_ACK_STATES={st}")
        else:
            warnings.append(f"{st} mailed under explicit acknowledgement")

    return PolicyDecision(
        allowed=not reasons,
        reasons=tuple(reasons),
        warnings=tuple(warnings),
        checked_at=time.time(),
    )


def preflight_campaign(*, provider, suppression_sources, from_address):
    """Campaign-level checks, run once before any send."""
    problems = []
    if not from_address or not all(from_address.get(k) for k in
                                   ("name", "address_line1", "address_city",
                                    "address_state", "address_zip")):
        problems.append("incomplete return address")

    missing = [s for s in REQUIRED_SUPPRESSION_SOURCES
               if s not in set(suppression_sources)]
    if missing and getattr(provider, "live", False):
        problems.append(
            "live send without full suppression coverage; missing: " +
            ", ".join(missing))

    if problems:
        raise ComplianceError("; ".join(problems))
    return True
