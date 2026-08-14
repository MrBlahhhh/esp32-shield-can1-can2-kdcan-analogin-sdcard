#!/usr/bin/env python3
"""
BOM in MacroFab's import format.

  python gen/export_macrofab.py [--other PATH] [--out-dir fab]

MacroFab matches parts on the **manufacturer part number**, not on a
distributor's code:

    "If your supplied bill of materials has the manufacturer part number
     (MPN) and associated part designator the system will try to auto match
     and select parts based on the supplied information."

That is the whole reason this script exists. `fab/bom.csv` is written for
JLCPCB and carries LCSC codes in its part-number column -- upload that and
every line comes back unmatched, because `C37593` means nothing to anyone
but LCSC. The MPNs are recovered from `fab/lcsc-moq.csv`, which accumulates LCSC
code, MOQ, multiple, MPN, manufacturer and package out of real cart exports
(see learn_moq.py). That is the only place this project sees an MPN.

Required by MacroFab: designator and MPN. Package, value and populate are
optional and all three are included, because they give the matcher something
to disambiguate with and they let a human read the sheet.

Designators must be unique across the file. They are, and it is checked
here rather than assumed -- a duplicate is silently destructive, since the
importer would have two rows claiming the same position.

Output is .xlsx. Their importer wants a spreadsheet, not a CSV. A CSV is
written alongside for reading in a terminal; do not upload that one.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.abspath(os.path.join(HERE, ".."))
OTHER_DEFAULT = r"C:\Projects\gatecontrol\hw"

COLUMNS = ["Designator", "Quantity", "Value", "Package", "Manufacturer",
           "MPN", "Populate"]


def load_ledger():
    """{LCSC code: (MPN, manufacturer, package)} from fab/lcsc-moq.csv.

    That file accumulates out of real LCSC cart exports via learn_moq.py, and
    is the only place this project sees a manufacturer part number at all --
    fab/bom.csv is written for JLCPCB and carries LCSC codes.

    Package comes from there too rather than being derived from the
    footprint. A KiCad footprint name is a library identifier, not a package:
    "SOIC-8-1EP_3.9x4.9mm_P1.27mm_EP2.95x4.9mm_Mask2.71x3.4mm" is accurate
    and useless, where the catalogue simply says "SO-8-EP".
    """
    path = os.path.join(PROJ, "fab", "lcsc-moq.csv")
    out = {}
    if os.path.exists(path):
        for r in csv.DictReader(open(path, encoding="utf-8")):
            out[r["LCSC"].strip()] = ((r.get("MPN") or "").strip(),
                                      (r.get("Manufacturer") or "").strip(),
                                      (r.get("Package") or "").strip())
    return out


def package_from_footprint(fp):
    """Last-resort package when the catalogue does not supply one. Pulls the
    imperial chip size out of a KiCad name -- 'C_0805_2012Metric' -> '0805'
    -- and otherwise strips the leading type letter, 'D_SOD-123' -> 'SOD-123'."""
    for p in fp.split("_"):
        if len(p) == 4 and p.isdigit():
            return p
    if len(fp) > 2 and fp[1] == "_":
        return fp[2:]
    return fp


def read_bom(path, ledger):
    rows, unresolved = [], []
    for r in csv.DictReader(open(path, encoding="utf-8")):
        des = [d.strip() for d in r["Designator"].strip('"').split(",")
               if d.strip()]
        if not des:
            continue
        lcsc = (r.get("JLCPCB Part #") or "").strip()
        mpn, maker, pkg = ledger.get(lcsc, ("", "", ""))
        if not mpn:
            unresolved.append((lcsc, r.get("Comment", "")))
        if not pkg:
            pkg = package_from_footprint((r.get("Footprint") or "").strip())
        rows.append({
            "Designator": ",".join(des),
            "Quantity": len(des),
            "Value": (r.get("Comment") or "").strip(),
            "Package": pkg,
            "Manufacturer": maker,
            "MPN": mpn,
            "Populate": 1,
        })
    return rows, unresolved


def check_designators(rows):
    seen, dupes = set(), []
    for r in rows:
        for d in r["Designator"].split(","):
            if d in seen:
                dupes.append(d)
            seen.add(d)
    return len(seen), dupes


def write(rows, base):
    with open(base + ".csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    try:
        import xlsxwriter
    except ImportError:
        print("  xlsxwriter not installed -- wrote CSV only. MacroFab wants "
              "the .xlsx; `pip install xlsxwriter` and re-run.")
        return False
    wb = xlsxwriter.Workbook(base + ".xlsx")
    ws = wb.add_worksheet("BOM")
    head = wb.add_format({"bold": True})
    for c, name in enumerate(COLUMNS):
        ws.write(0, c, name, head)
    for i, r in enumerate(rows, 1):
        for c, name in enumerate(COLUMNS):
            ws.write(i, c, r[name])
    ws.set_column(0, 0, 34)
    ws.set_column(2, 5, 22)
    wb.close()
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--other", default=OTHER_DEFAULT)
    ap.add_argument("--out-dir", default=os.path.join(PROJ, "fab"))
    args = ap.parse_args()

    ledger = load_ledger()

    ok = True
    for name, proj in (("logger", PROJ), ("gate", args.other)):
        src = os.path.join(proj, "fab", "bom.csv")
        if not os.path.exists(src):
            print("%-7s no fab/bom.csv, skipped" % name)
            continue
        rows, unresolved = read_bom(src, ledger)
        n, dupes = check_designators(rows)
        base = os.path.join(args.out_dir, "macrofab-bom-%s" % name)
        write(rows, base)
        print("%-7s %2d lines, %3d designators -> %s.xlsx"
              % (name, len(rows), n, os.path.basename(base)))
        if dupes:
            ok = False
            print("        DUPLICATE DESIGNATORS: %s" % ", ".join(dupes))
        if unresolved:
            ok = False
            print("        NO MPN, MacroFab cannot match these:")
            for lcsc, what in unresolved:
                print("          %-10s %s" % (lcsc, what))
    if ok:
        print("\nEvery line carries an MPN and every designator is unique.")
    print("Upload the .xlsx. The .csv beside it is for reading, not for "
          "uploading.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
