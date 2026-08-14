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


def assembled_only(args, na, nb, spare):
    """The LCSC order when JLCPCB is assembling the boards.

    Economic PCBA sources and fits every SMD part itself, so ordering those
    from LCSC as well is buying them twice. Only the lines an SMT line will
    not fit remain, which here means the through-hole ones -- relays,
    terminal blocks, the optocoupler and the JST headers.

    Quantities are the build, plus --spares if asked, rounded to each part's
    real minimum. No spares by default: with the boards arriving assembled
    there is no attrition to cover, only repair, and that is a separate
    decision from this file."""
    rows, elsewhere = [], []
    for name, count, proj in ((THIS_NAME, na, PROJ),
                              (OTHER_NAME, nb, args.other)):
        for row in read_elsewhere(os.path.join(proj, "fab",
                                               "order-elsewhere.csv")):
            per = row.get("Qty per board") or row.get("Qty") or "0"
            qty = int(per) * count if str(per).isdigit() else 0
            m = re.search(r"LCSC (C\d+)", row.get("Note") or "")
            if m and qty:
                rows.append([m.group(1), qty, name, row.get("Designators", ""),
                             row.get("What", "")])
            else:
                elsewhere.append((name, row.get("Designators", ""), qty,
                                  row.get("What", "")))

    merged = {}
    for pn, qty, src, des, what in rows:
        e = merged.setdefault(pn, [0, [], what])
        e[0] += qty
        e[1].append("%s %s" % (src, des))
    for pn, e in merged.items():
        q = e[0] + spare
        e[0] = moq_round(pn, q, "") if args.moq else q

    fab = os.path.join(PROJ, "fab")
    os.makedirs(fab, exist_ok=True)
    with open(os.path.join(fab, "order-tht-paste.csv"), "w", newline="",
              encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["LCSC Part Number", "Quantity"])
        for pn in sorted(merged):
            w.writerow([pn, merged[pn][0]])
    with open(os.path.join(fab, "order-tht-paste.txt"), "w",
              encoding="utf-8") as fh:
        for pn in sorted(merged):
            fh.write("%s,%d\n" % (pn, merged[pn][0]))

    print("LCSC order with JLCPCB assembling -- %d x %s + %d x %s"
          % (na, THIS_NAME, nb, OTHER_NAME))
    print("=" * 58)
    print("Only what an SMT line will not fit. Every SMD part on both boards")
    print("comes with the assembled boards; buying them here as well is")
    print("paying for them twice.")
    print("")
    print("  %-10s %5s  %-34s %s" % ("part", "qty", "what", "used by"))
    for pn in sorted(merged):
        q, who, what = merged[pn]
        print("  %-10s %5d  %-34s %s" % (pn, q, what[:34], "; ".join(who)))
    print("")
    print("  %d lines, %d pieces%s"
          % (len(merged), sum(v[0] for v in merged.values()),
             ", including %d spare of each" % spare if spare else
             ", no spares"))
    print("")
    print("Still not from LCSC at all:")
    for name, des, qty, what in elsewhere:
        print("  [%s] %-14s x%-4d %s" % (name, des[:14], qty, what[:46]))
    print("")
    print("wrote fab/order-tht-paste.csv and .txt")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--boards", type=int, default=5,
                    help="how many %s boards (default 5)" % THIS_NAME)
    ap.add_argument("--other-boards", type=int, default=None,
                    help="how many %s boards (defaults to --boards)"
                         % OTHER_NAME)
    # Hand assembly loses parts. An 0805 leaves the tweezers and is gone, and
    # the ones that matter are not the resistors -- those come in hundreds
    # whatever you ask for -- but the lines whose minimum is 1 or 5, where
    # the order quantity is exactly the build quantity and losing one stops
    # the build. Spares are added BEFORE rounding, so on a 100-piece minimum
    # they cost nothing at all.
    ap.add_argument("--spares", type=int, default=0,
                    help="extra pieces per line, added before MOQ rounding")
    # When JLCPCB assembles the boards, it sources and fits every SMD part
    # itself under Economic PCBA. Buying those from LCSC as well is paying
    # twice -- $222.92 of overlap on the first order this was used for. What
    # is still needed is only what the assembler will not fit: Economic PCBA
    # is SMT top-side only, so the through-hole lines stay hand-work.
    ap.add_argument("--assembled", action="store_true",
                    help="only the parts an SMT assembler will NOT fit -- "
                         "writes fab/order-tht-paste.csv")
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
    na = args.boards
    nb = args.other_boards if args.other_boards is not None else args.boards
    spare = args.spares

    if args.assembled:
        return assembled_only(args, na, nb, spare)

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
        # Accumulate, do not assign. Two different (comment, footprint)
        # groups can legitimately resolve to the same part number -- and
        # when one of them was wrong they did: C387601 was the crystal on
        # one board and, mistakenly, the 10uF 100V on the other. Assigning
        # here made the second group silently replace the first, so one of
        # the two quantities vanished from the order without a word.
        e = parts.setdefault(keep, {"a": 0, "b": 0, "comment": c, "fp": f})
        e["a"] += sum(v["a"] for v in pns.values())
        e["b"] += sum(v["b"] for v in pns.values())

    rows, shared = [], 0
    saved = 0
    for pn, d in parts.items():
        build = d["a"] * na + d["b"] * nb
        need = build + spare
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
            qa = d["a"] * na + spare
            sep += moq_round(pn, qa, d["fp"]) if args.moq else qa
        if d["b"]:
            qb = d["b"] * nb + spare
            sep += moq_round(pn, qb, d["fp"]) if args.moq else qb
        if d["a"] and d["b"]:
            shared += 1
            saved += sep - order
        rows.append((pn, order, need, d["a"] * na, d["b"] * nb,
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
    lines.append("Combined parts order -- %d x %s + %d x %s%s"
                 % (na, THIS_NAME, nb, OTHER_NAME,
                    ", +%d spare of each" % spare if spare else ""))
    lines.append("=" * 58)
    lines.append("")
    lines.append("Paste fab/order-combined-paste.txt into LCSC's quick-order")
    lines.append("box, or upload order-combined.csv to their BOM tool.")
    lines.append("")
    lines.append("Quantities are the build plus the spares, rounded up to")
    lines.append("each part's real minimum from fab/lcsc-moq.csv. Anything")
    lines.append("not in that table is left alone and LCSC raises it at")
    lines.append("checkout. --no-moq turns the rounding off.")
    if spare:
        lines.append("")
        lines.append("Spares are added BEFORE rounding, so on a 100-piece")
        lines.append("minimum they cost nothing. They only change the lines")
        lines.append("whose minimum is 1 or 5 -- which are the ones where")
        lines.append("losing a part actually stops the build.")
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
    for name, count, path in (
            (THIS_NAME, na, os.path.join(PROJ, "fab",
                                         "order-elsewhere.csv")),
            (OTHER_NAME, nb, os.path.join(args.other, "fab",
                                          "order-elsewhere.csv"))):
        for row in read_elsewhere(path):
            per = row.get("Qty per board") or row.get("Qty") or "0"
            # No spares here -- added once per part after merging, below.
            # A part can appear on several rows (the logger lists J1 and J2
            # separately), and adding spares per row would multiply them.
            qty = int(per) * count if str(per).isdigit() else 0
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
        # Spares once per part, then the same real-minimum rounding the SMD
        # lines get. These are relays and terminal blocks -- minimums of 1
        # and 5, so the spares are the whole point here.
        for pn, e in merged.items():
            e[0] = (moq_round(pn, e[0] + spare, "") if args.moq
                    else e[0] + spare)
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
