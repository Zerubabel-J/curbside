"""Curbside CLI."""
import argparse
import json
import os
import sys

from curbside import __version__, pipeline
from curbside.config import settings, SOURCES
from curbside.store import Store
from curbside.mail.providers import load_suppression
from curbside.compliance import policy


def _key():
    k = os.environ.get("GEMINI_API_KEY", "").strip()
    if not k:
        sys.exit("GEMINI_API_KEY not set  (source ~/.gemini_env)")
    return k


def _return_address():
    name = os.environ.get("LOB_FROM_NAME") or os.environ.get("CURBSIDE_FROM_NAME")
    line = os.environ.get("LOB_FROM_LINE1") or os.environ.get("CURBSIDE_FROM_LINE1")
    city = os.environ.get("LOB_FROM_CITY") or os.environ.get("CURBSIDE_FROM_CITY")
    st   = os.environ.get("LOB_FROM_STATE") or os.environ.get("CURBSIDE_FROM_STATE")
    zp   = os.environ.get("LOB_FROM_ZIP") or os.environ.get("CURBSIDE_FROM_ZIP")
    if all([name, line, city, st, zp]):
        return f"{name}, {line}, {city}, {st} {zp}"
    return None


def _acked_states():
    return tuple(s.strip().upper() for s in
                 os.environ.get("CURBSIDE_ACK_STATES", "").split(",") if s.strip())


def _addresses():
    return [l.strip() for l in settings.addresses_file.read_text().splitlines()
            if l.strip() and not l.startswith("#")]


# ---------------------------------------------------------------- commands

def cmd_run(a, store):
    key = _key()
    settings.ensure_dirs()
    budget = pipeline.Budget(store, a.budget or settings.budget_usd)
    supp = load_suppression(settings.suppression_file)

    ra = _return_address()
    if not ra:
        sys.exit("Set a return address before composing mailable pieces:\n"
                 "  CURBSIDE_FROM_NAME, _LINE1, _CITY, _STATE, _ZIP\n"
                 "A physical return address is legally required on mailed pieces.")

    print(f"source: {settings.imagery().name} "
          f"({settings.imagery().license}, {settings.imagery().resolution_in}in)")
    print("discover"); print("  ", pipeline.discover(store, _addresses(), supp))
    print("image");    pipeline.image(store, a.limit)
    print("qualify");  pipeline.qualify(store, key, budget, a.limit)
    if not a.no_render:
        print("render");  pipeline.render(store, key, budget, a.limit,
                                          use_segmentation=not a.no_segmentation)
        print("compose"); pipeline.compose(store, ra, a.limit)
    cmd_status(a, store)
    pending = len(store.ready_for("composed"))
    if pending:
        print(f"\n  {pending} awaiting approval → curbside review")


def cmd_status(a, store):
    counts, spend = store.counts(), store.spend_by_stage()
    order = ["discovered","imaged","qualified","rendered","composed","approved",
             "mailed","rejected","suppressed","blocked","failed"]
    print("\n" + "=" * 56)
    for s in order:
        if counts.get(s):
            print(f"  {s:<14} {counts[s]:>6}")
    print("-" * 56)
    for stage, usd in spend.items():
        print(f"  {stage:<14} ${usd:>10.4f}")
    total = store.total_spend()
    print(f"  {'TOTAL':<14} ${total:>10.4f}")
    n = counts.get("composed", 0) + counts.get("approved", 0) + counts.get("mailed", 0)
    if n:
        print(f"  {'per piece':<14} ${total/n:>10.4f}")
    print("=" * 56)
    print(f"  {settings.imagery().attribution}")


def cmd_review(a, store):
    rows = store.ready_for("composed")
    if not rows:
        print("nothing awaiting approval"); return
    print(f"{len(rows)} awaiting approval:\n")
    for r in rows:
        q = json.loads(r["qualification"]) if r["qualification"] else {}
        c = json.loads(r["qc"]) if r["qc"] else {}
        d = policy.check_lead(r["address"], has_disclosure=True,
                              has_return_address=True, has_opt_out=True,
                              acknowledged_states=_acked_states())
        flag = "" if d.allowed else f"  ⚠ {d.reasons[0][:50]}"
        print(f"  [{r['id']}] {r['address']}{flag}")
        print(f"        {q.get('surface','?')} cond={q.get('condition_score','?')}/10 "
              f"mask={c.get('mask_frac',0):.1%} drift={c.get('outside_drift_frac',0):.2%} "
              f"via={(c.get('mask') or {}).get('mode','?')}")
        print(f"        {r['postcard_path']}")
    print(f"\n  approve: curbside approve {' '.join(str(r['id']) for r in rows)}")


def cmd_approve(a, store):
    ids = ([r["id"] for r in store.ready_for("composed")] if a.all
           else [int(i) for i in a.ids])
    if not ids:
        print("nothing to approve"); return
    for i in ids:
        row = store.get(i)
        if not row or row["state"] != "composed":
            print(f"  [{i}] not awaiting approval"); continue
        store.advance(i, "approved", note="human approved")
        print(f"  [{i}] approved")


def cmd_mail(a, store):
    settings.ensure_dirs()
    budget = pipeline.Budget(store, a.budget or settings.budget_usd)
    supp = load_suppression(settings.suppression_file)
    res = pipeline.mail(store, a.provider, supp, budget,
                        acknowledged_states=_acked_states(),
                        limit=a.limit, daily_cap=a.daily_cap)
    print(f"\nsent={res['sent']} blocked={res['blocked']} "
          f"{'LIVE' if res['live'] else '(dry run — nothing mailed)'}")
    cmd_status(a, store)


def cmd_retry(a, store):
    print(f"reset {store.retry_failed(settings.max_attempts)} leads")


def cmd_sources(a, store):
    for k, s in SOURCES.items():
        mark = "*" if k == settings.source else " "
        print(f" {mark} {k:<16} {s.resolution_in:>4.1f}in  {s.license:<28} "
              f"{','.join(s.states)}")
    print("\n  select with CURBSIDE_SOURCE=<key>")


def cmd_doctor(a, store):
    """Report what is configured and what would block a live send."""
    print(f"curbside {__version__}\n")
    ok = lambda b: "OK " if b else "-- "
    print(f" {ok(bool(os.environ.get('GEMINI_API_KEY')))} GEMINI_API_KEY")
    print(f" {ok(bool(_return_address()))} return address  {_return_address() or ''}")
    print(f" {ok(settings.suppression_file.exists())} suppression file")
    print(f" {ok(bool(os.environ.get('LOB_API_KEY')))} LOB_API_KEY (live mail)")
    live_ok = os.environ.get("CURBSIDE_ALLOW_LIVE_MAIL") == "1"
    print(f" {ok(live_ok)} CURBSIDE_ALLOW_LIVE_MAIL")
    print(f"\n source   {settings.source} — {settings.imagery().license}")
    print(f" budget   ${settings.budget_usd:.2f}   daily mail cap {settings.daily_mail_cap}")
    print(f" acked    {_acked_states() or '(none)'}")
    print(f"\n suppression sources required for live mail:")
    for s in policy.REQUIRED_SUPPRESSION_SOURCES:
        print(f"   - {s}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="curbside",
        description="AI-rendered direct mail for home-services contractors")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--budget", type=float, default=None)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--db", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="discover → compose")
    r.set_defaults(fn=cmd_run)
    r.add_argument("--no-render", action="store_true")
    r.add_argument("--no-segmentation", action="store_true",
                   help="skip the segmentation prior (diff-only masking)")

    sub.add_parser("status", help="state and spend").set_defaults(fn=cmd_status)
    sub.add_parser("review", help="pending approval").set_defaults(fn=cmd_review)
    sub.add_parser("sources", help="imagery sources").set_defaults(fn=cmd_sources)
    sub.add_parser("doctor", help="configuration check").set_defaults(fn=cmd_doctor)

    ap = sub.add_parser("approve"); ap.set_defaults(fn=cmd_approve)
    ap.add_argument("ids", nargs="*"); ap.add_argument("--all", action="store_true")

    m = sub.add_parser("mail", help="send approved pieces"); m.set_defaults(fn=cmd_mail)
    m.add_argument("--provider", default=None, choices=["dryrun", "lob"])
    m.add_argument("--daily-cap", type=int, default=None)

    rt = sub.add_parser("retry"); rt.set_defaults(fn=cmd_retry)

    a = p.parse_args(argv)
    if a.cmd == "mail" and a.provider is None:
        a.provider = settings.mail_provider
    settings.ensure_dirs()
    store = Store(a.db or settings.db_path)
    try:
        a.fn(a, store)
    except pipeline.BudgetExceeded as e:
        print(f"\n!! {e}"); cmd_status(a, store)
    except Exception as e:
        print(f"\n!! {type(e).__name__}: {e}")
        raise SystemExit(1)
    finally:
        store.close()


if __name__ == "__main__":
    main()
