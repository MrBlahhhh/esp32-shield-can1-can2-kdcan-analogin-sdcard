# The generator pipeline

Everything in this project is generated: the Python in this directory is the
source of truth and every `.kicad_sch` / `.kicad_pcb` / `fab/` / `plots/`
file is a build artifact. Nothing is ever hand-edited in KiCad — if it were,
the next regeneration would silently discard it. This file documents how the
pipeline works and, more importantly, the rules that were learned the hard
way, so the next boards can reuse all of it. The intended flow for a new design is at the bottom of this file.

## The one-command loops

```
# schematic loop (any Python 3)
python gen/generate_schematic.py && python gen/validate.py

# board loop (needs KiCad 9 installed, freerouting.jar in the repo root)
python -u gen/build_board.py --passes 60

# the checks DRC cannot make (after a build)
"C:\Program Files\KiCad\9.0\bin\python.exe" gen/audit_pcb.py
python gen/simulate.py            # needs ngspice, numpy, matplotlib

# outputs
python gen/export_plots.py        # schematic.pdf + three board renders
"...\bin\python.exe" gen/export_fab.py   # JLCPCB gerbers/BOM/CPL
```

## What each file owns

| file | owns |
|---|---|
| `generate_schematic.py` | the part and net tables (the design itself), sheet packing, symbol/wire/label emission, netclasses, title blocks |
| `sch_blocks.py` | hand-drawn schematic layouts for every recurring circuit — coordinates only, no electrical content |
| `generate_pcb.py` | board outline, placement zones, fixed placements (`FIXED`, `BUCK_FIXED`, `PIN_FIXED`), planes, keepouts |
| `build_board.py` | the 10-stage build driver, with a lock so two builds cannot fight |
| `route_bucks.py` | hand-shaped copper for switching loops. **Not run on this board** — the converters went with the power section, `BUCK_FIXED` is empty, and `build_board.py` skips the stage. Kept for the next design that has one |
| `finish_routing.py` | ties for duplicated connector pins (runs before AND after autorouting) |
| `stitch_planes.py` | a via from every GND/+3V3 pad to its plane, with hole-collision checks |
| `maze_route.py` | rip-up-and-retry router for whatever freerouting leaves open |
| `tidy_silk.py` | reference designator declutter; touches no copper |
| `validate.py` | compares KiCad's own netlist node-for-node against `netlist.txt`, runs ERC |
| `audit_pcb.py` | current capacity, antenna keepout, decoupling distance, thermal, drill overlaps |
| `simulate.py` | twelve ngspice/numpy/scipy studies of the circuits themselves |
| `audit_routes.py` | post-route extraction: IR drop over the real copper, bus lengths/skew, SW-node area |
| `audit_straps.py` | every ESP32 strapping pin's state at reset vs what the netlist hangs on it |
| `overstress.py` | closed-form worst-case for every external input |
| `export_plots.py` | schematic.pdf + board renders (top, back, iso) into `plots/` |
| `export_fab.py` | JLCPCB gerber zip, BOM csv, CPL csv into `fab/` (KiCad python) |

## Design rules the pipeline enforces mechanically

- Parts are identified by **(sheet, value, exact pad-net signature)**
  everywhere — placement tables, schematic blocks, buck routing. References
  renumber on every run and must never be used as keys.
- Values are **display-short** ("600R", "0.2A PTC", "AO3401A"); ratings live
  in the hidden Voltage/Tolerance/Note/MPN fields. `split_value()` separates
  voltage and tolerance automatically. Because values are matching keys, a
  value rename must be applied to `generate_schematic.py`, `sch_blocks.py`,
  `generate_pcb.py` and `audit_pcb.py` in the same commit.
- Every recurring or nontrivial circuit gets a **hand-drawn block** in
  `sch_blocks.py`. The column packer is only for true one-liners (bypass
  caps, single pull-resistors that belong to no connector) — anything that
  reads as "parts floating in space" should be blocked or attached.

## How to draw a schematic block (the hard-won rules)

A block is a dict: `sheet`, `anchor` (value, netset), `parts`
[(value, netset, dx, dy, rot)], `wires` [polylines], `junctions`, `rails`
[(net, x, y, facing)], `labels` {net: (x, y, angle)}. All coordinates are
mm relative to the anchor, on the **1.27 mm grid** — 12.07 is not a grid
point, 12.70 is; off-grid ends throw ERC "off connection grid" warnings.

**Rotation → pin positions** (sheet coordinates, y grows downward):

- Two-pin R/C/L/fuse (pins 1/2 at lib (0, ±3.81)):
  rot 0 → pin 1 top, pin 2 bottom; rot 180 → flipped;
  rot 90 → pin 1 left; rot 270 → pin 1 right.
- GSD FETs / BEC BJTs (G/B lib (−5.08, 0), D/C (2.54, 5.08), S/E (2.54, −5.08)):
  rot 0 → gate left, drain top, source bottom;
  rot 90 → gate bottom, drain left-top, source right-top;
  rot 270 → gate top, source left-bottom, drain right-bottom.
  The mapping is: rot 270 ≡ (lx,ly)→(ly,lx), rot 90 ≡ (lx,ly)→(−ly,−lx).
- TL431DBZ: A left, K right, REF top (rot 0).

**Connectivity rules** (each of these broke a build before it was learned):

- A wire must **end** at every junction on it. To tap a wire mid-span,
  split it into two wires that both end at the tap point and add a junction
  there. Three or more wire-ends (or wire-ends plus a pin) at one point
  need a junction; exactly two objects sharing an endpoint connect without
  one, and adding a junction to a bare two-object meeting point *breaks*
  the netlist.
- Crossing wires without a junction are legal and do not connect — use
  crossings freely, the way any dense schematic does.
- A wire routed through a third part's **pin point** connects to it. Route
  around pins you do not mean to touch.
- Labels attach anywhere along a wire or at a pin end. One label position
  per net per block. The emitter picks local vs global form automatically
  from whether the net leaves the sheet.
- Pins on power nets (GND, +3V3, +5V, +VBAT, +5VS, VBUS) may simply be
  left unwired: the emitter hangs the right power symbol on them. Only
  signal nets need wires or labels.
- **The anchor keeps its natural rotation** — only member parts get the
  block's `rot`. If the drawing needs a part rotated, that part cannot be
  the anchor (this is why SENSW anchors on the 2N7002, not the P-FET).
- Value text is placed above/below the body; leave a grid step of air
  between parallel runs or the text lands on the neighbouring wire.

**Label and text readability** (learned by zooming into ugly sheets):

- Every private net still needs exactly **one label for its name** — delete
  them all and KiCad silently renames the net `Net-(R5-Pad2)`, which breaks
  name-based netlist comparison and the schematic/board net linkage. But
  the label does not have to sit on the circuit: put it on a **short
  vertical spur** (one wire, one junction) rising into open sheet, angle 90.
- Labels at wire ends must extend **away** from the circuit: angle 180 on a
  left-pointing stub, 0 on a right-pointing one. A label anchored at a left
  stub-end with angle 0 prints its text right across the part it feeds.
- Part pitch on a shared run: **>= 10-12 mm** between centres, more when a
  value is long. Budget a full symbol's width between one part's text and
  the next part's body.
- Every pin that gets an auto GND/power symbol needs **~10 mm of empty
  sheet** beyond it for the symbol body plus its text.
- To actually judge a sheet, render it: `kicad-cli sch export svg`, then a
  headless browser screenshot (`msedge --headless --screenshot=... file:///
  ...svg`), and zoom in. Text collisions are invisible at full-page scale
  and glaring at reading scale — review at the scale a reader uses.

**Never put a control character in any emitted string.** A literal newline
inside a symbol property is accepted by KiCad's loader and then silently
breaks connectivity for every symbol after it in the file — the netlist
drops them with no error pointing anywhere near the cause. The emitter now
flattens all whitespace in properties; keep it that way.

## Writing simulation decks (ngspice gotchas, each cost an hour)

- `tran` picks its own tmax and will step right over ns-scale pulses.
  Always pass it explicitly: `tran <step> <stop> 0 <step>`.
- An "ideal diode" as a tanh-blended behavioral source leaks backward.
  Use a hard clamp: `B1 a b I=max(V(a)-V(b),0)/R_on`.
- An RC delay hung directly on a behavioral comparator output loads it and
  shifts the threshold. Buffer first: `Bbuf buf 0 V=V(comp)`, RC off `buf`.
- LTRA transmission lines go unstable next to coupled inductors; the ideal
  `T` element (`T1 p1 n1 p2 n2 Z0=120 TD=21n`) is solid. Calibrate drivers
  to the datasheet's loaded test condition, not open-circuit levels.
- `wrdata` on an AC vector writes complex pairs. Convert first:
  `let m = mag(v(out))` and write `m`.
- Keep component values in the decks in step with the schematic tables —
  the duplication is deliberate; a disagreement is a bug report.

## Fab outputs, plots, and the ordering package

- JLCPCB BOM header is exactly `Comment,Designator,Footprint,JLCPCB Part #`.
  Basic parts auto-match from the footprint+value; extended parts need a
  real LCSC C-number in the part's `lcsc=` field — **never guess one**.
  Parts without a number get listed in the project README for hand-matching
  in the order UI. Check BOM designator count == CPL count after export.
- `kicad-cli pcb render` needs its rotate argument quoted *inside* the
  string: `["--rotate", "'-30,0,25'"]` — without the inner quotes the
  leading `-` is eaten as a flag.
- STEP model for enclosure work: `kicad-cli pcb export step --subst-models`.
- **NTFS timestamp tunneling:** a regenerated file keeps its old
  CreationTime — even through delete-then-recreate within ~15 s — and
  Explorer's default "Date" column shows CreationTime, so fresh plots look
  stale to the user. After regenerating, stamp them:
  `Get-ChildItem plots | % { $_.CreationTime = $_.LastWriteTime }`.

## Build discipline (how a night of rebuilds went wrong)

- **One build at a time, ever.** The `.build_board.lock` exists for this;
  never put `rm -f .build_board.lock` inside a launch command "to be safe" —
  that is how two autorouters ended up writing the same board file.
- Stopping a build's shell does **not** stop freerouting: the java process
  survives and keeps routing a board that no longer exists. Kill java
  explicitly (`taskkill /F /IM java.exe`) after aborting a build.
- Always launch the driver with `python -u`. Block-buffered stdout means an
  empty log for ten minutes, which is indistinguishable from a hang and has
  caused healthy builds to be killed.
- A placement zone must have **more rect area than the sum of its parts'
  courtyards** (SOT-23 ~12 mm2, 0805 ~7 mm2, plus ~30 % packing loss). The
  shelf packer does not fail when a zone is too small -- it silently spills
  parts onto whatever is below. Watch the post-placement DRC line (the
  early tie stage prints it) instead of waiting ten minutes for routing.
- freerouting runs at below-normal priority (set in build_board.py) so a
  build does not take the machine down with it.

## Verification ladder

Every change climbs the same ladder, and each rung catches a class of
mistake the rung below cannot:

1. `generate_schematic.py` — dies loudly on unmatched block parts.
2. `validate.py` — ERC plus node-for-node netlist comparison.
3. `build_board.py` — DRC to 0 violations / 0 unconnected.
4. `audit_pcb.py` — the physics DRC does not know about.
5. `simulate.py` — does the circuit *work*, across tolerance and abuse.

## Starting a new board from this pipeline

The next boards are dedicated PCBs for **existing, running ESP32 projects**,
so the requirements are not invented — they are extracted:

0. Read the running project's firmware first. The pin map (`#define`s /
   board config), the peripheral drivers it initialises (which buses, which
   sensors, which voltages), and the modules on the current dev-board build
   ARE the part tables. List every external connection the firmware
   touches, then add the infrastructure the dev board was silently
   providing: power input + protection, USB, decoupling, boot straps,
   and (for anything in a vehicle) the ride-through/PWR_FAIL contract so
   firmware can keep its existing shutdown behaviour.

1. Copy `gen/` wholesale; delete the project-specific tables in
   `generate_schematic.py` (sheets/parts) and `generate_pcb.py` (zones,
   FIXED/BUCK_FIXED/PIN_FIXED, HOLES, board size) and `sch_blocks.py`.
2. Write the part tables first; run the schematic loop until validate
   passes with everything column-packed and ugly.
3. Draw blocks for each circuit cluster; re-run the loop after each one.
4. Define zones roughly, build the board, then iterate placement from the
   courtyard-overlap warnings and the renders.
5. Keep `simulate.py`'s decks in step with the schematic tables — the
   component values are duplicated there on purpose, so a disagreement is
   a bug report.
6. Before ordering: full ladder green, `export_fab.py`, BOM==CPL count,
   `docs/BRINGUP.md`-style staged checklist written from the studies'
   expected values, and JLC's free DFM at upload as the last gate.
