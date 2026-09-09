"""HTTP API over the pipeline.

The CLI and the API are two front-ends onto the same stage functions - no
logic lives here that isn't also reachable from the terminal.
"""
import io
import json
import os
import pathlib
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from curbside import __version__, pipeline
from curbside.api.schemas import LeadOut, StatsOut
from curbside.compliance import policy
from curbside.config import settings, SOURCES
from curbside.mail.providers import load_suppression, is_suppressed
from curbside.store import Store

app = FastAPI(
    title="Curbside",
    version=__version__,
    description="AI-rendered direct mail for home-services contractors",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CURBSIDE_CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-process job state for the async `run` endpoint. Ephemeral by design -
# except for seeded demo scans, which are restored from disk on boot so a
# container restart does not empty the demo.
_JOBS = {}
_SEED_FILE = settings.db_path.parent / "seeded_scans.json"


def _load_seeded_scans():
    if not _SEED_FILE.exists():
        return
    try:
        for job in json.loads(_SEED_FILE.read_text()):
            _JOBS[job["id"]] = job
    except (ValueError, OSError):
        pass


def _save_seeded_scans():
    """Persist completed scans so they outlive the process."""
    done = [j for j in _JOBS.values() if j.get("status") == "completed"]
    _SEED_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SEED_FILE.write_text(json.dumps(done, indent=2))
    return len(done)


def _store():
    return Store(settings.db_path)


def _acked():
    return tuple(s.strip().upper() for s in
                 os.environ.get("CURBSIDE_ACK_STATES", "").split(",") if s.strip())


def _return_address():
    parts = [os.environ.get(k) for k in
             ("CURBSIDE_FROM_NAME", "CURBSIDE_FROM_LINE1", "CURBSIDE_FROM_CITY",
              "CURBSIDE_FROM_STATE", "CURBSIDE_FROM_ZIP")]
    if all(parts):
        return f"{parts[0]}, {parts[1]}, {parts[2]}, {parts[3]} {parts[4]}"
    return None


def _supports_sale_date():
    """Whether the active state's parcel layer carries a sale date."""
    from curbside.sources.block import SUPPORTS_SALE_DATE
    states = settings.imagery().states or ()
    return bool(states) and states[0] in SUPPORTS_SALE_DATE


def _compliance_for(address):
    d = policy.check_lead(address, has_disclosure=True, has_return_address=True,
                          has_opt_out=True, acknowledged_states=_acked())
    return {"allowed": d.allowed, "reasons": list(d.reasons),
            "warnings": list(d.warnings)}


# ---------------------------------------------------------------- health

_load_seeded_scans()


@app.get("/health", tags=["ops"])
def health():
    return {"status": "ok", "version": __version__}


@app.get("/config", tags=["ops"])
def config():
    """What is configured, and what would block a live send."""
    return {
        "version": __version__,
        "source": {
            "key": settings.source,
            "name": settings.imagery().name,
            "license": settings.imagery().license,
            "resolution_in": settings.imagery().resolution_in,
            "states": list(settings.imagery().states),
        },
        "available_sources": {
            k: {"name": s.name, "license": s.license,
                "resolution_in": s.resolution_in, "states": list(s.states)}
            for k, s in SOURCES.items()
        },
        "supports_sale_date": _supports_sale_date(),
        "renderable": settings.imagery().renderable,
        "demo_mode": settings.demo_mode,
        "budget_usd": settings.budget_usd,
        "daily_mail_cap": settings.daily_mail_cap,
        "mail_provider": settings.mail_provider,
        "acknowledged_states": list(_acked()),
        "checks": {
            "gemini_key": bool(os.environ.get("GEMINI_API_KEY")),
            "return_address": _return_address(),
            "lob_key": bool(os.environ.get("LOB_API_KEY")),
            "live_mail_enabled": os.environ.get("CURBSIDE_ALLOW_LIVE_MAIL") == "1",
        },
        "required_suppression_sources": list(policy.REQUIRED_SUPPRESSION_SOURCES),
    }


# ---------------------------------------------------------------- leads

@app.get("/leads", tags=["leads"])
def list_leads(state: Optional[str] = None, limit: int = Query(100, le=1000)):
    s = _store()
    try:
        if state:
            rows = s.ready_for(state, limit)
        else:
            rows = s.db.execute(
                "SELECT * FROM leads ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [LeadOut.from_row(r, _compliance_for(r["address"])).dict()
                for r in rows]
    finally:
        s.close()


@app.get("/leads/{lead_id}", tags=["leads"])
def get_lead(lead_id: int):
    s = _store()
    try:
        row = s.get(lead_id)
        if not row:
            raise HTTPException(404, "lead not found")
        return LeadOut.from_row(row, _compliance_for(row["address"])).dict()
    finally:
        s.close()


@app.get("/leads/{lead_id}/image/{kind}", tags=["leads"])
def lead_image(lead_id: int, kind: str):
    """kind: before | after | mask | postcard"""
    column = {"before": "before_path", "after": "after_path",
              "mask": "mask_path", "postcard": "postcard_path"}.get(kind)
    if not column:
        raise HTTPException(400, "kind must be before, after, mask or postcard")
    s = _store()
    try:
        row = s.get(lead_id)
        if not row or not row[column]:
            raise HTTPException(404, f"no {kind} image for lead {lead_id}")
        p = pathlib.Path(row[column])
        if not p.exists():
            raise HTTPException(404, "image file missing on disk")
        return FileResponse(p, media_type="image/jpeg")
    finally:
        s.close()


class Decision(BaseModel):
    note: Optional[str] = None


@app.post("/leads/{lead_id}/approve", tags=["leads"])
def approve(lead_id: int, body: Decision = Decision()):
    s = _store()
    try:
        row = s.get(lead_id)
        if not row:
            raise HTTPException(404, "lead not found")
        if row["state"] != "composed":
            raise HTTPException(409, f"lead is {row['state']}, not composed")
        s.advance(lead_id, "approved", note=body.note or "approved via API")
        return {"id": lead_id, "state": "approved"}
    finally:
        s.close()


@app.post("/leads/{lead_id}/send", tags=["leads"])
def send_one(lead_id: int, body: Decision = Decision()):
    """Approve and send a single postcard - the per-card action in the UI."""
    s = _store()
    try:
        row = s.get(lead_id)
        if not row:
            raise HTTPException(404, "lead not found")
        if row["state"] not in ("composed", "approved"):
            raise HTTPException(409, f"lead is {row['state']}, not sendable")
        if row["state"] == "composed":
            s.advance(lead_id, "approved", note=body.note or "sent from card")

        supp = load_suppression(settings.suppression_file)
        budget = pipeline.Budget(s, settings.budget_usd)
        res = pipeline.mail(s, settings.mail_provider, supp, budget,
                            acknowledged_states=_acked(), limit=1,
                            log=lambda *_: None)
        after = s.get(lead_id)
        return {"id": lead_id, "state": after["state"],
                "sent": res["sent"], "blocked": res["blocked"],
                "live": res["live"]}
    except policy.ComplianceError as e:
        raise HTTPException(422, f"compliance: {e}")
    finally:
        s.close()


@app.post("/leads/{lead_id}/reject", tags=["leads"])
def reject(lead_id: int, body: Decision = Decision()):
    s = _store()
    try:
        row = s.get(lead_id)
        if not row:
            raise HTTPException(404, "lead not found")
        s.advance(lead_id, "rejected", note=body.note or "rejected via API")
        return {"id": lead_id, "state": "rejected"}
    finally:
        s.close()


# ---------------------------------------------------------------- stats

@app.get("/stats", response_model=None, tags=["stats"])
def stats():
    s = _store()
    try:
        counts = s.counts()
        total = s.total_spend()
        pieces = sum(counts.get(k, 0) for k in ("composed", "approved", "mailed"))
        return StatsOut(
            counts=counts,
            spend_by_stage=s.spend_by_stage(),
            total_spend=round(total, 4),
            per_piece=round(total / pieces, 4) if pieces else None,
            budget=settings.budget_usd,
            budget_remaining=round(settings.budget_usd - total, 4),
            source={"name": settings.imagery().name,
                    "license": settings.imagery().license,
                    "attribution": settings.imagery().attribution},
        ).dict()
    finally:
        s.close()


@app.get("/events/{lead_id}", tags=["stats"])
def lead_events(lead_id: int):
    s = _store()
    try:
        rows = s.db.execute(
            "SELECT from_state, to_state, note, created_at FROM events "
            "WHERE lead_id=? ORDER BY id", (lead_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        s.close()


# ---------------------------------------------------------------- pipeline

class RunRequest(BaseModel):
    addresses: Optional[list[str]] = None
    limit: Optional[int] = None
    budget: Optional[float] = None
    no_render: bool = False


def _run_pipeline(job_id, req: RunRequest):
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    ra = _return_address()
    log = []

    def capture(msg):
        log.append(str(msg))
        _JOBS[job_id]["log"] = log[-200:]

    s = _store()
    try:
        if not key:
            raise RuntimeError("GEMINI_API_KEY not set")
        if not ra:
            raise RuntimeError("return address not configured")

        settings.ensure_dirs()
        budget = pipeline.Budget(s, req.budget or settings.budget_usd)
        supp = load_suppression(settings.suppression_file)

        addresses = req.addresses
        if not addresses:
            addresses = [l.strip() for l in
                         settings.addresses_file.read_text().splitlines()
                         if l.strip() and not l.startswith("#")]

        _JOBS[job_id]["stage"] = "discover"
        capture(pipeline.discover(s, addresses, supp))
        _JOBS[job_id]["stage"] = "image"
        pipeline.image(s, req.limit, log=capture)
        _JOBS[job_id]["stage"] = "qualify"
        pipeline.qualify(s, key, budget, req.limit, log=capture)
        if not req.no_render:
            _JOBS[job_id]["stage"] = "render"
            pipeline.render(s, key, budget, req.limit, log=capture)
            _JOBS[job_id]["stage"] = "compose"
            pipeline.compose(s, ra, req.limit, log=capture)
        _JOBS[job_id].update(stage="done", status="completed")
    except Exception as e:
        _JOBS[job_id].update(status="failed", error=f"{type(e).__name__}: {e}")
    finally:
        s.close()


@app.post("/run", tags=["pipeline"])
def start_run(req: RunRequest, background: BackgroundTasks):
    import uuid
    if settings.demo_mode:
        raise HTTPException(403, "Demo mode: batch runs are disabled.")
    job_id = uuid.uuid4().hex[:12]
    _JOBS[job_id] = {"id": job_id, "status": "running", "stage": "queued", "log": []}
    background.add_task(_run_pipeline, job_id, req)
    return {"job_id": job_id, "status": "running"}


@app.get("/run/{job_id}", tags=["pipeline"])
def run_status(job_id: str):
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    return job


class ScanRequest(BaseModel):
    address: str
    radius_m: int = 250
    limit: int = 8
    max_year_built: Optional[int] = None
    #: Narrow to homes sold recently. Only honoured where the parcel layer
    #: carries a sale date (Wake County NC); silently unavailable elsewhere.
    sold_within_months: Optional[int] = None
    render: bool = True


def _run_scan(job_id, req: ScanRequest):
    """Address -> neighbours -> image -> qualify -> render -> postcard.

    Reports progress step by step so the UI can show what is happening rather
    than a spinner.
    """
    from curbside.sources.block import scan
    from curbside.store import Store

    key = os.environ.get("GEMINI_API_KEY", "").strip()
    ra = _return_address()
    job = _JOBS[job_id]
    steps = []

    def step(label, state="done", detail=""):
        steps.append({"label": label, "state": state, "detail": detail})
        job["steps"] = steps

    def running(label):
        steps.append({"label": label, "state": "running", "detail": ""})
        job["steps"] = steps

    def finish(detail=""):
        if steps:
            steps[-1]["state"] = "done"
            steps[-1]["detail"] = detail
        job["steps"] = steps

    s = _store()
    try:
        if not key:
            raise RuntimeError("GEMINI_API_KEY not set")
        if not ra:
            raise RuntimeError("return address not configured")
        settings.ensure_dirs()
        budget = pipeline.Budget(s, settings.budget_usd)
        supp = load_suppression(settings.suppression_file)

        running("Finding address coordinates")
        label, homes = scan(req.address, radius_m=req.radius_m,
                            limit=req.limit,
                            exclude_new_after=req.max_year_built,
                            sold_within_months=req.sold_within_months)
        finish(label)
        job["origin"] = label
        job["sale_filter"] = (
            "applied" if req.sold_within_months and _supports_sale_date()
            else "unavailable" if req.sold_within_months else "off")

        running("Scanning neighbouring properties")
        ids, resumed = [], 0
        for h in homes:
            addr = h.full_address()
            if is_suppressed(addr, supp):
                continue
            lead_id, created = s.add_lead(addr)
            if created:
                s.advance(lead_id, "discovered", lat=h.lat, lon=h.lon,
                          precision="block-scan", lead_source="block_scan")
            else:
                # Seen before. If it stalled part-way (imaged but never
                # rendered, or a failed render), nudge it back into the queue
                # so a re-scan finishes the job instead of reporting nothing.
                row = s.get(lead_id)
                if row and row["state"] == "failed" and row["fail_stage"] == "render":
                    s.advance(lead_id, "qualified", note="re-scan retry")
                    resumed += 1
                elif row and row["state"] == "qualified" and req.render:
                    resumed += 1
            ids.append(lead_id)
        finish(f"{len(ids)} homes found"
               + (f", {resumed} resumed" if resumed else ""))
        job["lead_ids"] = ids

        running("Fetching aerial imagery")
        r = pipeline.image(s, log=lambda *_: None, only=ids)
        finish(f"{r['imaged']} imaged")
        imaged = r["imaged"]

        running(f"Analysing {imaged} driveways" if imaged else "Analysing driveways")
        r = pipeline.qualify(s, key, budget, log=lambda *_: None, only=ids)
        finish(f"{r['passed']} candidates, {r['rejected']} skipped"
               if imaged else "already analysed")

        # Count what is actually queued for render - which includes leads
        # resumed from an earlier scan, not just ones qualified just now.
        candidates = len([r for r in s.ready_for("qualified") if r["id"] in set(ids)])

        if req.render and candidates:
            # The slow step. Name the count so a long wait reads as progress
            # rather than a stall - each render is ~12s, run concurrently.
            running(f"Rendering {candidates} driveway"
                    f"{'s' if candidates != 1 else ''} + quality checks")
            r = pipeline.render(s, key, budget, log=lambda *_: None, only=ids)
            finish(f"{r['rendered']} passed, {r['failed']} rejected by QC")

            running("Laying out postcards")
            r = pipeline.compose(s, ra, log=lambda *_: None, only=ids)
            finish(f"{r['composed']} ready to send")
        elif req.render:
            step("Rendering driveways", "done", "no candidates")

        job.update(status="completed", stage="done")
        _save_seeded_scans()
    except Exception as e:
        if steps:
            steps[-1]["state"] = "error"
            steps[-1]["detail"] = str(e)[:160]
        # Leads processed before the failure are still valid results.
        job.update(status="failed",
                   error=f"{type(e).__name__}: {e}"[:300], steps=steps)
    finally:
        s.close()


@app.post("/scan", tags=["pipeline"])
def start_scan(req: ScanRequest, background: BackgroundTasks):
    """Block scan: one address in, postcards out."""
    import uuid
    if settings.demo_mode:
        raise HTTPException(
            403, "Demo mode: live scanning is disabled on this deployment. "
                 "Browse the pre-generated scans instead.")
    job_id = uuid.uuid4().hex[:12]
    _JOBS[job_id] = {"id": job_id, "status": "running", "stage": "scanning",
                     "steps": [], "address": req.address, "lead_ids": []}
    background.add_task(_run_scan, job_id, req)
    return {"job_id": job_id, "status": "running"}


@app.get("/scans", tags=["pipeline"])
def list_scans():
    """Completed scans, newest first.

    In demo mode this is the entry point: live scanning is disabled, so the
    UI offers the pre-generated blocks instead of an address box.
    """
    out = []
    for job in _JOBS.values():
        if job.get("status") != "completed":
            continue
        ids = job.get("lead_ids") or []
        out.append({"job_id": job["id"], "address": job.get("address"),
                    "origin": job.get("origin"), "homes": len(ids)})
    return list(reversed(out))


@app.get("/scan/{job_id}", tags=["pipeline"])
def scan_status(job_id: str):
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    out = dict(job)
    ids = job.get("lead_ids") or []
    if ids and job.get("status") != "running":
        s = _store()
        try:
            rows = [s.get(i) for i in ids]
            out["leads"] = [LeadOut.from_row(r, _compliance_for(r["address"])).dict()
                            for r in rows if r]
        finally:
            s.close()
    return out


class MailRequest(BaseModel):
    provider: Optional[str] = None
    limit: Optional[int] = None
    daily_cap: Optional[int] = None


@app.post("/mail", tags=["pipeline"])
def send_mail(req: MailRequest):
    s = _store()
    try:
        settings.ensure_dirs()
        budget = pipeline.Budget(s, settings.budget_usd)
        supp = load_suppression(settings.suppression_file)
        log = []
        res = pipeline.mail(s, req.provider or settings.mail_provider, supp,
                            budget, acknowledged_states=_acked(),
                            limit=req.limit, daily_cap=req.daily_cap,
                            log=lambda m: log.append(str(m)))
        return {**res, "log": log}
    except policy.ComplianceError as e:
        raise HTTPException(422, f"compliance: {e}")
    except pipeline.BudgetExceeded as e:
        raise HTTPException(402, str(e))
    finally:
        s.close()
