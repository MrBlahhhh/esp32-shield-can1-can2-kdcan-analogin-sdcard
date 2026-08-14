#!/usr/bin/env python3
"""
Shopping list for building the boards yourself.

  python gen/export_order.py [--boards 5] [--spares-passive 20] [--spares-ic 1]

`fab/bom.csv` is written for JLCPCB's assembly flow, where quantities are
implicit and the through-hole connectors, the two supercapacitors and the
dev board itself are deliberately absent. None of that is right for buying the
parts loose and reflowing them at home, which needs the opposite: every part,
an explicit count, and spares for the ones that will get lost or tombstoned.

Writes three files to `fab/`:

  order-lcsc.csv       part number + quantity, ready to paste into LCSC's
                       bulk-order box
  order-elsewhere.csv  the parts LCSC/JLC cannot supply, with what to search
  order-summary.txt    a readable version of both, with the reasoning

Spares default to +20 pieces on passives (they cost fractions of a cent and
LCSC has minimum order quantities anyway, which will round most of them up
regardless) and +1 on anything with a package you would not want to hand-place
twice.
"""

from __future__ import annotations

import argparse
import collections
import csv
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.abspath(os.path.join(HERE, ".."))
FAB = os.path.join(PROJ, "fab")

# Parts no assembly house here can supply, or that are through-hole and were
# deliberately kept out of fab/. Buy these separately.
#
# The three electrolytics are the interesting ones: JLCPCB's SMD aluminium
# electrolytic library stops at 47 uF once 100 V is needed, so there is no
# part to order from them at all. Nichicon UCD / Rubycon MV are the reference
# series. Check the can diameter before ordering -- the land patterns are
# 10 mm and 16 mm, and an 18 mm part will not fit.
ELSEWHERE = [
    # Every designator here is through-hole, so it is deliberately absent
    # from fab/. The two supercapacitors are the only ones that are not just
    # a connector, and they are the only line on this board where the exact
    # part actually matters -- see the note.
    ("C6, C7",    2, "1 F 2.7 V low-ESR EDLC, 8 mm can, radial P3.5mm",
     "Eaton HB1030-2R5105-R or equivalent. TWO THINGS TO CHECK. (1) ESR must "
     "be under about 1 ohm: a 5.5 V coin-type EDLC is 30-200 ohm and cannot "
     "source the 120 mA this has to carry, so the rail would collapse the "
     "instant it was asked to. Cylindrical cells are tens of milliohms. "
     "(2) Anything from 0.1 F up works -- 0.1 F still gives about 500 ms of "
     "hold-up against the 2.5 s the design point buys"),
    ("J1",        1, "JST PH 4-pin vertical header, 2.00 mm",
     "B4B-PH-K-S(LF)(SN), LCSC C131334. The CAN1 + power harness: OBD-II "
     "pins 16, 4, 6, 14"),
    ("J2",        1, "JST PH 4-pin vertical header, 2.00 mm",
     "B4B-PH-K-S(LF)(SN), LCSC C131334, same part as J1. The aux bus: "
     "K-line or CAN 2, "
     "whichever AUXSEL is set to"),
    ("J9",        1, "JST PH 10-pin vertical header, 2.00 mm",
     "B10B-PH-K-S(LF)(SN), LCSC C158038. Sensor harness"),
    ("J3, J4",    2, "0.1\" pin SOCKET, 1x22 vertical",
     "RECEPTACLES, not pin headers: a DevKitC-1 ships with male pins already "
     "soldered pointing down, and it drops into these. Buy 1x22 strips -- "
     "most 1x40 socket strips do not cut cleanly and you lose a position"),
    ("J5, J7, J10", 3, "0.1\" pin header, 1x04 vertical", "any"),
    ("J6",        1, "0.1\" pin header, 1x03 vertical", "any"),
    ("harness",   1, "JST PH crimp housings + crimps",
     "PHR-4 x2 and PHR-10 x1 per board, plus SPH-002T-P0.5S crimps. Buy with "
     "the headers or the loom cannot be made up"),
    ("dev board", 1, "ESP32-S3-DevKitC-1, N8R8 or N16R8",
     "The MCU. Check the revision: v1.1 puts the onboard RGB LED on IO38, "
     "which this board leaves free for it. Some revisions use IO48, which is "
     "the WS2812 output here and would clash"),
]



# LCSC enforces a minimum order quantity and an order *multiple* per part, and
# asking for 95 of something sold in hundreds is a rejected line rather than a
# rounded one. Sampled from the live catalogue on 2026-08-13:
#
#   0805 resistor  C17414   min 100, multiple 100
#   0805 capacitor C49678   min  20, multiple  20
#   1206 capacitor C13585   min  20, multiple  20
#   SOT-23 BAT54S  C408389  min  50, multiple  50
#   SO-8 LM5164    C477928  min   1, multiple   1
#   ESP32 module   C2913202 min   1, multiple   1
#
# The pattern is commodity-versus-branded, not package: cheap jellybean parts
# are sold in reels of 20-100, anything with a manufacturer's name on it goes
# in ones. Rounding passives to 100 satisfies every 20/50/100 multiple at once
# and costs about thirty cents a line, which is cheaper than a second order.
MOQ_EXACT = {}                      # per-part overrides, if one ever surprises us
MOQ_PASSIVE = 100                   # 0402 .. 1210
MOQ_DISCRETE = 50                   # SOT-23 / SOD-123 jellybean semiconductors
_DISCRETE_FP = re.compile(r"SOT-\d|SOD-\d|SOT23|TO-236")


def moq_round(pn, qty, footprint):
    """Bump a quantity up to something LCSC will actually accept."""
    step = MOQ_EXACT.get(pn)
    if step is None:
        if re.search(r"_(0402|0603|0805|1206|1210)_", footprint):
            step = MOQ_PASSIVE
        elif _DISCRETE_FP.search(footprint):
            step = MOQ_DISCRETE
        else:
            step = 1               # branded ICs, connectors, inductors
    return ((qty + step - 1) // step) * step


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--boards", type=int, default=5)
    ap.add_argument("--spares-passive", type=int, default=20)
    ap.add_argument("--spares-ic", type=int, default=1)
    args = ap.parse_args()

    bom = os.path.join(FAB, "bom.csv")
    if not os.path.exists(bom):
        raise SystemExit("fab/bom.csv missing -- run gen/export_fab.py first")

    per_part = collections.defaultdict(lambda: {"n": 0, "refs": [], "cmt": "", "fp": ""})
    unspecified = []
    for row in csv.DictReader(open(bom, encoding="utf-8")):
        pn = row["JLCPCB Part #"].strip()
        refs = [r.strip() for r in row["Designator"].split(",") if r.strip()]
        if not pn:
            unspecified.append((row["Comment"], row["Designator"]))
            continue
        e = per_part[pn]
        e["n"] += len(refs)
        e["refs"] += refs
        e["cmt"] = row["Comment"]
        e["fp"] = row["Footprint"].split(":")[-1]

    def is_passive(fp):
        return bool(re.search(r"_(0402|0603|0805|1206|1210)_", fp))

    lines = []
    for pn, e in sorted(per_part.items(), key=lambda kv: -kv[1]["n"]):
        need = e["n"] * args.boards
        spare = args.spares_passive if is_passive(e["fp"]) else args.spares_ic
        lines.append((pn, need + spare, need, spare, e["cmt"], e["fp"],
                      ",".join(sorted(set(e["refs"])))))

    os.makedirs(FAB, exist_ok=True)
    out = os.path.join(FAB, "order-lcsc.csv")
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["LCSC Part Number", "Order Qty", "Needed", "Spares",
                    "Comment", "Footprint", "Designators"])
        for pn, order, need, spare, cmt, fp, refs in lines:
            w.writerow([pn, moq_round(pn, order, fp), need, spare,
                        cmt, fp, refs])

    # LCSC's BOM tool has a column-mapping step, and given a wide file it can
    # decide the wrong column is the part number -- feeding "100nF 16V" into a
    # part-number search returns nothing useful and the whole upload comes back
    # as unavailable. So also emit the narrowest possible file: two columns,
    # the exact header names LCSC expects, nothing to misread.
    #
    # Quantities here are rounded up to a multiple of 100 for 0805/1206
    # passives, because LCSC enforces a minimum order quantity and an order
    # multiple per part (100 for most 0805 resistors, 20-50 for capacitors).
    # A hundred 0805 resistors costs about thirty cents; being under the MOQ
    # costs a round trip.
    simple = os.path.join(FAB, "order-lcsc-simple.csv")
    paste = os.path.join(FAB, "order-lcsc-paste.txt")
    with open(simple, "w", newline="", encoding="utf-8") as fh, \
            open(paste, "w", encoding="utf-8") as pf:
        w = csv.writer(fh)
        w.writerow(["LCSC Part Number", "Quantity"])
        for pn, order, need, spare, cmt, fp, refs in lines:
            q = moq_round(pn, order, fp)
            w.writerow([pn, q])
            pf.write("%s,%d\n" % (pn, q))

    out2 = os.path.join(FAB, "order-elsewhere.csv")
    with open(out2, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Designators", "Qty per board", "Order Qty", "What", "Note"])
        for refs, n, what, note in ELSEWHERE:
            w.writerow([refs, n, n * args.boards, what, note])

    txt = os.path.join(FAB, "order-summary.txt")
    with open(txt, "w", encoding="utf-8") as fh:
        fh.write("Parts order for %d boards\n" % args.boards)
        fh.write("=" * 40 + "\n\n")
        fh.write("1. From LCSC -- paste fab/order-lcsc-paste.txt into their\n")
        fh.write("   quick-order box, or upload order-lcsc-simple.csv.\n")
        fh.write("   Order quantities are already rounded up to LCSC's\n")
        fh.write("   minimum order quantity and order multiple.\n\n")
        fh.write("   %-12s %6s %7s  %-18s %s\n"
                 % ("part", "order", "needed", "comment", "designators"))
        for pn, order, need, spare, cmt, fp, refs in lines:
            fh.write("   %-12s %6d %7d  %-18s %s\n"
                     % (pn, moq_round(pn, order, fp), need, cmt[:18], refs[:46]))
        fh.write("\n   %d distinct parts, %d pieces after MOQ rounding\n"
                 % (len(lines),
                    sum(moq_round(l[0], l[1], l[5]) for l in lines)))
        fh.write("   LCSC enforces the true per-part minimum at checkout, so\n"
                 "   anything undercalled here is bumped there, not rejected.\n")
        fh.write("\n2. Not available from LCSC/JLC -- source separately\n\n")
        for refs, n, what, note in ELSEWHERE:
            fh.write("   %-12s x%-3d %s\n" % (refs, n * args.boards, what))
            fh.write("   %s\n\n" % ("             " + note))

    print("wrote %s  (%d parts, %d pieces after MOQ rounding)"
          % (os.path.relpath(out, PROJ), len(lines),
             sum(moq_round(l[0], l[1], l[5]) for l in lines)))
    print("wrote %s  (2 columns, for LCSC's BOM tool)"
          % os.path.relpath(simple, PROJ))
    print("wrote %s  (paste straight into LCSC quick order)"
          % os.path.relpath(paste, PROJ))
    print("wrote %s  (%d items)" % (os.path.relpath(out2, PROJ), len(ELSEWHERE)))
    print("wrote %s" % os.path.relpath(txt, PROJ))
    if unspecified:
        print("\n%d BOM lines have no part number and are NOT in the order:"
              % len(unspecified))
        for cmt, refs in unspecified:
            print("   %-20s %s" % (cmt, refs))
        return 1
    print("\nEvery line in fab/bom.csv carries a part number.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
