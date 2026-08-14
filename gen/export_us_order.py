#!/usr/bin/env python3
"""
The parts order from a US distributor instead of LCSC.

  python gen/export_us_order.py [--logger 10] [--gate 5]
                                [--logger-assembled] [--gate-assembled]

Why this exists: a 35% tariff plus international freight makes LCSC's unit
prices much less decisive than they look, and US distributors have **no
minimum order quantity**. LCSC sells 0805 resistors in hundreds, so an order
needing twelve buys a hundred; Digi-Key sells twelve. On this project that is
1463 pieces against 3894, and it closes most of the gap on its own.

Three problems have to be solved to make the order usable, and they are the
reason this is a script and not a spreadsheet:

1. **Digi-Key does not know LCSC codes.** `C37593` means nothing there. MPNs
   come from `fab/lcsc-moq.csv`, which accumulates them out of cart exports.

2. **Half the BOM is Chinese-brand.** UNI-ROYAL and FOJAN resistors, MDD
   diodes, JSCJ FETs -- all fine parts, none stocked in the US. For plain
   passives the substitution is free: any 1% 0805 of the same value is the
   same component. Those are emitted as Yageo equivalents, with the part
   number constructed from Yageo's own scheme:

       RC + size + F(=1%) + R-07 + value + L,  value coded with the
       multiplier letter standing in for the decimal point

   so 100R = 100 ohms, 60R4 = 60.4, 10K, 31K6, 121K, 1M. That scheme was
   verified against the live catalogue for 100R and 31K6 before being
   trusted for the rest, and every constructed number is marked so in the
   output. Ceramics follow Yageo's CC0805KRX7R9BB<3-digit> the same way.

3. **Some substitutions are NOT free**, and those are refused rather than
   guessed. A relay or a terminal block has a footprint; swapping the maker
   swaps the pinout, and the board has already been laid out. Those are
   listed separately for a human to choose, with the spec they must meet.

Outputs, all in fab/:
  order-us.csv       MPN and quantity -- upload to Digi-Key's BOM manager
  order-us.txt       the readable version, including what to choose by hand
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.abspath(os.path.join(HERE, ".."))
OTHER = r"C:\Projects\gatecontrol\hw"

# Brands a US distributor carries, orderable under the same MPN.
STOCKED = {"TI", "NXP", "MICROCHIP", "onsemi", "TDK", "YAGEO", "Littelfuse",
           "BOURNS", "SEMTECH", "HRS", "JST", "DIODES", "AOS", "ECS",
           "Samsung Electro-Mechanics", "ST(Semtech)", "LITEON"}

# Industry-standard numbers that many makers second-source. The Chinese brand
# on the LCSC line is irrelevant -- Digi-Key will have the same number.
GENERIC_MPN = {"SS34", "SS14", "BAT54S", "2N7002", "1N4148W", "SMAJ5.0A",
               "SMAJ36A", "SMAJ6.0A", "SMAJ40CA", "DMG2301L", "LTV-814",
               "AO3400A", "SRV05-4.TCT", "TLV431ASN1T1G"}

# Footprint-critical. Refuse to substitute; the board is already laid out.
BY_HAND = {
    "SRD-05VDC-SL-C": "SPDT power relay, 5 V coil, THT, 19.2 x 15.6 mm body, "
                      "SANYOU/Songle pinout (coil 2+5, COM 1, NO 3, NC 4)",
    "KF301-5.0-2P": "2-pole screw terminal, 5.00 mm pitch, THT",
    "KF301-5.0-3P": "3-pole screw terminal, 5.00 mm pitch, THT",
    "TX322540M4FBCE2T": "40 MHz crystal, 3225 4-pad, 12 pF load, "
                        "-40/+85 C or wider",
    "KT-0805G": "green LED, 0805, ~525 nm",
    "NCD0805Y1": "yellow LED, 0805, ~595 nm",
}

# Digi-Key price breaks for jellybeans, measured on Yageo RC0805 and CC0805.
# Buying UP to a break is often cheaper than buying under it -- 4 pieces at
# the qty-1 price costs more than 10 at the qty-10 price -- so quantities are
# lifted to the next break when that does not increase the bill.
BREAKS = [(1, 0.100, 0.140), (10, 0.0340, 0.0760),
          (100, 0.0171, 0.0450), (250, 0.0128, 0.0331)]


def yageo_code(ohms):
    """4.7k -> '4K7', 100 -> '100R', 31600 -> '31K6', 1e6 -> '1M'."""
    for div, letter in ((1e6, "M"), (1e3, "K"), (1.0, "R")):
        if ohms >= div:
            v = ohms / div
            s = ("%.3g" % v)
            if "." in s:
                a, b = s.split(".")
                return a + letter + b
            return s + letter
    return None


def parse_ohms(text):
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([kKMR]?)", text or "")
    if not m:
        return None
    return float(m.group(1)) * {"": 1.0, "R": 1.0, "k": 1e3, "K": 1e3,
                                "M": 1e6}[m.group(2)]


def bump(qty, kind):
    """Lift to the next price break when it is not more expensive."""
    col = 1 if kind == "res" else 2
    best, cost = qty, None
    for q, r, c in BREAKS:
        unit = (r, c)[col - 1]
        if q >= qty:
            total = q * unit
            if cost is None or total <= cost:
                best, cost = q, total
            break
        cost = qty * unit
    for q, r, c in BREAKS:
        if q >= qty and q * (r, c)[col - 1] <= qty * _unit_at(qty, col):
            return q
    return best


def _unit_at(qty, col):
    u = (BREAKS[0][1], BREAKS[0][2])[col - 1]
    for q, r, c in BREAKS:
        if qty >= q:
            u = (r, c)[col - 1]
    return u


def collect(nl, ng, logger_asm, gate_asm, spares):
    """{lcsc: [qty, comment, footprint]} for everything still to be bought."""
    need = {}
    smd = []
    if not logger_asm:
        smd.append((PROJ, nl))
    if not gate_asm:
        smd.append((OTHER, ng))
    for proj, cnt in smd:
        for r in csv.DictReader(open(os.path.join(proj, "fab", "bom.csv"),
                                     encoding="utf-8")):
            pn = r["JLCPCB Part #"].strip()
            n = len([d for d in r["Designator"].strip('"').split(",")
                     if d.strip()])
            e = need.setdefault(pn, [0, r["Comment"], r["Footprint"]])
            e[0] += n * cnt
    # Through-hole is never fitted by an SMT line, so it is always needed.
    for proj, cnt in ((PROJ, nl), (OTHER, ng)):
        p = os.path.join(proj, "fab", "order-elsewhere.csv")
        if not os.path.exists(p):
            continue
        for r in csv.DictReader(open(p, encoding="utf-8")):
            m = re.search(r"LCSC (C\d+)", r.get("Note") or "")
            per = r.get("Qty per board") or r.get("Qty") or "0"
            if m and str(per).isdigit() and int(per):
                e = need.setdefault(m.group(1),
                                    [0, r.get("What", "")[:40], "THT"])
                e[0] += int(per) * cnt
    for e in need.values():
        e[0] += spares
    return need


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--logger", type=int, default=10)
    ap.add_argument("--gate", type=int, default=5)
    ap.add_argument("--logger-assembled", action="store_true")
    ap.add_argument("--gate-assembled", action="store_true")
    ap.add_argument("--spares", type=int, default=2)
    args = ap.parse_args()

    led = {r["LCSC"]: r for r in
           csv.DictReader(open(os.path.join(PROJ, "fab", "lcsc-moq.csv"),
                               encoding="utf-8"))}
    need = collect(args.logger, args.gate, args.logger_assembled,
                   args.gate_assembled, args.spares)

    order, made, manual, unknown = [], [], [], []
    seen_mpn = {}
    for pn, (qty, what, fp) in sorted(need.items()):
        row = led.get(pn, {})
        mpn = (row.get("MPN") or "").strip()
        maker = (row.get("Manufacturer") or "").strip()
        pkg = (row.get("Package") or "").strip()
        if mpn in BY_HAND:
            manual.append((mpn, qty, what, BY_HAND[mpn]))
            continue
        if maker in STOCKED or mpn in GENERIC_MPN:
            order.append((mpn, qty, what, pkg, ""))
            continue
        # Chinese-brand jellybean -> construct the Yageo equivalent.
        size = "0805" if "0805" in pkg else "1206" if "1206" in pkg else None
        is_res = size and not re.search(r"[pnu]?F\b", what)
        if is_res:
            # Tolerance decides the SERIES, not just the suffix. RC is 1%
            # thick film; a 0.1% divider resistor needs RT, thin film, and
            # dropping a 1% part into one is a measurement error nothing on
            # the bench would catch.
            #
            # The first cut of this wrote `"%" not in what.replace("1%","")`
            # to mean "no tolerance other than 1%". On "10k 0.1%" that strips
            # the 1% out of the 0.1% and leaves "10k 0.", which has no percent
            # sign, so the precision part sailed through as RC0805FR-0710KL --
            # the same number already emitted for the ordinary 10k.
            tm = re.search(r"(\d+(?:\.\d+)?)\s*%", what)
            tol = float(tm.group(1)) if tm else 1.0
            ohms = parse_ohms(what)
            code = yageo_code(ohms) if ohms else None
            if code and tol >= 1.0:
                sub = "RC%sFR-07%sL" % (size, code)
            elif code and abs(tol - 0.1) < 1e-9:
                sub = "RT%sBRD07%sL" % (size, code)
            else:
                sub = None
            if sub:
                order.append((sub, bump(qty, "res"), what, size,
                              "constructed, was %s" % mpn))
                made.append(sub)
                continue
        if size and re.search(r"([\d.]+)\s*nF", what):
            nf = float(re.search(r"([\d.]+)\s*nF", what).group(1))
            pf = int(nf * 1000)
            code = "%d%d" % (int(str(pf)[:2]), len(str(pf)) - 2)
            sub = "CC%sKRX7R9BB%s" % (size, code)
            order.append((sub, bump(qty, "cap"), what, size,
                          "constructed, was %s" % mpn))
            made.append(sub)
            continue
        unknown.append((pn, mpn, maker, qty, what, pkg))

    # A part number must appear once. Two rows for one MPN means either a
    # duplicate or -- as happened with the 0.1% resistor -- a substitution
    # that collapsed two different parts onto the same number.
    dupes = []
    for mpn, qty, what, pkg, note in order:
        seen_mpn.setdefault(mpn, []).append(what)
    for mpn, whats in seen_mpn.items():
        if len(whats) > 1:
            dupes.append((mpn, whats))

    fab = os.path.join(PROJ, "fab")
    with open(os.path.join(fab, "order-us.csv"), "w", newline="",
              encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Manufacturer Part Number", "Quantity",
                    "Customer Reference", "Note"])
        for mpn, qty, what, pkg, note in order:
            w.writerow([mpn, qty, what, note])

    L = []
    L.append("US distributor order -- %d x logger%s, %d x gate%s"
             % (args.logger, " (assembled)" if args.logger_assembled else "",
                args.gate, " (assembled)" if args.gate_assembled else ""))
    L.append("=" * 64)
    L.append("")
    L.append("Upload order-us.csv to Digi-Key's BOM manager. Quantities are")
    L.append("the exact build plus %d spares -- there are no minimums here,"
             % args.spares)
    L.append("and some lines are lifted to the next price break only because")
    L.append("that costs less than buying under it.")
    L.append("")
    L.append("  %-26s %6s  %s" % ("part number", "qty", "what"))
    for mpn, qty, what, pkg, note in order:
        L.append("  %-26s %6d  %-22s %s" % (mpn, qty, what[:22], note))
    L.append("")
    L.append("  %d lines, %d pieces" % (len(order), sum(o[1] for o in order)))
    if made:
        L.append("")
        L.append("%d of those are CONSTRUCTED Yageo equivalents" % len(made))
        L.append("-" * 64)
        L.append("The original is a Chinese brand no US distributor carries.")
        L.append("Same value, tolerance and package, so the substitution is")
        L.append("free -- but the numbers are built from Yageo's scheme, not")
        L.append("read off a catalogue page. The scheme was checked against")
        L.append("the live site for 100R and 31K6. Spot-check a few.")
    if manual:
        L.append("")
        L.append("CHOOSE THESE YOURSELF -- footprint-critical")
        L.append("-" * 64)
        L.append("Substituting the maker substitutes the pinout, and the")
        L.append("board is already laid out. Match the spec exactly:")
        for mpn, qty, what, spec in manual:
            L.append("")
            L.append("  %-22s x%-4d  (was %s)" % (what[:22], qty, mpn))
            L.append("        %s" % spec)
    if unknown:
        L.append("")
        L.append("NOT CLASSIFIED -- look these up by hand")
        L.append("-" * 64)
        for pn, mpn, maker, qty, what, pkg in unknown:
            L.append("  %-10s %-24s %-18s x%-4d %s"
                     % (pn, mpn[:24], maker[:18], qty, what[:20]))
    text = "\n".join(L) + "\n"
    open(os.path.join(fab, "order-us.txt"), "w", encoding="utf-8").write(text)
    print(text)
    print("wrote fab/order-us.csv and order-us.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
