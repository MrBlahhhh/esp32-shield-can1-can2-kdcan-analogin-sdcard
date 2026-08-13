#!/usr/bin/env python3
"""
Vibration: will the tall parts survive being bolted to a car?

  python gen/audit_mechanical.py

This board lives in a dash, and the two ride-through capacitors are 16 mm
diameter cans standing **22 mm** off the board — by a wide margin the tallest
and heaviest things on it, and held on by two solder joints each. They were
16 x 17.5 mm until the bank was enlarged to 760 uF, so this check exists
because that change made them worse, and nothing else here looks at mechanics
at all.

Three questions, in the order they matter:

1. **Where is the board's first resonance?** A PCB is a plate on its mounting
   holes. Excite it near that frequency and the deflection multiplies.
2. **Is the deflection at resonance small enough for the parts to survive
   10^7 cycles?** Steinberg's empirical limit for a component on a vibrating
   board, which is the standard screen for this.
3. **Do the tall capacitors need staking?** A tall can is a mass on a lever:
   the solder joints see a moment proportional to mass x height, and both
   went up.

Inputs are ISO 16750-3 vibration for passenger-car body mounting. This is a
screening calculation, not FEA -- it is meant to say "fine", "stake it" or
"go and model this properly", and the assumptions are printed so a wrong
answer can be traced to the input that caused it.
"""

from __future__ import annotations

import math
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.abspath(os.path.join(HERE, ".."))
PCB = os.path.join(PROJ, "esp32s3-can-sd-logger.kicad_pcb")

# --- board -----------------------------------------------------------------
H_MM = 1.6                 # FR4 thickness (the stackup)
E_PA = 22e9                # FR4 in-plane Young's modulus, typical 1080/7628 weave
NU = 0.18                  # Poisson
RHO = 1900.0               # kg/m3, FR4 + copper, populated boards run higher
POPULATED_FACTOR = 1.5     # mass added by parts, as a multiple of bare laminate

# --- ISO 16750-3 passenger-car body mounting, random vibration -------------
# Test VIII (sprung masses): 10-1000 Hz, ~0.05 g^2/Hz in the flat region.
PSD_G2_HZ = 0.05
# Q is not a constant here: it is computed as 2*sqrt(fn), Steinberg's rule
# of thumb, and printed with the result.

# --- parts to judge: (ref, can diameter mm, height mm, mass g) -------------
# Masses are volume x 1.9 g/cm3, the usual figure for an aluminium can with
# electrolyte, rounded up. Weigh one if a number here ever decides something.
def can_mass_g(d_mm, h_mm, density=1.9):
    return math.pi * (d_mm / 2) ** 2 * h_mm / 1000.0 * density


def board_extent():
    if not os.path.exists(PCB):
        return 84.0, 100.0
    t = open(PCB, encoding="utf-8").read()
    pts = []
    for blk in re.finditer(r"\(gr_(?:line|rect|arc)(.*?)\(layer \"Edge\.Cuts\"\)", t, re.S):
        pts += [(float(x), float(y)) for x, y in
                re.findall(r"\((?:start|end|mid|xy) ([-\d.]+) ([-\d.]+)\)", blk.group(1))]
    if not pts:
        return 84.0, 100.0
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return max(xs) - min(xs), max(ys) - min(ys)


def mounting_holes():
    if not os.path.exists(PCB):
        return []
    t = open(PCB, encoding="utf-8").read()
    out = []
    for blk in re.finditer(r'\(footprint "([^"]*)"(.*?)\n\t\)', t, re.S):
        if "MountingHole" not in blk.group(1):
            continue
        at = re.search(r"\(at ([-\d.]+) ([-\d.]+)", blk.group(2))
        if at:
            out.append((float(at.group(1)), float(at.group(2))))
    return out


# Footprints whose name does not encode a height, with the body height in mm
# and an estimated mass. The parent board's tall parts were all SMD
# electrolytics, whose footprint names carry their dimensions -- CP_Elec_16x22
# is a 16 mm can 22 mm tall -- so the regex below was the whole story. Nothing
# on this board is one of those, and the check quietly reported "no parts over
# 8 mm tall" while sitting under two supercapacitor cans and two 22-way
# sockets, which are the tallest things here by a wide margin and the ones a
# car will shake hardest.
EXTRA_TALL = {
    # footprint fragment          diameter/width, height mm, mass g
    "CP_Radial_D8.0mm":           (8.0,  13.0, 2.2),   # 1 F EDLC cell
    "PinSocket_1x22":             (2.5,   8.5, 1.6),   # dev-board receptacle
}


def tall_parts():
    """Anything that stands more than 8 mm off the board."""
    if not os.path.exists(PCB):
        return []
    t = open(PCB, encoding="utf-8").read()
    out = []
    for blk in re.finditer(r'\(footprint "([^"]*)"(.*?)\n\t\)', t, re.S):
        fp, body = blk.group(1), blk.group(2)
        rm = re.search(r'\(property "Reference" "([^"]+)"', body)
        if not rm:
            continue
        m = re.match(r"CP_Elec_([\d.]+)x([\d.]+)", fp)
        if m:
            d, h = float(m.group(1)), float(m.group(2))
            if h >= 8.0:
                out.append((rm.group(1), fp, d, h, can_mass_g(d, h)))
            continue
        for frag, (d, h, mass) in EXTRA_TALL.items():
            if frag in fp:
                out.append((rm.group(1), fp, d, h, mass))
                break
    return sorted(out, key=lambda r: -r[3])


def main():
    failures, warnings = [], []
    bx, by = board_extent()
    holes = mounting_holes()

    print("Board %.0f x %.0f mm, %.1f mm FR4, %d mounting holes" % (bx, by, H_MM, len(holes)))
    for x, y in holes:
        print("   hole at %6.1f, %6.1f" % (x, y))

    # --- 1. first resonance -------------------------------------------------
    # Rectangular plate, simply supported on all four edges, is the standard
    # screening idealisation. Corner-mounted-only is softer than this, so treat
    # the answer as an upper bound and say so.
    a, b = bx / 1000.0, by / 1000.0
    h = H_MM / 1000.0
    D = E_PA * h ** 3 / (12 * (1 - NU ** 2))
    mass_per_area = RHO * h * POPULATED_FACTOR
    f1 = (math.pi / 2) * math.sqrt(D / mass_per_area) * (1 / a ** 2 + 1 / b ** 2)
    print("\n1. First resonance")
    print("   plate flexural rigidity D  : %.3f N.m" % D)
    print("   areal mass (populated x%.1f): %.3f kg/m2" % (POPULATED_FACTOR, mass_per_area))
    print("   f1, simply supported edges : %.0f Hz" % f1)
    print("   NOTE this idealises the board as supported all round. With only")
    print("   %d mounting holes the real first mode is lower -- typically half"
          % len(holes))
    print("   to two thirds of this, so treat %.0f Hz as the optimistic bound."
          % f1)
    f1_real = f1 * 0.55
    print("   working figure (0.55 x)    : %.0f Hz" % f1_real)
    if f1_real < 100:
        warnings.append("first mode ~%.0f Hz sits inside the ISO 16750-3 band "
                        "(10-1000 Hz) low enough to be driven hard" % f1_real)

    # --- 2. Steinberg deflection ------------------------------------------
    # Miles' equation for the response of a single-degree-of-freedom system to
    # a flat PSD: Grms_out = sqrt(pi/2 * fn * PSD * Q). Multiplying the input
    # Grms by Q instead is wrong by a wide margin -- the response is driven by
    # the PSD *at* the resonance, not by the whole band's energy.
    grms_in = math.sqrt(PSD_G2_HZ * (1000 - 10))
    q = 2.0 * math.sqrt(f1_real)            # Steinberg's rule of thumb
    grms_out = math.sqrt(math.pi / 2 * f1_real * PSD_G2_HZ * q)
    # Single-amplitude displacement of the plate centre, metres -> mm.
    z = 9.8 * grms_out / (2 * math.pi * f1_real) ** 2 * 1000.0
    z3 = 3 * z
    print("\n2. Deflection at resonance (ISO 16750-3, %.2f g2/Hz flat 10-1000 Hz)" % PSD_G2_HZ)
    print("   input                      : %.2f Grms over the band" % grms_in)
    print("   Q = 2*sqrt(fn)             : %.0f" % q)
    print("   response (Miles)           : %.1f Grms" % grms_out)
    print("   1-sigma centre deflection  : %.3f mm" % z)
    print("   3-sigma                    : %.3f mm" % z3)

    # Steinberg: allowable Z = 0.00022*B / (c * h * sqrt(L)) for 10^7 cycles.
    # Every length in that expression is in INCHES and the answer is in inches
    # too -- mixing millimetres in gives a limit ~25x too small and condemns
    # every part on the board, which is how this was caught.
    B_in = max(bx, by) / 25.4
    h_in = H_MM / 25.4
    print("\n3. Steinberg 10^7-cycle limit per part (worked in inches, shown in mm)")
    print("   %-6s %-12s %5s %6s %8s %9s %9s  %s"
          % ("ref", "package", "h mm", "mass g", "L mm", "allowed", "3-sigma", ""))
    parts = tall_parts()
    if not parts:
        print("   (no parts over 8 mm tall found)")
    for ref, fp, d, hgt, mass in parts:
        L_in = d / 25.4
        c = 1.0                      # two-terminal body; Steinberg's table has
                                     # no entry for a tall can, so the neutral
                                     # factor is used and flagged here
        allowed_mm = 0.00022 * B_in / (c * h_in * math.sqrt(L_in)) * 25.4
        ok = z3 <= allowed_mm
        margin = allowed_mm / z3 if z3 else 999
        print("   %-6s %-12s %5.1f %6.1f %8.1f %8.3f %9.3f  %s"
              % (ref, fp.replace("CP_Elec_", ""), hgt, mass, d,
                 allowed_mm, z3,
                 "ok, %.1fx margin" % margin if ok else "EXCEEDS"))
        if not ok:
            failures.append("%s: 3-sigma deflection %.3f mm exceeds the %.3f mm "
                            "Steinberg limit" % (ref, z3, allowed_mm))
        elif margin < 2.0:
            warnings.append("%s: only %.1fx margin on the Steinberg limit -- "
                            "thin for a part this tall" % (ref, margin))

    # --- 4. the lever argument ---------------------------------------------
    print("\n4. Tall-can overturning moment (what the taller can actually cost)")
    print("   %-6s %6s %6s %10s %12s" % ("ref", "h mm", "mass g", "M at 1 G", "vs 16x17.5"))
    ref_mass = can_mass_g(16, 17.5)
    ref_m = ref_mass * 17.5 / 2
    for ref, fp, d, hgt, mass in parts:
        m_arm = mass * hgt / 2       # g.mm per G, centre of mass at half height
        print("   %-6s %6.1f %6.1f %9.1f  %11.2fx"
              % (ref, hgt, mass, m_arm, m_arm / ref_m if ref_m else 0))
    if parts and max(p[3] for p in parts) >= 20:
        warnings.append(
            "cans 20 mm or taller: bond them to the board with an adhesive "
            "bead (RTV or epoxy) at assembly. Two solder joints on a 22 mm "
            "lever is the classic automotive fatigue failure, and it is far "
            "cheaper to add glue than to respin for a lower-profile bank")

    print("\n%d failures, %d warnings" % (len(failures), len(warnings)))
    for f in failures:
        print("  FAIL  %s" % f)
    for w in warnings:
        print("  warn  %s" % w)
    print("\nAssumptions: E=%.0f GPa, rho=%.0f kg/m3 x %.1f populated, Q=%.0f, "
          "can density 1.9 g/cm3.\nThey are screening values -- if a number here "
          "decides something, measure it." % (E_PA / 1e9, RHO, POPULATED_FACTOR, q))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
