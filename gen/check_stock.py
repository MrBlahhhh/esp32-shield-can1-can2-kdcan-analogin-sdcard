#!/usr/bin/env python3
"""
Is the order actually orderable?

  python gen/check_stock.py [--file fab/order-combined.csv]

Every part number in these projects was checked against the catalogue when
it was chosen -- which proved the part EXISTS. It says nothing about whether
anyone can buy it today, and a BOM full of real part numbers that are out of
stock is not an order, it is a list of disappointments.

So this asks. For each line it pulls live stock and price, flags anything
that cannot cover the quantity needed, and for those goes looking for a
replacement of the same value in the same package that can.

Stock comes from jlcsearch (tscircuit's index of JLCPCB's parts library).
Two things follow from that and both matter:

  * It is JLCPCB's ASSEMBLY inventory, not LCSC's retail shelf. They
    overlap heavily and a part with a million pieces in one is not going to
    be missing from the other, but a part showing a few hundred here is
    worth confirming on LCSC before relying on it.
  * A part absent from this index is not necessarily unbuyable -- it may
    simply not be in the assembly library. Those are reported as UNKNOWN
    rather than as failures.

Substitutions are proposed, never applied. Swapping a part number is a
design decision: a 5% resistor is a fine pull-up and a bad feedback
divider, and only the person who wrote the schematic knows which this one
is. The tolerance is printed next to every suggestion for that reason.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.abspath(os.path.join(HERE, ".."))

API = "https://jlcsearch.tscircuit.com/api/search?q="

# curl, not urllib: the service 403s on urllib's user agent and there is no
# point pretending to be a browser from a build script.
CURL = ["curl", "-s", "-m", "25"]


def query(term, tries=3):
    """Search, with a retry. The service throttles a fast run of requests
    and an empty answer from a throttled call is indistinguishable from an
    empty answer about a real part -- which is how a first pass at this
    reported six parts missing that were all in stock."""
    for attempt in range(tries):
        try:
            # encoding="utf-8" is not optional. Without it subprocess
            # decodes with the Windows default cp1252, the reader thread
            # dies on the degree sign in every temperature range the API
            # returns, stdout comes back EMPTY, and an empty answer is
            # indistinguishable from "no such part". Every diode and IC in
            # the order was reported missing; the plain resistors, whose
            # descriptions have no degree sign, were reported fine.
            out = subprocess.run(CURL + [API + term.replace(" ", "%20")],
                                 capture_output=True, text=True, timeout=40,
                                 encoding="utf-8", errors="replace")
            got = json.loads(out.stdout or "{}").get("components")
            if got:
                return got
        except Exception:
            pass
        time.sleep(0.8 * (attempt + 1))
    return []


def find(pn, comment, footprint):
    """Locate one part number. Searching by C-number works for most of the
    index and silently returns nothing for some of it, so fall back to the
    part's own description and pick the matching number out of the result."""
    for c in query(pn):
        if "C%s" % c.get("lcsc") == pn:
            return c
    # The index does not match every C-number as a search term, so fall back
    # to the part's own description -- and then to a truncated form of it,
    # because "ADS1115IDGSR" finds nothing while "ADS1115" finds the part.
    pkg = package_of(footprint)
    terms = [("%s %s" % (comment, pkg)) if pkg else comment]
    root = re.match(r"([A-Za-z]{2,}\d{3,})", comment or "")
    if root:
        terms.append(root.group(1))
    for term in terms:
        for c in query(term):
            if "C%s" % c.get("lcsc") == pn:
                return c
    return None


def package_of(footprint):
    """'R_0805_2012Metric' -> '0805'. Empty when it is not a chip package."""
    m = re.search(r"_(\d{4})_\d+Metric", footprint or "")
    return m.group(1) if m else ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=os.path.join(PROJ, "fab",
                                                   "order-combined.csv"))
    ap.add_argument("--out", default=os.path.join(PROJ, "fab", "stock.txt"))
    args = ap.parse_args()

    rows = list(csv.DictReader(open(args.file, encoding="utf-8")))
    print("checking %d parts against live stock\n" % len(rows))

    ok, short, unknown = [], [], []
    for i, r in enumerate(rows, 1):
        pn = r["LCSC"].strip()
        need = int(r["Order qty"])
        hit = find(pn, r["Comment"], r["Footprint"])
        if hit is None:
            unknown.append((pn, need, r))
            state = "UNKNOWN"
            stock = None
        else:
            stock = hit.get("stock") or 0
            if stock >= need:
                ok.append((pn, need, stock, hit, r))
                state = "ok"
            else:
                short.append((pn, need, stock, hit, r))
                state = "SHORT"
        print("  %3d/%d  %-9s need %-6d %-8s %s"
              % (i, len(rows), pn, need,
                 "-" if stock is None else str(stock), state))
        time.sleep(0.5)

    # --- look for replacements for anything short OR unfindable -----------
    # Unknown is not innocent. C29055 was unknown here and the part the
    # combined order had REPLACED -- C28233, a basic part with a quarter of
    # a million in stock -- was sitting right there. Consolidating by
    # quantity picked the rarer of the two.
    fixes = {}
    for pn, need, stock, hit, r in short + [(p, n, 0, None, rr)
                                            for p, n, rr in unknown]:
        pkg = package_of(r["Footprint"])
        term = "%s %s" % (r["Comment"], pkg) if pkg else r["Comment"]
        # If the design asked for a tolerance, a substitute that does not
        # state it is not a substitute. 10k 0.1% sits in an analogue divider
        # and the first thing this tool ever suggested for it was a 1% part,
        # which would have quietly changed a measurement.
        tol = re.search(r"(\d+(?:\.\d+)?)\s*%", r["Comment"] or "")
        # Same for voltage, and this one is worse, because a cap that is
        # right in every other respect and short of volts fails in service
        # rather than on the bench. A candidate that does not STATE a rating
        # at or above the design's is not accepted -- silence is not
        # evidence of a 100V part.
        vm = re.search(r"(\d+)V\b", r["Comment"] or "")

        def volts_ok(c, want=vm):
            if not want:
                return True
            m = re.search(r"(\d+)V\b", (c.get("description") or ""))
            return bool(m) and int(m.group(1)) >= int(want.group(1))

        cands = [c for c in query(term)
                 if (c.get("stock") or 0) >= need
                 and "C%s" % c.get("lcsc") != pn
                 and (not pkg or str(c.get("package") or "").startswith(pkg))
                 and volts_ok(c)
                 and (not tol
                      or tol.group(0).replace(" ", "")
                      in ((c.get("description") or "") + (c.get("mfr") or ""))
                      .replace(" ", ""))]
        cands.sort(key=lambda c: (not c.get("is_basic"), -(c.get("stock") or 0)))
        if cands:
            fixes[pn] = cands[:3]

    lines = []
    lines.append("Stock check -- %s" % os.path.basename(args.file))
    lines.append("=" * 62)
    lines.append("")
    lines.append("  in stock for the quantity needed : %d" % len(ok))
    lines.append("  short                            : %d" % len(short))
    lines.append("  not in the index                 : %d" % len(unknown))
    lines.append("")
    if short:
        lines.append("SHORT -- cannot cover the order")
        lines.append("-" * 62)
        for pn, need, stock, hit, r in sorted(short, key=lambda x: x[2]):
            lines.append("  %-9s need %-6d stock %-8d  %s"
                         % (pn, need, stock, r["Comment"]))
            for c in fixes.get(pn, []):
                lines.append("        -> C%-8s %-22s stock %-9s %s $%s"
                             % (c.get("lcsc"), (c.get("mfr") or "")[:22],
                                c.get("stock"),
                                "basic" if c.get("is_basic") else "     ",
                                round(c.get("price") or 0, 5)))
            if pn not in fixes:
                lines.append("        -> nothing equivalent found in stock")
        lines.append("")
    if unknown:
        lines.append("NOT IN THE INDEX -- check on LCSC directly")
        lines.append("-" * 62)
        lines.append("  Absent from JLCPCB's assembly library is not the same")
        lines.append("  as unbuyable; most of these are stocked at LCSC. The")
        lines.append("  ADS1115 below is absent from it and was sitting in a")
        lines.append("  real LCSC cart at the same time.")
        lines.append("")
        lines.append("  These stay in the paste file UNCHANGED. Paste it, and")
        lines.append("  if LCSC rejects one of these lines, that is the answer")
        lines.append("  -- come back and pick a replacement then.")
        for pn, need, r in unknown:
            lines.append("  %-9s need %-6d %s" % (pn, need, r["Comment"]))
        lines.append("")
    lines.append("Substitutions above are SUGGESTIONS. Check tolerance and")
    lines.append("voltage before taking one: a 5%% part is a fine pull-up and")
    lines.append("a bad feedback divider, and this tool cannot tell which a")
    lines.append("given resistor is.")

    # --- a cart that can actually be bought -------------------------------
    swaps, cart = [], []
    for pn, need, stock, hit, r in ok:
        cart.append((pn, need))
    for pn, need, stock, hit, r in short:
        best = (fixes.get(pn) or [None])[0]
        if best:
            cart.append(("C%s" % best["lcsc"], need))
            swaps.append((pn, "C%s" % best["lcsc"], r["Comment"],
                          str(stock), str(best.get("stock"))))
        else:
            cart.append((pn, need))
    # "Not in the index" is NOT a reason to substitute. This index carries
    # JLCPCB's assembly library, which is a subset of what LCSC sells, so a
    # miss here means "cannot be machine-placed", not "cannot be bought" --
    # the ADS1115, C37593, is absent from it and sat in a real LCSC cart at
    # the same time.
    #
    # Swapping on a miss actively did harm. The 100nF 100V input cap, absent
    # for the same reason, was replaced with C28233, a 16V part, on a 24V
    # rail. Nothing downstream would have caught it: the number is valid,
    # the package is right, the value is right, and the catalogue line for
    # it carries no voltage to compare against.
    #
    # So keep the original and say it needs a look. An unverified line the
    # user checks beats a wrong line the tool was confident about.
    for pn, need, r in unknown:
        cart.append((pn, need))

    # The combined paste file carries seven through-hole lines that are not
    # in the assembly CSV -- relays, terminals, the optocoupler, the JST
    # headers. They belong in the same cart, so carry them across rather
    # than hand back a file that is quietly missing the connectors.
    have = {pn for pn, _q in cart}
    # ...and skip anything that was just swapped OUT, or it walks straight
    # back in from the old file and the cart contains both the part that is
    # not in stock and its replacement.
    have |= {old for old, _n, _c, _h, _s in swaps}
    # And CHECK them on the way through. These rode into the cart unverified
    # once already and one of them -- a relay -- was a part number LCSC has
    # never heard of. A line that is not in the assembly BOM is still a line
    # in the order.
    src = os.path.join(os.path.dirname(args.out), "order-combined-paste.txt")
    tht = []
    if os.path.exists(src):
        for line in open(src, encoding="utf-8"):
            bits = line.strip().split(",")
            if len(bits) != 2 or bits[0] in have:
                continue
            pn, qty = bits[0], int(bits[1])
            hit = find(pn, "", "")
            tht.append((pn, qty, None if hit is None else (hit.get("stock") or 0),
                        "" if hit is None else (hit.get("mfr") or "")))
            cart.append((pn, qty))
            have.add(pn)
            time.sleep(0.4)
    if tht:
        lines.append("")
        lines.append("Through-hole lines, also checked")
        lines.append("-" * 62)
        for pn, qty, stock, mfr in tht:
            lines.append("  %-9s x%-5d %-26s %s"
                         % (pn, qty, mfr[:26],
                            "NOT IN INDEX -- verify on LCSC" if stock is None
                            else ("stock %d" % stock if stock >= qty
                                  else "SHORT, stock %d" % stock)))

    cart_path = os.path.join(os.path.dirname(args.out),
                             "order-verified-paste.txt")
    with open(cart_path, "w", encoding="utf-8") as fh:
        for pn, qty in cart:
            fh.write(pn + "," + str(qty) + chr(10))

    # The same cart as a CSV for LCSC's BOM upload, which wants a header row
    # and is happier with a description to fall back on. Swapped lines carry
    # a note saying what they replaced, so the file explains itself when it
    # is read back in six months.
    what = {}
    for _pn, _need, _stock, _hit, r in ok:
        what[_pn] = r["Comment"]
    for _pn, _need, _stock, _hit, r in short:
        what[_pn] = r["Comment"]
    for _pn, _need, r in unknown:
        what[_pn] = r["Comment"]
    for old, new, comment, _h, _s in swaps:
        what[new] = comment
    for pn, qty, _stock, mfr in tht:
        what.setdefault(pn, mfr)
    replaced = {new: old for old, new, _c, _h, _s in swaps}

    csv_path = os.path.join(os.path.dirname(args.out), "order-verified.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["LCSC Part Number", "Quantity", "Description", "Note"])
        for pn, qty in cart:
            note = ("replaces %s, which was short or unlisted"
                    % replaced[pn]) if pn in replaced else ""
            w.writerow([pn, qty, what.get(pn, ""), note])

    if swaps:
        lines.append("")
        lines.append("SWAPPED in fab/order-verified-paste.txt")
        lines.append("-" * 62)
        for old, new, comment, had, now in swaps:
            lines.append("  %-9s -> %-9s %-14s  %s -> %s in stock"
                         % (old, new, comment, had, now))
        lines.append("")
        lines.append("  That file is the one to paste. It is the same order")
        lines.append("  with these lines replaced by something buyable.")

    text = "\n".join(lines) + "\n"
    open(args.out, "w", encoding="utf-8").write(text)
    print("\n" + text)
    print("wrote %s, %s and %s" % (args.out, cart_path, csv_path))
    return 1 if short else 0


if __name__ == "__main__":
    sys.exit(main())
