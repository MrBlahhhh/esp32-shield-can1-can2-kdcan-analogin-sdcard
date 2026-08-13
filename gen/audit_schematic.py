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

# Glyph box. KiCad's default schematic text is 1.27 mm; the generator shrinks
# some of it.
#
# 0.85 is measured, not guessed. Plotting the whole schematic to PDF and
# taking the extent of all 1141 horizontal strings KiCad drew gives a median
# advance of 0.851 x size, mean 0.855. The 0.70 this started with was an
# eyeballed figure and it understated every string by a sixth -- an 10-
# character net label came out 8.9 mm wide against the 12.1 mm KiCad actually
# plots, which is enough to miss a collision three characters deep.
#
# Height stays at exactly `size`, because in KiCad the text size IS the cap
# height. The PDF reports a 2.43 mm box for 1.27 mm text, but that is the
# ascent-plus-descent of the font the viewer substituted, not the ink.
CHAR_W = 0.85
LINE_H = 1.30


def _blocks(src, start, token):
    """Yield (name, body) for each `(token "name" ...)` at the top level of
    src[start:], by matching brackets rather than counting indentation."""
    i, depth = start, 0
    while i < len(src):
        c = src[i]
        if c == "(":
            if depth == 1 and src.startswith(token, i + 1):
                m = re.match(r'\(%s "([^"]*)"' % token, src[i:])
                if m:
                    j, d = i, 0
                    while j < len(src):
                        if src[j] == "(":
                            d += 1
                        elif src[j] == ")":
                            d -= 1
                            if d == 0:
                                break
                        j += 1
                    yield m.group(1), src[i:j + 1]
                    # Skip the whole block. Depth is untouched because every
                    # bracket inside it has been stepped over, not counted.
                    i = j + 1
                    continue
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return
        i += 1


def lib_info(src):
    """Per library symbol: whether pin numbers are drawn, and where the pins
    are. Pin numbers are the text most likely to collide with something,
    because KiCad draws them wherever the pin is and nothing moves them.

    The pins live one level down, in the `NAME_1_1` unit sub-symbols, but the
    placed symbols on the sheet refer to the PARENT by its full lib_id. The
    first version of this keyed the table on the unit names, so every lookup
    from the sheet missed, every symbol came back with an empty pin list, and
    the pin-number collisions this function exists to find were never once
    tested. It reported clean because it never looked.
    """
    info = {}
    root = src.find("(lib_symbols")
    if root < 0:
        return info
    for name, body in _blocks(src, root, "symbol"):
        # `(pin_numbers (hide yes))` on the parent hides them for every unit.
        head = body.split("(symbol ", 1)[0]
        hidden = "(pin_numbers" in head and "(hide yes)" in head.split("(pin_names")[0]
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


def symbol_spans(src):
    """(start, end, rotation) for every placed symbol block.

    A property's `(at x y a)` angle is stored RELATIVE to the symbol it
    belongs to -- KiCad adds the symbol's own rotation before drawing. The
    generator writes 270 on the fields of a part rotated 90 precisely so the
    text comes out horizontal, and reading that 270 on its own says the text
    is vertical. Every field on every rotated part was modelled as a tall
    narrow box instead of a long flat one, which is how "SMAJ26CA" and "TERM"
    came to be printed through each other with the audit reporting clean.
    """
    spans = []
    for m in re.finditer(
            r'\(symbol\s*\(lib_id "[^"]*"\)\s*\(at [-\d.]+ [-\d.]+ ([-\d.]+)\)', src):
        i, depth = m.start(), 0
        while i < len(src):
            if src[i] == "(":
                depth += 1
            elif src[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        spans.append((m.start(), i, float(m.group(1))))
    return spans


def owner_rotation(spans, pos):
    for s, e, theta in spans:
        if s <= pos <= e:
            return theta
    return 0.0

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

    spans = symbol_spans(src)
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
        # Effective angle: the symbol's rotation plus the field's own.
        eff = float(_r) + owner_rotation(spans, m.start())
        texts.append((key, val) + text_box(val, float(x), float(y), eff,
                                           size, jm.group(1) if jm else ""))

    for m in re.finditer(
            r'\(symbol\s*\(lib_id "([^"]*)"\)\s*\(at ([-\d.]+) ([-\d.]+) ([-\d.]+)\)', src):
        lib_id, sx, sy, theta = (m.group(1), float(m.group(2)),
                                 float(m.group(3)), float(m.group(4)))
        rm = re.search(r'\(property "Reference" "([^"]*)"', src[m.end():m.end() + 400])
        ref = rm.group(1) if rm else lib_id
        hidden, pins = libs.get(lib_id, (True, []))
        # Pin ends in sheet coordinates. KiCad stores a pin's anchor at its
        # connection point, so this is exactly where a wire has to land.
        import math as _m
        ends = []
        for px, py, ang, ln, num in pins:
            ox, oy = rot(-theta, px, -py)
            ends.append((sx + ox, sy + oy))
        symbols.append((lib_id, sx, sy, ref, ends))
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


def key(x, y):
    return (round(x, 2), round(y, 2))


def on_segment(px, py, seg, eps=0.02):
    """Does (px, py) lie on axis-aligned segment seg? A wire ending in the
    middle of another wire is a junction, and the two are one conductor."""
    x1, y1, x2, y2 = seg
    if abs(x1 - x2) < eps:
        return abs(px - x1) < eps and min(y1, y2) - eps <= py <= max(y1, y2) + eps
    if abs(y1 - y2) < eps:
        return abs(py - y1) < eps and min(x1, x2) - eps <= px <= max(x1, x2) + eps
    return False


def wire_groups(wires):
    """Union-find over wire endpoints. Returns {point: group id}."""
    parent = {}

    def find(a):
        parent.setdefault(a, a)
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for x1, y1, x2, y2 in wires:
        union(key(x1, y1), key(x2, y2))
    # A wire that stops on another wire's body is joined to it.
    pts = list(parent)
    for px, py in pts:
        for seg in wires:
            if on_segment(px, py, seg):
                union((px, py), key(seg[0], seg[1]))
    return {p: find(p) for p in parent}


def label_only(symbols, wires, power=()):
    """Symbols the reader has to join up in their own head.

    A symbol is counted when no pin of it reaches another symbol along drawn
    wire AND at least one pin ends in a bare net label. Two exclusions, both
    deliberate:

      * A symbol with no pins -- a mounting hole, a logo, a fiducial -- is
        not connectivity and is not counted either way.

      * A pin that ends on a POWER SYMBOL is drawn, not named. A decoupling
        capacitor with +3V3 above it and GND below is exactly how a person
        draws a decoupling capacitor; calling it orphaned because neither
        neighbour is a signal part made the number meaningless -- it counted
        59 symbols when the drawing had 48 real problems, and the 11 it was
        wrong about were the ones already drawn correctly.

    What is left is the real complaint: a part sitting in open space with a
    name at each end and no line to follow.
    """
    groups = wire_groups(wires)
    owner, powered = {}, {}
    for ref, lib_id, pins in symbols:
        for px, py in pins:
            g = groups.get(key(px, py))
            if g is not None:
                owner.setdefault(g, set()).add(ref)
    for px, py in power:
        g = groups.get(key(px, py))
        if g is not None:
            powered[g] = True
    loose = []
    for ref, lib_id, pins in symbols:
        if not pins:
            continue
        wired = labelled = 0
        for px, py in pins:
            g = groups.get(key(px, py))
            if g is None:
                labelled += 1
            elif len(owner.get(g, ())) > 1:
                wired += 1
            elif powered.get(g):
                pass
            else:
                labelled += 1
        if not wired and labelled:
            loose.append((ref, lib_id))
    return loose


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

        real = [(sym[3], sym[0], sym[4]) for sym in symbols
                if not sym[0].startswith("power:")]
        rails = [p for sym in symbols if sym[0].startswith("power:")
                 for p in sym[4]]
        loose = label_only(real, wires, rails)

        total_overlap += len(clashes)
        total_sym += len([sym for sym in real if sym[2]])
        total_loose += len(loose)
        print("  %-26s %3d symbols  %3d wires  %3d text clashes  %3d unwired"
              % (name, len(symbols), len(wires), len(clashes), len(loose)))
        for c in clashes[:6]:
            print("        clash: %s" % c)
        for ref, lib_id in loose[:6]:
            print("        label-only: %-6s %s" % (ref, lib_id))

    print("\n  %d text clashes, %d of %d symbols unwired (%.0f%%)"
          % (total_overlap, total_loose, total_sym,
             total_loose / max(total_sym, 1) * 100))
    return 1 if (total_overlap or total_loose) else 0


if __name__ == "__main__":
    sys.exit(main())
