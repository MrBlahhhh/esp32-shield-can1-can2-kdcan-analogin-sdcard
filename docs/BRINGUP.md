# First-power bring-up checklist

Work through this in order on the first board back from fab. Every step has
an expected value; stop at the first one that misses and debug there, because
later stages assume the earlier ones.

**Before anything:** confirm the socket row pitch against a real DevKitC-1
before you solder the sockets. `gen/generate_pcb.py` assumes 22.86 mm and
that number came from a search result, not from Espressif's DXF. If it is
wrong the board is scrap, and finding out with a ruler costs nothing.

**Bench setup:** the dev board NOT yet fitted, bench supply on the aux
harness only where a stage says so, nothing on the sensor loom.

## Stage 0 — before any power

- [ ] Visual: the two 22-way sockets are **receptacles**, facing up. The
      DevKitC-1 has male pins soldered pointing down and drops into them.
- [ ] Both supercapacitor cans: **polarity**. The stripe is the negative
      terminal and pin 1 on the land is positive. These are the only
      polarised electrolytics on the board and they are in series, so a
      reversed cell is not obvious from the silk alone — check both against
      `gen/audit_polarity.py`'s fab checklist.
- [ ] `D2` (SS14) orientation. Cathode band toward `+5V`, anode toward the
      cap bank. Backwards it shorts out the 100 Ω charge resistor *and*
      blocks the discharge, so the hold-up does nothing and the USB port
      sees 4 A at plug-in. It was wired backwards once already.
- [ ] Meter, diode mode, from `+5V` and `+3V3` to GND: neither reads a dead
      short. Both should read a few hundred ohms rising as the decoupling
      charges.
- [ ] Meter across the supercap bank: open, or slowly rising. A short here
      is a reversed cell.

## Stage 1 — rails, from USB, no dev board

The board cannot power itself. Feed 5 V and 3.3 V in on the rail break-out
header (`J7`) to test without risking a dev board.

- [ ] 5 V in, current-limited to 200 mA. Inrush settles within a second or
      two, then **~30 mA** and slowly falling as the bank charges. It takes
      about four minutes to reach full charge through the 100 Ω — that is by
      design, see `docs/PARTS.md`.
- [ ] 3.3 V in: **< 5 mA** with nothing else attached.
- [ ] Green LED on.
- [ ] `PWR_FAIL` (test-point row) **low** with 5 V present. Wind the 5 V
      supply down: it must snap **high at 4.20 ± 0.05 V** and snap back low
      about 140 mV higher. If it sits permanently high, check `U1`'s pinout
      against the exact part bought — shunt references are not consistent
      between vendors in SOT-23.
- [ ] Kill the 5 V supply with the bank charged and watch the rail on a
      scope: it should coast for **seconds**, not milliseconds. Collapsing
      immediately means `D2` is backwards or the cells are open.
- [ ] Nothing warm after five minutes.

## Stage 2 — the dev board

- [ ] Power off, bank discharged. Fit the DevKitC-1, **checking pin 1 at
      both ends** against the silkscreen outline.
- [ ] Power from the dev board's USB-C only. It should enumerate normally.
- [ ] Confirm both USB-C sockets are physically reachable with the loom
      plugged in — the outline reserves 12 mm at each end for the plug.
- [ ] Re-measure 5 V and 3.3 V on the break-out header: they now come *up*
      from the dev board.

## Stage 3 — buses, one at a time

- [ ] **I²C**: scan the bus. Two ADS1115s must answer, at **0x48 and 0x49**.
      Only one means the second part's ADDR pin is not on +3V3.
- [ ] **CAN 1**: with `TERM` open and a terminated bus on `J1`, check for
      frames. The transceiver is `U5`.
- [ ] **CAN 2**: `AUXSEL` bridged 2-3 and `AUXCL` closed. Frames on `J2`.
      Nothing at all means the MCP2518FD is not clocking — scope `Y1`.
- [ ] **K-line**: `AUXSEL` back to its default 1-2, `AUXCL` open. The line
      should idle at battery voltage and pull below 1.5 V when the FET is
      driven. Remember TX is inverted in hardware.

Only one CAN bus and the K-line can be usefully exercised in one loom
configuration — the aux pins are either/or by design.

## Stage 4 — card and analog

- [ ] Card detect reads a card present/absent correctly. If it never
      changes, note that LCSC lists this socket as having no detect switch;
      it does, and `docs/PARTS.md` explains why that listing is wrong.
- [ ] Write a file, pull the USB plug mid-write, re-read the card. The file
      must be closed and intact. This is the whole point of the hold-up.
- [ ] Feed a known 5.00 V into a channel: the ADS1115 should read
      **0.836 V** differentially (0–16 V divider, 2.21/13.21).
- [ ] Short `SENS_RTN` to a deliberate 200 mV offset against board ground:
      the reading must **not** move. If it does, the return attenuator does
      not match the channel — check that both use the same 0.1 % values.
- [ ] Battery sense with 13.8 V on the harness: **1.045 V** at the pin,
      13.8 V after the firmware's 13.195 divisor.
