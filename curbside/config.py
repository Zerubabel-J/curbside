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

@dataclass(frozen=True)
class ImagerySource:
    key: str
    name: str
    service: str
    license: str
    attribution: str
    resolution_in: float
    states: tuple


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
        resolution_in=6.0,
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
    qc_max_outside_frac: float = _env("CURBSIDE_QC_MAX_OUTSIDE", 0.06, float)
    qc_min_mask_frac: float = _env("CURBSIDE_QC_MIN_MASK", 0.008, float)
    qc_max_mask_frac: float = _env("CURBSIDE_QC_MAX_MASK", 0.45, float)

    # --- retries ---
    max_attempts: int = _env("CURBSIDE_MAX_ATTEMPTS", 3, int)

    # --- paths ---
    db_path: pathlib.Path = field(default_factory=lambda: VAR / "curbside.db")
    images_dir: pathlib.Path = field(default_factory=lambda: VAR / "images")
    output_dir: pathlib.Path = field(default_factory=lambda: VAR / "output")
    outbox_dir: pathlib.Path = field(default_factory=lambda: VAR / "outbox")
    addresses_file: pathlib.Path = field(default_factory=lambda: DATA / "addresses.txt")
    suppression_file: pathlib.Path = field(default_factory=lambda: DATA / "suppression.txt")

    # --- mail ---
    mail_provider: str = _env("CURBSIDE_MAIL_PROVIDER", "dryrun")

    def imagery(self):
        if self.source not in SOURCES:
            raise ValueError(f"unknown source {self.source!r}; have {list(SOURCES)}")
        return SOURCES[self.source]

    def ensure_dirs(self):
        for d in (VAR, self.images_dir, self.output_dir, self.outbox_dir):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
