#!/usr/bin/env python3
"""
Fold an LCSC cart export into fab/lcsc-moq.csv.

  python gen/learn_moq.py <cart-export.csv> [more.csv ...]

Minimum order quantities cannot be derived. They are not a function of
package, price, or anything else visible from outside: across 67 real parts
they take seven distinct values -- 1, 2, 5, 10, 20, 50, 100 -- and two 0805
resistors from the same manufacturer in the same series can differ. An
earlier version of this project guessed them from the package and got 45 of
70 wrong, which is worse than not rounding at all: too high buys a hundred
of something sold in twenties, and off-multiple is rejected at checkout.

There is no readable API for them either. LCSC's own is behind Akamai,
EasyEDA's mirror answers 403 from CloudFront, and JLCPCB's parts index -- the
one thing that does answer -- carries stock and price but not MOQ.

What does carry them, exactly and for free, is a cart export. Every round
trip through the cart is therefore worth keeping: paste an order, export it,
run it through here, and those part numbers never need guessing again. The
table only grows, so each order is quantised more correctly than the last.

Merging is last-writer-wins per part number, which is right -- a later export
reflects a later state of LCSC's catalogue.
"""

from __future__ import annotations

import csv
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.abspath(os.path.join(HERE, ".."))
LEDGER = os.path.join(PROJ, "fab", "lcsc-moq.csv")


FIELDS = ["LCSC", "MOQ", "Multiple", "MPN", "Manufacturer", "Package"]


def load(path):
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            out[r["LCSC"].strip()] = (int(r["MOQ"] or 1),
                                      int(r["Multiple"] or 1),
                                      r.get("MPN", ""),
                                      r.get("Manufacturer", ""),
                                      r.get("Package", ""))
    return out


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    before = load(LEDGER)
    table = dict(before)
    added, changed = [], []

    for path in sys.argv[1:]:
        with open(path, encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                pn = (r.get("LCSC#") or "").strip()
                if not pn:
                    continue
                try:
                    moq = int(r.get("MOQ") or 1)
                    mult = int(r.get("Multiple") or 1)
                except ValueError:
                    continue
                mpn = (r.get("MPN") or "").strip()
                # Manufacturer and package ride along because a cart export
                # is the only place this project sees them, and MacroFab
                # wants an MPN and a package rather than an LCSC code. One
                # ledger, fed from the same round trips, instead of a second
                # file that has to be kept in step.
                new = (moq, mult, mpn,
                       (r.get("Manufacturer") or "").strip(),
                       (r.get("Package") or "").strip())
                old = table.get(pn)
                if old is None:
                    added.append((pn, moq, mult, mpn))
                elif old[:2] != new[:2]:
                    changed.append((pn, old[0], old[1], moq, mult, mpn))
                table[pn] = new

    with open(LEDGER, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(FIELDS)
        for pn in sorted(table):
            w.writerow([pn] + list(table[pn]))

    print("fab/lcsc-moq.csv: %d parts (was %d)" % (len(table), len(before)))
    if added:
        print("\n  new (%d):" % len(added))
        for pn, moq, mult, mpn in sorted(added):
            print("    %-10s MOQ %-4d x%-4d %s" % (pn, moq, mult, mpn[:28]))
    if changed:
        print("\n  CHANGED (%d) -- LCSC moved these:" % len(changed))
        for pn, om, ox, nm, nx, mpn in sorted(changed):
            print("    %-10s %d/x%d -> %d/x%d   %s"
                  % (pn, om, ox, nm, nx, mpn[:28]))
    if not added and not changed:
        print("  nothing new -- the ledger already had every line")
    return 0


if __name__ == "__main__":
    sys.exit(main())
