"""HTTP API over the pipeline.

The CLI and the API are two front-ends onto the same stage functions - no
logic lives here that isn't also reachable from the terminal.
"""
import io
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
from curbside.mail.providers import load_suppression
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

# In-process job state for the async `run` endpoint.
_JOBS = {}


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


def _compliance_for(address):
    d = policy.check_lead(address, has_disclosure=True, has_return_address=True,
                          has_opt_out=True, acknowledged_states=_acked())
    return {"allowed": d.allowed, "reasons": list(d.reasons),
            "warnings": list(d.warnings)}


# ---------------------------------------------------------------- health

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
