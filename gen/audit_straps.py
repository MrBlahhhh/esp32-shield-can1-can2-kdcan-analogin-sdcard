#!/usr/bin/env python3
"""
Boot-strap audit: what does every ESP32-S3 strapping pin see at reset?

  python gen/audit_straps.py     (any Python 3)

The classic way a new board fails to boot is a strapping pin biased the
wrong way by something that seemed unrelated -- an LED, a pull-up on a
shared line, a peripheral that drives its input on power-up. This walks
the schematic tables and lists everything attached to each strapping
net, then applies the S3's rules:

  GPIO0  (MCU_BOOT)  high = SPI boot (normal), low = download
  GPIO3  (IO3)       JTAG source strap; floating is invalid
  GPIO45 (IO45)      VDD_SPI voltage strap; low = 3.3 V flash (required)
  GPIO46 (IO46)      with GPIO0: ROM messages / download entry
  EN     (MCU_EN)    must rise cleanly after the 3V3 rail
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import generate_schematic as sch  # noqa: E402

# Empty, and that is the finding rather than a gap.
#
# Every ESP32-S3 strapping pin -- IO0, IO3, IO45, IO46 and EN -- is on the
# DevKitC-1, which brings its own pull-ups, its own RC on EN and its own
# buttons. This board must not touch them, and it does not: J2 pins 3, 13
# and 14 (EN, IO3, IO46) and J3 pins 14 and 15 (IO0, IO45) are all declared
# no-connect in the socket map.
#
# What this file checks now is that they STAY untouched. Listing them here
# with an expected level would report five floating nets on every run, which
# is true of this board and irrelevant -- the dev board is what drives them.
STRAPS = []

# Socket pins carrying a strapping signal. If any of these ever acquires a
# net, something on this board is fighting the dev board's own bias network,
# and the failure mode is a board that will not boot in a way that looks like
# a dead dev board rather than a wiring fault.
#
# Keyed on the socket's MPN, not its reference designator. This table said
# J2/J3 for about ten minutes, which was true until the harness was split
# into two plugs and every connector after it renumbered -- the sockets
# became J3/J4 and the audit started checking two nonexistent parts. That is
# precisely the drift gen/audit_docs.py exists to catch, reproduced inside an
# audit. Designators are an output of assign_refs(); MPNs are an input.
SOCKET_STRAP_PINS = [
    ("ESP32-S3-DevKitC-1 J1", "3",  "EN / RST"),
    ("ESP32-S3-DevKitC-1 J1", "13", "IO3"),
    ("ESP32-S3-DevKitC-1 J1", "14", "IO46"),
    ("ESP32-S3-DevKitC-1 J3", "14", "IO0 / BOOT"),
    ("ESP32-S3-DevKitC-1 J3", "15", "IO45"),
]


def attachments(net):
    out = []
    for sh in sch.SHEETS:
        for p in sh["parts"]:
            if net in p["pins"].values():
                other = sorted(set(p["pins"].values()) - {net})
                out.append((p["prefix"], p["value"], other))
    return out


def main():
    fails = []
    print("Boot-strap audit (from the schematic tables)")
    for net, want, why in STRAPS:
        parts = attachments(net)
        pull_up = any("+3V3" in o or "+5V" in o for pre, v, o in parts
                      if pre == "R")
        pull_dn = any("GND" in o for pre, v, o in parts if pre == "R")
        cap = any(pre == "C" for pre, v, o in parts)
        drivers = [(pre, v) for pre, v, o in parts
                   if pre not in ("R", "C", "SW", "J", "U", "TP")]
        state = "high" if pull_up and not pull_dn else \
                "low" if pull_dn and not pull_up else \
                "high" if net == "MCU_EN" and pull_up else "FLOATING"
        ok = state == want
        print("\n  %-8s wants %-4s -> sits %-8s %s"
              % (net, want, state, "ok" if ok else "WRONG"))
        for pre, v, o in parts:
            print("      %-3s %-16s with %s" % (pre, v, ", ".join(o)))
        if not ok:
            fails.append("%s sits %s at reset, needs %s (%s)"
                         % (net, state, want, why))
        if drivers:
            fails.append("%s has an active part attached: %s"
                         % (net, drivers))

    # The real check on a carrier board: the dev board owns its own straps,
    # and this board must leave every one of them alone. A net appearing on
    # one of these socket pins means something here is fighting the dev
    # board's bias network -- and the symptom is a board that will not boot,
    # looking for all the world like a dead dev board.
    print("\nSocket pins carrying a strapping signal (must stay open)")
    # Reference designators are handed out by assign_refs(), which only runs
    # inside generate_schematic's main(). Importing the module gets the parts
    # but not their names, so ask for them.
    sch.assign_refs()
    by_mpn = {}
    for sh in sch.SHEETS:
        for part in sh["parts"]:
            if part["mpn"]:
                by_mpn.setdefault(part["mpn"], part)
    for mpn, pin, what in SOCKET_STRAP_PINS:
        part = by_mpn.get(mpn)
        if part is None:
            fails.append("%r is not on this board -- SOCKET_STRAP_PINS is stale" % mpn)
            print("  FAIL  %s absent" % mpn)
            continue
        ref = part.get("ref", "?")
        net = part["pins"].get(pin)
        ok = net is None and pin in part.get("nc", ())
        print("  %-5s %-3s pin %-3s %-12s %s"
              % ("ok" if ok else "FAIL", ref, pin, what,
                 "open, declared no-connect" if ok
                 else "carries %s" % (net or "nothing, but not declared NC")))
        if not ok:
            fails.append("%s pin %s (%s) is not left open: %s"
                         % (ref, pin, what, net or "undeclared"))

    print("\nSummary")
    if not fails:
        print("    No strapping pin is on this board, because the MCU is not")
        print("    on this board -- and all five socket pins that carry one")
        print("    are left open, so the dev board's bias network is intact.")
    for f in dict.fromkeys(fails):
        print("  - " + f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
