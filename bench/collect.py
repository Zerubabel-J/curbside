#!/usr/bin/env python3
"""Build the render bench - a fixed set of real homes to iterate against.

The app was the wrong place to judge render quality. A scan mixes imaging
failures, qualification decisions and QC refusals together, so a bad postcard
could mean any of five things and improving the prompt was guesswork.

This pulls a fixed set of street-level photographs once, and keeps them. Every
prompt experiment then runs against the same houses, so a change in the output
is a change in the prompt - not a different house.

Categories deliberately include homes with no driveway at all. The client's
instruction was explicit: a front walkway should become a circular driveway,
not a rejection.

    python bench/collect.py            # fetch (costs Street View quota)
    python bench/collect.py --list     # show what is already collected
"""
import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from curbside.sources import streetview

HERE = pathlib.Path(__file__).resolve().parent
HOMES = HERE / "homes"
INDEX = HERE / "homes.json"

#: Chosen to span the cases the renders have to handle, not to flatter them.
#: `kind` is the judgement being tested, not a property attribute.
TARGETS = [
    # Plain straight driveways - the bread and butter.
    ("8160 Foxchase Dr, Indianapolis, IN 46256", "straight"),
    ("8142 Foxchase Dr, Indianapolis, IN 46256", "straight"),
    ("1316 Madison St, Hollywood, FL 33019", "straight"),
    ("332 Balboa St, Hollywood, FL 33019", "straight"),
    ("900 Diplomat Pkwy, Hollywood, FL 33019", "straight"),
    ("742 Hollywood Blvd, Hollywood, FL 33019", "straight"),
    ("1237 Garfield St, Hollywood, FL 33019", "straight"),
    ("5845 N New Jersey St, Indianapolis, IN 46220", "straight"),

    # Wide frontage - room for a circular driveway to be plausible.
    ("220 Esplanade Way, Palm Beach, FL 33480", "wide"),
    ("260 Everglade Ave, Palm Beach, FL 33480", "wide"),
    ("240 Tangier Ave, Palm Beach, FL 33480", "wide"),
    ("2036 Amesbury Cir, Wellington, FL 33414", "wide"),
    ("296 Wychmere Ter, Wellington, FL 33414", "wide"),
    ("3050 N Meridian St, Indianapolis, IN 46208", "wide"),

    # Walkway only, no driveway. The client wants these CONVERTED, not skipped.
    ("5363 N Kenwood Ave, Indianapolis, IN 46208", "walkway"),
    ("95 W 54th St, Indianapolis, IN 46208", "walkway"),
    ("5789 Central Ave, Indianapolis, IN 46220", "walkway"),
    ("18 W 54th St, Indianapolis, IN 46208", "walkway"),

    # Worn or stained surfaces - the "before" that sells hardest.
    ("5355 N Kenwood Ave, Indianapolis, IN 46208", "worn"),
    ("5761 Central Ave, Indianapolis, IN 46220", "worn"),
    ("8102 Talliho Dr, Indianapolis, IN 46256", "worn"),
    ("8113 Talliho Dr, Indianapolis, IN 46256", "worn"),

    # Florida suburban, newer build - the client's actual market.
    ("1107 Pelican Ln, Hollywood, FL 33019", "florida"),
    ("1246 Adams St, Hollywood, FL 33019", "florida"),
    ("1047 S Southlake Dr, Hollywood, FL 33019", "florida"),
    ("1495 Windjammer Way, Hollywood, FL 33019", "florida"),
    ("7825 SW 128 St, Pinecrest, FL 33156", "florida"),
    ("11100 SW 73 Ct, Pinecrest, FL 33156", "florida"),
    ("8730 SW 34th St, Miami, FL 33165", "florida"),
    ("8889 SW 78 Ct, Miami, FL 33156", "florida"),
]


def slug(address):
    keep = "".join(c if c.isalnum() else "_" for c in address.lower())
    return "_".join(p for p in keep.split("_") if p)[:48]


def collect():
    HOMES.mkdir(parents=True, exist_ok=True)
    index = json.loads(INDEX.read_text()) if INDEX.exists() else {}
    got = failed = skipped = 0

    for address, kind in TARGETS:
        key = slug(address)
        path = HOMES / f"{key}.jpg"
        if path.exists():
            skipped += 1
            continue
        try:
            ok, meta = streetview.fetch(address, path, parcel_source=None)
        except Exception as e:
            ok, meta = False, f"{type(e).__name__}: {e}"
        if not ok:
            print(f"  FAIL  {address[:46]:48} {str(meta)[:44]}")
            failed += 1
            continue
        index[key] = {"address": address, "kind": kind,
                      "captured": meta.get("captured"),
                      "distance_m": meta.get("camera_distance_m"),
                      "precision": meta.get("geocode_precision")}
        print(f"  ok    {address[:46]:48} {kind:9} {meta.get('captured','?')}")
        got += 1

    INDEX.write_text(json.dumps(index, indent=2, sort_keys=True))
    print(f"\ncollected {got}, already had {skipped}, failed {failed}")
    print(f"index: {INDEX}")


def show():
    if not INDEX.exists():
        print("nothing collected yet - run without --list")
        return
    index = json.loads(INDEX.read_text())
    by_kind = {}
    for key, meta in sorted(index.items()):
        by_kind.setdefault(meta["kind"], []).append(meta["address"])
    for kind, addrs in sorted(by_kind.items()):
        print(f"\n{kind}  ({len(addrs)})")
        for a in addrs:
            print(f"   {a}")
    print(f"\ntotal {len(index)} homes")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    show() if args.list else collect()
