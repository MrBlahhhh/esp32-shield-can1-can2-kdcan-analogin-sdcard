#!/usr/bin/env python3
"""
Build the board end to end, from the schematic tables to a routed PCB.

  python gen/build_board.py [--freerouting path/to/freerouting.jar]
                            [--passes N] [--skip-route]

Stages, in order:

  1. generate_pcb.py    place every part, draw the outline and planes
  2. route_bucks.py     hand-shaped routing for the two buck power loops
  3. finish_routing.py  tie duplicated connector pins, before anything else
                        can take the one corridor those ties can use
  4. stitch_planes.py   a via from every GND / +3V3 pad to its plane
  5. export_dsn.py      Specctra export with In1/In2 marked as power planes
  6. freerouting        route the remaining signals on F.Cu / B.Cu
  7. import_ses.py      bring the routes back and refill the pours
  8. finish_routing.py  a second go at any tie the router undid
  9. maze_route.py      rip up and re-route whatever is still open
 10. tidy_silk.py       shrink and shuffle the reference designators

Stage 9 is what finishes the board.  An autorouter routes the easy nets
first and fences the awkward ones in, and the last few connections are
not hard because they are long -- they are hard because something is
parked in the only way out.  Stage 10 touches no copper.

Stages 1-5 and 7-10 need KiCad's bundled Python for pcbnew, so this driver
re-invokes them with that interpreter; run the driver itself with any
Python 3.  Freerouting needs Java.  Without --freerouting it looks for
freerouting.jar next to the project and in the current directory, and
skips routing if it cannot find one.
"""

from __future__ import annotations

import argparse
import glob
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.abspath(os.path.join(HERE, ".."))
FAB = os.path.join(PROJ, "fab")


def kicad_python():
    for pat in (r"C:\Program Files\KiCad\*\bin\python.exe",
                "/Applications/KiCad/KiCad.app/Contents/Frameworks/"
                "Python.framework/Versions/*/bin/python3",
                "/usr/bin/python3"):
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[-1]
    raise SystemExit("KiCad's Python not found; pcbnew is required")


def find_jar(explicit):
    if explicit:
        return explicit
    for pat in (os.path.join(PROJ, "freerouting*.jar"),
                os.path.join(os.getcwd(), "freerouting*.jar")):
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[-1]
    return None


def run(cmd, label):
    print("\n=== %s ===" % label)
    res = subprocess.run(cmd, cwd=PROJ, capture_output=True, text=True)
    for line in (res.stdout or "").splitlines():
        if "image handler" in line:
            continue
        print("   " + line)
    if res.returncode != 0:
        print((res.stderr or "").strip()[-1500:])
        raise SystemExit("%s failed (exit %d)" % (label, res.returncode))


class Lock:
    """Refuse to run two builds against the same board at once.

    Every stage reads and rewrites esp32s3-can-sd-logger.kicad_pcb. Two
    builds overlapping means one regenerates the board while the other is
    midway through routing it, and the second then imports a Specctra
    session that no longer matches -- which is exactly how one run here
    died at "Specctra session import failed" after ten minutes of work.
    """

    path = os.path.join(PROJ, ".build_board.lock")

    def __enter__(self):
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as fh:
                who = fh.read().strip()
            raise SystemExit(
                "another build is already running (%s). Wait for it, or "
                "delete %s if it died." % (who, self.path))
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("pid %d" % os.getpid())
        return self

    def __exit__(self, *exc):
        if os.path.exists(self.path):
            os.remove(self.path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--freerouting", default=None)
    ap.add_argument("--passes", type=int, default=20)
    ap.add_argument("--skip-route", action="store_true")
    args = ap.parse_args()

    py = kicad_python()
    print("kicad python: %s" % py)
    os.makedirs(FAB, exist_ok=True)

    run([py, os.path.join(HERE, "generate_pcb.py")], "place parts")
    # Stage 2 is gone with the two LM5164 islands. The dev board makes both
    # rails from USB, so there is no switching loop on this board to shape by
    # hand -- generate_pcb.BUCK_FIXED is empty and route_bucks.py has nothing
    # to match. Skipped rather than deleted: if a converter ever comes back,
    # so does the stage.
    run([py, os.path.join(HERE, "finish_routing.py"), "--early"],
        "tie duplicated connector pins")
    run([py, os.path.join(HERE, "stitch_planes.py")], "plane vias")

    if args.skip_route:
        print("\n--skip-route: leaving signals unrouted")
        return

    jar = find_jar(args.freerouting)
    if jar is None:
        print("\nno freerouting.jar found -- signals left unrouted.")
        print("get one from https://github.com/freerouting/freerouting"
              " and pass --freerouting <path>")
        return

    run([py, os.path.join(HERE, "export_dsn.py")], "specctra export")

    dsn = os.path.join(FAB, "board.dsn")
    ses = os.path.join(FAB, "board.ses")
    if os.path.exists(ses):
        os.remove(ses)
    print("\n=== autoroute signals (freerouting, %d passes) ===" % args.passes)
    # Below-normal priority: freerouting saturates every core for minutes at
    # a time, which turns the machine into a space heater and starves
    # whatever else is running. The router finishes a little later; the
    # computer stays usable.
    flags = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
    res = subprocess.run(["java", "-jar", jar, "-de", dsn, "-do", ses,
                          "-mp", str(args.passes)],
                         capture_output=True, text=True,
                         creationflags=flags)
    for line in (res.stdout or "").splitlines():
        if "Auto-routing was" in line or "optimization was" in line:
            print("   " + line.split("INFO")[-1].strip())
    if not os.path.exists(ses):
        raise SystemExit("freerouting produced no session file")

    run([py, os.path.join(HERE, "import_ses.py"), ses], "import routes")
    run([py, os.path.join(HERE, "finish_routing.py")], "finish + verify")
    run([py, os.path.join(HERE, "maze_route.py"), "--reset"],
        "rip-up and re-route the leftovers")
    run([py, os.path.join(HERE, "tidy_silk.py")], "tidy the silkscreen")


if __name__ == "__main__":
    with Lock():
        main()
