#!/usr/bin/env python3
"""
Check that every polarised part faces the right way, and print a fab checklist.

  python gen/audit_polarity.py

Two failure modes, and they need different answers.

**Backwards in the design.** A diode whose anode and cathode are on the wrong
nets. ERC will not catch it -- a diode is two pins and both are connected --
and neither will DRC, because the copper is fine. It is only wrong in a way
that shows up when the board is powered. This script checks it against
`netlist.txt` using the pin convention KiCad's `Device:D` symbol uses:
**pin 1 is the cathode, pin 2 is the anode**. (Confirmed two ways in this
design: `D2`, the 3.6 V rail zener, has pin 1 on `+3V3` and pin 2 on `GND`;
`D5`, the USB OR-ing diode, has pin 1 on `+5V` and pin 2 on `VBUS`, and the
current has to flow VBUS to +5V.)

**Backwards at the assembler.** The design is right and JLCPCB's library
orients the package differently from KiCad's, so the part comes back rotated
180 degrees. Nothing in this repository can detect that -- it depends on their
part library -- but "look hard at every diode" is not a procedure. So the
second half of this script prints, for every polarised part, where it sits on
the board and which net its **pin 1** is on, giving a concrete thing to compare
against JLC's placement preview instead of an instruction to squint.

Bidirectional TVS parts (value ending `CA`) are listed as orientation-free,
because they are: two zeners back to back, symmetric by construction. Getting
one of those backwards costs nothing, and knowing which ones they are means
the checklist is short enough to actually work through.

Exit status is non-zero if a polarity check fails.
"""

from __future__ import annotations

import csv
import os
import re
import sys
import collections

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.abspath(os.path.join(HERE, ".."))
PCB = os.path.join(PROJ, "esp32s3-can-sd-logger.kicad_pcb")

# What each polarised part must connect where: (cathode_net, anode_net),
# which is pin 1 then pin 2 on every KiCad diode symbol.
#
# Keyed on the VALUE, not the reference designator. As designators this table
# went stale the moment the harness was split into two plugs and every part
# after it renumbered -- D2 stopped being the rail zener and became the
# hold-up Schottky, so the audit compared the new part against the old rule
# and reported a mismatch that was real for the wrong reason. Values survive
# renumbering; designators are an output of assign_refs().
DIODE_EXPECT = {
    # the hold-up bank's discharge path: conducts bank -> rail, and must NOT
    # conduct rail -> bank or it shorts out the inrush limiter
    "SS14":     ("+5V", "SCAP_TOP"),
    # unidirectional TVS on the sensor rail: cathode to the rail it clamps
    "SMAJ6.0A": ("+5VS", "GND"),
    # power LED: anode to +3V3, cathode down through its series resistor
    "green":    ("PWR_LED_K", "+3V3"),
}

# BAT54S is a *series* pair in SOT-23: pin 1 = anode of D1, pin 3 = the common
# node (cathode of D1, anode of D2), pin 2 = cathode of D2. As a signal clamp
# the signal goes on the common pin, GND on 1 and the rail on 2 -- then the
# lower diode conducts when the signal falls below ground and the upper one
# when it rises above the rail. Reversing it clamps nothing and shorts the rail
# to ground through two forward drops.
BAT54S_EXPECT = {"1": "GND", "2": "+3V3"}   # pin 3 is the signal, checked as "not a rail"


def load_bom():
    out = {}
    with open(os.path.join(PROJ, "bom.csv"), encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            for ref in row["References"].split():
                out[ref] = (row["Value"], row["Footprint"])
    return out


def load_pins():
    pins = collections.defaultdict(dict)
    with open(os.path.join(PROJ, "netlist.txt"), encoding="utf-8") as fh:
        for line in fh:
            p = line.split()
            if len(p) < 2:
                continue
            for ref in p[1:]:
                m = re.fullmatch(r"([A-Za-z#]{1,4}\d{1,3})\.(\w+)", ref)
                if m:
                    pins[m.group(1)][m.group(2)] = p[0]
    return pins


def load_placement():
    """ref -> (x, y, rotation, layer) straight out of the routed board."""
    place = {}
    if not os.path.exists(PCB):
        return place
    text = open(PCB, encoding="utf-8").read()
    for blk in re.finditer(r'\(footprint "[^"]*"(.*?)\n\t\)', text, re.S):
        body = blk.group(1)
        ref = re.search(r'\(property "Reference" "([^"]+)"', body)
        at = re.search(r"\(at ([-\d.]+) ([-\d.]+)(?: ([-\d.]+))?\)", body)
        lay = re.search(r'\(layer "([^"]+)"', body)
        if ref and at:
            place[ref.group(1)] = (
                float(at.group(1)), float(at.group(2)),
                float(at.group(3) or 0.0),
                (lay.group(1) if lay else "?"),
            )
    return place


def main():
    bom, pins, place = load_bom(), load_pins(), load_placement()
    failures = []

    def is_bidirectional(value):
        # SMxJnnCA / SMCJnnCA -- the CA suffix is the bidirectional variant.
        return bool(re.search(r"\d+CA\b", value))

    print("Polarity against netlist.txt  (Device:D pin 1 = cathode, pin 2 = anode)")
    print("  %-5s %-12s %-11s %-11s %s" % ("ref", "value", "pin1 (K)", "pin2 (A)", "verdict"))
    for ref in sorted([r for r in bom if re.fullmatch(r"D\d+", r)], key=lambda r: int(r[1:])):
        value, _fp = bom[ref]
        pp = pins.get(ref, {})
        if value.startswith("BAT54S"):
            bad = [p for p, want in BAT54S_EXPECT.items() if pp.get(p) != want]
            sig = pp.get("3", "?")
            ok = not bad and sig not in ("GND", "+3V3", "+5V")
            if not ok:
                failures.append("%s (BAT54S): pins %s, expected 1=GND 2=+3V3 3=signal" % (ref, pp))
            print("  %-5s %-12s %-11s %-11s %s" % (
                ref, value, pp.get("1", "?"), pp.get("2", "?"),
                "ok (signal on 3 = %s)" % sig if ok else "FAIL"))
            continue
        k, a = pp.get("1", "?"), pp.get("2", "?")
        if is_bidirectional(value):
            print("  %-5s %-12s %-11s %-11s %s" % (ref, value, k, a, "bidirectional - orientation free"))
            continue
        want = DIODE_EXPECT.get(value)
        if want is None:
            print("  %-5s %-12s %-11s %-11s %s" % (ref, value, k, a, "no rule - review by hand"))
            failures.append("%s: polarised (%s) but no expectation encoded "
                            "in DIODE_EXPECT" % (ref, value))
            continue
        ok = (k, a) == want
        if not ok:
            failures.append("%s: pin1=%s pin2=%s, expected pin1=%s pin2=%s" % (ref, k, a, want[0], want[1]))
        print("  %-5s %-12s %-11s %-11s %s" % (ref, value, k, a, "ok" if ok else "FAIL"))

    print("\nElectrolytics  (pin 1 = +, pin 2 = -)")
    for ref in sorted([r for r in bom if re.fullmatch(r"C\d+", r)], key=lambda r: int(r[1:])):
        value, fp = bom[ref]
        if "CP_Elec" not in fp:
            continue
        pp = pins.get(ref, {})
        plus, minus = pp.get("1", "?"), pp.get("2", "?")
        ok = minus == "GND" and plus != "GND"
        if not ok:
            failures.append("%s: pin1=%s pin2=%s, expected pin2 on GND" % (ref, plus, minus))
        print("  %-5s %-8s %-24s +=%-8s -=%-8s %s" % (ref, value, fp.split(":")[-1], plus, minus, "ok" if ok else "FAIL"))

    # ---- the checklist that replaces "look hard at every diode" --------------
    print("\nFab checklist -- compare against JLCPCB's placement preview")
    print("Only these parts can be fitted backwards. Everything else is symmetric.")
    print("  %-5s %-14s %-8s %-8s %-6s %s" % ("ref", "value", "x", "y", "rot", "pin 1 is"))
    rows = 0
    for ref in sorted(bom, key=lambda r: (r[0], int(re.sub(r"\D", "", r) or 0))):
        value, fp = bom[ref]
        polarised = (
            (re.fullmatch(r"D\d+", ref) and not is_bidirectional(value)) or
            "CP_Elec" in fp or
            re.fullmatch(r"U\d+", ref) or
            re.fullmatch(r"Q\d+", ref)
        )
        if not polarised:
            continue
        x, y, rot, layer = place.get(ref, (0, 0, 0, "?"))
        pin1 = pins.get(ref, {}).get("1", "?")
        if value.startswith("BAT54S"):
            note = "pin 1 = lower anode"   # SOT-23 series pair, no cathode band
        elif re.fullmatch(r"D\d+", ref):
            note = "pin 1 = cathode (band)"
        elif "CP_Elec" in fp:
            note = "pin 1 = +"
        else:
            note = "pin 1"
        print("  %-5s %-14s %-8.2f %-8.2f %-6.0f %s -> %s" % (ref, value, x, y, rot, note, pin1))
        rows += 1
    print("  (%d parts that have an orientation)" % rows)

    print("\n%d checks failed" % len(failures))
    for f in failures:
        print("  %s" % f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
