"""API response models. Plain dataclasses -> dicts; no framework coupling."""
import json
from dataclasses import asdict, dataclass, field
from typing import Optional


def _load(v):
    if not v:
        return None
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return None


@dataclass
class LeadOut:
    id: int
    address: str
    state: str
    lat: Optional[float] = None
    lon: Optional[float] = None
    precision: Optional[str] = None
    attempts: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0
    qualification: Optional[dict] = None
    qc: Optional[dict] = None
    fail_stage: Optional[str] = None
    fail_error: Optional[str] = None
    has_before: bool = False
    has_after: bool = False
    has_postcard: bool = False
    compliance: Optional[dict] = None

    @classmethod
    def from_row(cls, row, compliance=None):
        return cls(
            id=row["id"], address=row["address"], state=row["state"],
            lat=row["lat"], lon=row["lon"], precision=row["precision"],
            attempts=row["attempts"],
            created_at=row["created_at"], updated_at=row["updated_at"],
            qualification=_load(row["qualification"]),
            qc=_load(row["qc"]),
            fail_stage=row["fail_stage"], fail_error=row["fail_error"],
            has_before=bool(row["before_path"]),
            has_after=bool(row["after_path"]),
            has_postcard=bool(row["postcard_path"]),
            compliance=compliance,
        )

    def dict(self):
        return asdict(self)


@dataclass
class StatsOut:
    counts: dict = field(default_factory=dict)
    spend_by_stage: dict = field(default_factory=dict)
    total_spend: float = 0.0
    per_piece: Optional[float] = None
    budget: float = 0.0
    budget_remaining: float = 0.0
    source: dict = field(default_factory=dict)

    def dict(self):
        return asdict(self)
