#!/usr/bin/env python3
"""Render the bench and lay the results out for judgement.

One job: make render quality visible and comparable. Every experiment runs
against the same houses from `collect.py`, so a difference in the output is a
difference in the prompt.

    python bench/render.py --kind walkway --shape circular
    python bench/render.py --design travertine --limit 6
    python bench/render.py --sheet            # rebuild contact sheets only

Output lands in `bench/out/<run>/`, one before/after pair per render plus a
contact sheet per house. Nothing here touches the pipeline, the database or
the deployment.
"""
import argparse
import concurrent.futures as futures
import json
import os
import pathlib
import shutil
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw, ImageFont

from curbside.vision import gemini, driveways
from curbside.render.compositing import composite, derive_mask, ground_mask

HERE = pathlib.Path(__file__).resolve().parent
HOMES = HERE / "homes"
INDEX = HERE / "homes.json"
OUT = HERE / "out"


def _font(size, bold=False):
    for p in (f"/usr/share/fonts/truetype/dejavu/DejaVuSans{'-Bold' if bold else ''}.ttf",
              f"/usr/share/fonts/TTF/DejaVuSans{'-Bold' if bold else ''}.ttf"):
        if pathlib.Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def _sky_drift(src, rendered):
    """Share of pixels above the eaves that changed.

    The failure that matters is not a bad driveway but a convincing render of
    a *different* house, and it shows up above the roofline where a driveway
    edit has no business reaching.
    """
    import numpy as np
    a = np.asarray(Image.open(src).convert("RGB")).astype(np.int16)
    b = np.asarray(Image.open(rendered).convert("RGB").resize(
        (a.shape[1], a.shape[0]))).astype(np.int16)
    d = np.abs(a - b).max(axis=2)
    return float((d[:int(a.shape[0] * 0.35)] > 40).mean())


def render_one(home_key, meta, design, shape, key, run_dir, mask_it=True):
    """One render. Returns a dict describing what happened."""
    src = HOMES / f"{home_key}.jpg"
    tag = f"{home_key}__{design}__{shape}"
    raw = run_dir / f"{tag}__raw.jpg"
    keep = run_dir / f"{tag}__best.jpg"
    out = run_dir / f"{tag}.jpg"

    # Walk the framing ladder. Each framing fails deterministically on the
    # houses it cannot do - three attempts at one wording produced 81/83/96%
    # drift on the same house - so the recovery is a different wording, not
    # another roll of the dice.
    t0 = time.time()
    cost = 0.0
    used = None
    best = None

    for name, prompt in driveways.render_ladder(design, shape):
        try:
            ok, msg, c = gemini.render(src, raw, key, prompt=prompt)
        except Exception as e:
            return {"home": home_key, "design": design, "shape": shape,
                    "ok": False, "error": f"{type(e).__name__}: {e}"}
        cost += c
        if not ok:
            return {"home": home_key, "design": design, "shape": shape,
                    "ok": False, "error": str(msg)[:120], "cost": cost}
        drift = _sky_drift(src, raw)
        # Keep the best attempt, not the last one: if every framing drifts we
        # still want the least-bad render to look at rather than whichever
        # happened to run last.
        if best is None or drift < best[1]:
            best = (name, drift)
            shutil.copyfile(raw, keep)
        if drift < 0.10:
            break

    if best is None:
        return {"home": home_key, "design": design, "shape": shape,
                "ok": False, "error": "no render produced", "cost": cost}
    shutil.copyfile(keep, raw)
    used = best[0]

    result = {"home": home_key, "design": design, "shape": shape,
              "ok": True, "cost": cost, "seconds": round(time.time() - t0, 1),
              "framing": used,
              "address": meta["address"], "kind": meta["kind"]}

    # Both paths composite; they differ in which region the model is allowed
    # to have changed. Resurfacing is bounded by the old paving. Reshaping is
    # bounded by the ground plane, since a new footprint legitimately covers
    # lawn - but the house, trees and sky above the horizon are preserved by
    # construction either way.
    if mask_it:
        try:
            if driveways.SHAPES[shape]["covers_lawn"]:
                mask = ground_mask(src, raw)
            else:
                mask = derive_mask(src, raw, threshold=50, min_blob_frac=0.004)
            composite(src, raw, mask, out)
            result["masked"] = True
            result["mask_frac"] = round(float(mask.mean()), 4)
        except Exception as e:
            Image.open(raw).save(out)
            result["masked"] = False
            result["mask_error"] = f"{type(e).__name__}: {e}"
    else:
        Image.open(raw).save(out)
        result["masked"] = False

    # Fidelity check: is this still a photograph of the same house?
    #
    # The failure that matters is not a bad driveway - it is a *beautiful*
    # render of a subtly different house. Same address, same shape, different
    # material produced one pixel-faithful result and one where a tree and a
    # lamp post vanished. Eyes miss that at a glance; arithmetic does not.
    try:
        import numpy as np
        from PIL import Image as _I
        a = np.asarray(_I.open(src).convert("RGB")).astype(np.int16)
        b = np.asarray(_I.open(out).convert("RGB").resize(
            (a.shape[1], a.shape[0]))).astype(np.int16)
        d = np.abs(a - b).max(axis=2)
        h = a.shape[0]
        result["changed"] = round(float((d > 40).mean()), 3)
        # Above the eaves there is only sky, roofline and canopy. Anything
        # changing there is the model redrawing the scene, not the driveway.
        result["sky_drift"] = round(float((d[:int(h * 0.35)] > 40).mean()), 3)
        # Two failure modes, opposite in character, and a single number
        # cannot separate them. A render that repaints the house scores badly
        # on sky drift. A render that changes *nothing* scores perfectly on
        # sky drift while being worthless - and a photograph of a hedge with
        # no visible driveway will always score perfectly. A result is only
        # good if it left the scene alone AND actually resurfaced something.
        ground = d[int(h * 0.55):]
        result["ground_change"] = round(float((ground > 40).mean()), 3)

        # Sky drift alone misses the worst reshaping failure. The ground mask
        # preserves everything above the horizon, so the sky can be pixel
        # perfect while the model has moved the house to the background and
        # rebuilt the whole lot beneath it. The band just under the horizon
        # holds the facade; if that stops matching, it is a different house
        # however good the driveway looks. Measured: recomposed renders sit at
        # 22-37% here while faithful ones sit at 1-8%.
        facade = d[int(h * 0.35):int(h * 0.55)]
        result["facade_change"] = round(float((facade > 40).mean()), 3)
        # Calibrated against the bench rather than guessed. Across 27 renders
        # the values cluster at 0-7% and then jump to 14, 33, 41, 66, 78, 96 -
        # a real gap, not a gradient. Below it the drift is foliage shimmer
        # and JPEG noise on a render that is otherwise pixel-faithful; above
        # it the model has repainted the scene. A 4% line rejected renders
        # that were visibly perfect, which is how a good result gets thrown
        # away for failing a number nobody checked against an image.
        result["faithful"] = result["sky_drift"] < 0.10
        result["did_work"] = result["ground_change"] > 0.04
        result["house_kept"] = result["facade_change"] < 0.15
        result["good"] = (result["faithful"] and result["did_work"]
                          and result["house_kept"])
    except Exception as e:
        result["fidelity_error"] = f"{type(e).__name__}: {e}"

    result["path"] = str(out)
    return result


def contact_sheet(home_key, meta, results, run_dir):
    """Before on the left, every variant beside it, labelled."""
    src = HOMES / f"{home_key}.jpg"
    good = [r for r in results if r.get("ok") and r.get("path")]
    if not good:
        return None

    cell = 420
    pad, header, label = 12, 54, 34
    cols = 1 + len(good)
    W = cols * cell + (cols + 1) * pad
    H = header + cell + label + 2 * pad

    sheet = Image.new("RGB", (W, H), (248, 247, 245))
    d = ImageDraw.Draw(sheet)
    d.text((pad, 14), f"{meta['address']}   ·   {meta['kind']}",
           font=_font(21, True), fill=(20, 24, 32))

    def place(img, i, caption, accent=(90, 96, 106)):
        x = pad + i * (cell + pad)
        y = header
        sheet.paste(img.resize((cell, cell), Image.LANCZOS), (x, y))
        d.rectangle([x, y, x + cell - 1, y + cell - 1],
                    outline=(214, 210, 204), width=1)
        d.text((x + 2, y + cell + 7), caption, font=_font(16, True), fill=accent)

    place(Image.open(src).convert("RGB"), 0, "BEFORE", (52, 58, 70))
    for i, r in enumerate(good, start=1):
        cap = f"{driveways.DESIGNS[r['design']]['name']} · {driveways.SHAPES[r['shape']]['name']}"
        place(Image.open(r["path"]).convert("RGB"), i, cap, (190, 58, 20))

    path = run_dir / f"sheet__{home_key}.jpg"
    sheet.save(path, "JPEG", quality=92)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", help="only homes of this kind (straight/wide/walkway/worn/florida)")
    ap.add_argument("--home", help="one home key substring")
    ap.add_argument("--design", action="append", help="repeatable; default = three")
    ap.add_argument("--shape", action="append", help="repeatable; default = resurface")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--run", default=None, help="output folder name")
    ap.add_argument("--no-mask", action="store_true",
                    help="show the model's raw output, unmasked")
    args = ap.parse_args()

    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        sys.exit("GEMINI_API_KEY not set - source ~/.gemini_env")
    if not INDEX.exists():
        sys.exit("no bench homes - run: python bench/collect.py")

    index = json.loads(INDEX.read_text())
    homes = sorted(index.items())
    if args.kind:
        homes = [(k, m) for k, m in homes if m["kind"] == args.kind]
    if args.home:
        homes = [(k, m) for k, m in homes if args.home in k]
    if args.limit:
        homes = homes[:args.limit]
    if not homes:
        sys.exit("no homes matched")

    designs = args.design or ["travertine", "charcoal_granite", "clay_paver"]
    shapes = args.shape or ["resurface"]
    for d in designs:
        if d not in driveways.DESIGNS:
            sys.exit(f"unknown design {d!r}; have {list(driveways.DESIGNS)}")
    for s in shapes:
        if s not in driveways.SHAPES:
            sys.exit(f"unknown shape {s!r}; have {list(driveways.SHAPES)}")

    run = args.run or time.strftime("%m%d-%H%M")
    run_dir = OUT / run
    run_dir.mkdir(parents=True, exist_ok=True)

    jobs = [(hk, m, d, s) for hk, m in homes for d in designs for s in shapes]
    est = len(jobs) * 0.067
    print(f"{len(homes)} homes x {len(designs)} designs x {len(shapes)} shapes "
          f"= {len(jobs)} renders  (~${est:.2f})")
    print(f"out: {run_dir}\n")

    results = []
    with futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(render_one, hk, m, d, s, key, run_dir,
                            not args.no_mask): (hk, d, s)
                for hk, m, d, s in jobs}
        for f in futures.as_completed(futs):
            r = f.result()
            results.append(r)
            mark = "ok  " if r.get("ok") else "FAIL"
            if r.get("ok"):
                if not r.get("faithful", True):
                    flag = "  ⚠ REDREW SCENE"
                elif not r.get("house_kept", True):
                    flag = "  ⚠ MOVED THE HOUSE"
                elif not r.get("did_work", True):
                    flag = "  ⚠ NO VISIBLE CHANGE"
                else:
                    flag = "  ✓"
                extra = (f"sky {r.get('sky_drift', 0):.0%} "
                         f"face {r.get('facade_change', 0):.0%} "
                         f"ground {r.get('ground_change', 0):.0%}{flag}")
            else:
                extra = r.get("error", "")[:44]
            print(f"  {mark} {r['home'][:30]:32} {r['design']:18} "
                  f"{r['shape']:10} {extra}")

    # Only faithful renders reach a contact sheet. A sheet exists to be judged
    # on driveway quality, and a render that repainted the house is not a
    # worse driveway - it is a different photograph, and leaving it in the
    # grid invites judging the wrong thing.
    sheets = []
    for hk, m in homes:
        rs = [r for r in results
              if r["home"] == hk and r.get("good", True)]
        p = contact_sheet(hk, m, rs, run_dir)
        if p:
            sheets.append(p)

    spent = sum(r.get("cost", 0) for r in results)
    ok = sum(1 for r in results if r.get("ok"))
    unfaithful = [r for r in results if r.get("ok") and not r.get("faithful", True)]
    idle = [r for r in results if r.get("ok") and r.get("faithful", True)
            and not r.get("did_work", True)]
    good = [r for r in results if r.get("good")]
    (run_dir / "results.json").write_text(json.dumps(results, indent=2))
    print(f"\n{ok}/{len(results)} rendered  ·  ${spent:.2f}  ·  "
          f"{len(sheets)} contact sheets")
    print(f"{len(good)}/{ok} usable  ({len(unfaithful)} redrew the scene, "
          f"{len(idle)} changed nothing)")
    if unfaithful:
        print(f"{len(unfaithful)} redrew the scene rather than editing it:")
        for r in unfaithful:
            print(f"   {r['home'][:34]:36} {r['design']:18} "
                  f"sky drift {r['sky_drift']:.0%}")
    print(f"look at: {run_dir}/sheet__*.jpg")


if __name__ == "__main__":
    main()
