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

## The generic table was not as checked as it claimed

The header of this page says every number was opened and read. That was true
of the parts chosen deliberately. It was **not** true of `GENERIC_LCSC`, the
lookup that hands out a part number for any plain resistor or capacitor by
value and package — that table carried a comment saying it had been verified
against the live catalogue, and it had not been. Nine of its entries were
wrong, and they were wrong in the way that survives every other check:

| Wanted | Was | What it actually is | Now |
|---|---|---|---|
| 2.2 nF 50 V 0805 | `C1547` | 12 pF, and **0402** | `C107146` |
| 121 kΩ 0805 | `C25089` | 20 Ω, and 0402 | `C17438` |
| 31.6 kΩ 0805 | `C25100` | 27 Ω, and 0402 | `C2930156` |
| 4.7 kΩ 1206 | `C17909` | 120 Ω | `C17936` |
| SMAJ5.0A, SMA | `C908214` | right part, **SOD-123 body** | `C113952` |
| SMAJ36A, SMA | `C908224` | right part, SOD-123 body | `C113967` |
| green LED 0805 | `C965798` | 0603 | `C2297` |
| yellow LED 0805 | `C965800` | 0603 | `C84261` |
| 600 Ω @ 100 MHz bead | `C216149` | **60 Ω** — a factor of ten | `C81034` |

Six of the nine are package errors, and a package error is the worst kind
here: the part number is valid, the part is in stock, the value is right, and
it arrives and does not fit the land pattern. Nothing in ERC, DRC, the
netlist compare or the stock check looks at the body size of what was
ordered against the footprint it was ordered for.

`gen/verify_cart.py` now does exactly that, and it is the reason all nine
were found at once. Two ways to run it:

```
python gen/verify_cart.py --live                # before there is a cart
python gen/verify_cart.py <cart-export.csv>     # after, on what LCSC has
```

`--live` looks every number in `fab/order-combined.csv` up in the catalogue
and compares package, value, tolerance and voltage against what the
schematic asked for. Use that one — a wrong number found there is a one-line
edit, and the same number found in a cart export has already been paid for.

Two of its own bugs are worth knowing, because both reported correct parts as
wrong: `1N4148W` parsed as the value "1" against a description leading with
"150 mA", and the Würth bead's MPN `742792022` parsed as 742 megohms. Values
that are really part numbers are now skipped rather than compared.

### The substitution that made this necessary twice

`check_stock.py` used to replace any part it could not find in the index. The
index is JLCPCB's **assembly** library, which is a subset of what LCSC sells,
so a miss there means "cannot be machine-placed", not "cannot be bought" —
`C37593`, the ADS1115, is absent from it and was sitting in a real LCSC cart
at the same moment. Substituting on a miss put `C28233`, a **16 V** capacitor,
into a 100 V position on the 24 V input rail. Valid number, right value,
right package, no voltage in its catalogue line to compare against.

Unlisted parts now stay in the paste file unchanged, and any substitute that
*is* made must state a voltage at or above the design's — silence is not
evidence of a 100 V part.

## Before you order

1. Re-check stock. These were read on 2026-08-12 and stock moves.
   `python gen/check_stock.py`, then `python gen/verify_cart.py --live`.
2. Confirm the DevKitC-1 header row pitch against Espressif's DXF. It is the
   one dimension that decides whether the board is usable at all.
3. The two supercapacitors are the only line where the exact part matters
   beyond its value: ESR must be under about 1 Ω. A 5.5 V coin-type EDLC is
   30–200 Ω and cannot source the 120 mA the hold-up has to carry.
   Cylindrical cells clear that easily — the Eaton HV0810 class is 200 mΩ,
   so two in series drop 48 mV at 120 mA. (An earlier draft of this page said
   "tens of milliohms"; that overstated it. 200 mΩ is the number, and it is
   still comfortably inside the requirement.)
4. They are also the most expensive item on the board, and 1 F is far more
   than anything needs. See [COST.md](COST.md) — 0.33 F is still six times
   the hold-up the parent board shipped with.
