#!/usr/bin/env python3
"""Gemini calls: qualify (vision) and render (image edit)."""
import base64, json, os, pathlib, time, urllib.error, urllib.request

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/interactions"
QUALIFY_MODEL = "gemini-3.5-flash-lite"
RENDER_MODEL  = "gemini-3.1-flash-image"

PRICES = {
    "gemini-3.5-flash-lite":       {"in": 0.30, "out": 2.50},
    "gemini-3.1-flash-image":      {"per_image": 0.067},
    "gemini-3.1-flash-lite-image": {"per_image": 0.0336},
}

QUALIFY_PROMPT = """This is a top-down aerial photograph (3 inch/pixel) of a US
residential property, centred on the home. You are qualifying it for a DRIVEWAY
UPGRADE direct-mail campaign - the offer is a premium paver driveway, so both
worn driveways AND plain-but-sound ones are valid candidates.

Identify the driveway: the paved strip running from the street to the house or
garage. Ignore the public sidewalk and the road itself.

QUALIFY when a driveway is visible and could be upgraded to pavers - including
plain concrete or asphalt in decent condition.

REJECT only when: no driveway is visible at all; the property is clearly not a
single-family home; or the driveway is so obscured by trees, shadow or vehicles
that it cannot be located.

Score condition 1-10 where 1 is pristine new and 10 is badly broken. A low
score is fine - it still qualifies for an upgrade."""

QUALIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "single_family_home": {"type": "boolean"},
        "driveway_visible":   {"type": "boolean"},
        "surface":            {"type": "string",
                               "enum": ["concrete","asphalt","gravel","pavers","dirt","none","unclear"]},
        "condition_score":    {"type": "integer",
                               "description": "1 = pristine new, 10 = badly cracked/stained/broken"},
        "obstruction":        {"type": "string",
                               "enum": ["none","trees","shadow","vehicles","heavy"]},
        "qualified":          {"type": "boolean"},
        "reason":             {"type": "string", "description": "one sentence"},
    },
    "required": ["single_family_home","driveway_visible","surface",
                 "condition_score","obstruction","qualified","reason"],
}

RENDER_PROMPT = """You are performing a LOCAL EDIT on an aerial photograph.
Almost all of this image must come back untouched.

EDIT EXACTLY ONE THING: the driveway - the paved strip running between the
street and the house or garage. Replace its surface with terracotta and
warm-red clay pavers in a herringbone pattern, edged by a charcoal soldier
course two pavers wide. The red must read clearly against the grey pavement
and green lawn around it.

DO NOT TOUCH ANYTHING ELSE. The house, its roof, the lawn, trees, the public
sidewalk, the street, neighbouring lots and any parked vehicle must be
returned byte-for-byte as they arrived. Do not repaint, restyle, brighten or
re-render them. Do not extend the paved area beyond the driveway's existing
outline - not onto the lawn, not onto the sidewalk, not onto the street.

Every shadow currently falling across the driveway stays, at its original
position and opacity.

Sanity check before answering: the driveway is a small part of this frame. If
you have changed most of the image, you have made a mistake - go back and
change only the driveway.

Keep the same framing, scale and camera angle. Photographic grain throughout."""


_CONSTRAINTS = """

BOUNDARY DISCIPLINE - follow the existing driveway outline exactly. Do not
extend onto the lawn, the public sidewalk, the street, or the neighbouring
property. Where the old driveway ended, the new one ends.

PRESERVE PIXEL-IDENTICAL: the house and roof, all lawn and trees, every shadow
falling across the driveway at its original opacity, the public sidewalk, the
street, neighbouring properties, and any vehicle parked on the driveway.

Keep the same framing, scale and camera angle. Photographic grain throughout."""

# Tried in order. Attempt 1 is the strongest marketing image; later attempts
# trade visual punch for boundary discipline so a home is not lost entirely.
RENDER_LADDER = [
    ("bold", RENDER_PROMPT),
    ("tight", """You are performing a LOCAL EDIT on an aerial photograph. Almost
all of this image must come back untouched.

Edit exactly one thing: the driveway - the SHORT private strip connecting the
house or garage to the street. It is NOT the public road, which runs across
the frame and has cars parked along it.

Replace only that strip with terracotta clay pavers in a herringbone pattern,
edged by a charcoal border.

Sanity check before answering: the driveway is a small part of this frame. If
you have changed most of the image, or anything that runs edge to edge, you
have made a mistake.""" + _CONSTRAINTS),
    ("conservative", """Make a small, careful edit to this aerial photograph.

Resurface ONLY the existing driveway with clean warm-toned pavers. Keep the
edit modest and tightly inside the driveway's current outline - it is better
to change slightly too little than to spill onto the lawn, the sidewalk or
the street.

Everything else in the photograph must be returned exactly as it arrived."""
     + _CONSTRAINTS),
]


def _post(payload, key, timeout=180, attempts=3):
    """POST with a short retry.

    Transient read timeouts and 5xx responses are common enough that failing a
    whole lead on the first one wastes the render we already paid for.
    """
    body = json.dumps(payload).encode()
    last = None
    for attempt in range(attempts):
        req = urllib.request.Request(
            ENDPOINT, data=body,
            headers={"Content-Type": "application/json", "x-goog-api-key": key})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r), None
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:300]
            # 4xx is our fault - retrying will not help.
            if e.code < 500 and e.code != 429:
                return None, f"HTTP {e.code}: {detail}"
            last = f"HTTP {e.code}: {detail}"
        except Exception as e:
            last = str(e)
        if attempt < attempts - 1:
            time.sleep(1.5 * (attempt + 1))
    return None, last


def _output(resp, kind="text"):
    """Interactions API: steps[] -> model_output -> content[]. Skips 'thought'."""
    for step in resp.get("steps", []):
        if step.get("type") != "model_output":
            continue
        for block in step.get("content", []):
            if kind == "text" and block.get("type") == "text":
                return block.get("text")
            if kind == "image":
                for f in ("data", "image_bytes", "b64_json"):
                    v = block.get(f)
                    if isinstance(v, str) and len(v) > 500:
                        return v
                inline = block.get("inline_data") or block.get("inlineData") or {}
                if isinstance(inline.get("data"), str) and len(inline["data"]) > 500:
                    return inline["data"]
    return None


def ask_json(img_path, prompt, schema, key, model=None):
    """Generic structured vision call. Returns (dict|None, error, cost)."""
    model = model or QUALIFY_MODEL
    b64 = base64.b64encode(pathlib.Path(img_path).read_bytes()).decode()
    resp, err = _post({
        "model": model,
        "input": [{"type": "text", "text": prompt},
                  {"type": "image", "mime_type": "image/jpeg", "data": b64}],
        "response_format": {"type": "text", "mime_type": "application/json",
                            "schema": schema},
    }, key)
    if err:
        return None, err, 0.0
    p = PRICES[model]
    cost = (1100/1e6)*p["in"] + (200/1e6)*p["out"]
    text = _output(resp, "text")
    if not text:
        return None, "no text in response", cost
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```")[1].lstrip("json").strip()
    try:
        return json.loads(t), None, cost
    except json.JSONDecodeError:
        return None, f"unparseable: {t[:200]}", cost


def qualify(img_path, key, model=QUALIFY_MODEL):
    b64 = base64.b64encode(pathlib.Path(img_path).read_bytes()).decode()
    resp, err = _post({
        "model": model,
        "input": [{"type": "text", "text": QUALIFY_PROMPT},
                  {"type": "image", "mime_type": "image/jpeg", "data": b64}],
        "response_format": {"type": "text", "mime_type": "application/json",
                            "schema": QUALIFY_SCHEMA},
    }, key)
    if err:
        return None, err, 0.0
    p = PRICES[model]
    cost = (1100/1e6)*p["in"] + (220/1e6)*p["out"]
    text = _output(resp, "text")
    if not text:
        return None, "no text in response", cost
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```")[1].lstrip("json").strip()
    try:
        return json.loads(t), None, cost
    except json.JSONDecodeError:
        return None, f"unparseable: {t[:200]}", cost


def render(img_path, out_path, key, model=RENDER_MODEL, prompt=None):
    b64 = base64.b64encode(pathlib.Path(img_path).read_bytes()).decode()
    resp, err = _post({
        "model": model,
        "input": [{"type": "text", "text": prompt or RENDER_PROMPT},
                  {"type": "image", "mime_type": "image/jpeg", "data": b64}],
        "response_format": {"type": "image", "mime_type": "image/jpeg"},
    }, key)
    if err:
        return False, err, 0.0
    cost = PRICES[model]["per_image"]
    b = _output(resp, "image")
    if not b:
        return False, f"no image: {json.dumps(resp)[:250]}", cost
    pathlib.Path(out_path).write_bytes(base64.b64decode(b))
    return True, "ok", cost
