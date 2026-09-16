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
    #: Extra query parameters this service needs. Miami-Dade is a MapServer
    #: whose default export burns street labels and parcel lines into the
    #: image; layer 29 is the raw raster.
    extra_params: dict = field(default_factory=dict)

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
    # --- South East Florida: the target market -------------------------
    # Neither county grants commercial use explicitly; both publish warranty
    # disclaimers only. Florida public-records law (Microdecisions v. Skinner,
    # 889 So.2d 871) suggests agencies cannot assert copyright over public
    # records, but that is a lawyer's call, not an assumption to build on.
    # Broward is deliberately absent: its terms require prior written
    # permission, and its endpoint blocks automated access (HTTP 403).
    "miami_dade": ImagerySource(
        key="miami_dade",
        name="Miami-Dade County Imagery 2024",
        # layers=show:29 is essential - the default export burns street labels,
        # road centrelines and magenta parcel lines into the image.
        service=("https://gisweb.miamidade.gov/arcgis/rest/services/MapCache/"
                 "MDCImagery/MapServer/export"),
        extra_params={"layers": "show:29", "transparent": "false"},
        license="public records, no stated restriction",
        attribution="Imagery: Miami-Dade County",
        resolution_in=3.0,
        states=("FL",),
    ),
    "palm_beach": ImagerySource(
        key="palm_beach",
        name="Palm Beach County Aerial 2026",
        # The service name really is misspelled ("Aerialphotgraphy"). The
        # correctly-spelled variant requires a token.
        service=("https://gis.pbcgov.org/image/rest/services/"
                 "Aerialphotgraphy_2026_WebMercator/ImageServer/exportImage"),
        license="public records, no stated restriction",
        attribution="Imagery: Palm Beach County",
        resolution_in=6.0,
        states=("FL",),
    ),
}

DEFAULT_SOURCE = _env("CURBSIDE_SOURCE", "indiana")


@dataclass
class Settings:
    # --- models ---
    qualify_model: str = _env("CURBSIDE_QUALIFY_MODEL", "gemini-3.5-flash-lite")
    render_model: str = _env("CURBSIDE_RENDER_MODEL", "gemini-3.1-flash-image")

    # --- imagery ---
    #: "aerial" uses state/county orthoimagery; "street" uses Google Street
    #: View for a front-of-house shot. Street reads better on a postcard - the
    #: recipient recognises their own front door - but Google's terms prohibit
    #: their imagery in print advertising, so it is prototype-only.
    view: str = _env("CURBSIDE_VIEW", "aerial")
    source: str = DEFAULT_SOURCE
    crop_meters: float = _env("CURBSIDE_CROP_METERS", 32.0, float)
    #: Street View framing. 80 degrees fits a typical lot with its driveway;
    #: a slight upward pitch keeps the roofline in frame without tilting the
    #: driveway out of the bottom.
    street_fov: int = _env("CURBSIDE_STREET_FOV", 80, int)
    street_pitch: int = _env("CURBSIDE_STREET_PITCH", 8, int)
    image_px: int = _env("CURBSIDE_IMAGE_PX", 1024, int)

    # --- spend ---
    budget_usd: float = _env("CURBSIDE_BUDGET", 5.00, float)
    daily_mail_cap: int = _env("CURBSIDE_DAILY_MAIL_CAP", 50, int)

    # --- QC thresholds ---
    mask_threshold: int = _env("CURBSIDE_MASK_THRESHOLD", 26, int)
    #: Street-level renders shift foliage and lighting across the whole frame,
    #: so a diff at the aerial threshold captures the garden along with the
    #: driveway. Measured: 26 -> 16.6% mask (driveway + beds), 50 -> 2.7%
    #: (driveway only).
    mask_threshold_street: int = _env("CURBSIDE_MASK_THRESHOLD_STREET", 50, int)
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

    # --- mail ---
    mail_provider: str = _env("CURBSIDE_MAIL_PROVIDER", "dryrun")

    def imagery(self):
        if self.source not in SOURCES:
            raise ValueError(f"unknown source {self.source!r}; have {list(SOURCES)}")
        return SOURCES[self.source]

    @property
    def street_view(self):
        return self.view == "street"

    @property
    def parcel_source(self):
        """County parcel layer matching the active imagery source, if any.

        Aiming a street-level camera needs the real property position. Google
        answers RANGE_INTERPOLATED for a large share of addresses - an estimate
        along the street that can sit over 100 m from the house.
        """
        from curbside.sources.parcels import SOURCES as PARCELS
        return self.source if self.source in PARCELS else None

    @property
    def renderable_sources(self):
        return {k: v for k, v in SOURCES.items() if v.renderable}

    def ensure_dirs(self):
        for d in (VAR, self.images_dir, self.output_dir, self.outbox_dir):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
