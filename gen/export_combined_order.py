#!/usr/bin/env python3
"""
One parts order covering both boards.

  python gen/export_combined_order.py [--boards 5] [--other PATH]

Two projects are being built at once and they share most of their
jellybeans -- 0805 resistors, 0805 capacitors, the same Schottkys, the same
SOT-23 FETs. Ordered separately each shared line pays LCSC's minimum order
quantity TWICE, which on a 100-piece passive minimum is 200 pieces to fit
maybe 30. Ordered together it pays once.

This reads the finished assembly BOM from each project, adds the quantities
per part number, and rounds the SUM up to the minimum rather than rounding
each project's share up on its own. It also merges the two through-hole
lists, which do not overlap at all but belong in the same basket.

Outputs, all in fab/:
  order-combined.csv       every line, both boards, with the split
  order-combined-paste.txt LCSC quick-order format: part number, qty
  order-combined.txt       the readable version, with the saving
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from export_order import MOQ_REAL, moq_round  # noqa: E402  one definition

# Every part number that has come back in an LCSC cart export. Appearing
# there is the strongest evidence available that a number is real and
# orderable -- stronger than the parts index, which is JLCPCB's assembly
# library and both misses parts LCSC sells and lists parts it no longer does.
KNOWN_GOOD = set(MOQ_REAL)

# The other project. Not a sibling directory by any rule, so it is named
# rather than guessed, and --other overrides it.
OTHER_DEFAULT = r"C:\Projects\gatecontrol\hw"

THIS_NAME = "logger"
OTHER_NAME = "gate"


def read_bom(path):
    """-> {part number: (qty per board, comment, footprint)}"""
    out = {}
    with open(path, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            pn = (row.get("JLCPCB Part #") or "").strip()
            desig = (row.get("Designator") or "").strip().strip('"')
            n = len([d for d in desig.split(",") if d.strip()])
            if not pn or not n:
                continue
            comment = (row.get("Comment") or "").strip()
            fp = (row.get("Footprint") or "").strip()
            if pn in out:
                n += out[pn][0]
            out[pn] = (n, comment, fp)
    return out


def read_elsewhere(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            rows.append(row)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--boards", type=int, default=5,
                    help="how many of EACH board (default 5)")
    ap.add_argument("--other", default=OTHER_DEFAULT)
    # Rounding is ON now, and it is no longer a guess. fab/lcsc-moq.csv holds
    # real minimums read out of LCSC cart exports by gen/learn_moq.py, and
    # moq_round leaves any part that is not in that table exactly alone. So
    # the choice is no longer "guess or do not round" but "round the lines we
    # have real numbers for", which has no downside.
    ap.add_argument("--no-moq", dest="moq", action="store_false",
                    default=True,
                    help="do not round to the known minimum order quantities")
    args = ap.parse_args()
    n = args.boards

    a = read_bom(os.path.join(PROJ, "fab", "bom.csv"))
    b = read_bom(os.path.join(args.other, "fab", "bom.csv"))

    # The two projects sometimes picked DIFFERENT part numbers for the same
    # part -- same value, same package, different LCSC line. Merging only by
    # part number would miss those and pay the minimum twice for what is one
    # component. Group by (comment, footprint) first and pick one number per
    # group, keeping whichever is used in greater quantity.
    #
    # This is reported, not silent. The two numbers are equivalent by the
    # designs' own description of them, but "equivalent by the description"
    # is not the same as "the same part", and one of a pair may simply be a
    # worse choice that nobody noticed. Look at the list before ordering.
    groups = {}
    for src, table in (("a", a), ("b", b)):
        for pn, (q, c, f) in table.items():
            groups.setdefault((c, f), {}).setdefault(pn, {"a": 0, "b": 0})
            groups[(c, f)][pn][src] += q

    substitutions = []
    parts = {}
    for (c, f), pns in groups.items():
        if len(pns) > 1:
            # Prefer the number LCSC has actually confirmed, and only fall
            # back to "whichever is used more" between two equally unknown
            # ones. Quantity alone picked C29055 over C28233 for the 100V
            # decoupler -- more of the board used it, and it is discontinued,
            # so the one dead number in the pair beat the live one and the
            # whole line came back rejected.
            keep = max(pns, key=lambda p: (p in KNOWN_GOOD,
                                           pns[p]["a"] + pns[p]["b"]))
            for p in pns:
                if p != keep:
                    substitutions.append((c, f, p, keep))
        else:
            keep = next(iter(pns))
        parts[keep] = {
            "a": sum(v["a"] for v in pns.values()),
            "b": sum(v["b"] for v in pns.values()),
            "comment": c, "fp": f,
        }

    rows, shared = [], 0
    saved = 0
    for pn, d in parts.items():
        need = (d["a"] + d["b"]) * n
        # Ask for what the boards actually need and let LCSC's cart bump
        # each line to that part's own minimum.
        #
        # This used to pre-round to a guessed minimum -- 100 for anything in
        # a chip package, 50 for a SOT-23 -- extrapolated from six sampled
        # parts. Several of those guesses were wrong, and a wrong guess is
        # worse in both directions: too high and you buy a hundred of
        # something sold in twenties, too low or off-multiple and the line
        # is rejected outright. LCSC knows the real number for every part
        # and applies it at checkout; guessing at it from here does not.
        order = moq_round(pn, need, d["fp"]) if args.moq else need
        # What it would have cost to order the two separately.
        sep = 0
        if d["a"]:
            sep += (moq_round(pn, d["a"] * n, d["fp"]) if args.moq
                    else d["a"] * n)
        if d["b"]:
            sep += (moq_round(pn, d["b"] * n, d["fp"]) if args.moq
                    else d["b"] * n)
        if d["a"] and d["b"]:
            shared += 1
            saved += sep - order
        rows.append((pn, order, need, d["a"] * n, d["b"] * n,
                     d["comment"], d["fp"], sep))
    rows.sort(key=lambda r: (-(r[3] > 0 and r[4] > 0), -r[2]))

    fab = os.path.join(PROJ, "fab")
    os.makedirs(fab, exist_ok=True)

    with open(os.path.join(fab, "order-combined.csv"), "w", newline="",
              encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["LCSC", "Order qty", "Needed", THIS_NAME, OTHER_NAME,
                    "Comment", "Footprint"])
        for pn, order, need, qa, qb, c, f, _sep in rows:
            w.writerow([pn, order, need, qa, qb, c, f])

    with open(os.path.join(fab, "order-combined-paste.txt"), "w",
              encoding="utf-8") as fh:
        for pn, order, *_ in rows:
            fh.write("%s,%d\n" % (pn, order))

    lines = []
    lines.append("Combined parts order -- %d x %s + %d x %s"
                 % (n, THIS_NAME, n, OTHER_NAME))
    lines.append("=" * 58)
    lines.append("")
    lines.append("Paste fab/order-combined-paste.txt into LCSC's quick-order")
    lines.append("box, or upload order-combined.csv to their BOM tool.")
    lines.append("")
    lines.append("Quantities are what the boards NEED. LCSC will raise any")
    lines.append("line to that part's own minimum at checkout -- which is")
    lines.append("the right place for it to happen, because LCSC knows the")
    lines.append("number and this file does not. --moq restores the old")
    lines.append("guessed rounding, which got several parts wrong.")
    lines.append("")
    lines.append("  %-9s %6s %7s %7s %7s  %s"
                 % ("part", "order", "needed", THIS_NAME, OTHER_NAME, "comment"))
    for pn, order, need, qa, qb, c, f, _sep in rows:
        mark = "*" if qa and qb else " "
        lines.append("  %-9s %6d %7d %7d %7d %s %s"
                     % (pn, order, need, qa, qb, mark, c))
    lines.append("")
    lines.append("  %d distinct parts, %d pieces"
                 % (len(rows), sum(r[1] for r in rows)))
    lines.append("  * = on both boards (%d parts)" % shared)
    lines.append("")
    lines.append("  Ordering the two projects separately would take %d pieces."
                 % sum(r[7] for r in rows))
    lines.append("  Combining saves %d pieces of minimum-order padding."
                 % saved)
    if substitutions:
        lines.append("")
        lines.append("Consolidated -- same value and package, two part numbers")
        lines.append("-" * 58)
        lines.append("  Each pair below was ordered as ONE line. Equivalent by")
        lines.append("  what the two designs say about them; check before you")
        lines.append("  order, because a pair can also mean one is just wrong.")
        for c, f, dropped, keep in substitutions:
            lines.append("    %-14s %-22s  use %-9s not %s"
                         % (c, f.replace("_2012Metric", "")
                              .replace("_3216Metric", ""), keep, dropped))
    # Several through-hole lines name an LCSC part in their note. Those can
    # ride along in the same basket instead of becoming a second errand, so
    # they get pulled out and added to the paste file. The ones with no part
    # number -- socket strip, M3 hardware, the two modules -- are the only
    # things that genuinely have to be bought elsewhere.
    extra, no_pn = [], []
    for name, path in ((THIS_NAME, os.path.join(PROJ, "fab",
                                                "order-elsewhere.csv")),
                       (OTHER_NAME, os.path.join(args.other, "fab",
                                                 "order-elsewhere.csv"))):
        for row in read_elsewhere(path):
            per = row.get("Qty per board") or row.get("Qty") or "0"
            qty = int(per) * n if str(per).isdigit() else 0
            m = re.search(r"LCSC (C\d+)", row.get("Note") or "")
            if m and qty:
                extra.append((m.group(1), qty, name,
                              row.get("Designators", ""), row.get("What", "")))
            else:
                no_pn.append((name, row.get("Designators", ""), qty,
                              row.get("What", ""), row.get("Note", "")))

    if extra:
        merged = {}
        for pn, q, src, des, what in extra:
            e = merged.setdefault(pn, [0, [], what])
            e[0] += q
            e[1].append("%s %s" % (src, des))
        lines.append("")
        lines.append("Through-hole, also from LCSC -- same basket")
        lines.append("-" * 58)
        for pn, (q, where, what) in sorted(merged.items()):
            lines.append("  %-9s x%-4d %-46s %s"
                         % (pn, q, what[:46], "; ".join(where)))
        with open(os.path.join(fab, "order-combined-paste.txt"), "a",
                  encoding="utf-8") as fh:
            for pn, (q, _w, _t) in sorted(merged.items()):
                fh.write(pn + "," + str(q) + chr(10))

    lines.append("")
    lines.append("Buy elsewhere -- no LCSC part number")
    lines.append("-" * 58)
    for name, des, qty, what, note in no_pn:
        lines.append("")
        lines.append("  [%s] %-12s x%d" % (name, des, qty))
        lines.append("        %s" % what)
        if note.strip():
            lines.append("        %s" % note.strip())

    text = "\n".join(lines) + "\n"
    with open(os.path.join(fab, "order-combined.txt"), "w",
              encoding="utf-8") as fh:
        fh.write(text)

    print(text)
    print("wrote fab/order-combined.csv, -paste.txt and .txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
