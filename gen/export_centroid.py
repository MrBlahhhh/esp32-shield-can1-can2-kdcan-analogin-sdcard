#!/usr/bin/env python3
"""
Centroid file with an origin anybody can agree on.

  "C:\\Program Files\\KiCad\\9.0\\bin\\python.exe" gen/export_centroid.py

`fab/positions.csv` is KiCad's own export, and it is what JLCPCB wants: X
measured right from the board origin, Y **negated**, because KiCad's Y axis
points down and the export flips the sign rather than moving the origin. So
every Y in it is negative -- the logger runs -91.5 to -13.0 on a board that
occupies 0..100.

JLCPCB's tooling knows that convention. Not every assembler does, and a
centroid whose origin is unstated and whose Y is negative is ambiguous in
exactly the way that gets a board built mirrored. PCBWay's own guidance asks
only that the file contain "the position and orientation of all surface
mount parts" and says nothing about which corner is zero.

This writes the same data referenced to the **bottom-left corner of the
board outline**, X right, Y up, both positive. That is the convention most
assemblers assume when nothing else is stated, and it can be checked by eye:
no coordinate should be negative, and none should exceed the board size.

Both files are kept. Send positions.csv to JLCPCB and this one to anybody
else, or send this one to everybody -- it is unambiguous either way.
"""

import csv
import os
import sys

import pcbnew

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.abspath(os.path.join(HERE, ".."))
MM = 1e6

BOARDS = [
    (PROJ, "esp32s3-can-sd-logger.kicad_pcb", "logger"),
    (r"C:\Projects\gatecontrol\hw", "gate-controller.kicad_pcb", "gate"),
]


def main():
    ok = True
    for proj, pcb, name in BOARDS:
        path = os.path.join(proj, pcb)
        if not os.path.exists(path):
            print("%-7s %s not found, skipped" % (name, pcb))
            continue
        board = pcbnew.LoadBoard(path)
        bb = board.GetBoardEdgesBoundingBox()
        x0, y1 = bb.GetX() / MM, (bb.GetY() + bb.GetHeight()) / MM
        w, h = bb.GetWidth() / MM, bb.GetHeight() / MM

        # PCBWay: "a completed pick & place file including all the
        # designators/references the same as the ones in BOM, with THT
        # designators excluded". So the BOM decides who is in this file --
        # not the board, which also holds the connectors, sockets and
        # supercapacitors. Taking every footprint gave 140 rows against the
        # logger's 128 BOM designators, and a centroid that disagrees with
        # the BOM is the one thing they ask you not to send.
        want = set()
        bom = os.path.join(proj, "fab", "bom.csv")
        for r in csv.DictReader(open(bom, encoding="utf-8")):
            want |= {d.strip() for d in r["Designator"].strip('"').split(",")
                     if d.strip()}

        rows = []
        for f in board.GetFootprints():
            if f.GetReference() not in want:
                continue
            if f.GetPadCount() == 0:
                continue
            p = f.GetPosition()
            # KiCad Y grows downward from the top edge. Distance ABOVE the
            # bottom edge is therefore (bottom - y), which is the flip.
            x = p.x / MM - x0
            y = y1 - p.y / MM
            layer = "top" if f.GetLayer() == pcbnew.F_Cu else "bottom"
            rot = f.GetOrientationDegrees() % 360
            rows.append((f.GetReference(), round(x, 4), round(y, 4),
                         layer, round(rot, 2)))
        rows.sort(key=lambda r: (r[0][:1], int("".join(c for c in r[0]
                                                      if c.isdigit()) or 0)))

        out = os.path.join(proj, "fab", "centroid-%s.csv" % name)
        with open(out, "w", newline="", encoding="utf-8") as fh:
            w_ = csv.writer(fh)
            w_.writerow(["Designator", "X (mm)", "Y (mm)", "Layer",
                         "Rotation"])
            w_.writerows(rows)

        bad = [r for r in rows
               if r[1] < 0 or r[2] < 0 or r[1] > w + 0.5 or r[2] > h + 0.5]
        # The count must equal the BOM's, not merely be a subset of it: a
        # designator in the BOM with no position is a part the machine
        # cannot place, and it is the specific failure PCBWay warns about.
        short = want - {r[0] for r in rows}
        print("%-7s %3d parts -> fab/centroid-%s.csv   board %.1f x %.1f mm"
              % (name, len(rows), name, w, h))
        print("        X %.3f..%.3f   Y %.3f..%.3f   origin = bottom-left"
              % (min(r[1] for r in rows), max(r[1] for r in rows),
                 min(r[2] for r in rows), max(r[2] for r in rows)))
        if short:
            ok = False
            print("        IN THE BOM BUT NOT PLACED: %s"
                  % ", ".join(sorted(short)))
        if bad:
            ok = False
            print("        OFF THE BOARD: %s"
                  % ", ".join("%s(%.2f,%.2f)" % (r[0], r[1], r[2])
                              for r in bad[:6]))
    if ok:
        print("\nEvery part sits inside its board outline, all coordinates "
              "positive.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
