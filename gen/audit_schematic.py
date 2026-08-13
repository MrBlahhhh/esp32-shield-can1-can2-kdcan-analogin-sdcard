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


def lib_info(src):
    """Per library symbol: whether pin numbers are drawn, and where the pins
    are. Pin numbers are the text most likely to collide with something,
    because KiCad draws them wherever the pin is and nothing moves them."""
    info = {}
    for m in re.finditer(r'\n\t\t\(symbol "([^"]+)"\n(.*?)\n\t\t\)\n', src, re.S):
        name, body = m.group(1), m.group(2)
        if "/" in name or name.count("_") > 90:
            continue
        hidden = "(pin_numbers" in body and "(hide yes)" in body.split("(pin_names")[0]
        pins = []
        for q in re.finditer(
                r'\(pin \w+ \w+\s*\(at ([-\d.]+) ([-\d.]+) ([-\d.]+)\)'
                r'\s*\(length ([-\d.]+)\)(.*?)\(number "([^"]+)"', body, re.S):
            x, y, ang, ln, tail, num = q.groups()
            if "(hide yes)" in tail:
                continue
            pins.append((float(x), float(y), float(ang), float(ln), num))
        info[name] = (hidden, pins)
    return info


def rot(theta, x, y):
    import math
    r = math.radians(theta)
    c, s2 = round(math.cos(r), 6), round(math.sin(r), 6)
    return x * c - y * s2, x * s2 + y * c


def text_box(val, x, y, angle, size=1.27, justify=""):
    """Bounding box of a stroke-font string.

    Rotated if it is on its side, and anchored the way KiCad anchors it:
    left-justified text grows to the right of its position, right-justified
    to the left, and only unjustified text is centred. Getting that wrong is
    half a string's width of error, which is enough to invent collisions and
    to hide real ones.
    """
    w = len(val) * size * CHAR_W
    # Cap height, which for KiCad's stroke font is the nominal size. 1.2x
    # was padding invented to be safe and it reported adjacent labels on a
    # 2.54 mm connector pitch as colliding when the plot shows 1.3 mm of
    # daylight between them.
    h = size
    vertical = int(angle) % 180 == 90
    if vertical:
        w, h = h, w
    if "left" in justify:
        x0, x1 = (x - w / 2, x + w / 2) if vertical else (x, x + w)
        y0, y1 = (y, y + h) if vertical else (y - h / 2, y + h / 2)
    elif "right" in justify:
        x0, x1 = (x - w / 2, x + w / 2) if vertical else (x - w, x)
        y0, y1 = (y - h, y) if vertical else (y - h / 2, y + h / 2)
    else:
        x0, x1, y0, y1 = x - w / 2, x + w / 2, y - h / 2, y + h / 2
    return (x0, y0, x1, y1)


def sheets():
    for name in sorted(os.listdir(PROJ)):
        if name.endswith(".kicad_sch"):
            yield name


def parse(path):
    """Text boxes, symbol bodies and wire endpoints from one sheet."""
    src = open(path, encoding="utf-8").read()
    libs = lib_info(src)

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

    # The generator writes compact s-expressions and KiCad's own libraries
    # write tab-indented ones, so match on the tokens rather than on layout.
    for m in re.finditer(
            r'\(property "([^"]*)" "([^"]*)"\s*\(at ([-\d.]+) ([-\d.]+) ([-\d.]+)\)',
            src):
        key, val, x, y, _r = m.groups()
        tail = src[m.end():m.end() + 260]
        head = tail.split("(property")[0]
        # Two spellings. KiCad 9 writes "(hide yes)"; the generator still
        # emits the older bare "hide" token, which KiCad honours. Missing the
        # second form made every hidden power-symbol reference look like a
        # visible text item sitting on its own value -- about half the
        # collisions this audit first reported were that.
        if not val.strip() or "(hide yes)" in head or "hide)" in head:
            continue
        # A power symbol's or a flag's reference (#PWR012, #FLG003) is
        # never drawn -- the rail name in the Value field is the label.
        # Excluded by name as well as by the hide flag, so the audit does
        # not depend on which spelling of that flag the generator emits.
        if val.startswith("#"):
            continue
        if key in ("Footprint", "Datasheet", "Description", "MPN", "Note",
                   "Intersheet References", "ki_keywords", "ki_description",
                   "ki_fp_filters", "Sim.Device", "Sim.Pins"):
            continue
        size = 1.27
        sm = re.search(r"\(size ([\d.]+)", tail)
        if sm:
            size = float(sm.group(1))
        jm = re.search(r"\(justify ([a-z ]+)\)", head)
        texts.append((key, val) + text_box(val, float(x), float(y), float(_r),
                                           size, jm.group(1) if jm else ""))

    for m in re.finditer(
            r'\(symbol\s*\(lib_id "([^"]*)"\)\s*\(at ([-\d.]+) ([-\d.]+) ([-\d.]+)\)', src):
        lib_id, sx, sy, theta = (m.group(1), float(m.group(2)),
                                 float(m.group(3)), float(m.group(4)))
        symbols.append((lib_id, sx, sy))
        hidden, pins = libs.get(lib_id, (True, []))
        if hidden:
            continue
        for px, py, ang, ln, num in pins:
            # KiCad draws the number about a third of the way along the pin,
            # just off the line. Close enough to catch a label sitting on it.
            import math
            a = math.radians(ang)
            mx = px + math.cos(a) * ln * 0.45
            my = py + math.sin(a) * ln * 0.45
            ox, oy = rot(-theta, mx, -my)
            tx, ty = sx + ox, sy + oy
            texts.append(("pin", num) + text_box(num, tx, ty, 0))

    # Net labels of every flavour.
    for kind in ("label", "global_label", "hierarchical_label"):
        for m in re.finditer(
                r'\(%s "([^"]*)"(?:\s*\(shape \w+\))?\s*'
                r'\(at ([-\d.]+) ([-\d.]+) ([-\d.]+)\)' % kind, src):
            val, lx, ly = m.group(1), float(m.group(2)), float(m.group(3))
            tail = src[m.end():m.end() + 160]
            jm = re.search(r"\(justify ([a-z ]+)\)", tail)
            texts.append((kind, val) + text_box(val, lx, ly, float(m.group(4)),
                                                1.27, jm.group(1) if jm else ""))

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
                    clashes.append("%-9s %-14s / %-9s %-14s at (%.0f, %.0f)"
                                   % (_k1[:9], v1[:14], _k2[:9], v2[:14],
                                      (ax1 + ax2) / 2, (ay1 + ay2) / 2))

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
        for c in clashes[:6]:
            print("        clash: %s" % c)

    print("\n  %d text clashes, %d of %d symbols unwired (%.0f%%)"
          % (total_overlap, total_loose, total_sym,
             total_loose / max(total_sym, 1) * 100))
    return 1 if (total_overlap or total_loose) else 0


if __name__ == "__main__":
    sys.exit(main())
