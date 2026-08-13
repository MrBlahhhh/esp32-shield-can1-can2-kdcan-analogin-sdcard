#!/usr/bin/env python3
"""
Is the schematic readable? Two questions nothing else asks.

  python gen/audit_schematic.py     (any Python 3)

`gen/validate.py` proves the schematic is electrically what the generator
meant. It says nothing about whether a person can read it, and a generated
schematic fails that in two specific, measurable ways:

  1. TEXT ON TOP OF THINGS. Reference designators, values and net labels are
     placed by rule, and rules collide. A value printed across a symbol body
     or through another part's name is not a cosmetic problem -- it is the
     one artefact somebody reads at a bench with a probe in their hand.

  2. PARTS CONNECTED ONLY BY NAME. A symbol whose pins carry net labels and
     no wire is electrically fine and visually orphaned: it sits in a field
     of other orphans and the reader has to do the netlist join in their
     head. Wires are what make a schematic a drawing rather than a table.

Both are reported per sheet with the worst offenders named, so the fix can
be aimed rather than guessed at.
"""

from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.abspath(os.path.join(HERE, ".."))

# Rough glyph box. KiCad's default schematic text is 1.27 mm; the generator
# shrinks some of it. Width per character is about 0.7 of the height for the
# stroke font, and this only has to be good enough to catch overlaps.
CHAR_W = 0.70
LINE_H = 1.30


def sheets():
    for name in sorted(os.listdir(PROJ)):
        if name.endswith(".kicad_sch"):
            yield name


def parse(path):
    """Text boxes, symbol bodies and wire endpoints from one sheet."""
    src = open(path, encoding="utf-8").read()

    # Skip the (lib_symbols ...) block. It is the library definitions, not
    # placed parts: every symbol in it carries Reference/Value properties at
    # the same coordinates, which reads as hundreds of text collisions that
    # do not exist on the drawing.
    start = src.find("(lib_symbols")
    if start >= 0:
        depth, i = 0, start
        while i < len(src):
            if src[i] == "(":
                depth += 1
            elif src[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        src = src[:start] + src[i + 1:]

    texts, symbols, wires = [], [], []

    for m in re.finditer(
            r'\(property "([^"]*)" "([^"]*)"\s*\(at ([-\d.]+) ([-\d.]+)[^)]*\)'
            r'(.*?)\n\t\t\)', src, re.S):
        key, val, x, y, tail = m.groups()
        if not val.strip() or "(hide yes)" in tail:
            continue
        size = 1.27
        sm = re.search(r"\(size ([\d.]+)", tail)
        if sm:
            size = float(sm.group(1))
        w = len(val) * size * CHAR_W
        texts.append((key, val, float(x) - w / 2, float(y) - size * 0.6,
                      float(x) + w / 2, float(y) + size * 0.6))

    for m in re.finditer(r'\(symbol\s*\(lib_id "([^"]*)"\)\s*\(at ([-\d.]+) ([-\d.]+)', src):
        symbols.append((m.group(1), float(m.group(2)), float(m.group(3))))

    for m in re.finditer(r'\(wire\s*\(pts\s*\(xy ([-\d.]+) ([-\d.]+)\)\s*\(xy ([-\d.]+) ([-\d.]+)\)', src):
        wires.append(tuple(float(g) for g in m.groups()))

    return texts, symbols, wires


def near_wire(x, y, wires, tol=2.6):
    """Is (x, y) on or near the end of any wire? Pins sit on wire ends."""
    for x1, y1, x2, y2 in wires:
        if abs(x1 - x) <= tol and abs(y1 - y) <= tol:
            return True
        if abs(x2 - x) <= tol and abs(y2 - y) <= tol:
            return True
    return False


def main():
    total_overlap = 0
    total_sym = 0
    total_loose = 0
    print("Schematic readability")
    for name in sheets():
        texts, symbols, wires = parse(os.path.join(PROJ, name))
        if not symbols:
            continue

        clashes = []
        for i in range(len(texts)):
            for j in range(i + 1, len(texts)):
                _k1, v1, ax1, ay1, ax2, ay2 = texts[i]
                _k2, v2, bx1, by1, bx2, by2 = texts[j]
                if (min(ax2, bx2) - max(ax1, bx1) > 0.05
                        and min(ay2, by2) - max(ay1, by1) > 0.05):
                    clashes.append("%s / %s" % (v1[:14], v2[:14]))

        # A symbol is "loose" if no wire end lands anywhere near its origin.
        # Crude, but it separates the wired blocks from the shelf-packed
        # parts that are held together only by net labels.
        loose = [s for s in symbols
                 if not s[0].startswith("power:")
                 and not near_wire(s[1], s[2], wires, tol=12.0)]

        total_overlap += len(clashes)
        total_sym += len([s for s in symbols if not s[0].startswith("power:")])
        total_loose += len(loose)
        print("  %-26s %3d symbols  %3d wires  %3d text clashes  %3d unwired"
              % (name, len(symbols), len(wires), len(clashes), len(loose)))
        for c in clashes[:4]:
            print("        clash: %s" % c)

    print("\n  %d text clashes, %d of %d symbols unwired (%.0f%%)"
          % (total_overlap, total_loose, total_sym,
             total_loose / max(total_sym, 1) * 100))
    return 1 if (total_overlap or total_loose) else 0


if __name__ == "__main__":
    sys.exit(main())
