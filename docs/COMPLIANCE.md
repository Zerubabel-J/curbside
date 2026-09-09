# Compliance

**This is not legal advice.** It records the risks identified, the mitigations
implemented in code, and the questions that still require counsel.

## The risk copyright does not cover

CC0 imagery resolves *whose photograph it is*. It does not resolve whether
mailing someone an AI-altered image of their own home is lawful. Four distinct
exposures remain:

**Right of publicity / commercial appropriation.** Using an identifiable
person's property in advertising directed at them. New York Civil Rights Law
§§ 50–51 requires written consent for advertising use; California Civil Code
§ 3344 provides statutory damages.

**Intrusion upon seclusion.** Some states treat depicting a residence in
unsolicited commercial mail as an intrusion claim, independent of how the
image was obtained.

**State UDAP.** An AI-altered image presented without disclosure may be read as
a deceptive depiction. Massachusetts Ch. 93A carries treble damages; the Texas
DTPA provides a broad private right of action.

**FTC Section 5.** Applies to mailed advertising as it does to any other
medium. There is no direct-mail CAN-SPAM analogue, but general unfairness and
deception authority reaches it.

## Mitigations implemented

| Risk | Mitigation | Enforcement |
|---|---|---|
| Deceptive depiction | AI-rendering disclosure printed on every piece | `compose` raises `ComplianceError` without one; weak wording rejected |
| No sender identity | Physical return address required | Composition fails without it |
| No opt-out route | Opt-out printed on every piece | Included in the footer |
| High-risk jurisdictions | CA, IL, NY, MA, WA, TX gated | Blocked until `CURBSIDE_ACK_STATES` names the state |
| Mailing opted-out recipients | Suppression checked at discover **and** at send | `pipeline.discover`, `pipeline.mail` |
| Accidental live send | Four independent opt-ins | `LobProvider.preflight` |
| Runaway spend | Hard budget cap, daily mail cap | `Budget.check` before every paid call |

### The disclosure

```
Illustration only. The 'after' image is a computer-generated rendering of a
public aerial photograph of this address and does not depict actual work
performed.
```

`assert_disclosure` requires the words *rendering*, *illustration*, or
*computer-generated*. A postcard cannot be composed without passing it.

## Suppression

Direct mail is **not** covered by the FTC/FCC Do Not Call registry — that is
telephony. Mail suppression is a separate, largely voluntary stack:

| Source | Status |
|---|---|
| Local do-not-mail list | Implemented (`data/suppression.txt`) |
| DMAchoice (ANA consumer opt-out) | **Required before live mail** |
| USPS Deceased Do Not Contact | **Required before live mail** |
| NCOALink move update | **Required before live mail**, also needed for postal discounts |

`preflight_campaign` refuses a live provider until all four are configured.

## Live-send interlocks

A live send requires **all four**:

1. `LOB_API_KEY` beginning `live_` (a `test_` key never sends)
2. `CURBSIDE_ALLOW_LIVE_MAIL=1`
3. A complete return address
4. Full suppression coverage

Any missing element raises before a single piece is sent.

## Open questions for counsel

1. Does an AI-rendered image of a private residence, mailed to its owner,
   implicate right of publicity or intrusion in the states of operation?
2. Is the disclosure wording sufficient, and must it appear on the imaged face
   rather than the footer?
3. Does the CC0 dedication survive a commercial derivative used in print, in
   the view of the issuing state agency?
4. Are there state-specific mailing disclosures beyond the return address?
5. Does depicting work not yet performed require a bonded or licensed
   contractor disclosure in the target state?

**Recommendation:** resolve 1 and 2 before any live send. Both are independent
of the imagery license, and neither is answerable by engineering.

## Audit trail

Every lead records its qualification, QC report, compliance decision and state
transitions with timestamps in `var/curbside.db`. Any mailed piece can be
traced to its source imagery, its license, the model that rendered it, and the
human who approved it.
