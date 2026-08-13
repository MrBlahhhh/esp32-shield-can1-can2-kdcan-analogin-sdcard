#!/usr/bin/env python3
"""
Move the schematic text that landed on top of other schematic text.

  python gen/tidy_sch_text.py        (any Python 3)

`gen/generate_schematic.py` places every reference and value by rule, and the
rules are good ones -- clear the pin stub, clear the symbol graphic, sit on
the far side of a power symbol from its own pin. What no rule can see is the
*other* part's text, because at the moment a symbol is emitted the neighbour
it will collide with has not been placed yet. That is how a schematic ends up
with "PULLUP1" printed through "AIN1_PU" four times over: two correct rules
arriving at the same square millimetre.

`gen/audit_schematic.py` already measures this exactly. This is the other half
of the loop -- it uses that same box model to move the offenders, then the
audit re-measures and says whether it worked.

Only symbol Reference and Value fields move. Those are annotation: KiCad
derives nothing from where they sit, so no move here can change the netlist.
Net labels are deliberately left alone -- a label is anchored to the wire end
it names, and sliding it away from that wire trades a text collision for a
worse problem. Where a label is one of the two colliding items, the symbol
field is the one that gives way.

Moves are kept small (5.08 mm ceiling) and on the 1.27 mm grid. A field
dragged further than that stops reading as belonging to its own part, which
is the thing being fixed. Anything with nowhere to go is left where it is and
reported rather than dropped somewhere worse -- the same bargain tidy_silk.py
makes on the board.
"""

from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from audit_schematic import lib_info, rot, text_box  # noqa: E402

# Candidate displacements, nearest first. On the 1.27 mm grid because
# everything else on the sheet is, and a field 0.4 mm off the grid looks like
# a mistake even when it is clear of its neighbours.
STEP = 1.27
MAX_MOVE = 5.08

# A stub may be pulled further than a field may be moved, and the ceilings are
# different because the constraints are different. A reference dragged more
# than 5 mm stops reading as belonging to its own part. A stub has no such
# limit -- it is just wire, and a longer one is exactly what a person draws
# when a label needs room. 10.16 mm is two grid squares of extra run, enough
# to clear the widest net name on these sheets.
PULL_MAX = 10.16

# Skipped for the same reasons audit_schematic skips them: not drawn, or not
# text a reader is looking at.
SKIP_KEYS = ("Footprint", "Datasheet", "Description", "MPN", "Note",
             "Intersheet References", "ki_keywords", "ki_description",
             "ki_fp_filters", "Sim.Device", "Sim.Pins")


def candidates():
    """Displacements to try, nearest first, ties broken vertically.

    Vertical first because a reference and a value already live above and
    below their part: moving one further out along that axis keeps the
    reading order a person expects, where sliding it sideways puts it over
    the neighbouring component.
    """
    out = []
    n = int(MAX_MOVE / STEP)
    for i in range(-n, n + 1):
        for j in range(-n, n + 1):
            dx, dy = i * STEP, j * STEP
            if dx == 0 and dy == 0:
                continue
            if abs(dx) + abs(dy) > MAX_MOVE + 1e-6:
                continue
            out.append((abs(dy) * 1.0 + abs(dx) * 1.15, dx, dy))
    out.sort()
    return [(dx, dy) for _, dx, dy in out]


CANDIDATES = candidates()


def mask_lib_symbols(src):
    """Blank the (lib_symbols ...) block, preserving every byte offset.

    The library definitions carry Reference/Value properties of their own, at
    coordinates that mean nothing on the drawing. Deleting the block would
    shift every offset after it and the rewrite would land in the wrong
    place, so it is overwritten with spaces instead.
    """
    start = src.find("(lib_symbols")
    if start < 0:
        return src
    depth, i = 0, start
    while i < len(src):
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                break
        i += 1
    blanked = re.sub(r"[^\n]", " ", src[start:i + 1])
    return src[:start] + blanked + src[i + 1:]


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

def overlap(a, b):
    return (min(a[2], b[2]) - max(a[0], b[0]) > 0.05
            and min(a[3], b[3]) - max(a[1], b[1]) > 0.05)


def parse(path):
    """Movable fields (with the span to rewrite) and fixed text, one sheet."""
    raw = open(path, encoding="utf-8").read()
    src = mask_lib_symbols(raw)
    libs = lib_info(raw)

    spans = symbol_spans(src)
    movable, fixed = [], []

    for m in re.finditer(
            r'\(property "([^"]*)" "([^"]*)"\s*\(at ([-\d.]+) ([-\d.]+) ([-\d.]+)\)',
            src):
        key, val, x, y, ang = m.groups()
        tail = src[m.end():m.end() + 260]
        head = tail.split("(property")[0]
        if not val.strip() or "(hide yes)" in head or "hide)" in head:
            continue
        if val.startswith("#") or key in SKIP_KEYS:
            continue
        size = 1.27
        sm = re.search(r"\(size ([\d.]+)", tail)
        if sm:
            size = float(sm.group(1))
        jm = re.search(r"\(justify ([a-z ]+)\)", head)
        just = jm.group(1) if jm else ""
        # The span of the "(at x y a)" token itself -- the only bytes that
        # change. Rewriting anything wider risks disturbing the effects block.
        at = re.search(r"\(at ([-\d.]+) ([-\d.]+) ([-\d.]+)\)", src[m.start():m.end()])
        movable.append({
            "key": key, "val": val, "x": float(x), "y": float(y),
            # Effective angle: the symbol's rotation plus the field's own.
            # The file keeps them separate; KiCad adds them before drawing.
            "ang": float(ang) + owner_rotation(spans, m.start()),
            "ang_file": float(ang), "size": size, "just": just,
            "span": (m.start() + at.start(), m.start() + at.end()),
        })

    # Pin numbers: drawn by KiCad wherever the pin is, and nothing can move
    # them, so they are an obstacle rather than a candidate.
    for m in re.finditer(
            r'\(symbol\s*\(lib_id "([^"]*)"\)\s*\(at ([-\d.]+) ([-\d.]+) ([-\d.]+)\)', src):
        lib_id, sx, sy, theta = (m.group(1), float(m.group(2)),
                                 float(m.group(3)), float(m.group(4)))
        hidden, pins = libs.get(lib_id, (True, []))
        if hidden:
            continue
        import math
        for px, py, ang, ln, num in pins:
            a = math.radians(ang)
            mx = px + math.cos(a) * ln * 0.45
            my = py + math.sin(a) * ln * 0.45
            ox, oy = rot(-theta, mx, -my)
            fixed.append(("pin", num, text_box(num, sx + ox, sy + oy, 0)))

    wires, wire_spans = [], []
    for m in re.finditer(
            r'\(wire\s*\(pts\s*\(xy ([-\d.]+) ([-\d.]+)\)\s*\(xy ([-\d.]+) ([-\d.]+)\)', src):
        wires.append(tuple(float(g) for g in m.groups()))
        wire_spans.append((m.start(), m.end()))

    pin_pts = set()
    for m in re.finditer(
            r'\(symbol\s*\(lib_id "([^"]*)"\)\s*\(at ([-\d.]+) ([-\d.]+) ([-\d.]+)\)', src):
        lib_id, sx, sy, theta = (m.group(1), float(m.group(2)),
                                 float(m.group(3)), float(m.group(4)))
        for px, py, ang, ln, num in libs.get(lib_id, (True, []))[1]:
            ox, oy = rot(-theta, px, -py)
            pin_pts.add((round(sx + ox, 2), round(sy + oy, 2)))

    # Bodies and junctions are obstacles too, even though the audit only
    # counts text on text. Clearing a value off its neighbour's value and
    # dropping it onto that neighbour's symbol instead is not a fix, and
    # that is exactly what happened first time round: "SMAJ26CA" came off
    # "TERM" and landed on the jumper JP1 is drawn as. The audit stays a
    # text-on-text measure -- adding graphics to it would make the number
    # incomparable with every earlier run -- but the placer has to see them.
    for m in re.finditer(
            r'\(symbol\s*\(lib_id "([^"]*)"\)\s*\(at ([-\d.]+) ([-\d.]+) ([-\d.]+)\)', src):
        lib_id, sx, sy, theta = (m.group(1), float(m.group(2)),
                                 float(m.group(3)), float(m.group(4)))
        pins = libs.get(lib_id, (True, []))[1]
        if not pins:
            continue
        xs, ys = [], []
        for px, py, ang, ln, num in pins:
            ox, oy = rot(-theta, px, -py)
            xs.append(sx + ox)
            ys.append(sy + oy)
        # Inflated by 1.0 mm because the extent is the box around the PINS and
        # a symbol's graphic routinely stands proud of it -- a capacitor's
        # plates, a jumper's blob, a diode's triangle.
        fixed.append(("body", lib_id, (min(xs) - 1.0, min(ys) - 1.0,
                                       max(xs) + 1.0, max(ys) + 1.0)))

    for m in re.finditer(r'\(junction\s*\(at ([-\d.]+) ([-\d.]+)\)', src):
        jx, jy = float(m.group(1)), float(m.group(2))
        fixed.append(("junction", "", (jx - 0.7, jy - 0.7, jx + 0.7, jy + 0.7)))

    # Labels slide, they do not jump. KiCad attaches a label to whatever wire
    # passes through its anchor, so moving one ALONG its own wire keeps it on
    # exactly the same net -- and that is the whole degree of freedom needed
    # here, because the collisions left after the fields are sorted are all a
    # label sitting on the pin number of the part at the end of its wire.
    # Moving it off the wire would silently disconnect it, so the candidate
    # set is generated from the segment and never leaves it.
    for kind in ("label", "global_label", "hierarchical_label"):
        for m in re.finditer(
                r'\(%s "([^"]*)"(?:\s*\(shape \w+\))?\s*'
                r'\(at ([-\d.]+) ([-\d.]+) ([-\d.]+)\)' % kind, src):
            val, lx, ly = m.group(1), float(m.group(2)), float(m.group(3))
            tail = src[m.end():m.end() + 160]
            jm = re.search(r"\(justify ([a-z ]+)\)", tail)
            stretch = stub_end(lx, ly, wires, pin_pts)
            # A label on the free end of a stub is the ONLY thing holding
            # that end: slide it inward and the wire end is left dangling,
            # which is three "Unconnected wire endpoint" warnings out of ERC
            # and, on a real board, a net that quietly lost a pin. At a free
            # end the label may only be pulled outward, taking the wire with
            # it. Sliding is for labels that sit mid-wire or on a corner,
            # where something else anchors the end.
            cands = [] if stretch else slide_candidates(lx, ly, wires)
            if stretch is not None:
                # Pulling the free end of a stub outward is what a person does
                # when a label lands on a junction or on the pin number of the
                # part it names. The wire keeps both of its attachments -- the
                # pin at one end, the label at the other -- so nothing about
                # the netlist changes; the label just gets some clear wire to
                # sit against.
                wi, dx, dy = stretch
                n = int(PULL_MAX / STEP)
                pulls = [(dx * STEP * k, dy * STEP * k) for k in range(1, n + 1)]
                stretch_offs = set(pulls)
                cands = cands + pulls
            else:
                stretch_offs = set()
            if not cands:
                fixed.append((kind, val,
                              text_box(val, lx, ly, float(m.group(4)), 1.27,
                                       jm.group(1) if jm else "")))
                continue
            at = re.search(r"\(at ([-\d.]+) ([-\d.]+) ([-\d.]+)\)",
                           src[m.start():m.end()])
            movable.append({
                "key": kind, "val": val, "x": lx, "y": ly,
                "ang": float(m.group(4)), "size": 1.27,
                "just": jm.group(1) if jm else "", "cands": cands,
                "span": (m.start() + at.start(), m.start() + at.end()),
                "stretch": stretch, "stretch_offs": stretch_offs,
                "x0": lx, "y0": ly,
            })

    return raw, movable, fixed, wires, wire_spans


def stub_end(lx, ly, wires, pin_pts):
    """If the label sits on the free outer end of exactly one wire, return
    (wire index, unit direction outward). Otherwise None.

    "Free" means the endpoint is not a component pin and no second wire
    starts there -- pulling on a shared corner would drag another segment's
    end away from whatever it was touching.
    """
    hit = None
    for i, (x1, y1, x2, y2) in enumerate(wires):
        for (ex, ey), (ox, oy) in (((x1, y1), (x2, y2)), ((x2, y2), (x1, y1))):
            if abs(ex - lx) > 0.02 or abs(ey - ly) > 0.02:
                continue
            if hit is not None:
                return None            # a corner, not a free end
            if (round(ex, 2), round(ey, 2)) in pin_pts:
                return None            # that end is a pin
            dx, dy = ex - ox, ey - oy
            n = max(abs(dx), abs(dy))
            if n < 0.02:
                return None
            hit = (i, round(dx / n), round(dy / n))
    return hit


def slide_candidates(lx, ly, wires, margin=1.27):
    """Offsets that keep a label on the wire it is already sitting on.

    Only along the segment, only on the grid, and never within `margin` of
    an endpoint -- a label parked exactly on a junction or a pin reads as
    belonging to the wrong thing even when the connectivity is right.
    """
    out, seen = [], set()
    for x1, y1, x2, y2 in wires:
        vertical = abs(x1 - x2) < 0.02
        horizontal = abs(y1 - y2) < 0.02
        if vertical and abs(lx - x1) < 0.02 and min(y1, y2) - 0.02 <= ly <= max(y1, y2) + 0.02:
            lo, hi = min(y1, y2) + margin, max(y1, y2) - margin
            n = int((hi - lo) / STEP) + 1
            for k in range(n + 1):
                d = round(lo + k * STEP - ly, 4)
                if abs(d) > 1e-6 and abs(d) <= MAX_MOVE and (0, d) not in seen:
                    seen.add((0, d))
                    out.append((0.0, d))
        elif horizontal and abs(ly - y1) < 0.02 and min(x1, x2) - 0.02 <= lx <= max(x1, x2) + 0.02:
            lo, hi = min(x1, x2) + margin, max(x1, x2) - margin
            n = int((hi - lo) / STEP) + 1
            for k in range(n + 1):
                d = round(lo + k * STEP - lx, 4)
                if abs(d) > 1e-6 and abs(d) <= MAX_MOVE and (d, 0) not in seen:
                    seen.add((d, 0))
                    out.append((d, 0.0))
    out.sort(key=lambda p: abs(p[0]) + abs(p[1]))
    return out


def box_of(f, dx=0.0, dy=0.0):
    return text_box(f["val"], f["x"] + dx, f["y"] + dy, f["ang"], f["size"],
                    f["just"])


def solve(movable, fixed):
    """Nudge colliding fields until they are clear. Returns (moves, stuck)."""
    boxes = [box_of(f) for f in movable]
    fixed_boxes = [b for _, _, b in fixed]

    def collisions(i, box):
        n = 0
        for b in fixed_boxes:
            if overlap(box, b):
                n += 1
        for j, b in enumerate(boxes):
            if j != i and overlap(box, b):
                n += 1
        return n

    # Worst first. A field sitting on three things should get the pick of the
    # free space; if the singles move first they fill it and the pile-up is
    # left with nowhere to go.
    order = sorted(range(len(movable)),
                   key=lambda i: -collisions(i, boxes[i]))

    moves, stuck = [], []
    for i in order:
        before = collisions(i, boxes[i])
        if not before:
            continue
        best = None
        for dx, dy in movable[i].get("cands") or CANDIDATES:
            box = box_of(movable[i], dx, dy)
            after = collisions(i, box)
            if after == 0:
                best = (dx, dy, box, 0)
                break
            # Remember the least-bad option in case nothing is fully clear.
            if best is None or after < best[3]:
                best = (dx, dy, box, after)
        if best is None or best[3] >= before:
            stuck.append(movable[i])
            continue
        dx, dy, box, after = best
        boxes[i] = box
        movable[i]["x"] += dx
        movable[i]["y"] += dy
        if (round(dx, 4), round(dy, 4)) in {(round(a, 4), round(b, 4))
                                            for a, b in movable[i].get("stretch_offs", ())}:
            px, py = movable[i].get("pulled", (0.0, 0.0))
            movable[i]["pulled"] = (px + dx, py + dy)
        moves.append((movable[i], dx, dy, before, after))
        if after:
            stuck.append(movable[i])
    return moves, stuck


def rewrite(raw, movable, wires, wire_spans):
    """Splice the new coordinates in, back to front so offsets hold."""
    edits = []
    for f in movable:
        s, e = f["span"]
        edits.append((s, e, "(at %s %s %s)"
                      % (fmt(f["x"]), fmt(f["y"]),
                         fmt(f.get("ang_file", f["ang"])))))
        pulled = f.get("pulled")
        if not pulled:
            continue
        # The label was moved by pulling the free end of its stub out with
        # it. Follow the wire's endpoint so the two stay attached.
        wi = f["stretch"][0]
        x1, y1, x2, y2 = wires[wi]
        ex, ey = f["x0"], f["y0"]               # where the end used to be
        if abs(x1 - ex) < 0.02 and abs(y1 - ey) < 0.02:
            x1, y1 = f["x"], f["y"]
        else:
            x2, y2 = f["x"], f["y"]
        ws, we = wire_spans[wi]
        edits.append((ws, we, "(wire (pts (xy %s %s) (xy %s %s)"
                      % (fmt(x1), fmt(y1), fmt(x2), fmt(y2))))
    out = raw
    for s, e, text in sorted(edits, key=lambda t: -t[0]):
        out = out[:s] + text + out[e:]
    return out


def fmt(v):
    """Match the generator's number style: no trailing zeros, no '-0'."""
    s = ("%.4f" % v).rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def main():
    print("Tidy schematic text")
    total_moved = total_stuck = 0
    for name in sorted(os.listdir(PROJ)):
        if not name.endswith(".kicad_sch"):
            continue
        path = os.path.join(PROJ, name)
        raw, movable, fixed, wires, wire_spans = parse(path)
        if not movable:
            continue
        # Repeat until it stops improving. One pass is greedy: it moves the
        # worst offender first and can leave two items each still sitting on
        # the other, because when the first one moved the second had not yet
        # got out of its way. A second pass sees the new positions and the
        # pair comes apart. Four is a ceiling, not a target -- it converges
        # in two.
        moved, stuck = [], []
        for _ in range(4):
            step_moved, stuck = solve(movable, fixed)
            moved += step_moved
            if not step_moved or not stuck:
                break
        if moved:
            open(path, "w", encoding="utf-8").write(
                rewrite(raw, movable, wires, wire_spans))
        total_moved += len(moved)
        total_stuck += len(stuck)
        print("  %-26s %3d fields  %3d moved  %3d still crowded"
              % (name, len(movable), len(moved), len(stuck)))
        for f, dx, dy, before, after in moved[:6]:
            print("        %-10s %-14s by (%+.2f, %+.2f)  %d -> %d"
                  % (f["key"], f["val"][:14], dx, dy, before, after))
        for f in stuck[:4]:
            print("        stuck: %-10s %-14s at (%.0f, %.0f)"
                  % (f["key"], f["val"][:14], f["x"], f["y"]))

    print("\n  %d fields moved, %d still crowded" % (total_moved, total_stuck))
    return 0


if __name__ == "__main__":
    sys.exit(main())
