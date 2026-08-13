# Fork plan: add-on board for the ESP32-S3-DevKitC-1

This repository is a fork of `esp32-autosport`, taken at `3540e04`. The parent
is a complete board with the ESP32-S3-WROOM-1 module on it. This one keeps
everything that makes that board useful in a car — the protected 12 V front
end, both converters, the ride-through bank, CAN, microSD, the four
conditioned analog channels — and deletes the MCU, because a
**ESP32-S3-DevKitC-1** plugs in on top instead.

The full history came across, so every audit, every simulation study and the
firmware-in-the-loop harness are already here and already pass. That is the
point of forking rather than starting over: the parent's verification is the
baseline this has to keep meeting.

## Why the DevKitC-1 specifically

It carries **the same ESP32-S3-WROOM-1 module**. Every GPIO assignment in the
parent maps across unchanged, `fwsim`'s `autosport` board model stays valid,
and the firmware needs no port. A smaller board would have been cheaper and
prettier, and would have cost a pin remap, a memory-config change and a
re-verification of all 49 firmware checks.

It also has enough pins. The design needs **21 GPIOs**:

| Function | Pins | GPIOs |
|---|---|---|
| Analog channels | 4 | 1, 2, 4, 5 |
| Battery monitor | 1 | 6 |
| microSD 4-bit bus | 6 | 9, 10, 11, 12, 13, 14 |
| microSD supply + detect | 2 | 7, 8 |
| CAN TX / RX / silent | 3 | 17, 18, 21 |
| I²C | 2 | 38, 39 |
| Power-fail, sensor enable | 2 | 15, 16 |
| WS2812 data | 1 | 48 |

The DevKitC-1 breaks out GPIO0–21 and GPIO35–48 across two 22-pin headers.
Excluding GPIO35–37 (octal PSRAM on the N8R8/N16R8 variants) that is **33
usable**, against 21 needed. Every pin the design wants is present.

## Header pinout, from the v1.1 user guide

The shield's two headers must mirror these exactly. Pin 1 is at the same end
on both.

| J1 | | | J3 | |
|---|---|---|---|---|
| 1 | 3V3 | | 1 | GND |
| 2 | 3V3 | | 2 | GPIO43 / TX |
| 3 | RST (EN) | | 3 | GPIO44 / RX |
| 4 | **GPIO4** AIN3 | | 4 | **GPIO1** AIN1 |
| 5 | **GPIO5** AIN4 | | 5 | **GPIO2** AIN2 |
| 6 | **GPIO6** VBAT_SNS | | 6 | GPIO42 |
| 7 | **GPIO7** SD_PWR_EN | | 7 | GPIO41 |
| 8 | **GPIO15** PWR_FAIL | | 8 | GPIO40 |
| 9 | **GPIO16** SENS_EN | | 9 | **GPIO39** I2C_SCL |
| 10 | **GPIO17** CAN_TX | | 10 | GPIO38 — **onboard RGB LED** |
| 11 | **GPIO18** CAN_RX | | 11 | GPIO37 — PSRAM |
| 12 | **GPIO8** SD_CD | | 12 | GPIO36 — PSRAM |
| 13 | GPIO3 (strap) | | 13 | GPIO35 — PSRAM |
| 14 | GPIO46 (strap) | | 14 | GPIO0 BOOT |
| 15 | **GPIO9** SD_D3 | | 15 | GPIO45 (strap) |
| 16 | **GPIO10** SD_D2 | | 16 | **GPIO48** WS2812 |
| 17 | **GPIO11** SD_D1 | | 17 | GPIO47 |
| 18 | **GPIO12** SD_D0 | | 18 | **GPIO21** CAN_S |
| 19 | **GPIO13** SD_CMD | | 19 | GPIO20 — USB D+ |
| 20 | **GPIO14** SD_CLK | | 20 | GPIO19 — USB D− |
| 21 | 5V | | 21 | GND |
| 22 | GND | | 22 | GND |

Board dimensions and row spacing are **not** in the HTML user guide — Espressif
publish them only as a DXF. Get that before drawing the outline; do not
estimate the row pitch from a photograph.

## The one clash, and the fix

**GPIO38 drives the DevKitC-1 v1.1's onboard addressable RGB LED**, and the
parent design uses GPIO38 for `I2C_SDA`. Left alone, every I²C transaction
would flicker the dev board's LED — cosmetically awful, and it hangs the
LED's input capacitance on the bus.

**Move `I2C_SDA` to GPIO47** (J3 pin 17), which is free, not a strapping pin
and not used by anything else here. `I2C_SCL` stays on GPIO39. That leaves
GPIO38 to the dev board.

This is not free: it changes `ADS_SDA_PIN` in the firmware and the
`autosport` board model in `fwsim/shim/sim.cpp`. Study 14 exercises the
ADS1115 path specifically, so forgetting one of them fails the suite rather
than shipping quietly — which is exactly why that study exists.

> **Check the board revision before ordering.** Some DevKitC-1 revisions put
> the RGB LED on GPIO48 instead of GPIO38. GPIO48 is the WS2812 output here,
> so on those boards the clash moves rather than disappears. v1.1 is GPIO38.

## What comes out

Deleted with the MCU, because the dev board provides them:

- `U5` ESP32-S3-WROOM-1 module and its decoupling
- `J2` USB-C receptacle, `U7`/`U8` USBLC6 ESD arrays
- The whole USB over-voltage cutoff: `U6`, `Q4`, `Q5`, `PF2`, `D5`, `R27`–`R30`
- `SW1`/`SW2` RESET and BOOT buttons and their pull-ups
- The three strapping-pin pull-downs `R36`–`R38` and the `J7` spare-IO header

## What stays

Everything that is the reason for the board:

- 12 V input: `F1`, `FB1`, `D1` clamp, `U1` + `Q1` ideal diode
- Both converters, `U3` (+5 V) and `U4` (+3V3), and their ripple-injection networks
- The 760 µF ride-through bank and the `U2` TLV431 power-fail detector
- `Q2`/`Q3` sensor-rail switch and the fused `+5VS` tap
- CAN: `U12` TJA1051, `L3` choke, split termination (still off by default), clamps
- microSD with its switched supply and card-slot ESD
- Four analog channels with the 0.1 % dividers, `U13` ADS1115, battery monitor
- `U9` WS2812 buffer and the shift-light header

## Consequences to work through

1. **The dev board's 3V3 and the shield's 3V3 will be tied together** through
   the header. The DevKitC-1 has its own LDO fed from 5 V; with `U4` also
   driving that net, two regulators are paralleled. Either feed the dev board
   5 V only and leave its 3V3 pin unconnected, or feed 3V3 and leave its 5V
   pin unconnected — **not both**. This needs deciding before the schematic is
   redrawn, and it is the single most likely way to damage a dev board.
2. **Mechanical**: outline, header positions and whether the dev board sits
   over the shield or beside it. The USB port must stay reachable, and the
   module's antenna should not sit over a ground pour.
3. **Height**: the 22 mm ride-through cans are the tallest parts here too, and
   now there is a dev board stacked as well.
4. **`gen/generate_pcb.py`** places by zone; removing the module frees the
   middle of the board and everything can shrink. Board area drives PCB cost.
5. **Re-verify, do not assume.** `validate.py`, the four audits,
   `simulate.py` and `simulate_firmware.py` all pass right now on the forked
   tree. They should still pass at every step, and `audit_docs.py` will object
   the moment the prose stops matching.

## Order of work

1. Get the DXF and settle the mechanical question (item 1 above especially).
2. Strip the MCU block from `gen/generate_schematic.py`; add the two headers.
3. Move `I2C_SDA` to GPIO47 in the generator, the firmware and `fwsim`.
4. Re-run `validate.py` and the firmware suite; expect study 14 to catch the
   I²C move if only two of the three places get updated.
5. Re-place and re-route with `gen/build_board.py`, then shrink the outline.
6. Re-run every audit, regenerate `fab/`, re-run `export_order.py`.
