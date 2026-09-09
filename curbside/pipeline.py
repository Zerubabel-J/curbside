"""Pipeline stages. Each is independent, resumable and separately costed."""
import json
import pathlib

from curbside.config import settings
from curbside.store import Store
from curbside.sources.geocode import geocode
from curbside.sources.imagery import fetch
from curbside.vision import gemini
from curbside.render.compositing import composite, qc, save_mask_preview, verify_region
from curbside.render.segmentation import segment, consensus_mask
from curbside.compose.postcard import build as build_postcard, focus_from_mask
from curbside.compliance import policy
from curbside.mail.providers import (get_provider, load_suppression,
                                     is_suppressed, MailError, UndeliverableError)


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    """Hard ceiling, checked before every paid call."""

    def __init__(self, store, cap):
        self.store, self.cap = store, cap

    def check(self, projected=0.0):
        spent = self.store.total_spend()
        if spent + projected > self.cap:
            raise BudgetExceeded(
                f"budget ${self.cap:.2f} would be exceeded "
                f"(spent ${spent:.4f} + ${projected:.4f})")

    def remaining(self):
        return self.cap - self.store.total_spend()


# ---------------------------------------------------------------- stages

def discover(store, addresses, suppression):
    added = suppressed = 0
    for a in addresses:
        if is_suppressed(a, suppression):
            suppressed += 1
            continue
        _, created = store.add_lead(a)
        added += created
    return {"added": added, "suppressed": suppressed}


def image(store, limit=None, log=print):
    done = failed = 0
    for lead in store.ready_for("discovered", limit):
        lat, lon, prec, err = geocode(lead["address"])
        if lat is None:
            store.fail(lead["id"], "imagery", err)
            log(f"  [{lead['id']}] geocode failed: {err}")
            failed += 1
            continue
        path = settings.images_dir / f"{lead['id']:06d}_before.jpg"
        ok, msg = fetch(lat, lon, path)
        if not ok:
            store.fail(lead["id"], "imagery", msg)
            log(f"  [{lead['id']}] imagery failed: {msg}")
            failed += 1
            continue
        store.advance(lead["id"], "imaged", lat=lat, lon=lon,
                      precision=prec, before_path=str(path))
        log(f"  [{lead['id']}] imaged ({prec})")
        done += 1
    return {"imaged": done, "failed": failed}


def qualify(store, key, budget, limit=None, log=print):
    passed = rejected = failed = 0
    for lead in store.ready_for("imaged", limit):
        budget.check(0.001)
        q, err, cost = gemini.qualify(lead["before_path"], key,
                                      model=settings.qualify_model)
        store.add_cost(lead["id"], "qualify", cost, settings.qualify_model)
        if err:
            store.fail(lead["id"], "qualify", err)
            log(f"  [{lead['id']}] qualify failed: {err[:80]}")
            failed += 1
            continue
        if not q["qualified"]:
            store.advance(lead["id"], "rejected", note=q["reason"], qualification=q)
            log(f"  [{lead['id']}] REJECT  {q['surface']} "
                f"cond={q['condition_score']}/10 — {q['reason'][:52]}")
            rejected += 1
            continue
        store.advance(lead["id"], "qualified", qualification=q)
        log(f"  [{lead['id']}] PASS    {q['surface']} "
            f"cond={q['condition_score']}/10 obs={q['obstruction']}")
        passed += 1
    return {"passed": passed, "rejected": rejected, "failed": failed}


def render(store, key, budget, limit=None, log=print, use_segmentation=True):
    """Render, mask by consensus, QC on boundary and semantics, composite."""
    rendered = failed = 0
    for lead in store.ready_for("qualified", limit):
        budget.check(gemini.PRICES[settings.render_model]["per_image"])

        # Segmentation prior — decided from the ORIGINAL, before any render,
        # so a bad render cannot define its own mask.
        prior, seg_meta = (segment(lead["before_path"], key=key)
                           if use_segmentation else (None, {"strategy": "off"}))
        if seg_meta.get("cost"):
            store.add_cost(lead["id"], "segment", seg_meta["cost"],
                           settings.qualify_model)

        raw = settings.output_dir / f"{lead['id']:06d}_raw.jpg"
        ok, msg, cost = gemini.render(lead["before_path"], raw, key,
                                      model=settings.render_model)
        store.add_cost(lead["id"], "render", cost, settings.render_model)
        if not ok:
            store.fail(lead["id"], "render", msg)
            log(f"  [{lead['id']}] render failed: {msg[:80]}")
            failed += 1
            continue

        mask, mask_meta = consensus_mask(
            lead["before_path"], raw, prior,
            threshold=settings.mask_threshold)

        report = qc(lead["before_path"], raw, mask,
                    drift_threshold=settings.qc_drift_threshold,
                    max_outside_frac=settings.qc_max_outside_frac)
        report["segmentation"] = seg_meta
        report["mask"] = mask_meta

        preview = settings.output_dir / f"{lead['id']:06d}_mask.jpg"
        save_mask_preview(lead["before_path"], mask, preview)

        if not report["passed"]:
            store.advance(lead["id"], "failed", note="qc: " + "; ".join(report["reasons"]),
                          fail_stage="render", fail_error=json.dumps(report["reasons"]),
                          qc=report, mask_path=str(preview))
            log(f"  [{lead['id']}] QC FAIL — {'; '.join(report['reasons'])}")
            failed += 1
            continue

        # Semantic QC: boundary discipline proves the edit stayed in the mask,
        # not that the mask was on a driveway.
        sem, sem_err, sem_cost = verify_region(lead["before_path"], preview, key)
        store.add_cost(lead["id"], "qc", sem_cost, settings.qualify_model)
        report["semantic"] = sem if not sem_err else {"error": sem_err}
        if sem and not sem_err and not sem.get("is_driveway"):
            report["passed"] = False
            store.advance(lead["id"], "failed",
                          note=f"qc: edited {sem.get('highlighted_object')}, not driveway",
                          fail_stage="render", fail_error="wrong region",
                          qc=report, mask_path=str(preview))
            log(f"  [{lead['id']}] QC FAIL — edited {sem.get('highlighted_object')}")
            failed += 1
            continue

        final = settings.output_dir / f"{lead['id']:06d}_after.jpg"
        composite(lead["before_path"], raw, mask, final)
        store.advance(lead["id"], "rendered", after_path=str(final),
                      mask_path=str(preview), qc=report)
        log(f"  [{lead['id']}] rendered  mask={report['mask_frac']:.1%} "
            f"drift={report['outside_drift_frac']:.2%} "
            f"via={mask_meta.get('mode')} region={(sem or {}).get('highlighted_object','?')}")
        rendered += 1
    return {"rendered": rendered, "failed": failed}


def compose(store, return_address, limit=None, log=print):
    composed = failed = 0
    for lead in store.ready_for("rendered", limit):
        card = settings.output_dir / f"{lead['id']:06d}_postcard.jpg"
        focus = None
        if lead["mask_path"] and pathlib.Path(lead["mask_path"]).exists():
            focus = focus_from_mask(lead["mask_path"], lead["before_path"])
        try:
            build_postcard(lead["before_path"], lead["after_path"],
                           lead["address"], card,
                           return_address=return_address, focus=focus)
        except Exception as e:
            store.fail(lead["id"], "compose", str(e))
            log(f"  [{lead['id']}] compose failed: {e}")
            failed += 1
            continue
        store.advance(lead["id"], "composed", postcard_path=str(card))
        log(f"  [{lead['id']}] postcard {card.name}")
        composed += 1
    return {"composed": composed, "failed": failed}


def mail(store, provider_name, suppression, budget, acknowledged_states=(),
         limit=None, daily_cap=None, log=print):
    """Only 'approved' leads. Compliance-gated per piece."""
    provider = get_provider(provider_name, outbox=settings.outbox_dir)
    from_address = getattr(provider, "from_address", {"name": "n/a",
        "address_line1": "n/a", "address_city": "n/a",
        "address_state": "n/a", "address_zip": "n/a"})

    policy.preflight_campaign(
        provider=provider,
        suppression_sources=policy.REQUIRED_SUPPRESSION_SOURCES
            if not getattr(provider, "live", False) else ("local do-not-mail list",),
        from_address=from_address)

    cap = daily_cap if daily_cap is not None else settings.daily_mail_cap
    sent = blocked = 0

    # 'blocked' leads are re-examined each run: a state acknowledged since the
    # last attempt should proceed without manual resurrection.
    queue = list(store.ready_for("approved", limit))
    for lead in store.ready_for("blocked", limit):
        d = policy.check_lead(lead["address"], has_disclosure=True,
                              has_return_address=True, has_opt_out=True,
                              acknowledged_states=acknowledged_states)
        if d.allowed and lead["postcard_path"]:
            store.advance(lead["id"], "approved", note="compliance re-check passed")
            queue.append(store.get(lead["id"]))

    for lead in queue:
        if sent >= cap:
            log(f"  daily cap {cap} reached — stopping")
            break

        if is_suppressed(lead["address"], suppression):
            store.advance(lead["id"], "suppressed", note="on suppression list")
            log(f"  [{lead['id']}] SUPPRESSED")
            blocked += 1
            continue

        decision = policy.check_lead(
            lead["address"], has_disclosure=True, has_return_address=True,
            has_opt_out=True, acknowledged_states=acknowledged_states)
        if not decision.allowed:
            store.advance(lead["id"], "blocked",
                          note="compliance: " + "; ".join(decision.reasons))
            log(f"  [{lead['id']}] BLOCKED — {decision.reasons[0][:70]}")
            blocked += 1
            continue

        budget.check(provider.cost_per_piece)
        try:
            res = provider.send(lead, lead["postcard_path"])
        except UndeliverableError as e:
            store.fail(lead["id"], "mail", str(e))
            log(f"  [{lead['id']}] undeliverable")
            blocked += 1
            continue
        except MailError as e:
            store.fail(lead["id"], "mail", str(e))
            log(f"  [{lead['id']}] mail error: {str(e)[:90]}")
            blocked += 1
            continue

        store.add_cost(lead["id"], "mail", res["cost"], provider.name)
        store.advance(lead["id"], "mailed",
                      note=f"{provider.name}:{res['id']} live={res.get('live')}")
        log(f"  [{lead['id']}] {'MAILED' if res.get('live') else 'mailed (dry)'} "
            f"→ {res['id']}")
        sent += 1

    return {"sent": sent, "blocked": blocked, "live": getattr(provider, "live", False)}
