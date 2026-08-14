
## Have JLCPCB build it? Quote of 14 Aug 2026, 10 boards

Economic PCBA, top side only, 3-day PCB plus 3–4 day assembly, 1.41 kg.

| | |
|---|---|
| PCB (10 off, special offer) | **$13.00** |
| Setup fee | $8.18 |
| Stencil | $1.53 |
| Components, **48 items** | $145.73 |
| Extended-component fees | $64.47 |
| SMT assembly | $5.77 |
| Nitrogen reflow | $0.99 |
| Panel / large size | $0.00 |
| **Total** | **$239.67** — $23.97 a board |

**48 items is every part type on the board**, `C37593` included. That matters
because this page previously warned the ADS1115 was absent from JLCPCB's
assembly library and that 20 TSSOP-10s would have to be hand-fitted whatever
the quote said. **That was wrong.** The claim came from the tscircuit parts
index, which reported `C37593` missing on some queries and present on others,
and which was also wrong about it once before — it sat in a real LCSC cart
while the index denied it. JLCPCB's own quoting system has it. Treat that
index as evidence of stock and price, not as the authority on what can be
placed; the quote form is the authority.

### Against building it by hand

|  | 10 boards | per board |
|---|---|---|
| JLCPCB assembled | $239.67 | $23.97 |
| Bare PCB + parts, hand-built | $156.55 | $15.65 |

**1.53×** — a much better ratio than the gate controller's 2.3×, for two
reasons: the fixed fees are spread over ten boards instead of five, and this
board has four times the placements to spread them over. One-off fees are
30% of this quote against 52% of the gate's.

The parts pricing is not where the difference is. JLCPCB charge $145.73 for
components; the same parts on the LCSC order come to $143.55. **$2.18
apart** — they are not marking parts up. The whole delta is:

```
  setup + extended fees   $72.65
  assembly labour         $ 5.77
  stencil + nitrogen      $ 2.52
  parts margin            $ 2.18
                          ──────
                          $83.12
```

So $83 buys 1280 placements, about **6.5 cents each**. The gate works out at
17 cents. Both are cheap against doing it by hand, and this board is far the
better candidate — 128 placements per board, including two TSSOP-10s.

The $156.55 hand figure uses the marginal parts cost, since the combined LCSC
order is going in anyway. A logger-only parts order paying its own minimums
would be $163.25, putting hand assembly at $176.25 and narrowing the gap to
$63.

**Both boards assembled: $315.12.** The six through-hole lines are hand-fitted
either way — Economic PCBA is SMT top-side only — so they are common to both
routes and cancel out of the comparison.
Ω at 1 %, others 0.1 %** | **0.235 %** | **0.136 %** | **$1.21** |
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
