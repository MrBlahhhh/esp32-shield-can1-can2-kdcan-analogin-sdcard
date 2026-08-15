# Wanted, not yet built

Things this board should eventually do. Distinct from the "Known open" list
in the README, which is defects and unverified assumptions in what already
exists — everything here is an addition nobody has started.

---

## Order: ready, paused on tariffs

**Nothing is blocking this technically.** It is on hold because US customs
adds **35% of merchandise value**, which turned a $315 order into $475.71.
See [COST.md](COST.md) for the full breakdown and why it does not change the
build-versus-assemble answer, only the absolute number.

State as of 14 Aug 2026, all verified:

| | |
|---|---|
| Route chosen | **JLCPCB assembles both boards** — Economic PCBA, top side |
| Logger | 10 boards, $13.00 PCB + $226.67 assembly, 48/48 part types placed |
| Gate | 5 boards, $7.00 PCB + $68.45 assembly, 25/25 part types placed |
| JLCPCB merchandise | $315.12 |
| LCSC | `fab/order-tht-paste.csv` — 6 through-hole lines, 75 pieces, $9.64 |
| Duty + shipping | $110.30 + $50.29 on the JLCPCB side |

### To resume

1. **Re-run the checks; they are cheap and stock moves.**
   `python gen/check_stock.py`, then `python gen/verify_cart.py --live`.
   Both should report nothing.
2. **Regenerate the orders** at whatever board counts you want:
   ```
   python gen/export_combined_order.py --boards 10 --other-boards 5 --assembled
   ```
   Drop `--assembled` and add `--spares 2` if hand-building after all.
3. Re-upload the gerbers, `fab/bom.csv` and `fab/positions.csv` to JLCPCB.
   The quotes above will have moved.
4. After the LCSC cart exists, `python gen/learn_moq.py <export.csv>` to keep
   the minimums current, and `python gen/verify_cart.py <export.csv>`.

### Worth reconsidering when it restarts

- **Board counts.** 10 loggers was chosen when the marginal cost of extra
  boards looked small. With 35% on top, each extra board carries its duty
  too. Five of each is $200-ish less.
- **Two layers instead of four** for the gate controller. Noted in that
  project's `docs/ORDER.md` as an untested saving; it only matters if the
  fab bill is being squeezed.
- **Spares.** The current LCSC order has none, because assembled boards have
  no attrition to cover. If a board arrives faulty there is nothing to repair
  it with, and the SMD side means LCSC minimums all over again.

---

## Wideband AFR, via a DIY-EFI TinyWB

**Status:** analogue path needs nothing. Serial path costs a GPIO and is
probably not worth it. <https://diy-efi.co.uk/product/tinywb_module>

An LSU 4.9 controller, 17 × 21 mm, giving a **0–5 V analogue output scaled
10–20 AFR** plus a custom serial protocol.

### The analogue route works as-built

This is the sensor the analogue front end was designed around — the schematic
says so in as many words: "AFR on a 0-5 V wideband output".

| | |
|---|---|
| Signal | `AIN1_IN`–`AIN4_IN`, pins 3/4/7/8 of the 10-way sensor harness |
| Scaling | 5 V lands at 0.836 V through the fixed 0–16 V divider — 6688 counts on the ADS1115, **0.75 mV** referred to input, about **0.0003 AFR** |
| Ground | `SENS_RTN` on pins 5/6 is a Kelvin sense that subtracts out exhaust-to-chassis offset. On a sensor bolted to the manifold that is not a nicety |
| Power | `+5VS` on pins 1/2 |

**The 0.2 A PTC on `+5VS` is not a problem, despite appearances.** The LSU
4.9 heater takes amps, but it runs from **a fused 12 V supply at the sensor
connector** — the module's own 5 V is logic only. Worth writing down because
the first look at this said the rail could not do it.

### Range, not resolution, is the thing to check

0–5 V maps to **10–20 AFR**, and 10.0 AFR is λ 0.68. A turbo car at WOT
commonly sits 10.5–11.0, so the useful data is near the bottom of the range
and **anything richer than 10 AFR clips**. The resolution is absurdly good
either way; the range is the exposure.

**Ask DIY-EFI whether the analogue scaling is configurable** before doing
anything else. Many controllers rescale 0–5 V to something like 8–18 AFR,
which fixes this for free.

### The serial route, if the range turns out to be fixed

Three obstacles, worst first:

- **No spare GPIO.** All 24 usable DevKitC-1 pins are allocated. A UART means
  taking a pin off something else.
- **The one UART on a connector will not read TTL.** K-line is IO1/IO2 and
  reaches the aux harness, but its RX divider is 22 k/10 k, sized for 9–16 V
  automotive levels. 5 V TTL through it lands at **1.56 V** against a 2.48 V
  worst-case input-high threshold. Swapping to 6.8 k/10 k gives 2.98 V and
  costs you K-line.
- **The protocol is "custom"** and not publicly documented.

Cheaper pins to trade, if it comes to it: the WS2812 shift-light output on
J6 (IO48, already 5 V-capable through its buffer) or the Qwiic I²C pins on
J5. Both are on headers.

Same conclusion as [the EGT note](#exhaust-gas-temperature-via-mcp9600):
no board change either way. The decision is which GPIO you are willing to
lose, and it should not be made until the analogue range question is
answered.

## Exhaust gas temperature, via MCP9600

**Status:** scoped, not started. **Needs no board change.**

K-type EGT probes read through an MCP9600 hanging off J5, the I2C header.
Checked against the finished PCB, not the schematic:

| what | where it already is |
|---|---|
| Bus | J5 at x = 93 mm, the right board edge. The DevKitC-1 sits at x = 40 and x = 63, so it is reachable with the module in and the loom plugged |
| Pinout | 1 = GND, 2 = +3V3, 3 = SDA, 4 = SCL |
| Pull-ups | 4.7 kΩ on both lines, already fitted |
| Power | 3.3 V on pin 2. The MCP9600 wants 2.7–5.5 V and draws ~300 µA — nothing to the DevKit's LDO |
| Addresses | 0x60–0x67, against the ADS1115s at 0x48/0x49. No conflict, up to 8 channels |
| GPIO | none needed. This is the reason it is an MCP9600 and not a MAX31856 — see below |

**J5 is a 0.1″ pin header, not a JST-SH socket, despite being labelled
"I2C / Qwiic".** A Qwiic cable does not plug straight in; it needs an adapter
or flying leads. The pin order does match the Qwiic colour convention
(black/red/blue/yellow → GND/3V3/SDA/SCL) so it maps one to one.

### Getting to the engine bay

The sensors are in the bay and the logger is not, and **I²C does not leave
the board** — it is a few-inches PCB bus, single-ended, with no error
detection and no retry. A metre of SDA/SCL through ignition noise will lock
up, and because both ADS1115s share that bus, an EGT glitch takes out the
whole analog front end. A stuck SDA hangs everything until reset.

Three ways round it, in preference order:

1. **Thermocouple extension wire, MCP9600 at the logger.** The chosen
   approach. The chip stays on J5 and chromel/alumel extension cable runs to
   the bay. This also puts the cold junction somewhere thermally stable,
   which the part wants — its CJC sensor is *inside* the chip, so wherever
   the chip is, that is the reference. A cabin-mounted logger is a better
   cold junction than an engine bay one. **The cable must be chromel/alumel;
   copper before the terminals wrecks the accuracy.**
2. **MCP9600 plus a small MCU in the bay, temperatures back over CAN.** The
   automotive answer, and what commercial EGT modules do. Worth it beyond
   about two metres or four-plus channels. The aux harness can carry the
   second CAN pair, but that is a solder-jumper choice against K-line and
   the R53 wants K-line — so either give up K-line or share CAN1 with the
   OBD bus at a high ID.
3. **Differential I²C extender.** A PCA9615 at each end over twisted pair.
   Zero board change, hangs off J5 as a module, but it is still I²C
   semantics underneath.

### Before wiring anything

- **Use ungrounded (isolated-junction) probes.** EGT probes bolted into a
  manifold are usually grounded-junction — the tip is bonded to the exhaust,
  which is chassis. The MCP9600's inputs are not isolated, so a grounded
  probe gives a ground loop through the exhaust, and two probes on different
  cylinders give a loop between each other.
- Most breakouts carry their own ~10 kΩ pull-ups. In parallel with the
  board's 4.7 kΩ that is ~3.2 kΩ, about 1 mA — fine. Cut the jumpers on the
  extras if more than a couple are stacked.

### Why not the MAX31856, which is the better part

$3.39 against $9.93, TSSOP-14 against QFN-20 with an exposed pad, plus 50/60
Hz rejection and open-circuit detection. **It is SPI, and there is no chip
select to give it** — all 24 usable DevKitC-1 GPIOs are allocated. That one
fact decides the whole question.

The MCP9600's package is the hardest on the board by some margin. For one or
two channels a ready-made breakout is cheaper than a board spin and much
easier than hand-soldering a QFN-EP.

### Work remaining

All firmware.

- MCP9600 driver following the existing raw-register pattern in `main.cpp` —
  no library, same as the ADS1115 code. Hot-junction read, cold-junction
  read, thermocouple type and filter coefficient config.
- Open-circuit and out-of-range handling. A disconnected probe must log as
  absent, not as a temperature.
- EGT fields in the log format, and the matching decoder change.
- Bus-fault containment: an EGT device that stops responding must not stall
  the ADS1115 reads on the same bus.
