# ESP32-S3 automotive I/O carrier — two CAN buses, K-line, microSD, analog

## TL;DR

A carrier board an **ESP32-S3-DevKitC-1 plugs into**, turning it into a
vehicle data logger. The dev board brings the MCU and its power; this board
brings everything that has to touch a car.

- **Two CAN buses at once.** One on the ESP32's own TWAI controller, one on
  an MCP2518FD over SPI — because the S3 has exactly one TWAI and two
  transceivers on it would only ever give you a choice, not both.
- **K-line (ISO 9141 / KWP2000) as well**, so a BMW/MINI K+DCAN cable is
  covered end to end. A solder jumper picks whether the aux harness pins
  carry K-line or the second CAN pair; the two cannot share a wire.
- **Four analog channels, read differentially.** The sensor loom's own
  ground comes back as a Kelvin wire through an attenuator matched to the
  signal channels', so chassis offset subtracts out exactly instead of
  landing on every reading.
- **The card survives ignition-off.** A supercapacitor bank holds the 5 V
  rail for about 825 ms after the supply goes away — six times what the board
  this forked from shipped with, and long enough to finish the block in
  flight and close the file.
- **The Python in `gen/` is the source.** It generates, places, routes,
  audits and circuit-simulates the whole board; the KiCad files are build
  outputs. `python gen/build_board.py` rebuilds the PCB from nothing.
- **The firmware runs before the board exists.** `gen/simulate_firmware.py`
  compiles the real sketch for the PC against a model of this board and
  feeds it CAN frames, sensor voltages and a power cut.
- **State: routed clean, simulated, firmware ported, never manufactured.**

## Why a carrier and not a shield

It was going to be a shield. It measured out at 1.4× the dev board's entire
area before a single track was drawn, so it isn't one — the DevKitC-1 drops
into two 22-way sockets on a board considerably larger than itself.

Worth being plain: at **98 × 100 mm** this is no smaller than the standalone
board it forked from. What it buys is a replaceable MCU with no module to
reflow, and a second CAN bus and a K-line the parent never had.

| | |
|---|---|
| Board | 98 × 100 mm, 4 layer |
| Parts | 153 component instances, 69 distinct BOM lines |
| Nets | 90 |
| Assembly | 128 surface-mount designators across 47 fab BOM lines |

## What's on it

| Block | Detail |
|---|---|
| **CAN 1** | TJA1051T/3 on the ESP32's TWAI. Common-mode choke, split termination (off by default), bidirectional clamps. |
| **CAN 2** | MCP2518FD (SOIC-14) on SPI with a 40 MHz crystal, its own TJA1051, choke, termination and clamps. |
| **K-line** | Discrete: 2N7002 low-side driver, clamped 22k/10k receiver, optional ISO 9141 tester pull-up on a jumper. |
| **Analog** | 4 channels, fixed 0–16 V divider (1 %, calibrated in firmware), pull-up jumper per channel for 2-wire senders, two ADS1115s, shared Kelvin ground return. |
| **microSD** | 1-bit SDMMC, switched card supply, ESD arrays on every contact. |
| **Hold-up** | 2 × 0.33 F 2.7 V supercaps in series on the 5 V rail, TLV431 power-fail detector tripping at 4.20 V. |
| **Shift light** | 74AHCT1G125 buffer so WS2812 DIN is a real 5 V, fused tap, 3-pin header. |

There is **no power conversion**. Both rails come up from the dev board's
sockets — 5 V from its USB-C behind its own Schottky, 3.3 V from its LDO.
The only 12 V the board sees is OBD-II pin 16, and that is sense-only.

## Pin map

All 24 usable DevKitC-1 GPIOs are allocated; nothing is spare. Pins are
grouped by which socket row they are on, because each row is 22 through-holes
that cut a 53 mm slot through every inner layer — a signal crossing one has
no return path underneath it.

| J1 row (left of the board) | | J3 row (right) | |
|---|---|---|---|
| IO4–IO7 | AIN1–4 | IO1, IO2 | K-line RX / TX |
| IO8 | battery sense | IO39–IO42 | CAN 2 SPI (SCK/MOSI/MISO/CS) |
| IO9, IO10 | I²C SCL / SDA | IO21 | CAN 2 interrupt |
| IO11 | SD card detect | IO47 | SD supply enable |
| IO12–IO14 | SD D0 / CMD / CLK | IO48 | WS2812 data |
| IO15 | power fail | | |
| IO16–IO18 | CAN 1 S / TX / RX | | |

Exactly one net crosses sides — `SD_PWR_EN`, a load-switch gate — and it
routes around the end of the row rather than through it.

The analog channels must live in IO1–IO10 because ADC2 is unusable with the
radio up. After the SD bus takes IO9–IO14 and IO3 is excluded as a strapping
pin, IO4–IO8 are exactly the five that remain.

## Ordering

```
python gen/export_fab.py      # gerbers, positions, JLC BOM
python gen/export_order.py    # LCSC shopping list + what to buy elsewhere
```

Every one of the 47 fab BOM lines carries an LCSC part number, and each was
checked against the live catalogue rather than assumed — see
[`docs/PARTS.md`](docs/PARTS.md) for what that turned up, including three
part numbers that were confidently wrong and two footprints that would not
have soldered. [`docs/COST.md`](docs/COST.md) prices the result and identifies
about $5.70 a board of savings that cost nothing functional.

`fab/order-elsewhere.csv` lists what LCSC's assembly flow cannot supply:
the through-hole connectors, the two supercapacitors and the dev board.

## Verification

| | |
|---|---|
| `gen/validate.py` | schematic re-extracted through KiCad and compared net for net; ERC clean |
| `gen/build_board.py` | places, routes, and runs DRC — **0 violations, 0 unconnected** |
| `gen/simulate_firmware.py` | **50 checks**, the real sketch compiled for the host against a model of this board |
| `gen/mutate_firmware.py` | deliberately breaks the firmware 17 ways to prove the suite notices — **17 of 17 caught** |
| `gen/simulate.py` | ngspice: analog channel, crank, CAN bus, system budgets, log fidelity |
| `gen/audit_*.py` | docs, polarity, straps, paste, mechanical, PCB, routes |

Study 0 of the firmware suite is the one everything else rests on: it dumps
the simulator's pin map and compares it to `netlist.txt`, so a model built
from prose instead of the design fails loudly rather than validating the
wrong board.

## Known open

- **`ROW_PITCH` is an assumption.** Espressif publish the DevKitC-1's outline
  only as a DXF and it is not in the HTML user guide. 22.86 mm (0.9 in) is
  what `gen/generate_pcb.py` uses. **If the real figure is 25.4 mm the
  sockets will not accept the board.** Settle this before ordering.
- **No antenna keepout.** The module is on the dev board now, radiating from
  about 8.5 mm above this laminate. Copper underneath still detunes it, but
  which end of the outline to clear needs the same DXF.
- **The hold-up timings are calculated, not measured.** 825 ms shed /
  254 ms unshed come from `t = C·dV/I`; the ngspice study for the supercap
  bank has not been written, and ESR and the Schottky's drop over
  temperature will both eat into them.
- **Check the dev board revision.** v1.1 puts the onboard RGB LED on IO38,
  which this board leaves free for it. Some revisions use IO48, which is the
  WS2812 output here and would clash.
- **Two bypass capacitors sit 8.7 mm from the pin they bypass**, against the
  6 mm `gen/audit_pcb.py` asks for. That is the width of the SOIC-8 itself:
  the shelf packer keeps each bypass immediately beside its IC, but fills
  left to right, so the capacitor lands on the opposite side of the package
  from the supply pin. Down from 42 mm, and fine for 500 kbit/s CAN with
  22 µF of bulk on the same rail, but it is the one placement the audit
  still objects to.
