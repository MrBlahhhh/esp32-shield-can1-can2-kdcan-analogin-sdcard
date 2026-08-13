# Part numbers, and how they were checked

Every LCSC number on this board was opened and read before it was trusted:
manufacturer part number, package, and the one or two parameters that decide
whether the part is the right one. This page records what that turned up,
because the interesting entries are the ones that were wrong.

The habit exists because this project has been bitten before. On the parent
board `C8544` was written down for an MMBT3906 and is in fact an **S9018
NPN** — the wrong polarity, in the same package, which would have left a USB
over-voltage cutoff permanently inoperative. `C7975` for an SN74AHCT1G125 is
an **LMV324 quad op-amp**. Neither is catchable by ERC, DRC, a netlist
compare or a simulation. They are only catchable by looking.

## Wrong on the first pass

| Wanted | First guess | What it actually is | Corrected to |
|---|---|---|---|
| MCP2518FD, SOIC-14 | `C2148396` | 18.7 kΩ 0805 resistor, **out of stock** | `C626759` |
| 40 MHz crystal, 3225 | `C255909` | **16 MHz** crystal | `C5186937` |
| JST PH 10-pin header | `C157966` | JST **XH 9-pin**, 2.5 mm pitch | `C158038` |

Three of the five part numbers introduced in one sitting were wrong, and all
three were plausible: right category, right-looking number, and the BOM would
have exported clean.

## Footprints that would not have soldered

Both polyfuses were specified on a **1206** land and both parts are **1812**.
Bourns' MSMF series is 4.5 × 3.2 mm; a 1206 land is 3.2 × 1.6 mm. This came
across from the parent board, where it had survived every audit — nothing in
the toolchain compares a part's package against the footprint it was assigned,
because nothing in the toolchain knows the part's package. Reading the
catalogue entry is what knows.

- `C17313` MF-MSMF050-2 — 0.5 A hold, 1812
- `C719178` MF-MSMF020/60-2 — 0.2 A hold, 60 V, 1812

## The generic-passive table is keyed on package now

`GENERIC_LCSC` in `gen/generate_schematic.py` maps a jellybean value to a
part number. It used to be keyed on value alone, which is fine until the same
value appears in two packages — and this board has 33 Ω on both an 0805 and a
1206 land, the second for the K-line driver's fault current. Keyed on value
only, the 1206 position ordered the 0805 part: right resistance, wrong
footprint, every board.

It is keyed on `(value, package)` now, and `generic_lcsc()` returns nothing
rather than guessing when a combination is missing. An unmatched line has no
part number, `export_order.py` refuses to write it, and somebody has to go
and look — which is the correct failure.

## Values chosen to match stock, not the other way round

Several values moved so that a real, stocked part exists:

| Was | Now | Why |
|---|---|---|
| 28.7k / 12.0k divider | **43k / 18k** | Same trip point to 2 mV (4.202 V vs 4.204 V); both are stocked 1 % parts and 28.7k is not |
| 33 Ω K-line series | **20 Ω** | 33 Ω does not exist in 1206 in the basic library. 20 Ω keeps the dominant level inside 0.2 × Vb with either pull-up configuration; 100 Ω would not |
| 510 Ω tester pull-up | **750 Ω** | Also absent in 1206, and 750 is the better number anyway: 190 mW while dominant against 510 Ω's 280 mW, which a 250 mW part could not carry |
| 22 Ω charge resistor | **100 Ω** | Charging 0.5 F dissipates 5.5 J whatever the resistance; 22 Ω puts 1.0 W into a 250 mW part for the first several seconds. 100 Ω peaks at 0.22 W |
| 100 nF anti-alias | **470 nF** | 100 nF against the 1.84 kΩ source gives an 865 Hz corner, above the ADS1115's 430 Hz Nyquist. Caught by `gen/simulate.py` once 0–16 V became the only range |

## Where LCSC's own data is wrong

`C719027` (Hirose DM3D-SF, the microSD socket) is listed as **"Card
Detection: No"**. It has one. DigiKey describes the same part as
"10 (8 + 2) position" with a normally-open card-detect switch, and the KiCad
footprint carries the two extra pads. The schematic wires pin 9 as
`SD_CD` and that is correct — do not "fix" it.

## The rest

Opened and confirmed, with stock at the time of checking:

| Part | LCSC | Confirmed as |
|---|---|---|
| TJA1051T/3 | `C58988` | NXP CAN transceiver, SOIC-8 |
| MCP2518FD | `C626759` | Microchip CAN FD controller, SOIC-14 |
| ADS1115IDGSR | `C37593` | TI 16-bit I²C ADC, VSSOP-10 |
| SN74AHCT1G125 | `C7484` | TI single bus buffer, SOT-23-5 |
| TLV431A | `C127592` | onsemi shunt reference, SOT-23 |
| SRV05-4 | `C13612` | Semtech 4-channel TVS array, SOT-23-6 |
| ACT45B-510 | `C76584` | TDK CAN choke, 51 µH, AEC-Q200 |
| 2N7002 | `C8545` | N-channel, 60 V — the standoff the K-line needs |
| DMG2301L | `C7472914` | P-channel, 20 V, 3 A |
| BAT54S | `C408389` | dual **series** Schottky, SOT-23 |
| SS14 | `C2480` | 40 V Schottky, SMA |
| SMAJ26CA | `C134976` | 26 V **bidirectional** TVS |
| SMAJ40CA | `C223989` | 40 V bidirectional TVS, 400 W |
| SMAJ6.0A | `C223993` | 6 V **unidirectional** TVS |
| KT-0805G | `C2297` | green LED, 0805 |
| B4B / B8B / B10B-PH | `C131334` / `C157974` / `C158038` | JST PH, 2.0 mm |
| DM3D-SF | `C719027` | Hirose microSD, push-pull |
| TX322540M4FBCE2T | `C5186937` | 40 MHz, 12 pF CL, −40/+85 °C, 30 Ω ESR |

The crystal's temperature range is the reason it is that part and not the
first 40 MHz one found: `C5380316` is also 40 MHz in the same package and is
rated **−20 to +70 °C**, which a car in a summer car park exceeds.

## Before you order

1. Re-check stock. These were read on 2026-08-12 and stock moves.
2. Confirm the DevKitC-1 header row pitch against Espressif's DXF. It is the
   one dimension that decides whether the board is usable at all.
3. The two supercapacitors are the only line where the exact part matters
   beyond its value: ESR must be under about 1 Ω. A 5.5 V coin-type EDLC is
   30–200 Ω and cannot source the 120 mA the hold-up has to carry.
