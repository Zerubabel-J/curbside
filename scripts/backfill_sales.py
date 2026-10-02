#!/usr/bin/env python3
"""Fill in sale date, price and owner on leads created before those columns.

Leads made by earlier versions carry only an address. The sheet shows them
with blank County, Price and Owner columns beside rows that have everything,
which reads as a broken table rather than an old record.

This looks each one up in the county records by address and fills what it
finds. Leads with no Florida record - the Indiana addresses from early block
scans - are left exactly as they are, because inventing a value would be worse
than an empty cell.

    python scripts/backfill_sales.py            # report what would change
    python scripts/backfill_sales.py --apply    # write it
"""
import argparse
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from curbside.config import settings
from curbside.sources import sold
from curbside.store import Store


def zip_of(address):
    m = re.search(r"\b(\d{5})(?:-\d{4})?\s*$", address.strip())
    return m.group(1) if m else None


def street_of(address):
    """The house number and street, upper-cased, for matching."""
    return address.split(",")[0].strip().upper()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--months", type=int, default=24,
                    help="how far back to search the records")
    args = ap.parse_args()

    s = Store(settings.db_path)
    try:
        rows = list(s.db.execute(
            "SELECT id, address FROM leads WHERE sale_price IS NULL"))
        if not rows:
            print("nothing to backfill")
            return

        # Group by ZIP so each county is queried once per ZIP rather than once
        # per lead - 115 leads across a dozen ZIPs is a dozen queries.
        by_zip = {}
        skipped = []
        for r in rows:
            z = zip_of(r["address"])
            if not z or not z.startswith("33"):
                skipped.append(r["address"])
                continue
            by_zip.setdefault(z, []).append(r)

        print(f"{len(rows)} leads with no sale data")
        print(f"  {len(skipped)} outside Florida - leaving alone")
        print(f"  {sum(len(v) for v in by_zip.values())} to look up "
              f"across {len(by_zip)} ZIPs\n")

        filled = missed = 0
        for z, leads in sorted(by_zip.items()):
            try:
                found, _ = sold.search(z, min_price=0, months=args.months,
                                       limit=400)
            except sold.SoldError as e:
                print(f"  {z}: lookup failed - {str(e)[:60]}")
                missed += len(leads)
                continue

            index = {street_of(f.as_address()): f for f in found}
            for r in leads:
                hit = index.get(street_of(r["address"]))
                if not hit:
                    missed += 1
                    continue
                if args.apply:
                    s.db.execute(
                        "UPDATE leads SET sale_date=?, sale_price=?, owner=?,"
                        " lead_source=? WHERE id=?",
                        (hit.sold_on.isoformat() if hit.sold_on else None,
                         hit.price, hit.owner or None, hit.county, r["id"]))
                filled += 1
            print(f"  {z}: {len(leads)} leads, "
                  f"{sum(1 for r in leads if street_of(r['address']) in index)} matched")

        if args.apply:
            s.db.commit()
        print(f"\n{filled} filled, {missed} not found in the records"
              + ("" if args.apply else "  (dry run - pass --apply to write)"))
    finally:
        s.close()


if __name__ == "__main__":
    main()
