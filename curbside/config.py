"""Configuration. Environment overrides everything; sane defaults otherwise.

Nothing here is secret — secrets come from the environment only, never a file
in the repo.
"""
import os
import pathlib
from dataclasses import dataclass, field

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
VAR = ROOT / "var"


def _env(name, default, cast=str):
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    try:
        return cast(v) if cast is not bool else v.lower() in ("1", "true", "yes")
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# Imagery sources. Each must be public-domain or otherwise licensed for
# commercial derivative use in print — see docs/LICENSING.md.
# --------------------------------------------------------------------------

# Below roughly 6 in/px a driveway edge is not resolvable: renders drift and QC
# rejects nearly everything. Sources coarser than this are kept for reference
# but excluded from rendering.
RENDERABLE_MAX_IN = 6.0


@dataclass(frozen=True)
class ImagerySource:
    key: str
    name: str
    service: str
    license: str
    attribution: str
    resolution_in: float
    states: tuple

    @property
    def renderable(self):
        return self.resolution_in <= RENDERABLE_MAX_IN


SOURCES = {
    "indiana": ImagerySource(
        key="indiana",
        name="Indiana Current Imagery",
        service=("https://di-ingov.img.arcgis.com/arcgis/rest/services/"
                 "DynamicWebMercator/Indiana_Current_Imagery/ImageServer/exportImage"),
        license="CC0-1.0",
        attribution="Imagery: Indiana Geographic Information Office (IGIO), CC0-1.0",
        resolution_in=3.0,
        states=("IN",),
    ),
    "connecticut": ImagerySource(
        key="connecticut",
        name="Connecticut Ortho 2023",
        service=("https://cteco.uconn.edu/ctraster/rest/services/images/"
                 "Ortho_2023/ImageServer/exportImage"),
        license="CC0-1.0",
        attribution="Imagery: CT ECO / UConn CLEAR, CC0-1.0",
        resolution_in=3.0,
        states=("CT",),
    ),
    "north_carolina": ImagerySource(
        key="north_carolina",
        name="NC OneMap Latest Orthoimagery",
        service=("https://services.nconemap.gov/secure/rest/services/Imagery/"
                 "Orthoimagery_Latest/ImageServer/exportImage"),
        license="public-domain-unrestricted",
        attribution="Imagery: NC OneMap, NC Center for Geographic Information",
        # Measured live at 0.5 m/px, not the 6in the county documents suggest.
        # Too coarse for reliable driveway rendering - renders routinely fail
        # QC on drift because the model cannot resolve the driveway edge.
        # Kept for reference; not recommended for production rendering.
        resolution_in=19.7,
        states=("NC",),
    ),
}

DEFAULT_SOURCE = _env("CURBSIDE_SOURCE", "indiana")


@dataclass
class Settings:
    # --- models ---
    qualify_model: str = _env("CURBSIDE_QUALIFY_MODEL", "gemini-3.5-flash-lite")
    render_model: str = _env("CURBSIDE_RENDER_MODEL", "gemini-3.1-flash-image")

    # --- imagery ---
    source: str = DEFAULT_SOURCE
    crop_meters: float = _env("CURBSIDE_CROP_METERS", 32.0, float)
    image_px: int = _env("CURBSIDE_IMAGE_PX", 1024, int)

    # --- spend ---
    budget_usd: float = _env("CURBSIDE_BUDGET", 5.00, float)
    daily_mail_cap: int = _env("CURBSIDE_DAILY_MAIL_CAP", 50, int)

    # --- QC thresholds ---
    mask_threshold: int = _env("CURBSIDE_MASK_THRESHOLD", 26, int)
    qc_drift_threshold: int = _env("CURBSIDE_QC_DRIFT", 18, int)
    # Drift is measured, reported, and then DISCARDED by compositing - the
    # postcard uses the original photo outside the mask regardless. So this
    # is a warning signal, not damage, and can run loose.
    qc_max_outside_frac: float = _env("CURBSIDE_QC_MAX_OUTSIDE", 0.30, float)
    qc_min_mask_frac: float = _env("CURBSIDE_QC_MIN_MASK", 0.008, float)
    # On a narrow urban lot the driveway genuinely is a large share of the
    # frame. Above ~70% the model has repainted the scene, not the driveway.
    qc_max_mask_frac: float = _env("CURBSIDE_QC_MAX_MASK", 0.70, float)

    # --- concurrency ---
    # Per-lead work is network-bound (segment, render, QC), so a handful of
    # workers turns a serial batch into a near-parallel one. Keep modest to
    # stay well inside the model's rate limits.
    workers: int = _env("CURBSIDE_WORKERS", 4, int)

    # --- render retries ---
    # A single render is one roll of the dice; the bold prompt reads best but
    # drifts most. Retrying with tighter prompts recovers most failures for
    # the cost of one extra render on the leads that need it.
    render_attempts: int = _env("CURBSIDE_RENDER_ATTEMPTS", 2, int)

    # --- retries ---
    max_attempts: int = _env("CURBSIDE_MAX_ATTEMPTS", 3, int)

    # --- paths ---
    db_path: pathlib.Path = field(default_factory=lambda: VAR / "curbside.db")
    images_dir: pathlib.Path = field(default_factory=lambda: VAR / "images")
    output_dir: pathlib.Path = field(default_factory=lambda: VAR / "output")
    outbox_dir: pathlib.Path = field(default_factory=lambda: VAR / "outbox")
    addresses_file: pathlib.Path = field(default_factory=lambda: DATA / "addresses.txt")
    suppression_file: pathlib.Path = field(default_factory=lambda: DATA / "suppression.txt")

    # --- demo ---
    # A public URL with a "Scan block" button spends real API credit on every
    # click. Demo mode serves the seeded results read-only and refuses paid
    # work, so a shared link cannot run up a bill.
    demo_mode: bool = _env("CURBSIDE_DEMO_MODE", False, bool)

    # --- mail ---
    mail_provider: str = _env("CURBSIDE_MAIL_PROVIDER", "dryrun")

    def imagery(self):
        if self.source not in SOURCES:
            raise ValueError(f"unknown source {self.source!r}; have {list(SOURCES)}")
        return SOURCES[self.source]

    @property
    def renderable_sources(self):
        return {k: v for k, v in SOURCES.items() if v.renderable}

    def ensure_dirs(self):
        for d in (VAR, self.images_dir, self.output_dir, self.outbox_dir):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
