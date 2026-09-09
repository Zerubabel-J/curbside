"""Pipeline stages. Each is independent, resumable and separately costed."""
import concurrent.futures as _futures
import json
import pathlib
import threading

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


def _scoped(store, state, limit, only=None):
    """Leads in `state`, optionally restricted to a set of ids.

    Batch runs want the whole queue. A block scan wants only the homes it just
    found, so its progress counts describe that block and nothing else.
    """
    rows = store.ready_for(state, None if only else limit)
    if only:
        ids = set(only)
        rows = [r for r in rows if r["id"] in ids]
        if limit:
            rows = rows[:limit]
    return rows


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    """Hard ceiling, checked before every paid call."""

    def __init__(self, store, cap):
        self.store, self.cap = store, cap

    def check(self, projected=0.0):
        spent = self.store.api_spend()
        if spent + projected > self.cap:
            raise BudgetExceeded(
                f"budget ${self.cap:.2f} would be exceeded "
                f"(spent ${spent:.4f} + ${projected:.4f})")

    def remaining(self):
        return self.cap - self.store.api_spend()


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


def discover_sales(store, source_key, suppression, months=18, limit=100,
                   log=print, **filters):
    """Seed leads from a public-record sales source.

    Records arrive with coordinates, so these leads skip geocoding entirely -
    faster, and it avoids the OSM rate limit.
    """
    from curbside.sources.sales import get_source

    src = get_source(source_key)
    sales = src.recent_sales(months=months, limit=limit, **filters)
    log(f"  {src.name} — {len(sales)} sales, licence: {src.license}")

    added = suppressed = 0
    for sale in sales:
        addr = sale.full_address()
        if is_suppressed(addr, suppression):
            suppressed += 1
            continue
        lead_id, created = store.add_lead(addr)
        if created:
            fields = {"sale_date": sale.sale_date,
                      "sale_price": sale.sale_price,
                      "lead_source": sale.source}
            # Pre-geocoded: jump straight to 'imaged' readiness.
            if sale.lat and sale.lon:
                fields.update(lat=sale.lat, lon=sale.lon, precision="sales-record")
            store.advance(lead_id, "discovered", note=f"sold {sale.sale_date}", **fields)
            added += 1
    return {"added": added, "suppressed": suppressed,
            "source": src.key, "license": src.license}


def image(store, limit=None, log=print, only=None):
    done = failed = 0
    for lead in _scoped(store, "discovered", limit, only):
        # Sales records arrive pre-geocoded; only geocode when we must.
        if lead["lat"] and lead["lon"]:
            lat, lon, prec, err = lead["lat"], lead["lon"], lead["precision"], None
        else:
            lat, lon, prec, err = geocode(lead["address"])
        if lat is None:
            store.fail(lead["id"], "imagery", err)
            log(f"  [{lead['id']}] geocode failed: {err}")
            failed += 1
            continue
        path = settings.images_dir / f"{lead['id']:06d}_before.jpg"
        try:
            ok, msg = fetch(lat, lon, path)
        except Exception as e:
            ok, msg = False, f"{type(e).__name__}: {e}"
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


def qualify(store, key, budget, limit=None, log=print, workers=None, only=None):
    """Qualify leads, several at a time.

    Each call is ~4s of waiting on the API, so concurrency here is close to a
    linear speed-up. The store is written from the main thread only.
    """
    leads = list(_scoped(store, "imaged", limit, only))
    if not leads:
        return {"passed": 0, "rejected": 0, "failed": 0}

    workers = workers or settings.workers
    budget.check(0.001 * len(leads))

    def call(lead):
        return lead, gemini.qualify(lead["before_path"], key,
                                    model=settings.qualify_model)

    results = []
    if workers > 1 and len(leads) > 1:
        with _futures.ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(call, leads))
    else:
        results = [call(l) for l in leads]

    passed = rejected = failed = 0
    for lead, (q, err, cost) in results:
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


def render(store, key, budget, limit=None, log=print, use_segmentation=True,
           workers=None, only=None):
    """Render, mask by consensus, QC on boundary and semantics, composite.

    The three network calls per lead (segment, render, semantic QC) dominate
    wall time, so leads are processed concurrently. Only the main thread
    touches the store.
    """
    src = settings.imagery()
    if not src.renderable:
        alts = ", ".join(settings.renderable_sources)
        raise RuntimeError(
            f"{src.name} is {src.resolution_in:.0f} in/px - too coarse to "
            f"resolve a driveway edge, so renders will not pass QC. "
            f"Set CURBSIDE_SOURCE to one of: {alts}")

    leads = list(_scoped(store, "qualified", limit, only))
    if not leads:
        return {"rendered": 0, "failed": 0}

    workers = workers or settings.workers
    budget.check(gemini.PRICES[settings.render_model]["per_image"] * len(leads))

    def attempt(lead, prior, seg_meta, prompt, tag):
        """One render + both QC passes. Returns (bundle, passed)."""
        out = {"costs": []}
        raw = settings.output_dir / f"{lead['id']:06d}_raw.jpg"
        ok, msg, cost = gemini.render(lead["before_path"], raw, key,
                                      model=settings.render_model, prompt=prompt)
        out["costs"].append(("render", cost, settings.render_model))
        if not ok:
            out["error"] = ("render", msg)
            return out, False

        mask, mask_meta = consensus_mask(lead["before_path"], raw, prior,
                                         threshold=settings.mask_threshold)
        report = qc(lead["before_path"], raw, mask,
                    drift_threshold=settings.qc_drift_threshold,
                    max_outside_frac=settings.qc_max_outside_frac)
        report["segmentation"] = seg_meta
        report["mask"] = mask_meta
        report["attempt"] = tag

        preview = settings.output_dir / f"{lead['id']:06d}_mask.jpg"
        save_mask_preview(lead["before_path"], mask, preview)
        out.update(raw=raw, mask=mask, report=report, preview=preview)

        if not report["passed"]:
            return out, False

        sem, sem_err, sem_cost = verify_region(lead["before_path"], preview, key)
        out["costs"].append(("qc", sem_cost, settings.qualify_model))
        report["semantic"] = sem if not sem_err else {"error": sem_err}
        out["semantic"] = (sem, sem_err)
        if sem and not sem_err and not sem.get("is_driveway"):
            return out, False
        return out, True

    def work(lead):
        """Everything network-bound for one lead, with a retry ladder."""
        base = {"lead": lead, "costs": []}
        try:
            prior, seg_meta = (segment(lead["before_path"], key=key)
                               if use_segmentation else (None, {"strategy": "off"}))
            base["seg_meta"] = seg_meta
            if seg_meta.get("cost"):
                base["costs"].append(("segment", seg_meta["cost"], settings.qualify_model))

            last = None
            for tag, prompt in gemini.RENDER_LADDER[:settings.render_attempts]:
                out, passed = attempt(lead, prior, seg_meta, prompt, tag)
                base["costs"].extend(out.pop("costs", []))
                last = {**base, **out}
                if passed:
                    return last
            return last or base
        except Exception as e:
            base["error"] = ("render", f"{type(e).__name__}: {e}")
            return base

    if workers > 1 and len(leads) > 1:
        with _futures.ThreadPoolExecutor(max_workers=workers) as pool:
            bundles = list(pool.map(work, leads))
    else:
        bundles = [work(l) for l in leads]

    rendered = failed = 0
    for b in bundles:
        lead = b["lead"]
        for stage, usd, model in b["costs"]:
            store.add_cost(lead["id"], stage, usd, model)

        if b.get("error"):
            stage, msg = b["error"]
            store.fail(lead["id"], stage, msg)
            log(f"  [{lead['id']}] render failed: {str(msg)[:80]}")
            failed += 1
            continue

        report, preview = b["report"], b["preview"]

        if not report["passed"]:
            store.advance(lead["id"], "failed",
                          note="qc: " + "; ".join(report["reasons"]),
                          fail_stage="render",
                          fail_error=json.dumps(report["reasons"]),
                          qc=report, mask_path=str(preview))
            log(f"  [{lead['id']}] QC FAIL - {'; '.join(report['reasons'])}")
            failed += 1
            continue

        sem, sem_err = b.get("semantic", (None, None))
        if sem and not sem_err and not sem.get("is_driveway"):
            report["passed"] = False
            store.advance(lead["id"], "failed",
                          note=f"qc: edited {sem.get('highlighted_object')}, not driveway",
                          fail_stage="render", fail_error="wrong region",
                          qc=report, mask_path=str(preview))
            log(f"  [{lead['id']}] QC FAIL - edited {sem.get('highlighted_object')}")
            failed += 1
            continue

        final = settings.output_dir / f"{lead['id']:06d}_after.jpg"
        composite(lead["before_path"], b["raw"], b["mask"], final)
        store.advance(lead["id"], "rendered", after_path=str(final),
                      mask_path=str(preview), qc=report)
        log(f"  [{lead['id']}] rendered  mask={report['mask_frac']:.1%} "
            f"drift={report['outside_drift_frac']:.2%} "
            f"via={report['mask'].get('mode')} "
            f"region={(sem or {}).get('highlighted_object','?')}")
        rendered += 1

    return {"rendered": rendered, "failed": failed}


def compose(store, return_address, limit=None, log=print, only=None):
    composed = failed = 0
    for lead in _scoped(store, "rendered", limit, only):
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
