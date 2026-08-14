
## Other fabs worth quoting, and the one thing that decides it

**Tariffs follow country of ORIGIN, not who invoices you.** A Hong Kong or
Singapore reseller shipping boards made in Shenzhen is a mainland-China
origin at the border and attracts the same rate. Origin is where it was
manufactured. That rules out the obvious workaround before it starts, and
routing goods to disguise origin is customs fraud, not a saving.

So the question is not "who else is cheap" but "who else is cheap **and not
mainland China**". The number to beat is not JLCPCB, it is **JLCPCB + 35%**.

### Same country, same tariff — no help on duty

PCBWay, ALLPCB, Seeed Fusion, Elecrow, NextPCB are all Shenzhen. Worth
quoting on price and turn, but every one lands with the same duty as JLCPCB.
PCBWay and Seeed in particular will sometimes beat JLC on assembly setup
fees, which on this project were 52% of the gate's quote.

### Non-China Asia

Taiwan, South Korea, Vietnam, Thailand, Malaysia and India all have real PCB
capacity at different tariff rates. The catch is that little of it is aimed
at 5-off prototypes with online quoting; most wants volume and a purchase
order. India has a few prototype-friendly fabs serving mostly their domestic
market. Worth a look only if a quote is easy to get.

### Europe — the most plausible alternative

- **Aisler** (Netherlands) — small-batch, hobbyist-friendly, EU-made, online
  quoting. Closest thing to an OSH Park in Europe.
- **Eurocircuits** (Belgium/Germany) — prototype and small series, and they
  do assembly. Well regarded, properly engineered, online quoting.
- **Beta Layout / PCB-Pool**, **Multi-CB** (Germany) — similar.

EU prices are higher than mainland China, but the comparison is against
JLC+35% rather than JLC, and EU→US duty is a different and generally lower
rate. Eurocircuits is the one to quote for assembly, Aisler for bare boards.

### United States — priced, not competitive

MacroFab quoted **$1200 and that excluded pick and place**, against JLCPCB's
$475.71 all in. Duty would have to reach **265%** before JLCPCB assembled
cost what MacroFab quoted; it is 35%.

Bare board only: OSH Park prices 4-layer by area at roughly $10/in², and the
logger is about 15 in², so three boards would exceed $100 against $13 for
ten. Advanced Circuits, Sunstone, Bay Area Circuits and Royal Circuits are
all real, all 5–20× for prototype quantities. Screaming Circuits, PCB:NG and
Tempo do assembly at MacroFab-like levels.

US board fab is not close, and the tariff does not make it close.
oor stays open.

Worth knowing that all 19 Yageo part numbers that script *constructs* from
Yageo's own scheme came back **Exact** matches at Octopart, which validates
the scheme end to end.
eference the ADS1115's own gain error is 0.15 % typical and 0.30 %
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
