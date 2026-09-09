#!/usr/bin/env python3
"""Gemini calls: qualify (vision) and render (image edit)."""
import base64, json, os, pathlib, urllib.error, urllib.request

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
RESURFACING direct-mail campaign.

Identify the driveway: the paved strip running from the street to the house or
garage. Ignore the public sidewalk and the road itself.

REJECT if: no driveway is visible; the driveway is already new and in good
condition; the property is not a single-family home; or the driveway is too
obscured by trees, shadow or vehicles to assess.

Be strict. Every wrong PASS costs a wasted postcard."""

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

RENDER_PROMPT = """Photorealistic aerial edit of a residential property.

Replace the existing plain driveway with a premium PAVER driveway: interlocking
rectangular concrete pavers laid in a running-bond pattern, warm sandy-grey with
subtle tonal variation between individual pavers, bordered on every edge by a
contrasting darker charcoal soldier course two pavers wide.

The paver pattern and the border must both be clearly visible and regular at
this resolution - this is the visual centrepiece of a marketing image, so the
upgrade must be obvious at a glance against the plain grey pavement around it.

BOUNDARY DISCIPLINE - follow the existing driveway outline exactly. Do not
extend onto the lawn, the public sidewalk, the street, or the neighbouring
property. Where the old driveway ended, the new one ends.

PRESERVE PIXEL-IDENTICAL: the house and roof, all lawn and trees, every shadow
falling across the driveway at its original opacity and position, the public
sidewalk, the street, neighbouring properties, and any vehicle parked on the
driveway - the car stays exactly where and as it is.

Keep the same framing, scale and camera angle. Photographic grain throughout -
this must look like a photograph of a real paver driveway, not a flat graphic."""


def _post(payload, key, timeout=180):
    req = urllib.request.Request(
        ENDPOINT, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.read().decode()[:300]}"
    except Exception as e:
        return None, str(e)


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


def render(img_path, out_path, key, model=RENDER_MODEL):
    b64 = base64.b64encode(pathlib.Path(img_path).read_bytes()).decode()
    resp, err = _post({
        "model": model,
        "input": [{"type": "text", "text": RENDER_PROMPT},
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
