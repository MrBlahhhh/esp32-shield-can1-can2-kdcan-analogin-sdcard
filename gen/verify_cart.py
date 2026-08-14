#!/usr/bin/env python3
"""
Is every line in the cart the part the board actually wants?

  python gen/verify_cart.py <lcsc-cart-export.csv>
  python gen/verify_cart.py --live

Stock checking answers "can I buy this". It does not answer "is this the
right thing", and those fail differently: an out-of-stock line stops the
order, a wrong line sails through and arrives.

--live checks the order BEFORE there is a cart, by looking every part
number up in the catalogue instead of reading an export. That is the useful
direction: a wrong number found here is a one-line edit, and the same wrong
number found in a cart export has already been paid for. The file form
stays because it checks what LCSC actually put in the basket, which is the
only thing that proves the paste landed as intended.

An LCSC cart export carries the manufacturer part number, the package and
a description for every line. This compares all three against what the
schematic asked for and reports where they disagree:

  PACKAGE   the land pattern on the board versus the part's own body. This
            is the one that bites -- a SOD-123 diode ordered against an SMA
            footprint solders to nothing, and nothing upstream notices,
            because the part number is valid and the part is in stock.
  VALUE     10k against 10k. Cheap to check and it catches transposition.
  TOLERANCE only when the design asked for one. A 0.1% divider resistor
            that arrives at 1% is a measurement error, not a build error,
            and it will not be found with a meter on the bench.
  VOLTAGE   capacitors only, and only when the cart's rating is LOWER than
            the design's. Higher is free.

It reads the design side from fab/order-combined.csv, so it sees the value
and footprint that the schematic generated, not something retyped.
"""

from __future__ import annotations

import csv
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.abspath(os.path.join(HERE, ".."))
# The other project sharing the order. Named, not guessed -- same as
# export_combined_order.OTHER_DEFAULT.
OTHER_PROJ = r"C:\Projects\gatecontrol\hw"

# Footprint on the board -> tokens that may legitimately appear as the
# part's package. Several packages have two common spellings and LCSC uses
# both, sometimes on the same page.
PKG = {
    "0402": ("0402",), "0603": ("0603",), "0805": ("0805",),
    # Metric spellings too. NOT 3215 -- 1206 is 3216 metric, and 3215 is a
    # crystal package. Accepting it here passed ECS-.327-7-34B-TR, a
    # 32.768 kHz crystal in SMD3215-2P, as the converter's 10uF 100V bulk
    # input capacitor on a C_1206_3216Metric land.
    "1206": ("1206", "3216"), "1210": ("1210", "3225"),
    "1812": ("1812", "4532"),
    "D_SMA": ("SMA", "DO-214AC"),
    "D_SMB": ("SMB", "DO-214AA"),
    "D_SMC": ("SMC", "DO-214AB"),
    "D_SOD-123": ("SOD-123",),
    "SOT-23": ("SOT-23", "SOT23"),
    "SOT-23-5": ("SOT-23-5",),
    "SOT-23-6": ("SOT-23-6",),
    "SOIC-8": ("SOIC-8", "SO-8", "SOP-8"),
    "SOIC-14": ("SOIC-14", "SO-14"),
    "VSSOP-10": ("VSSOP-10", "MSOP-10", "TSSOP-10"),
    "DIP-4": ("DIP-4", "DIP4"),
}

MULT = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3,
        "": 1.0, "k": 1e3, "K": 1e3, "M": 1e6, "R": 1.0}


def expect_pkg(footprint):
    for key, toks in PKG.items():
        if key in (footprint or ""):
            return toks
    m = re.search(r"_(\d{4})_\d+Metric", footprint or "")
    return (m.group(1),) if m else ()


def number(text):
    """'4.7k' / '470nF' / '60.4' -> a float, or None."""
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([pnumkKMR]?)", text or "")
    if not m:
        return None
    return float(m.group(1)) * MULT.get(m.group(2), 1.0)


# Find a value of a given kind ANYWHERE in a catalogue line, by its unit.
# Reading the leading token instead does not work: the line may lead with the
# value ("100kO +/-1% 125mW 0805"), with the power ("125mW 2.21kOhm 150V"),
# with the voltage ("100V 100nF X7R") or with the temperature range
# ("-55C~+155C 10kOhm 125mW"). Anchoring on the unit sidesteps all of that,
# and it will not match "+/-1%", "125mW", "150V" or "+/-25ppm/C".
UNIT_RE = {
    "F": r"(\d+(?:\.\d+)?)\s*([pnuµmk]?)F\b",
    "H": r"(\d+(?:\.\d+)?)\s*([pnuµmk]?)H\b",
    "R": r"(\d+(?:\.\d+)?)\s*([pnuµmkKM]?)\s*(?:Ω|ohm|R)\b",
}


def unit_of(want):
    """Which unit the design's value is expressed in. A bare '100k' is a
    resistor -- that is the convention the schematic uses."""
    if re.search(r"[\d.]\s*[pnuµmk]?F\b", want):
        return "F"
    if re.search(r"[\d.]\s*[pnuµmk]?H\b", want):
        return "H"
    return "R"


def values_in(desc, unit):
    """Every value of that unit stated in a catalogue line."""
    out = []
    for m in re.finditer(UNIT_RE[unit], desc, re.IGNORECASE):
        mult = MULT.get(m.group(2), MULT.get(m.group(2).lower(), 1.0))
        out.append(float(m.group(1)) * mult)
    return out


def looks_like_value(text):
    """Is this field a component VALUE, or a part number that happens to
    start with a digit? Both live in the Comment column.

    '1N4148W' read as a value is 1, and the catalogue line for it leads
    with '150mA', so the two disagree by a factor of 150 and a perfectly
    correct diode gets reported wrong."""
    t = (text or "").strip()
    if not re.match(r"^[\d.]", t):
        return False
    if re.search(r"\d[A-Za-z]+\d", t):
        return False                      # 1N4148W, 1N5819, 2N7002
    lead = re.match(r"^(\d+(?:\.\d+)?)([pnumkKMR]?)", t)
    if lead and not lead.group(2) and "." not in lead.group(1) \
            and len(lead.group(1)) > 4:
        return False                      # 742792022 -- a Wurth MPN
    return True


def live_cart(part_numbers):
    """Build the same {pn: {Description, Package}} shape from the catalogue.

    Reuses check_stock's query, retries and all -- the throttling and the
    cp1252 trap it works around apply just as much here."""
    sys.path.insert(0, HERE)
    from check_stock import query  # noqa: E402  same service, same retries

    out, missing = {}, []
    for i, pn in enumerate(part_numbers, 1):
        sys.stderr.write("\r  looking up %d/%d " % (i, len(part_numbers)))
        hit = None
        for c in query(pn):
            if ("C" + str(c.get("lcsc"))) == pn:
                hit = c
                break
        if hit is None:
            missing.append(pn)
            continue
        out[pn] = {"Description": hit.get("description") or "",
                   "Package": hit.get("package") or "",
                   "Manufacture Part Number": hit.get("mfr") or ""}
    sys.stderr.write("\r" + " " * 30 + "\r")
    return out, missing


def bom_parts():
    """Every part number named by either project's own assembly BOM, in the
    same {Comment, Footprint} shape the combined order uses."""
    out = {}
    for proj in (PROJ, OTHER_PROJ):
        path = os.path.join(proj, "fab", "bom.csv")
        if not os.path.exists(path):
            continue
        for r in csv.DictReader(open(path, encoding="utf-8")):
            pn = (r.get("JLCPCB Part #") or "").strip()
            if pn:
                out[pn] = {"Comment": r.get("Comment", ""),
                           "Footprint": r.get("Footprint", "")}
    return out


def swap_map():
    """{original part number: the one check_stock substituted for it}."""
    path = os.path.join(PROJ, "fab", "order-verified.csv")
    out = {}
    if not os.path.exists(path):
        return out
    for r in csv.DictReader(open(path, encoding="utf-8")):
        m = re.search(r"replaces (C\d+)", r.get("Note") or "")
        if m:
            out[m.group(1)] = r["LCSC Part Number"].strip()
    return out


def main():
    # Catalogue descriptions carry an ohm sign and a degree sign, and a
    # Windows console is cp1252, which cannot encode either. Without this
    # the report dies partway through printing the disagreements -- after
    # doing all the work and before showing most of the answer.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    design_path = os.path.join(PROJ, "fab", "order-combined.csv")
    design = {r["LCSC"].strip(): r
              for r in csv.DictReader(open(design_path, encoding="utf-8"))}
    missing = []
    if sys.argv[1] == "--live":
        # The combined order holds only the numbers it KEPT. Where the two
        # projects named different parts for one value, the loser is dropped
        # before it reaches this file and is never checked -- which is how
        # three wrong numbers survived in the gate board: an 0805 capacitor
        # on a 1206 land, a 100k that is not in the catalogue, and C79666,
        # an IR2153S half-bridge gate driver standing in for a PTC fuse.
        # Consolidation replaced all three with the logger's numbers, so the
        # cart was correct and the design was not.
        #
        # So check every number each project's own BOM names, not just the
        # survivors.
        design.update(bom_parts())
        pns = list(design)
        # check_stock may have replaced a short line. The replacement is what
        # actually gets pasted, so it is the thing that needs checking -- and
        # it is the line least likely to have had a human look at it, because
        # a tool chose it. Check it against the design row it stands in for.
        subs = swap_map()
        pns = [subs.get(p, p) for p in pns]
        cart, missing = live_cart(pns)
    else:
        subs = {}
        cart = {r["LCSC#"].strip(): r
                for r in csv.DictReader(open(sys.argv[1],
                                             encoding="utf-8-sig"))}

    problems, blind, checked = [], [], 0
    for pn, d in design.items():
        bought = subs.get(pn, pn)
        c = cart.get(bought)
        if c is None:
            continue
        checked += 1
        if bought != pn:
            pn = "%s>%s" % (pn, bought)
        want_v, fp = d["Comment"], d["Footprint"]
        desc = c.get("Description") or ""
        pkg = c.get("Package") or ""
        blob = (desc + " " + pkg).replace("Ω", "").replace(" ", "")

        toks = expect_pkg(fp)
        if toks and not any(t.replace("-", "").lower()
                            in (pkg + desc).replace("-", "").lower()
                            for t in toks):
            problems.append(("PACKAGE", pn, want_v,
                             "board is %s, part is %s" % (fp, pkg)))

        # LCSC puts the value FIRST: "100kO +/-1% 125mW 0805 Thick Film".
        # Read that leading token and nothing else. Searching the whole
        # string finds the 1 of "+/-1%" and the 125 of "125mW", which is how
        # the first version of this reported every resistor on the board as
        # the wrong value.
        want_n = number(want_v)
        if want_n is not None and looks_like_value(want_v):
            unit = unit_of(want_v)
            got = values_in(desc, unit)
            if not desc.strip():
                # Some catalogue records carry no description at all. Nothing
                # to check against; say so rather than call it a pass.
                blind.append((pn, want_v))
            elif not got:
                # The design asked for a capacitance and the line states no
                # capacitance anywhere. That is how a crystal got accepted as
                # the converter's 10uF bulk input cap -- the old check read
                # the leading token, "Crystal", found no number, and reported
                # nothing at all.
                problems.append(("NO VALUE", pn, want_v,
                                 "no %s value in: %s"
                                 % ({"F": "capacitance", "H": "inductance"}
                                    .get(unit, "resistance"), desc[:36])))
            elif not any(abs(g - want_n) / max(want_n, 1e-15) <= 0.02
                         for g in got):
                problems.append(("VALUE", pn, want_v,
                                 "cart says %s" % desc[:44]))

        tol = re.search(r"(\d+(?:\.\d+)?)\s*%", want_v)
        if tol:
            if ("±%s%%" % tol.group(1)) not in desc.replace(" ", ""):
                problems.append(("TOLERANCE", pn, want_v,
                                 "cart says %s" % desc[:44]))

        vm = re.search(r"(\d+)V\b", want_v)
        cm = re.search(r"(\d+)V\b", desc)
        if vm and cm and int(cm.group(1)) < int(vm.group(1)):
            problems.append(("VOLTAGE", pn, want_v,
                             "cart part is only %sV" % cm.group(1)))

    print("Cart correctness -- %d lines cross-checked\n" % checked)
    if missing:
        # Absent from this index is not proof the number is wrong -- it
        # carries only JLCPCB's assembly library -- so it is reported apart
        # from the disagreements rather than counted as one.
        print("  not in the index, check by hand: %s\n" % ", ".join(missing))
    if blind:
        # No description in the record, so package and tolerance were checked
        # but the value could not be. Said out loud rather than counted as a
        # pass -- "checked" and "nothing to check with" are different answers.
        print("  no description on record, value not checked: %s\n"
              % ", ".join("%s (%s)" % (p, v) for p, v in blind))
    if not problems:
        print("  every line matches the value, package and rating the "
              "schematic asked for")
        return 0
    for kind, pn, want, why in sorted(problems):
        print("  %-9s %-9s design wants %-14s %s" % (kind, pn, want, why))
    print("\n  %d disagreement(s). PACKAGE is the one that will not solder."
          % len(problems))
    return 1


if __name__ == "__main__":
    sys.exit(main())
