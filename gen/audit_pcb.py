#!/usr/bin/env python3
"""
Physical audit of the routed board -- the checks DRC does not make.

  "C:\\Program Files\\KiCad\\9.0\\bin\\python.exe" gen/audit_pcb.py

DRC answers "is this manufacturable and connected".  It does not answer
"will it work", and the things that stop a first spin working are mostly
physical rather than electrical:

  1. current capacity   every rail's narrowest track and via count against
                        the current it actually has to carry
  2. antenna keepout    copper under the module's antenna kills the radio
  3. decoupling         a bypass cap 20 mm from the pin it bypasses is
                        decoration
  4. thermal            dissipation per part against the copper it sits on
  5. pin coverage       every symbol pin has a pad to land on
  6. card slot          a socket's mouth has to face off the board
  6. courtyard overlap  a screen for parts stacked on each other

Currents come from RAIL_CURRENT below, which is design intent, not
measurement -- the polyfuse and regulator ratings that set them are named
in the comment on each entry.  Track heating uses the IPC-2221 external
formula, which is the conservative one: I = k * dT^0.44 * A^0.725 with
k = 0.048 for outer layers, 0.024 for inner.
"""

from __future__ import annotations

import collections
import math
import os
import sys

import pcbnew

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.abspath(os.path.join(HERE, ".."))
BOARD = os.path.join(PROJ, "esp32s3-can-sd-logger.kicad_pcb")

OZ = 0.0348          # 1 oz copper, mm
DT_LIMIT = 20.0      # allowed rise over ambient, degrees C

# Worst-case continuous current per rail, amps, and where the number is from.
RAIL_CURRENT = {
    "+VBAT":   1.20,   # both bucks at full load, worst case 8 V input
    "+5V":     2.00,   # LM5164 5 V rail rating on this design
    "+3V3":    1.00,   # LM5164 3V3 rail, module peak is ~0.5 A
    "+5VS":    0.20,   # PF1 MF-MSMF020 hold current
    "LED_5V":  0.50,   # PF3 MF-MSMF050 hold current
    "VBUS":    0.50,   # PF2 MF-MSMF050 hold current
    "VBAT_F":  1.20,
    "VBAT_FB": 1.20,
    "SW_5V":   2.00,
    "SW_3V3":  1.00,
    "GND":     2.00,
}

# Pads that sit on a rail without being supplied by it.  The board file
# carries no pin names -- gen/generate_pcb.py builds footprints from pad
# numbers alone -- so the exceptions have to be named here.  Keyed on the
# part's value rather than its reference, which renumbers on every run.
NOT_SUPPLY = {
    ("LM74700", "4"): "CATHODE, the load-side sense input",
    # The USBLC6 VBUS pin is the clamp's top rail, not a supply: it draws
    # nothing in normal life and during a strike the discharge path is
    # dominated by GND. 7-8 mm to the rail caps is accepted; the data-line
    # pair the part exists for sits directly at the connector.
    ("USBLC6", "5"): "ESD clamp rail reference",
}

# Parts that dissipate enough to care about: ref-prefix match -> watts.
DISSIPATION = {
    "LM5164": 0.55,    # ~92 % efficient at 5 V / 2 A
    "IPD068N10": 0.010,   # 1.2 A^2 * 6.8 mohm
}


def ipc_width_needed(current, dt, inner):
    """Minimum track width in mm for `current` at `dt` rise (IPC-2221)."""
    k = 0.024 if inner else 0.048
    area_mil2 = (current / (k * dt ** 0.44)) ** (1.0 / 0.725)
    area_mm2 = area_mil2 * 0.0006452
    return area_mm2 / OZ


def track_current(width, dt, inner):
    k = 0.024 if inner else 0.048
    area_mil2 = (width * OZ) / 0.0006452
    return k * dt ** 0.44 * area_mil2 ** 0.725


def head(t):
    print("\n" + t)
    print("-" * len(t))


def courtyard_box(fp):
    """Board-coordinate bounding box of a footprint's courtyard.

    Summed from the courtyard graphics rather than GetCourtyard(), whose
    cached polygon is not re-transformed reliably after a rotation. On a
    saved board every position has settled, but the cache can still be the
    one built when the footprint was created at the origin.
    """
    xs, ys = [], []
    for g in fp.GraphicalItems():
        if g.GetLayer() in (pcbnew.F_CrtYd, pcbnew.B_CrtYd):
            bb = g.GetBoundingBox()
            xs += [bb.GetLeft(), bb.GetRight()]
            ys += [bb.GetTop(), bb.GetBottom()]
    if not xs:
        return fp.GetBoundingBox(False, False)
    return pcbnew.BOX2I(pcbnew.VECTOR2I(min(xs), min(ys)),
                        pcbnew.VECTOR2I(max(xs) - min(xs), max(ys) - min(ys)))


def main():
    board = pcbnew.LoadBoard(BOARD)
    fails = []

    # ------------------------------------------------ 1. current capacity ----
    head("1. Rail current capacity")
    inner_layers = {pcbnew.In1_Cu, pcbnew.In2_Cu}
    planes = {z.GetNetname() for z in board.Zones()
              if not z.GetIsRuleArea() and z.GetLayer() in inner_layers}
    by_net = collections.defaultdict(list)
    vias = collections.Counter()
    for t in board.GetTracks():
        net = t.GetNetname()
        if t.GetClass() == "PCB_VIA":
            vias[net] += 1
            continue
        by_net[net].append((pcbnew.ToMM(t.GetWidth()),
                            t.GetLayer() in inner_layers))
    print("    %-9s %6s %7s %8s %8s  %s"
          % ("net", "need", "min w", "that w", "vias", "verdict"))
    for net, amps in sorted(RAIL_CURRENT.items()):
        segs = by_net.get(net)
        if not segs:
            print("    %-9s %6.2fA  -- no tracks (plane-fed)" % (net, amps))
            continue
        if net in planes:
            # A plane net's narrowest track is a pad escape a via or two
            # long, not the path the current takes.  Judging the plane by
            # it condemned GND on its 0.3 mm stubs while 90 vias carried
            # the actual return.
            print("    %-9s %6.2fA  -- inner plane with %d vias; its tracks "
                  "are pad escapes" % (net, amps, vias[net]))
            continue
        w, inner = min(segs)
        need = ipc_width_needed(amps, DT_LIMIT, inner)
        cap = track_current(w, DT_LIMIT, inner)
        ok = w >= need
        print("    %-9s %6.2fA %6.3f %7.2fA %8d  %s"
              % (net, amps, w, cap, vias[net], "ok" if ok else "NARROW"))
        if not ok:
            fails.append("%s: narrowest track %.3f mm carries %.2f A at %d C "
                         "rise, needs %.3f mm for %.2f A"
                         % (net, w, cap, DT_LIMIT, need, amps))

    # ------------------------------------------------ 6. courtyard overlap ----
    head("6. Courtyard overlaps")
    # Every pair, not just the hand-placed ones. Uses pcbnew's own geometry,
    # which applies the footprint transform correctly -- reading the file by
    # hand does not, because footprint graphics are stored in local
    # coordinates and a rotated part comes out with its axes swapped.
    seen = []
    for fp in board.GetFootprints():
        bb = fp.GetBoundingBox(False, False)
        seen.append((fp.GetReference(), bb.GetLeft() / 1e6, bb.GetTop() / 1e6,
                     bb.GetRight() / 1e6, bb.GetBottom() / 1e6))
    clashes = []
    for i in range(len(seen)):
        for j in range(i + 1, len(seen)):
            r1, ax1, ay1, ax2, ay2 = seen[i]
            r2, bx1, by1, bx2, by2 = seen[j]
            ox, oy = min(ax2, bx2) - max(ax1, bx1), min(ay2, by2) - max(ay1, by1)
            if ox > 0.01 and oy > 0.01:
                clashes.append("%s x %s overlap %.2f x %.2f mm" % (r1, r2, ox, oy))
    # A bounding box is bigger than a courtyard, so near-neighbours show up
    # here that DRC is happy with. This is a screen, not the gate -- DRC in
    # gen/build_board.py is the gate.
    print("    %d footprints, %d bounding-box overlaps (screen only)"
          % (len(seen), len(clashes)))
    for c in clashes[:6]:
        print("      " + c)

    # ------------------------------------------------ 6. card slot faces out ----
    head("7. Card slots face a board edge")
    # A microSD card goes in contacts-first, so the socket's spring contacts
    # -- and the tails that solder to the numbered pads -- are at the BACK of
    # the slot. The mouth is therefore opposite the pads, and it has to face
    # off the board or the card cannot be inserted at all.
    #
    # This board shipped a revision with the socket at 0 degrees, mouth
    # pointing into the laminate. Nothing caught it: the netlist is identical
    # either way, DRC has no opinion about which way a connector faces, and
    # the fab layer's outline on the pad side reads like a card if you want
    # it to. The pads are the evidence, so this measures the pads.
    bx = board.GetBoardEdgesBoundingBox()
    bl, br_, bt, bb = (bx.GetLeft() / 1e6, bx.GetRight() / 1e6,
                       bx.GetTop() / 1e6, bx.GetBottom() / 1e6)
    slots = 0
    for fp in board.GetFootprints():
        # The library nickname comes back empty on a loaded board, so match on
        # the footprint name alone.
        name = str(fp.GetFPID().GetLibItemName()).lower()
        if "microsd" not in name and "sd_card" not in name:
            continue
        slots += 1
        cx = fp.GetPosition().x / 1e6
        cy = fp.GetPosition().y / 1e6
        pads = [p for p in fp.Pads() if p.GetNumber().isdigit()
                and int(p.GetNumber()) <= 8]
        if not pads:
            continue
        px = sum(p.GetPosition().x / 1e6 for p in pads) / len(pads)
        py = sum(p.GetPosition().y / 1e6 for p in pads) / len(pads)
        # Mouth points away from the contact block.
        mx, my = cx - (px - cx), cy - (py - cy)
        dx, dy = mx - cx, my - cy
        if abs(dx) >= abs(dy):
            edge, gap = ("right", br_ - cx) if dx > 0 else ("left", cx - bl)
        else:
            edge, gap = ("bottom", bb - cy) if dy > 0 else ("top", cy - bt)
        ok = gap < 12.0
        print("    %s %-5s mouth faces %-6s %5.1f mm of board that way"
              % ("ok  " if ok else "FAR ", fp.GetReference(), edge, gap))
        if not ok:
            fails.append("%s: card slot mouth faces %s with %.0f mm of board "
                         "in the way -- the card cannot be inserted"
                         % (fp.GetReference(), edge, gap))
    if not slots:
        print("    no card sockets on this board")

    # -------------------------------------------------- 2. antenna keepout ----
    head("2. ESP32 antenna keepout")
    # The module is not on this board any more -- it is on the DevKitC-1
    # plugged into J2/J3, so its antenna radiates from about 8.5 mm above
    # this laminate rather than off its edge. Copper underneath still
    # detunes it, and the right mitigation is still a keepout, but WHICH END
    # of the dev-board outline to put it under cannot be settled without
    # Espressif's DXF. Until then this check reports the absence rather than
    # guessing a rectangle: a keepout in the wrong place removes ground
    # plane where the board needs it and protects nothing.
    kas = [z for z in board.Zones() if z.GetIsRuleArea()]
    ant = max(kas, key=lambda z: z.GetBoundingBox().GetWidth()
              * max(-z.GetBoundingBox().GetTop(), 0), default=None)
    if ant is None or ant.GetBoundingBox().GetTop() >= 0:
        print("    no antenna keepout -- the module is on the dev board now,")
        print("    and which end its antenna sits over needs the DXF first")
        fails.append("no antenna keepout rule area (blocked on the DevKitC-1 DXF)")
    else:
        bb = ant.GetBoundingBox()
        print("    keepout x %.1f..%.1f  y %.1f..%.1f (overhangs the top edge)"
              % (bb.GetLeft() / 1e6, bb.GetRight() / 1e6,
                 bb.GetTop() / 1e6, bb.GetBottom() / 1e6))
        bad = []
        for t in board.GetTracks():
            for pos in (t.GetStart(), t.GetEnd()):
                if bb.Contains(pos):
                    bad.append("%s on %s" % (t.GetNetname(),
                                             board.GetLayerName(t.GetLayer())))
        if bad:
            for b in sorted(set(bad))[:6]:
                print("    intrudes: " + b)
            fails.append("copper inside the antenna keepout")
        else:
            print("    ok  no copper inside the keepout")

    # ----------------------------------------------------- 3. decoupling ----
    head("3. Decoupling cap distance to the pin it bypasses")
    planes = {z.GetNetname() for z in board.Zones()
              if not z.GetIsRuleArea() and z.GetLayer() in inner_layers}
    print("    Rails on an inner plane are fed by the plane pair, not by a")
    print("    track, so distance to the nearest cap says little about them:")
    print("    %s. Only trace-fed rails are judged here."
          % ", ".join(sorted(planes)) if planes else "    (none)")
    caps = []
    for f in board.GetFootprints():
        if not f.GetReference().startswith("C"):
            continue
        nets = {p.GetNetname() for p in f.Pads()}
        if "GND" in nets and len(nets) == 2:
            rail = (nets - {"GND"}).pop()
            caps.append((rail, f))
    worst = []
    for f in board.GetFootprints():
        if not f.GetReference().startswith("U"):
            continue
        for pad in f.Pads():
            net = pad.GetNetname()
            if net not in ("+3V3", "+5V", "+VBAT", "VBUS"):
                continue
            if net in planes:
                continue                     # plane-fed, distance is moot
            if any(k in f.GetValue() and pad.GetNumber() == n
                   for k, n in NOT_SUPPLY):
                continue                     # on the rail, not fed by it
            near = [(math.dist((pad.GetPosition().x, pad.GetPosition().y),
                               (c.GetPosition().x, c.GetPosition().y)) / 1e6, c)
                    for rail, c in caps if rail == net]
            if not near:
                continue
            d, c = min(near)
            worst.append((d, f.GetReference(), pad.GetNumber(), net,
                          c.GetReference()))
    worst.sort(reverse=True)
    for d, ref, pin, net, cref in worst[:8]:
        flag = "FAR " if d > 6.0 else "ok  "
        print("    %s %-4s pin %-3s %-6s nearest cap %-4s %5.1f mm"
              % (flag, ref, pin, net, cref, d))
        if d > 6.0:
            fails.append("%s pin %s (%s): nearest %s bypass is %.1f mm away"
                         % (ref, pin, net, net, d))

    # -------------------------------------------------------- 4. thermal ----
    head("4. Dissipation against copper area")
    for f in board.GetFootprints():
        watts = next((w for k, w in DISSIPATION.items()
                      if k in f.GetValue()),
                     None)
        if watts is None:
            continue
        bb = f.GetBoundingBox()
        area = pcbnew.ToMM(bb.GetWidth()) * pcbnew.ToMM(bb.GetHeight())
        # A rough plane-coupled rise: 1 cm^2 of 1 oz copper is ~ 60 C/W.
        tvias = sum(1 for p in f.Pads()
                    if p.GetAttribute() == 0 and p.GetSizeX() <= 6e5)
        # Each via into the plane is worth roughly 100 C/W in parallel with
        # the pad's own spreading resistance.
        rth = 60.0 * (100.0 / max(area, 1.0))
        if tvias:
            rth = 1.0 / (1.0 / rth + tvias / 100.0)
        rise = watts * rth
        print("    %-4s %-22s %.2f W over %5.1f mm2, %d thermal vias "
              "-> ~%.0f C rise"
              % (f.GetReference(), f.GetValue()[:22], watts, area, tvias, rise))
        if rise > 40.0:
            fails.append("%s rises ~%.0f C above ambient" % (f.GetReference(),
                                                             rise))

    # --------------------------------------------------- 5. pin coverage ----
    head("5. Every pad has a net, every part has pads")
    dangling = []
    for f in board.GetFootprints():
        pads = list(f.Pads())
        if not pads:
            dangling.append("%s has no pads" % f.GetReference())
            continue
        blank = [p.GetNumber() for p in pads
                 if not p.GetNetname() and p.GetNumber().strip()]
        if blank and not f.GetReference().startswith(("H", "TP")):
            dangling.append("%s pads with no net: %s"
                            % (f.GetReference(), ",".join(blank[:6])))
    if dangling:
        for d in dangling[:10]:
            print("    " + d)
    else:
        print("    ok  every pad on every part carries a net")

    # ------------------------------------------------ 6. overlapping holes ----
    head("6. Drilled holes that overlap each other")
    print("    DRC checks hole-to-hole clearance between different nets, so")
    print("    two vias on the same net can sit on the same point and pass.")
    print("    The fab drills that spot twice: the second hit lands in a hole")
    print("    and either breaks the bit or leaves a torn barrel.")
    holes = []
    for t in board.GetTracks():
        if t.GetClass() != "PCB_VIA":
            continue
        p = t.GetPosition()
        holes.append((pcbnew.ToMM(p.x), pcbnew.ToMM(p.y),
                      pcbnew.ToMM(t.GetDrill()) / 2.0,
                      "via %s" % t.GetNetname()))
    for f in board.GetFootprints():
        for pad in f.Pads():
            d = pcbnew.ToMM(pad.GetDrillSize().x)
            if d <= 0:
                continue
            p = pad.GetPosition()
            holes.append((pcbnew.ToMM(p.x), pcbnew.ToMM(p.y), d / 2.0,
                          "%s.%s" % (f.GetReference(), pad.GetNumber())))
    holes.sort()
    clashes = []
    for i, (x1, y1, r1, w1) in enumerate(holes):
        for x2, y2, r2, w2 in holes[i + 1:]:
            if x2 - x1 > r1 + r2 + 0.25:
                break
            gap = math.dist((x1, y1), (x2, y2)) - r1 - r2
            if gap < 0.25:
                clashes.append((gap, x1, y1, w1, w2))
    if clashes:
        for gap, x, y, w1, w2 in sorted(clashes)[:10]:
            how = "coincident" if gap < -0.05 else "%.2f mm apart" % max(gap, 0)
            print("    %-14s %-14s at (%.2f, %.2f)  %s" % (w1, w2, x, y, how))
        fails.append("%d pairs of drilled holes are closer than 0.25 mm, "
                     "including %d on the same point"
                     % (len(clashes), sum(1 for c in clashes if c[0] < -0.05)))
    else:
        print("    ok  %d holes, none closer than 0.25 mm" % len(holes))

    head("Summary")
    if not fails:
        print("    Nothing flagged.")
    for f in dict.fromkeys(fails):
        print("  - " + f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
