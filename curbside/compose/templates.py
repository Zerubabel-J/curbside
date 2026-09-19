#!/usr/bin/env python3
"""Postcard copy templates - the words, not the layout.

Direct mail gets about one second of attention between the mailbox and the
bin, so the headline is doing nearly all the work. These six take deliberately
different angles at that second, because which one wins is an empirical
question about a particular market and a particular list, not a matter of
taste. Run two against the same block and count the calls.

Each template is a set of overrides for `postcard.build`, so the layout,
compliance footer and print geometry are identical across all of them - only
the message changes. That keeps a comparison honest: any difference in
response is the copy, not the design.

Angles, and who each one is for:

  curb_appeal   Status and neighbours. Works in tidy, house-proud streets.
  value         Money. Works where owners are near a sale or refinancing.
  problem       The cracked slab. Works on visibly failing driveways.
  neighbourhood Social proof. Works once there is one job on the street.
  seasonal      A deadline. Works when booking calendars are the constraint.
  direct        No cleverness at all. The control to beat.
"""

#: Shared across templates unless one overrides it. The contractor's own
#: details are injected at send time, not stored here.
DEFAULTS = {
    "offer": "Free on-site estimate",
}

TEMPLATES = {
    "curb_appeal": {
        "name": "Curb appeal",
        "note": "Status and first impressions. Best in tidy, house-proud streets.",
        "headline": "That driveway is costing you curb appeal.",
        "subhead": "Here is your home with a new paver driveway.",
        "points": ("Adds lasting value to your home",
                   "Stays level for decades",
                   "Installed in about two days"),
        "offer": "Free on-site estimate",
    },

    "value": {
        "name": "Home value",
        "note": "Money angle. Best where owners may sell or refinance soon.",
        "headline": "The cheapest way to add value to this house.",
        "subhead": "Your driveway, resurfaced. Same home, different first impression.",
        "points": ("One of the highest-return exterior upgrades",
                   "Adds usable, level parking",
                   "Most driveways finished in two days"),
        "offer": "Free estimate, no obligation",
    },

    "problem": {
        "name": "Cracks and settling",
        "note": "Names the defect. Best on visibly failing driveways.",
        "headline": "Cracked. Stained. Settling at the edges.",
        "subhead": "Here is the same driveway, rebuilt properly.",
        "points": ("Fixes the sinking and pooling, not just the surface",
                   "Laid on a compacted base so it stays level",
                   "Backed by a written workmanship warranty"),
        "offer": "Free on-site inspection",
    },

    "neighbourhood": {
        "name": "Neighbours",
        "note": "Social proof. Use once there is at least one job on the street.",
        "headline": "We are working on your street this month.",
        "subhead": "Here is what your driveway could look like.",
        "points": ("Local crews, already working nearby",
                   "See finished work a few doors away",
                   "Neighbour pricing while we are in the area"),
        "offer": "Free estimate while we are on your street",
    },

    "seasonal": {
        "name": "Book the season",
        "note": "A deadline. Use when the booking calendar is the constraint.",
        "headline": "Book your driveway before the season fills.",
        "subhead": "This is your home with a new driveway.",
        "points": ("Installation slots booking now",
                   "Fixed written quote, no surprises",
                   "Most jobs completed in two days"),
        "offer": "Reserve a free estimate",
    },

    "direct": {
        "name": "Plain offer",
        "note": "No cleverness. The control every other template has to beat.",
        "headline": "A new driveway for this address.",
        "subhead": "We rendered your home with a new driveway. Take a look.",
        "points": ("Free estimate at your door",
                   "Written price before any work starts",
                   "Finished in about two days"),
        "offer": "Call for your free estimate",
    },
}

#: Used when no template is named.
DEFAULT_TEMPLATE = "curb_appeal"


def get(name=None):
    """Copy overrides for `name`, ready to splat into `postcard.build`.

    Returns only the keys `build` accepts - `name` and `note` describe the
    template to a human choosing one and are not printed on the piece.
    """
    key = name or DEFAULT_TEMPLATE
    if key not in TEMPLATES:
        raise ValueError(f"unknown template {key!r}; have {sorted(TEMPLATES)}")
    t = TEMPLATES[key]
    return {k: v for k, v in t.items() if k not in ("name", "note")}


def choices():
    """Every template as (key, name, note) - for a picker."""
    return [(k, v["name"], v["note"]) for k, v in TEMPLATES.items()]
