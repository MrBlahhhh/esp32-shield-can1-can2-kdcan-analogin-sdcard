# Where the money goes, and what to do about it

> **Status: implemented, then partly walked back on purpose.** Parts are
> **$15.02 a board against $18.29, an 18 % cut.** The 1 % resistors went back
> to 0.1 % on the two that set the gain, because there is no calibration step
> in the workflow -- see "Which resistors actually matter" below. The microSD
> swap was dropped after checking it properly.

Prices read from LCSC on 2026-08-13, at the tier a five-board build lands in.
Everything below is per board.

## Cost before these changes

| | per board |
|---|---:|
| SMD BOM, 49 lines | **$13.16** |
| Through-hole (connectors, sockets, supercaps) | **$5.13** |
| **Parts total** | **$18.29** |
| PCB, 98 × 100 mm 4-layer, 5 off | see note |
| ESP32-S3-DevKitC-1 | ~$8 |

Eight lines are 85 % of the SMD cost:

| Part | qty | each | line | share |
|---|---:|---:|---:|---:|
| MCP2518FD CAN controller | 1 | $2.76 | $2.76 | 21 % |
| ADS1115 ×2 | 2 | $1.32 | $2.64 | 20 % |
| **1 kΩ 0.1 % thin film** | 5 | $0.297 | **$1.49** | 11 % |
| Hirose DM3D-SF microSD | 1 | $1.12 | $1.12 | 9 % |
| **10 kΩ 0.1 % thin film** | 5 | $0.186 | **$0.93** | 7 % |
| TJA1051 ×2 | 2 | $0.46 | $0.92 | 7 % |
| TVS diodes (SMAJ26CA/40CA) | 10 | ~$0.076 | $0.76 | 6 % |
| 2.21 kΩ 0.1 % thin film | 5 | $0.053 | $0.27 | 2 % |

Plus the two 1 F supercapacitors at roughly **$1.80 each** — the largest
single item on the whole board once the through-hole side is counted.

## Recommended changes

### 1. Precision only where it changes the answer — saves $1.47

**Revised.** The original version of this section put all fifteen resistors on
1 % and leaned on a per-channel calibration constant to recover the accuracy.
That is the right trade only if somebody actually calibrates, and nobody was
going to, so the divider now carries 0.1 % where it counts and 1 % where it
does not.

#### Which resistors actually matter

Gain is `Rlow / (Rser + Rup + Rlow)`, so each resistor moves it in proportion
to its share of the total:

| | share | gain error per 1 % of tolerance |
|---|---:|---:|
| 1 kΩ series | 0.076 | **0.076 %** |
| 10 kΩ upper | 0.758 | 0.758 % |
| 2.21 kΩ lower | 0.833 | 0.833 % |

**The series resistor is ten times less sensitive than the other two — and it
was the most expensive 0.1 % part on the board**, at $0.30 against $0.19 and
$0.05. So it is the one to give up, and the two that set the ratio keep their
tolerance.

| build | worst case (linear) | realistic (RSS) | cost |
|---|---:|---:|---:|
| all 1 % | 1.667 % | 1.129 % | $0.05 |
| **1 kΩ at 1 %, others 0.1 %** | **0.235 %** | **0.136 %** | **$1.21** |
| all 0.1 % | 0.167 % | 0.113 % | $2.68 |

For reference the ADS1115's own gain error is 0.15 % typical and 0.30 %
maximum, so the middle row puts the divider just under the converter — the
point past which spending more stops buying anything. It saves $1.47 against
all-0.1 % and needs no calibration.

#### The original argument, for the record

The biggest surprise in the whole BOM: **fifteen resistors cost $2.68**, more
than the CAN controller. Yageo's RT/AT 0.1 % series is 0.30 and 0.19 dollars a
piece against $0.0027 for a stocked 1 % part — a hundredfold difference.

They are not buying what they look like they are buying:

- **The ADS1115's own gain error is ±0.15 % typical, ±0.30 % maximum.** A
  0.1 % divider contributes about 0.14 %. The converter is already the
  dominant error term, so tightening the resistors below it changes the total
  by almost nothing.
- **Absolute accuracy has to be calibrated anyway.** One constant per channel
  against a known voltage removes the divider error whatever its tolerance —
  and the schematic has said "exact scale is a firmware calibration constant"
  since the parent board.
- **The thing that cannot be calibrated away is the differential match**, and
  1 % is fine there. The ground correction's residue is the *mismatch* between
  the signal chain and the return chain: at 1 % that is about 2 % of the
  offset, so a 300 mV chassis offset leaves **6 mV** instead of 0.6 mV. Both
  are negligible against the 300 mV being removed.

Honest version of the trade: uncalibrated, 0.1 % gives ~0.33 % total error and
1 % gives ~1.4 %. Calibrated, they are indistinguishable. **If you will
calibrate once per channel, this is free money.** If you will not, keep the
0.1 % parts on the four signal channels and the shared return — that is ten
resistors, not fifteen, and saves $0.27 instead of $2.63.

### 2. Shrink the supercapacitor bank — saves roughly $2

1 F per cell buys 2500 ms of hold-up. Nothing needs that:

| cells | bank | shed | everything on |
|---|---|---:|---:|
| 0.10 F | 0.05 F | 250 ms | 77 ms |
| **0.33 F** | 0.165 F | **825 ms** | 254 ms |
| 0.47 F | 0.235 F | 1175 ms | 362 ms |
| 1.00 F (now) | 0.5 F | 2500 ms | 769 ms |

The parent board rode out **127 ms** and that was judged adequate. A healthy
card flushes in 18 ms; the worst stall the studies model is 500 ms, and
`shutdown()` clears the strip within 9 ms so the shed column is the one that
applies. **0.33 F is 6× the parent's proven margin** and still clears the
worst modelled stall with 65 % to spare.

Correction to `docs/PARTS.md` while I am here: I wrote that cylindrical cells
are "tens of milliohms". The Eaton HV0810 class is **200 mΩ**. Two in series
is 400 mΩ, which drops 48 mV at 120 mA — fine, and still inside the "under
about 1 Ω" requirement, but the note overstated the case.

### 3. Replace the Hirose microSD socket — NOT DONE, see below

$1.12 against $0.04–0.06 for a generic push-push TF socket. That is 9 % of the
SMD BOM in one connector.

The catch is real: **the cheap sockets have no card-detect switch.** That
costs the `SD_CD` signal, and firmware finds out there is no card by failing
to mount — which for a logger that checks at boot is not much of a loss. It
also *frees a GPIO*, and this board currently has none spare.

It needs a footprint change and push-push instead of push-pull (spring eject
rather than friction). Arguably better in a car.

### 4. Consider one ADS1115 instead of two — saves $1.32

The second one exists solely to give channel 4 a differential pair: one chip's
MUX offers three channels against a shared negative, not four.

With one chip you keep three fully differential channels and read the fourth
single-ended on the ESP32's own ADC — still four inputs, but the fourth
without ground correction and at ±1–2 %. Worth it only if three precision
channels is genuinely enough.

## Not recommended

**MCP2518FD → MCP2515 (saves $1.82).** Tempting: $0.94 against $2.76, and the
car runs classic CAN at 500 kbit/s so CAN FD is unused. But the MCP2515 has
**two receive buffers** where the MCP2518FD has 2 KB of message RAM and 32.
On a busy bus with SPI latency in the way, two buffers overrun and you lose
frames — and this is a logger, where a dropped frame is the failure mode that
matters. Keep the 2518.

**TJA1051 → SN65HVD230 (saves ~$0.30).** The HVD230 is 3.3 V only. This board
wants 5 V bus drive with a 3.3 V logic pin, which is exactly what the
TJA1051T/3 does. Not the same part.

**2-layer PCB.** The two socket rows already slot the planes; taking the
planes away entirely on a board carrying CAN, a 20 MHz SPI bus and four analog
channels is a false economy.

## Summary

| Change | saves | status |
|---|---:|---|
| 0.1 % only where it matters | **$1.47** | **done** |
| 1 F → 0.33 F supercaps | ~$1.80 estimated | **done** |
| Generic microSD socket | — | **dropped**, see below |
| **Achieved** | **$3.27** | **$18.29 → $15.02, an 18 % cut** |
| One ADS1115 | $1.32 | available, costs channel 4's ground correction |

The resistor saving is measured against the regenerated BOM. The supercapacitor saving is an estimate, because those
are bought outside the LCSC catalogue and the price depends on the cell.

## What happened to the socket

The $1.06 saving was real arithmetic against the wrong part. Those $0.04
sockets are SHOU HAN and XUNPU house parts whose only drawing is a scanned
PDF with no extractable dimensions, so **the land pattern cannot be verified**
— and this project's whole standard is that every part is checked before it is
trusted. Shipping an unverified footprint on five boards to save a dollar is
not a trade worth making.

The verifiable alternatives are much less attractive than the search results
suggested:

| Part | LCSC | qty 10 | KiCad footprint | card detect |
|---|---|---:|---|---|
| Hirose DM3D-SF (fitted) | `C719027` | $1.12 | yes | yes |
| Molex 47219-2001 | `C164170` | $0.63 | yes | no, hinged lid |
| Hirose DM3AT-SF-PEJM5 | `C114218` | $0.96 | yes | yes, push-push |

So the honest saving is **$0.49**, not $1.06, and it costs the card-detect
signal and swaps a push-pull for a hinged lid. That is 2.7 % of the board to
lose a feature. Not worth it — the Hirose stays.

Worth noting for a larger run: at volume the $0.04 parts become worth the
effort of drawing and verifying a footprint from the manufacturer's drawing.
At five boards it is not.

## A note on the PCB

98 × 100 mm sits **2 mm inside** the 100 × 100 tier boundary that most fabs
price against. Any growth past that steps into a materially more expensive
bracket, so treat 100 mm as a hard limit on both axes rather than a target to
grow into.
