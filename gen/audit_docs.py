#!/usr/bin/env python3
"""
Check that the prose still describes the board the generator builds.

  python gen/audit_docs.py

`gen/validate.py` proves the schematic matches `netlist.txt`. Nothing proved
that **the documentation** matches either, and it drifted badly: rev B inserted
parts (the two USBLC6 arrays, the USB OVP, the card-slot SRV05s), the generator
assigns designators sequentially as parts are added, and every reference-by-
designator in README.md written before that silently came to mean a different
component. `U7` went from the CAN transceiver to an ESD array; `U8` from the
ADS1115 to another one; the two bucks slid from U2/U3 to U3/U4.

None of it is caught by ERC, DRC or a netlist compare, because the netlist is
right — only the prose is wrong. It surfaces when somebody follows the docs at a
bench: probing `U7` for CAN traffic, or checking `C2` for the bulk electrolytic.

Two checks:

  1. Every designator the docs name in backticks exists, as a designator or a
     net name, in `netlist.txt`.
  2. Designators the docs identify by part type still carry that part in
     `bom.csv`. That table is curated -- prose is not machine-readable, so each
     claim is pinned here by hand once and then held.

Exit status is non-zero if anything fails, so this can gate a commit.
"""

from __future__ import annotations

import csv
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.abspath(os.path.join(HERE, ".."))

DOCS = ["README.md", os.path.join("docs", "BRINGUP.md"), os.path.join("fwsim", "README.md")]

# "the docs say this designator is this part" -- substring match against the
# BOM Value. Verified by hand against netlist.txt pin-for-pin on 2026-08-12.
CLAIMS = {
    "U1": "TLV431",           # power-fail detector
    "U2": "74AHCT1G125",      # WS2812 5 V buffer
    "U3": "SRV05",            # card-slot ESD
    "U4": "SRV05",            # card-slot ESD
    "U5": "TJA1051",          # CAN 1 transceiver
    "U6": "MCP2518FD",        # CAN 2 controller, on SPI
    "U7": "TJA1051",          # CAN 2 transceiver
    "U8": "ADS1115",          # 16-bit ADC, 0x48, channels 1-3
    "U9": "ADS1115",          # 16-bit ADC, 0x49, channel 4 + two spare
    "Q1": "DMG2301L",         # microSD supply switch
    "Q2": "2N7002",           # its level shifter
    "Q3": "2N7002",           # K-line low-side driver
    "Y1": "40MHz",            # MCP2518FD clock
    "L1": "51uH",             # CAN 1 choke
    "L2": "51uH",             # CAN 2 choke
    "D2": "SS14",             # hold-up discharge path
    "D3": "SMAJ6.0A",         # sensor rail clamp
    "D4": "green",            # the board's only LED
    "J1": "CAN1 + power",     # OBD harness, bus 1
    "J2": "Aux bus",          # K-line or CAN 2
    "J3": "DevKit J1",        # socket, left row
    "J4": "DevKit J3",        # socket, right row
    "J8": "microSD",          # card slot
    "J9": "Sensor harness",   # analog loom
    "C6": "1F",               # hold-up cell
    "C7": "1F",               # hold-up cell
    "PF1": "0.2A",            # OBD 12 V protection
    "PF2": "0.2A",            # sensor rail
    "PF3": "0.5A",            # shift-light tap
}



def load_bom():
    values = {}
    with open(os.path.join(PROJ, "bom.csv"), encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            for ref in row["References"].split():
                values[ref] = row["Value"]
    return values


def load_netlist():
    designators, nets = set(), set()
    with open(os.path.join(PROJ, "netlist.txt"), encoding="utf-8") as fh:
        for line in fh:
            parts = line.split()
            if len(parts) < 2:
                continue
            nets.add(parts[0])
            for ref in parts[1:]:
                m = re.fullmatch(r"([A-Za-z#]{1,4}\d{1,3})\.\w+", ref)
                if m:
                    designators.add(m.group(1))
    return designators, nets


def measure():
    """Every number the README quotes, recomputed from the artefacts."""
    m = {}
    pcb = open(os.path.join(PROJ, "esp32s3-can-sd-logger.kicad_pcb"), encoding="utf-8").read()
    m["tracks"] = len(re.findall(r"\n\t\(segment", pcb))
    m["vias"] = len(re.findall(r"\n\t\(via", pcb))
    m["footprints"] = pcb.count("(footprint ")
    m["nets"] = len(set(re.findall(r'\(net \d+ "([^"]*)"', pcb)))
    pts = []
    for blk in re.finditer(r"\(gr_(?:line|rect|arc)(.*?)\(layer \"Edge\.Cuts\"\)", pcb, re.S):
        pts += [(float(x), float(y)) for x, y in
                re.findall(r"\((?:start|end|mid|xy) ([-\d.]+) ([-\d.]+)\)", blk.group(1))]
    if pts:
        m["board_x"] = round(max(p[0] for p in pts) - min(p[0] for p in pts))
        m["board_y"] = round(max(p[1] for p in pts) - min(p[1] for p in pts))

    rows = list(csv.DictReader(open(os.path.join(PROJ, "bom.csv"), encoding="utf-8")))
    m["bom_lines"] = len(rows)
    m["instances"] = sum(len(r["References"].split()) for r in rows)

    fb = list(csv.DictReader(open(os.path.join(PROJ, "fab", "bom.csv"), encoding="utf-8")))
    m["fab_lines"] = len(fb)
    m["fab_with_pn"] = sum(1 for r in fb if r["JLCPCB Part #"].strip())
    blank = [r for r in fb if not r["JLCPCB Part #"].strip()]
    gen = [r for r in blank
           if re.search(r"_(0402|0603|0805|1206|1210)_", r["Footprint"])
           and re.match(r"^[\d.]", r["Comment"])]
    m["fab_generic"] = len(gen)
    m["fab_handmatch"] = len(blank) - len(gen)
    m["fab_unpicked"] = len(blank)
    d = set()
    for r in fb:
        d |= {x.strip() for x in r["Designator"].split(",") if x.strip()}
    m["fab_designators"] = len(d)
    nets_txt = set()
    for line in open(os.path.join(PROJ, "netlist.txt"), encoding="utf-8"):
        parts = line.split()
        if len(parts) >= 2:
            nets_txt.add(parts[0])
    m["nets_txt"] = len(nets_txt)

    # The README quotes how many checks the firmware suite runs, and the
    # number comes from the suite's own last run rather than from counting
    # check() calls in its source. Several of those sit inside loops, so the
    # source count is 41 against 48 actually executed -- close enough to look
    # right and wrong enough to be useless.
    res = os.path.join(PROJ, "sim", "fw", "result.txt")
    m["fw_checks"] = -1
    if os.path.exists(res):
        for line in open(res, encoding="utf-8"):
            if line.startswith("passed "):
                m["fw_checks"] = int(line.split()[1])

    cpl = {r["Designator"].strip() for r in
           csv.DictReader(open(os.path.join(PROJ, "fab", "positions.csv"), encoding="utf-8"))}
    m["cpl_designators"] = len(cpl)
    m["bom_cpl_agree"] = (d == cpl)
    return m


# Each entry: label, regex over README.md (one capture group per key), keys.
# Prose is not machine-readable, so the patterns are pinned by hand -- but the
# *values* come from the artefacts, so the numbers can never quietly rot again.
NUMBER_CLAIMS = [
    ("board size", r"\| Board \| (\d+) [x\u00d7] (\d+) mm, 4 layer", ["board_x", "board_y"]),
    ("parts row", r"\| Parts \| (\d+) component instances, (\d+) distinct BOM lines",
     ["instances", "bom_lines"]),
    ("nets row", r"\| Nets \| (\d+) \|", ["nets_txt"]),
    ("assembly row",
     r"\| Assembly \| (\d+) surface-mount designators across (\d+) fab BOM lines",
     ["fab_designators", "fab_lines"]),
    ("every fab line has a part number",
     r"Every one of the (\d+) fab BOM lines carries an LCSC part number",
     ["fab_with_pn"]),
    ("firmware check count", r"\*\*(\d+) checks\*\*, the real sketch", ["fw_checks"]),
]



def check_numbers(failures):
    m = measure()
    text = open(os.path.join(PROJ, "README.md"), encoding="utf-8").read()
    print("\nNumbers the README quotes, against the artefacts")
    for label, pat, keys in NUMBER_CLAIMS:
        hit = re.search(pat, text)
        if not hit:
            failures.append("%s: no line in README matches %r" % (label, pat))
            print("  FAIL  %-30s pattern not found -- prose reworded?" % label)
            continue
        got = [int(g) for g in hit.groups()]
        want = [m[k] for k in keys]
        # -1 means the artefact that would answer this has not been produced.
        # sim/fw/ is gitignored, so a fresh clone has no firmware-suite result
        # to compare against, and failing an audit because a suite has not
        # been run yet is noise rather than a finding.
        if -1 in want:
            print("  skip  %-30s no artefact yet -- run the suite that makes it"
                  % label)
            continue
        ok = got == want
        if not ok:
            failures.append("%s: README says %s, artefacts say %s" % (label, got, want))
        print("  %-5s %-30s README %-22s actual %s"
              % ("ok" if ok else "FAIL", label, got, want))
    if not m["bom_cpl_agree"]:
        failures.append("fab BOM and CPL designator sets differ")
    print("  %-5s %-30s %d designators each"
          % ("ok" if m["bom_cpl_agree"] else "FAIL", "fab BOM == CPL", m["fab_designators"]))
    return m


def main():
    values = load_bom()
    designators, nets = load_netlist()
    failures = []

    print("Designator claims (docs -> bom.csv)")
    for ref, want in sorted(CLAIMS.items(), key=lambda kv: (kv[0][0], int(re.sub(r"\D", "", kv[0])))):
        got = values.get(ref)
        ok = got is not None and want.lower() in got.lower()
        if not ok:
            failures.append("%s: docs say %s, bom.csv says %s" % (ref, want, got or "(absent)"))
        print("  %-5s %-6s %-22s %s" % ("ok" if ok else "FAIL", ref, want, got or "(absent)"))

    print("\nDangling references in the docs")
    dangling = 0
    for doc in DOCS:
        path = os.path.join(PROJ, doc)
        if not os.path.exists(path):
            continue
        text = open(path, encoding="utf-8").read()
        for tok in sorted(set(re.findall(r"`([A-Z]{1,3}\d{1,3})`", text))):
            if tok in designators or tok in nets:
                continue
            dangling += 1
            failures.append("%s: `%s` is neither a designator nor a net" % (doc, tok))
            print("  FAIL  %s names `%s`, which is in neither netlist column" % (doc, tok))
    if not dangling:
        print("  ok    every designator named in the docs exists")

    check_numbers(failures)

    print("\n%d checks failed" % len(failures))
    for f in failures:
        print("  %s" % f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
