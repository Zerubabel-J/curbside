#!/usr/bin/env python3
"""Driveway designs and the prompts that render them.

Why this exists separately from `gemini.py`. The first render prompts were
written while the model was paving people's lawns, so they grew a paragraph of
restrictions for every sentence of description - "DO NOT TOUCH ANYTHING ELSE",
"you have made a mistake", "the driveway is a modest part of this frame". They
worked: nothing outside the driveway changed. They also produced exactly what
the client complained about - *"just paint orange texture onto the driveways
that are already available"*.

The restrictions were solving a problem that masked compositing already solves.
The output is rebuilt as original-everywhere, rendered-only-inside-the-mask, so
a pixel outside the driveway cannot change no matter what the model returns.
Spending the prompt on warnings buys nothing and costs the description.

So these prompts describe the *finished installation* the way a landscape
architect would, and say what must not change once, briefly, at the end.

Two kinds of work:

  `RESURFACE`  the existing driveway shape, in a better material
  `RESHAPE`    a new footprint - a circular or teardrop turnaround - where the
               frontage has room for one

Reshaping was a direct instruction: *"I would like for it to turn straight
driveways into semi-circular driveways"*, and for homes with only a front
walkway, *"I wouldn't make a walkway a rejection criteria... I think you could
turn a walkway into a circular driveway"*. That changes the safety story - a
new footprint necessarily covers lawn - so reshaping carries its own mask rule
and is never silently applied to a home that only asked for resurfacing.
"""

# --------------------------------------------------------------------------
# Materials - described as a finished installation, not as a colour
# --------------------------------------------------------------------------

#: Each entry describes what the surface looks like when it is *finished and
#: photographed for a brochure*. Colour alone gives a flat repaint; the grain,
#: the joint, the border and the edge restraint are what make it read as a real
#: installation.
DESIGNS = {
    "travertine": {
        "name": "Ivory travertine",
        "surface": (
            "large-format ivory and champagne travertine pavers in a French "
            "pattern of four repeating sizes, each stone tumbled with softly "
            "eased edges and fine sand-coloured joints barely a finger wide"),
        "border": (
            "a double soldier course of the same travertine in a deeper walnut "
            "tone, running the full perimeter"),
        "notes": "warm, bright, Mediterranean; reads expensive in sunlight",
    },
    "charcoal_granite": {
        "name": "Charcoal granite",
        "surface": (
            "dark charcoal and graphite granite-look pavers in a tight "
            "running-bond, each unit with a subtle flamed texture that catches "
            "the light, joints swept with matching dark sand"),
        "border": (
            "a single silver-grey soldier course framing the whole surface, "
            "with a crisp mitred corner where it turns"),
        "notes": "modern, architectural; strong against a pale house",
    },
    "clay_paver": {
        "name": "Terracotta clay",
        "surface": (
            "genuine kiln-fired clay pavers in mixed terracotta, rust and "
            "umber tones laid in a 45-degree herringbone, the colour varying "
            "stone to stone the way real clay does"),
        "border": (
            "a charcoal clinker soldier course, two units deep at the street "
            "entrance where tyres track"),
        "notes": "traditional, warm; classic for older brick houses",
    },
    "exposed_aggregate": {
        "name": "Exposed aggregate",
        "surface": (
            "polished exposed-aggregate concrete in warm sandstone, the "
            "washed surface showing rounded river pebble through a pale "
            "matrix, divided by crisp saw-cut control joints in a wide "
            "diamond pattern"),
        "border": (
            "a smooth-troweled charcoal concrete band around the edge, "
            "slightly proud of the aggregate"),
        "notes": "seamless and monolithic; suits contemporary homes",
    },
    "bluestone": {
        "name": "Bluestone",
        "surface": (
            "thermal-finish bluestone slabs in random ashlar, blue-grey with "
            "occasional rust and lilac veining, thin dark joints, the surface "
            "flat and matte rather than glossy"),
        "border": (
            "a full-length bluestone kerb edging in a darker tone, set flush"),
        "notes": "understated and costly-looking; strong in shade",
    },
    "marble_chip": {
        "name": "Shell and marble",
        "surface": (
            "compacted crushed white marble and shell aggregate, bright and "
            "fine-grained, raked flat and held level, with a faint pale sheen "
            "in sun"),
        "border": (
            "a coral-stone edge restraint holding the aggregate, laid in "
            "irregular lengths"),
        "notes": "coastal Florida; pairs with white and pastel houses",
    },
}

#: Order the bench and the pipeline rotate through.
DESIGN_KEYS = list(DESIGNS)


# --------------------------------------------------------------------------
# Shapes
# --------------------------------------------------------------------------

SHAPES = {
    "resurface": {
        "name": "Resurface in place",
        "geometry": (
            "Keep the driveway's existing outline exactly. Do not widen it, "
            "do not extend it onto the lawn, and do not change where it meets "
            "the road or the garage."),
        "covers_lawn": False,
    },
    "circular": {
        "name": "Circular turnaround",
        "geometry": (
            "Replace the straight run with a CIRCULAR DRIVEWAY: the paving "
            "sweeps in from the kerb, curves in a broad arc across the front "
            "of the house past the entrance, and returns to the kerb a second "
            "time, leaving a planted island of lawn in the middle of the "
            "circle. Both ends meet the road at a proper dropped kerb. The "
            "arc is generous enough for a car to follow without reversing, "
            "and the island keeps a mature tree or planting bed if one is "
            "already there."),
        "covers_lawn": True,
    },
    "teardrop": {
        "name": "Teardrop entry",
        "geometry": (
            "Replace the straight run with a TEARDROP DRIVEWAY: a single "
            "entrance from the kerb that widens into a rounded turning court "
            "in front of the house, then narrows back to the same entrance. "
            "The court is wide enough to turn a car and to park two abreast "
            "at the house end. A planted bed sits inside the loop."),
        "covers_lawn": True,
    },
    "widened": {
        "name": "Widened with apron",
        "geometry": (
            "Keep the driveway where it is but widen it to a comfortable two "
            "cars, and add a generous parking apron in front of the garage "
            "that squares off neatly against the garage door. The edge nearest "
            "the house curves gently rather than running straight."),
        "covers_lawn": True,
    },
}

SHAPE_KEYS = list(SHAPES)


# --------------------------------------------------------------------------

#: How the instruction is *framed* turns out to matter more than what it says.
#: Measured on the same house, same material, changing only the opening words:
#:
#:     "You are performing a LOCAL EDIT..."      sky drift 80%
#:     "...photographed for a brochure" (desc)   sky drift 79%
#:     old production prompt                     sky drift  9%
#:     "Inpaint ONLY ... every other pixel       sky drift  9%, and the
#:      as locked and uneditable"                 smallest total change (16%)
#:
#: Sky drift is the share of pixels above the eaves that changed - it measures
#: whether the model edited the photograph or painted a new one. At 80% the
#: output is a beautiful picture of a *different* house, which destroys the
#: recognition the postcard depends on.
#:
#: Naming the region as locked, before describing anything, is what holds. The
#: material description then buys quality without costing fidelity.
_CRAFT = """as a newly completed premium installation: straight courses, even
joint widths, clean cuts where the pattern meets the border, no cracks,
staining or patching, following the photograph's existing perspective,
daylight and shadow direction"""


#: Three ways of saying the same thing. They are not redundant: measured on the
#: bench, no single framing wins everywhere, and the houses each one rescues
#: are different.
#:
#:     240 Tangier Ave     inpaint 90%   photoshop  0%   swap 66%
#:     8113 Talliho Dr     inpaint 72%   photoshop 66%   swap  0%
#:
#: Both of those houses failed three consecutive attempts at the same prompt -
#: the failure is deterministic per framing, so retrying one wording is waste
#: and changing the wording is what recovers the render. Trying the ladder in
#: order lifted the bench from 16/27 to 20/27.
_FRAMINGS = [
    ("inpaint", """Inpaint ONLY {region} of this photograph. Treat every other
pixel as locked and uneditable.

Inside that region paint {surface}, edged by {border}, {craft}.{geometry}{extra}

Outside that region reproduce the input photograph exactly. Do not re-render
the house, its roof, windows or garage door, the neighbouring houses, the
trees, the sky, the power lines or the street. The camera does not move and
the framing does not change."""),

    ("photoshop", """Act as a photo retoucher working in Photoshop. You have
made a selection around {region} only. Fill that selection with {surface},
edged by {border}, {craft}, matching the photograph's perspective, lighting
and shadow direction.{geometry}{extra}

The selection is the ONLY editable area. Every pixel outside it sits on a
locked layer and is returned bit-for-bit identical: the house, its roof and
windows, the sky, the trees, the neighbouring houses, parked cars, the road
and the kerb."""),

    ("add", """Add a new driveway to the ground in this photograph.{geometry}

It is paved in {surface}, edged by {border}, {craft}.{extra}

This is an addition to an existing photograph, not a new picture. Every pixel
that is not ground stays exactly as it is - the house at the same size and
position, its roof, walls, windows and doors, the trees, the sky, the
neighbouring buildings, the kerb line and the camera angle."""),

    ("swap", """This photograph shows a house. Perform a material swap on
{region} only: {surface}, edged by {border}, {craft}.{geometry}{extra}

Nothing else about the scene changes. Same camera position, same focal length,
same framing, same house, same trees, same sky, same time of day, same
shadows. The only difference between input and output is the ground surface."""),
]


def render_prompt(design_key, shape_key="resurface", framing=0):
    """One instruction for one design, in one of three framings.

    `framing` indexes `_FRAMINGS`. Callers that render once should walk the
    ladder rather than picking one - see `render_ladder`.
    """
    d = DESIGNS[design_key]
    s = SHAPES[shape_key]

    if s["covers_lawn"]:
        region = ("the driveway and the front lawn between the house and the "
                  "road - the ground plane only")
        extra = ("\n\nThe new footprint covers part of the lawn; that is "
                 "intended. Finish the cut edge of the remaining grass neatly "
                 "against the border course, and pave around any mature tree.")
    else:
        region = "the driveway paving"
        extra = ""

    _, template = _FRAMINGS[framing % len(_FRAMINGS)]
    return template.format(region=region, surface=d["surface"],
                           border=d["border"], craft=_CRAFT,
                           geometry=f"\n\n{s['geometry']}", extra=extra)


#: Which framing to try first, by kind of work. Measured on one house, same
#: shape, same material, changing only the framing:
#:
#:     teardrop   inpaint 43%   photoshop 70%   swap  2%
#:
#: `inpaint` wins for resurfacing - naming a small locked region suits a small
#: edit. It loses badly for reshaping, where the region is most of the ground
#: and "inpaint this area" reads as licence to redesign it. `swap`, which
#: fixes the camera and the scene and changes only the surface, holds far
#: better when the work is large. Trying them in the wrong order wastes two
#: renders and sometimes never reaches the one that works.
_ORDER = {
    # Resurfacing is a small edit inside a small region, and naming that
    # region as locked is what holds.
    "resurface": ("inpaint", "photoshop", "swap", "add"),

    # Reshaping is the opposite. "Inpaint this region" over most of the
    # ground reads as licence to redesign the lot, and the model rebuilds the
    # scene - 94% of the house redrawn on homes where `add` scores 1%.
    # Framing it as *adding a loop to the lawn* keeps it editing; framing it
    # as *this area becomes a driveway* makes it redesign. Measured on six
    # homes, `add` alone kept the house on three where the previous order
    # kept it on one.
    "circular":  ("add", "swap", "photoshop", "inpaint"),
    "teardrop":  ("add", "swap", "photoshop", "inpaint"),
    "widened":   ("add", "swap", "inpaint", "photoshop"),
}


def render_ladder(design_key, shape_key="resurface"):
    """Every framing, in the order to try them. Stop at the first that holds."""
    by_name = {name: i for i, (name, _) in enumerate(_FRAMINGS)}
    order = _ORDER.get(shape_key, tuple(by_name))
    return [(name, render_prompt(design_key, shape_key, by_name[name]))
            for name in order]


def qualify_prompt():
    """Whether a home can take a new driveway - resurfaced or newly shaped.

    The earlier prompt rejected any home whose only paved path was a walkway.
    The client's instruction was the opposite: *"I wouldn't make a walkway a
    rejection criteria... I think you could turn a walkway into a circular
    driveway."* A blank frontage is an opportunity, not a disqualification, so
    the question here is whether there is room to build - not whether paving
    already exists.
    """
    return """This is a street-level photograph of a US home, taken from the
road. You are deciding whether it is a candidate for a new driveway - either
resurfacing the one it has, or building a better-shaped one.

Report what you can see:

- `has_driveway`: is there a paved strip a car could drive on, running from
  the road toward the house or garage?
- `frontage_width_cars`: roughly how many cars could park side by side across
  the open front of the property, ignoring what is paved today. This measures
  the room available to build in.
- `has_front_lawn`: is there open lawn or low planting between the house and
  the road, unobstructed by mature trees, walls or water?
- `setback`: is the house close to the road, a normal suburban distance back,
  or set well back behind a deep front garden?
- `frontage_clear`: can you actually see the ground in front of the house, or
  is it hidden behind a hedge, wall, fence or dense foliage?

QUALIFY the property when the front of the house is visible and there is
either an existing driveway OR enough open frontage to build one. A home with
only a front walkway and an open lawn QUALIFIES - that is a candidate for a
circular driveway, not a rejection.

REJECT only when: the frontage is genuinely hidden from the road; the shot
faces down the street rather than at a property; the building is a duplex,
apartment block or commercial premises; or the front is entirely taken by
mature trees, water or a boundary wall with no room to build.

Score `condition` 1-10 for any existing paving, where 1 is pristine and 10 is
badly broken. A pristine driveway still qualifies - the offer is an upgrade.

Recommend `best_shape`. Prefer a NEW SHAPE wherever the lot allows one - the
offer being sold is a rebuilt driveway, not a recoloured one, and a homeowner
who sees the same outline in a different material has been shown nothing worth
paying for. Resurfacing is the fallback, not the default.

  `circular`  - the frontage is 3 cars wide or more with open lawn: build a
                full loop with a planted island. Prefer this whenever it fits.
  `teardrop`  - the frontage is 2-3 cars wide with some lawn: build a turning
                court that enters and leaves by the same point.
  `widened`   - a narrow drive hemmed in by trees, water or a boundary, but
                with lawn on one side to grow into.
  `resurface` - choose this ONLY when there is genuinely no room to change the
                footprint: the house sits hard against the road, or every
                side is blocked by mature planting, water or a wall."""


QUALIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "single_family_home": {"type": "boolean"},
        "has_driveway": {"type": "boolean"},
        "frontage_width_cars": {"type": "integer"},
        "has_front_lawn": {"type": "boolean"},
        "setback": {"type": "string",
                    "enum": ["close", "normal", "deep"]},
        "frontage_clear": {"type": "boolean"},
        "surface": {"type": "string",
                    "enum": ["concrete", "asphalt", "gravel", "pavers",
                             "dirt", "none", "unclear"]},
        "condition": {"type": "integer"},
        "best_shape": {"type": "string",
                       "enum": ["resurface", "circular", "teardrop", "widened"]},
        "qualified": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["single_family_home", "has_driveway", "frontage_width_cars",
                 "has_front_lawn", "setback", "frontage_clear", "surface",
                 "condition", "best_shape", "qualified", "reason"],
}
