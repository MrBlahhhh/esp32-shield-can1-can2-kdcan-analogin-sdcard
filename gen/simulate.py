#!/usr/bin/env python3
"""
Circuit-level simulation of the parts of this board a DRC cannot judge.

  python gen/simulate.py [--only frontend|analog|buck] [--no-plots]

Needs ngspice on PATH or at C:\\spice64\\bin, plus numpy and matplotlib.
Runs with any Python 3 -- unlike the pcbnew scripts it does not need
KiCad's interpreter.

Three decks, each answering a question the board file cannot:

  frontend  what actually arrives at the LM5164 VIN pins, and what Q1
            stands off, when each ISO 7637-2 pulse hits the harness
  analog    the transfer function, fault current and bandwidth of one
            sensor channel in all three jumper configurations
  buck      inductor ripple, saturation margin, output ripple and input
            capacitor RMS current across the 8-36 V input window

What these decks do NOT cover: the LM5164's own control loop. It is a
constant-on-time part with an encrypted TI model, so the buck deck drives
the power stage from an ideal duty-cycle source and answers questions
about the passives -- ripple, saturation, RMS current -- not about loop
stability or transient recovery. Those need TI's PSpice model.

Component values are duplicated from gen/generate_schematic.py rather
than parsed out of it, so treat a disagreement as a bug in this file.
"""

from __future__ import annotations

import argparse
import glob
import math
import os
import shutil
import subprocess
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.abspath(os.path.join(HERE, ".."))
SIM = os.path.join(PROJ, "sim")

# ISO 7637-2 pulses as (label, volts, source ohms, decay seconds, absolute).
#
# `absolute` is the difference between the disturbance pulses and load dump.
# Pulses 1 to 3b are drawn in the standard as a spike superimposed on the
# supply, so their U_S adds to 13.5 V.  Pulse 5 is a load dump: U_S is the
# peak the line actually reaches, and 35 V is what a centrally-suppressed
# alternator clamps to.  Adding 13.5 V to that would invent 13.5 V of stress
# and, at the 87 V level, credit the TVS with clamping a pulse that is
# already past every rating on the board.
PULSES = [
    ("pulse 1   -100 V  10 R  2 ms",     -100.0, 10.0, 2e-3,   False),
    ("pulse 2a   +50 V   2 R  50 us",      50.0,  2.0, 50e-6,  False),
    ("pulse 3a  -150 V  50 R  100 ns",   -150.0, 50.0, 100e-9, False),
    ("pulse 3b  +150 V  50 R  100 ns",    150.0, 50.0, 100e-9, False),
    ("pulse 5b   35 V 0.5 R  400 ms",      35.0,  0.5, 400e-3, True),
    ("pulse 5b   87 V 0.5 R  400 ms",      87.0,  0.5, 400e-3, True),
]

VBAT_NOM = 13.5
# What the board draws from the dev board's 5 V pin: transceivers,
# sensor excitation, the shift light and the dev board itself.
LOAD_A = 0.39          # everything running, worst case
UVLO = 6.0             # the converters stop drawing below this
# --------------------------------------------------------------- plumbing ---
def ngspice():
    exe = shutil.which("ngspice_con") or shutil.which("ngspice")
    if exe:
        return exe
    for pat in (r"C:\spice64\bin\ngspice_con.exe",
                r"C:\Program Files\ngspice*\bin\ngspice_con.exe",
                "/usr/bin/ngspice", "/usr/local/bin/ngspice",
                "/opt/homebrew/bin/ngspice"):
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[-1]
    raise SystemExit("ngspice not found -- put ngspice_con.exe on PATH")


def run_deck(name, deck, vectors):
    """Write, run and read back one deck.  Returns {vector: ndarray} plus
    'x' for the sweep variable."""
    os.makedirs(SIM, exist_ok=True)
    cir = os.path.join(SIM, name + ".cir")
    dat = os.path.join(SIM, name + ".dat")
    if os.path.exists(dat):
        os.remove(dat)
    with open(cir, "w", encoding="utf-8") as fh:
        fh.write(deck.replace("@DAT@", dat.replace("\\", "/")))
    res = subprocess.run([ngspice(), "-b", cir],
                         capture_output=True, text=True, cwd=SIM)
    if not os.path.exists(dat):
        sys.stderr.write((res.stdout or "")[-2000:])
        sys.stderr.write((res.stderr or "")[-2000:])
        raise SystemExit("%s: ngspice produced no data" % name)
    raw = np.loadtxt(dat)
    if raw.ndim == 1:
        raw = raw.reshape(1, -1)
    n = len(vectors)
    if raw.shape[1] == 2 * n:            # wrdata's default x,y,x,y,... layout
        cols = {v: raw[:, 2 * i + 1] for i, v in enumerate(vectors)}
        cols["x"] = raw[:, 0]
    elif raw.shape[1] == n + 1:          # one shared scale column
        cols = {v: raw[:, i + 1] for i, v in enumerate(vectors)}
        cols["x"] = raw[:, 0]
    else:
        raise SystemExit("%s: expected %d vectors, got %d columns"
                         % (name, n, raw.shape[1]))
    return cols


def head(t):
    print("\n" + t)
    print("=" * len(t))


def verdict(ok, text):
    print("    %s %s" % ("ok  " if ok else "FAIL", text))
    return [] if ok else [text]


# ------------------------------------------------------ shared model cards ---
MODELS = """
* Bidirectional TVS as the two junctions it physically is: a forward drop
* in series with the other die's avalanche.  BV is set so forward drop plus
* breakdown equals the datasheet V_br(min), and RS splits the dynamic
* resistance (V_clamp - V_br) / I_pp between the two halves.
.subckt tvs_smcj40ca a k
D1 a m DT
D2 k m DT
.model DT D(IS=1e-12 N=1.0 RS=0.433 BV=43.5 IBV=1m CJO=3.0n)
.ends

.subckt tvs_smaj40ca a k
D1 a m DT
D2 k m DT
.model DT D(IS=1e-12 N=1.0 RS=1.62 BV=43.5 IBV=1m CJO=1.5n)
.ends

* BAT54 signal Schottky: 200 mA, ~0.32 V at 10 mA, 30 V reverse.
.model BAT54 D(IS=2e-7 N=1.05 RS=0.6 BV=30 IBV=10u CJO=10p)

* IPD068N10N3G body diode.  The channel itself is a switch below, because
* the LM74700 holds it fully enhanced whenever the input is the higher side.
.model DBODY D(IS=5e-8 N=1.2 RS=0.02 BV=100 IBV=1m CJO=1.5n)
.model IDEALFET SW(vt=0 vh=0.02 ron=0.0068 roff=10meg)
"""
RANGES = [
    # One range. The 0-5 V and bypass settings went with the range and
    # bypass jumpers -- 0-16 V spans a 5 V and a 12 V sensor both, and one
    # fixed ratio is what makes the differential ground correction exact.
    ("0-16 V  (fixed, 2.21k)", False, 2.21e3, False, 16.0),
]


def analog_deck(bypass, rlow, pullup, mode):
    lower = ("Rlow out 0 %g" % rlow) if rlow else "Rlow out 0 1e12"
    upper = "Rup a out 1m" if bypass else "Rup a out 10k"
    pu = "Rpu p5 a 2.49k" if pullup else "Rpu p5 a 1e12"
    if mode == "dc":
        ctrl = "dc Vin -5 40 0.05"
        vec = "wrdata @DAT@ v(out) i(vclamp) v(a)"
        src = "Vin in 0 DC 0"
    elif mode == "ac":
        # wrdata splits a complex vector into two columns, so take the
        # magnitude first and write a real one.
        ctrl = "ac dec 60 1 1meg\nlet m = mag(v(out))"
        vec = "wrdata @DAT@ m"
        src = "Vin in 0 DC 2 AC 1"
    else:                                  # mux settling
        ctrl = "tran 200n 4m uic"
        vec = "wrdata @DAT@ v(out) v(adc)"
        src = "Vin in 0 PULSE(0 %g 1m 1u 1u 1m 4m)" % (16.0 if rlow and
                                                       rlow < 5e3 else 5.0)
    return """* One sensor channel: TVS, series limit, divider, clamp, ADC load
%s

V33 p33 0 DC 3.3
V5s p5  0 DC 5.0
%s

* D_n SMAJ40CA at the connector.
Xtvs in 0 tvs_smaj40ca

* R series/fault limit, then the divider and the range jumper.
Rser in a 1k
%s
%s
%s
* Anti-alias. 470n, sized against the 430 Hz Nyquist of the ADS1115 at
* 860 SPS -- the 100n this was gives 865 Hz, which was tolerable only
* while the 0-5 V range existed to be the default.
Cflt out 0 470n

* BAT54S pair: GND -> node -> +3V3.  Vclamp is the ammeter on the upper
* half, which is the one that carries a positive overvoltage.
Dlo 0 out BAT54
Vclamp out cm DC 0
Dhi cm p33 BAT54

* ADS1115 unbuffered switched-cap front end at +/-4.096 V FSR: about
* 710 k of equivalent input resistance, and the sampling capacitor it
* charges through the mux on-resistance.
Radc out adc 100
Rin adc 0 710k
Csamp adc 0 20p

.control
set filetype=ascii
set wr_singlescale
%s
%s
quit
.endc
.end
""" % (MODELS, src, pu, upper, lower, ctrl, vec)


def sim_analog(plots):
    head("2. Sensor channel: transfer, fault current, bandwidth")
    fails, curves = [], []
    print("    %-28s %10s %10s %10s  %s"
          % ("jumper setting", "at f.s.", "at 36 V", "clamp I", "verdict"))
    for label, byp, rlow, pu, span in RANGES:
        d = run_deck("analog_dc_%s" % label.split()[0],
                     analog_deck(byp, rlow, pu, "dc"),
                     ["v(out)", "i(vclamp)", "v(a)"])
        vin, vout, iclamp = d["x"], d["v(out)"], d["i(vclamp)"]
        at_fs = float(np.interp(span, vin, vout))
        at_36 = float(np.interp(36.0, vin, vout))
        i_36 = abs(float(np.interp(36.0, vin, iclamp)))
        at_neg = float(np.interp(-5.0, vin, vout))
        bad = []
        if at_fs < 1.5:
            bad.append("full scale only %.2f V -- half the range is wasted"
                       % at_fs)
        if at_36 > 3.9:
            bad.append("36 V input drives the pin to %.2f V" % at_36)
        if i_36 > 0.2:
            bad.append("36 V pushes %.0f mA into the BAT54S, rated 200 mA"
                       % (i_36 * 1e3))
        if at_neg < -0.45:
            bad.append("-5 V input drives the pin to %.2f V" % at_neg)
        # Four channels shorted to the top of the input window all inject
        # into +3V3 through their upper clamp diode.  That is fine while the
        # ESP32 is drawing its ~100 mA, and is a rail-pumping hazard when the
        # board is asleep or unpowered with the loom still live.
        if i_36 * 4 > 0.05:
            bad.append("all four channels at 36 V backfeed %.0f mA into +3V3"
                       % (i_36 * 4e3))
        note = ""
        # Only the ESP32's own SAR runs out of window below the rail; the
        # ADS1115 reads to 3.3 V happily at its 4.096 V FSR.
        if at_fs > 3.1:
            note = ("full scale %.2f V is past the ESP32 SAR's usable 3.1 V "
                    "-- read this setting on the ADS1115" % at_fs)
        print("    %-28s %9.3fV %9.3fV %9.1fmA  %s"
              % (label, at_fs, at_36, i_36 * 1e3,
                 "; ".join(bad) if bad else (note or "ok")))
        fails += ["%s: %s" % (label.split("(")[0].strip(), b) for b in bad]
        curves.append((label, vin, vout, iclamp))

    print()
    print("    Small-signal bandwidth (the anti-alias corner the ADS1115 and")
    print("    the ESP32 SAR both sample behind):")
    bws = []
    for label, byp, rlow, pu, span in RANGES:
        d = run_deck("analog_ac_%s" % label.split()[0],
                     analog_deck(byp, rlow, pu, "ac"), ["m"])
        f, mag = d["x"], np.abs(d["m"])
        ref = mag[0]
        below = np.where(mag <= ref / math.sqrt(2.0))[0]
        f3 = float(f[below[0]]) if len(below) else float(f[-1])
        bws.append((label, f3))
        print("        %-28s dc gain %.4f   -3 dB at %8.1f Hz"
              % (label, ref, f3))
    # Aliasing: the ADS1115's fastest rate is 860 SPS, so anything above
    # 430 Hz folds back into the reading.
    for label, f3 in bws:
        if f3 > 430.0:
            fails.append("%s: filter corner %.0f Hz is above the 430 Hz "
                         "Nyquist of the ADS1115 at 860 SPS, so wideband "
                         "sensor noise aliases into the reading"
                         % (label.split("(")[0].strip(), f3))
    if plots:
        plot_analog(curves, bws)
    return fails


def plot_analog(curves, bws):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 4.6),
                                   constrained_layout=True)
    for label, vin, vout, iclamp in curves:
        ax1.plot(vin, vout, lw=1.2, label=label)
    ax1.axhline(3.3, color="r", ls=":", lw=0.8)
    ax1.axhline(3.1, color="orange", ls=":", lw=0.8)
    ax1.set_xlabel("harness input, V")
    ax1.set_ylabel("ADC node, V")
    ax1.set_title("Transfer and clamp")
    ax1.grid(alpha=0.3)
    ax1.legend(fontsize=8)
    for label, vin, vout, iclamp in curves:
        ax2.plot(vin, np.abs(iclamp) * 1e3, lw=1.2, label=label)
    ax2.axhline(200, color="r", ls=":", lw=0.8)
    ax2.set_xlabel("harness input, V")
    ax2.set_ylabel("BAT54S current, mA")
    ax2.set_yscale("log")
    ax2.set_title("Current into the clamp")
    ax2.grid(alpha=0.3)
    ax2.legend(fontsize=8)
    out = os.path.join(SIM, "analog.png")
    fig.savefig(out, dpi=110)
    plt.close(fig)
    print("\n    wrote %s" % out)
BUCKS = [
    # label, Vout, L, Isat of the Sunlord SWPA8040S part, full load,
    # and one output MLCC's capacitance at that DC bias
    ("+5V  33 uH", 5.0, 33e-6, 3.0, 2.0, 11e-6),
    ("+3V3 22 uH", 3.3, 22e-6, 3.4, 1.0, 14e-6),
]
C_BANK = 760e-6        # 100 uF + 2 x 330 uF on +VBAT
P_BOARD = 0.35         # ESP32 logging + SD write bursts, after the shed
P_SENSORS = 0.40       # four sensors at 20 mA on +5VS, before the shed
T_FW = 5e-3            # firmware latency from interrupt to load shed
def crank_deck(vdip):
    """ISO 16750-2 style starting profile: drop to `vdip`, 15 ms at the
    bottom, partial recovery to 6.5 V while the starter turns, then back."""
    return """* engine crank: does the logger ride it or reset?
%s

Vbat  bt 0 DC 13.5 PWL(0 13.5  10m 13.5  11m %g  26m %g  31m 6.5
+ 431m 6.5  436m 13.5  1 13.5)
Lharn bt hr 5u
Rharn hr vf 0.11
Rcar  vf 0 200

Rdu  vf  sen 100k
Rdl  sen 0   12.7k
Rhys pf  sen 1meg
Rpu  p33 pf  10k
V33  p33 0   DC 3.3
Bq   pf  0   I = (V(pf)/50.0) / (1 + exp(-(V(sen) - 1.24)/0.005))

Bdio  vf vbp I = max(V(vf)-V(vbp), 0)/0.0068 + (V(vf)-V(vbp))*1e-6
Dbody vf vbp DBODY

Cblk  vbp b1 100u
Rblk  b1 0 0.30
Cr1   vbp b2 330u
Rr1   b2 0 0.15
Cr2   vbp b3 330u
Rr2   b3 0 0.15

Bload vbp 0 I = (0.35 / max(V(vbp), 2.0)) * (0.5 + 0.5*tanh((V(vbp) - %g)/0.3))

.control
set filetype=ascii
set wr_singlescale
tran 50u 600m 0 50u
wrdata @DAT@ v(vbp) v(pf) v(vf)
quit
.endc
.end
""" % (MODELS, vdip, vdip, UVLO)


def sim_crank(plots):
    head("6. Engine crank: ride or reset, and does PWR_FAIL chatter")
    print("    ISO 16750-2 style profile: dip at 11 ms, 15 ms at the bottom,")
    print("    then 400 ms at 6.5 V while the starter turns.  Sensors are")
    print("    assumed already shed (PWR_FAIL asserts on the way down).")
    print()
    fails = []
    print("    %-18s %10s %10s %10s  %s"
          % ("dip", "min +VBAT", "rails", "PF edges", "verdict"))
    for vdip, label in ((6.0, "warm crank 6.0V"), (4.5, "cold crank 4.5V")):
        d = run_deck("crank_%d" % (vdip * 10), crank_deck(vdip),
                     ["v(vbp)", "v(pf)", "v(vf)"])
        t, vbp, pf = d["x"], d["v(vbp)"], d["v(pf)"]
        vmin = float(vbp.min())
        dropped = bool((vbp < UVLO).any())
        # count PWR_FAIL rising edges -- more than one is chatter
        hi = pf > 1.65
        edges = int(np.sum(hi[1:] & ~hi[:-1]))
        bad = []
        if edges > 1:
            bad.append("PWR_FAIL chattered %d times" % edges)
        print("    %-18s %9.2fV %10s %10d  %s"
              % (label, vmin, "DROP" if dropped else "held", edges,
                 "; ".join(bad) if bad else
                 ("ok" if not dropped else
                  "ok (reset accepted: harness sat below the converters' "
                  "own dropout)")))
        fails += ["crank (%s): %s" % (label, b) for b in bad]
        if dropped and vdip >= 6.0:
            fails.append("crank (%s): rails dropped even though the harness "
                         "never went below %.1f V" % (label, vdip))
    return fails
def sim_canbus(plots):
    """TJA1051 through the common-mode choke and split termination into a
    real cable: does a dominant bit arrive clean at the far end?"""
    head("10. CAN bus: dominant bit through choke, split term and 5 m of bus")
    fails = []
    deck = """* CAN dominant-recessive-dominant through the on-board network
%s

* Driver calibrated to the TJA1051 datasheet: 2.0 V typical differential
* INTO the 60 ohm double termination, i.e. 3.0 V open-circuit behind
* 15 ohm per leg. Recessive releases both to a weak 2.5 V hold.
Vctl  c 0 PULSE(0 1 200n 5n 5n 1u 2u)
Vhi   vh 0 DC 4.0
Vlo   vl 0 DC 1.0
Vmid  vm 0 DC 2.5
Bh    th 0 V = V(c) > 0.5 ? V(vh) : V(vm)
Bl    tl 0 V = V(c) > 0.5 ? V(vl) : V(vm)
Rh    th canh_t 15
Rl    tl canl_t 15

* 51 uH common-mode choke, k = 0.995: near-transparent differentially.
Lh    canh_t canh 51u
Ll    canl_t canl 51u
K1    Lh Ll 0.995

* Split termination on-board: 60 + 60 with the centre decoupled.
Rs1   canh split 60
Rs2   canl split 60
Cs    split 0 4.7n

* Clamps' parasitic capacitance at the connector.
Ch    canh 0 30p
Cl    canl 0 30p

* 5 m of bus at 120 ohm, ~21 ns of flight, terminated at the far end.
* An ideal T element rather than LTRA: the lossy model went numerically
* wild against the coupled choke and reported more differential volts at
* the far end than the driver can produce.
T1    canh canl far_h far_l Z0=120 TD=21n
Rterm far_h far_l 120
Rfh   far_h 0 1meg
Rfl   far_l 0 1meg

.control
set filetype=ascii
set wr_singlescale
tran 1n 2u 0 1n
wrdata @DAT@ v(canh) v(canl) v(far_h) v(far_l)
quit
.endc
.end
""" % MODELS
    d = run_deck("canbus", deck, ["v(canh)", "v(canl)", "v(far_h)",
                                  "v(far_l)"])
    t = d["x"]
    dif_near = d["v(canh)"] - d["v(canl)"]
    dif_far = d["v(far_h)"] - d["v(far_l)"]
    w = (t > 0.6e-6) & (t < 1.1e-6)      # settled dominant portion
    dom_far = float(dif_far[w].mean())
    ring = float(dif_far.max() - dif_far[w].max())
    print("    dominant differential at the far end: %.2f V "
          "(ISO 11898 wants 1.5-3.0)" % dom_far)
    print("    worst overshoot beyond settled level: %.2f V" % max(ring, 0))
    if not 1.5 <= dom_far <= 3.0:
        fails.append("far-end dominant level %.2f V is outside 1.5-3.0 V"
                     % dom_far)
    if plots:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(9, 4), constrained_layout=True)
        ax.plot(t * 1e6, dif_near, lw=1.0, label="node (this board)")
        ax.plot(t * 1e6, dif_far, lw=1.0, label="far end, 5 m")
        ax.axhline(1.5, color="r", ls=":", lw=0.8)
        ax.set_xlabel("us")
        ax.set_ylabel("CANH - CANL, V")
        ax.grid(alpha=0.3)
        ax.legend(fontsize=8)
        ax.set_title("dominant bit through choke + split termination")
        out = os.path.join(SIM, "canbus.png")
        fig.savefig(out, dpi=110)
        plt.close(fig)
        print("    wrote %s" % out)
    return fails


# ================================================================= budgets ===
def sim_budgets(plots):
    """System-level budgets -- the failures that read 'marginal in July'
    rather than 'broken on the bench'. All analytic."""
    head("11. System budgets: thermal, fuse, I2C, LED chain, SD switch")
    fails = []

    # --- enclosure thermal --------------------------------------------------
    # Worst continuous: both bucks loaded (0.55 W loss each), ESP32 logging
    # with WiFi bursts (~1.0 W average), CAN + SD + sensors (~0.4 W).
    # The board mounts IN THE DASH: cabin hot-soak ambient (~70 C parked in
    # the sun), not the 85 C engine bay. The bay column stays as the answer
    # to "what happens if it ever moves there": a sealed plastic box in the
    # bay would exceed the electrolytics' 105 C rating.
    p_diss = 0.55 * 2 + 1.0 + 0.4
    print("    Enclosure thermal, %.1f W dissipated inside the box:" % p_diss)
    print("      %-34s %-8s %-8s %s"
          % ("enclosure", "rise", "70C dash", "85C bay"))
    for label, rth in (("sealed ABS ~100x120x40", 9.0),
                       ("vented ABS, same size", 5.5),
                       ("diecast aluminium on a bracket", 3.5)):
        rise = p_diss * rth
        t_dash, t_bay = 70 + rise, 85 + rise
        note = "ok in the dash" if t_dash <= 100 else "HOT even in the dash"
        if t_bay > 105:
            note += "; a bay mount would exceed the caps' 105 C"
        print("      %-34s +%4.1f C %6.1f C %6.1f C  %s"
              % (label, rise, t_dash, t_bay, note))
        if t_dash > 105:
            fails.append("%s exceeds 105 C even at dash ambient" % label)

    # --- OBD 12 V protection ------------------------------------------------
    # There is no input fuse any more, because there is no input: the board
    # runs from the dev board's USB-C. The only vehicle supply it touches is
    # OBD-II pin 16, which is PERMANENT battery -- live with the car parked --
    # so what PF1 protects against is this board failing short across it.
    #
    # A PTC holds ~87 % of its rating at a 70 C dash hot-soak. The load is the
    # optional K-line tester pull-up and nothing else; the 1.20 A that used to
    # be here was the two bucks' input current, and they are gone.
    i_load = 12.0 / 750.0              # the 750 ohm pull-up, if stuffed
    i_eff = 0.2 * 0.87
    util = i_load / i_eff
    print("\n    PF1 (0.2 A PTC) on OBD pin 16 at a 70 C dash: effective "
          "%.3f A, load %.3f A -> %.0f %% utilisation" % (i_eff, i_load, util * 100))
    if util > 0.75:
        fails.append("PF1 sits at %.0f%% of its derated rating" % (util * 100))

    # --- I2C rise time ------------------------------------------------------
    # 4.7k pull-ups; ~30 pF on-board (module + ADS1115 + trace). External
    # Qwiic devices add ~10 pF each plus ~50 pF per metre of cable.
    print("\n    I2C at 400 kHz needs t_r <= 300 ns (0.847*R*C):")
    for ext, label in ((0, "on-board only"),
                       (60, "2 Qwiic devices, 0.5 m cable"),
                       (150, "4 devices, 1.5 m of cable")):
        c = (30 + ext) * 1e-12
        tr = 0.847 * 4700 * c
        print("      %-28s %4.0f pF -> t_r %4.0f ns  %s"
              % (label, 30 + ext, tr * 1e9,
                 "ok" if tr <= 300e-9 else "drop the bus to 100 kHz"))

    # --- WS2812 chain -------------------------------------------------------
    print("\n    WS2812 header: PF3 holds 0.5 A -> %d LEDs at full white, "
          "~%d at typical shift-light duty"
          % (int(0.5 / 0.060), int(0.5 / 0.020)))

    # --- SD power switch ----------------------------------------------------
    r_on = 0.090                       # DMG2301L at Vgs = 3.3 V
    drop = r_on * 0.100
    print("\n    SD_VDD switch: DMG2301L ~%.0f mR at Vgs 3.3 -> %.0f mV "
          "drop in a 100 mA write burst; %.0f mV of margin to the card's "
          "2.7 V floor" % (r_on * 1e3, drop * 1e3, (3.3 - 2.7 - drop) * 1e3))

    # --- connector contacts -------------------------------------------------
    print("\n    JST-PH contacts are 2 A parts, and nothing here comes close: "
          "J1 pin 1 feeds a 100k sense divider plus at most 16 mA of K-line "
          "pull-up, and J9's two +5VS pins share the sensor rail's 0.2 A PTC. ok")
    return fails


# ================================================================ fidelity ===
def sim_fidelity(plots):
    """What sampling really does to logged data (scipy.signal).

    Study 2 reported the anti-alias corner abstractly ('891 Hz is above the
    430 Hz Nyquist'). This one answers the question a logger owner actually
    has: a realistic sensor waveform plus engine noise goes through each
    jumper mode's real RC, gets sampled at the ADS1115's 860 SPS, and the
    error against the true signal is measured in percent of full scale.
    """
    from scipy import signal as sig
    head("12. Logged-data fidelity through the channel filter at 860 SPS")
    fails = []
    rng = np.random.default_rng(7)
    fs_hi = 100_000.0
    t = np.arange(0, 2.0, 1 / fs_hi)
    # Sensor truth: an AFR-style sweep with a 3 Hz oscillation on it.
    truth = 2.5 + 1.5 * np.sin(2 * np.pi * 0.5 * t) \
        + 0.3 * np.sin(2 * np.pi * 3.0 * t)
    # Engine noise: alternator whine + ignition hash, 100 mV rms, band-
    # limited 200 Hz - 5 kHz, exactly the stuff that folds down if the
    # channel filter lets it through.
    noise = rng.normal(0, 1, t.size)
    b, a = sig.butter(3, [200 / (fs_hi / 2), 5000 / (fs_hi / 2)], "bandpass")
    noise = sig.lfilter(b, a, noise)
    noise *= 0.100 / max(noise.std(), 1e-12)
    vin = truth + noise

    fs_adc = 860.0
    step = int(fs_hi / fs_adc)
    print("    input: AFR-style sweep + 100 mV rms of 0.2-5 kHz engine")
    print("    noise; sampled at 860 SPS through each mode's real filter.")
    print()
    print("    %-26s %9s %10s  %s"
          % ("jumper setting", "corner", "log error", "verdict"))
    for label, gain, f3 in (("0-5 V   (RANGE C-A)", 15.0 / 26.0, 261.0),
                            ("0-16 V  (RANGE C-B)", 0.167, 891.0),
                            ("bypass  (BYPASS)", 0.999, 1647.0)):
        blp, alp = sig.butter(1, f3 / (fs_hi / 2))
        filtered = sig.lfilter(blp, alp, vin * gain)
        sampled = filtered[::step]
        # The truth, seen through the same DC gain, at the sample instants:
        ideal = (truth * gain)[::step]
        err = sampled - ideal
        # remove the filter's own settling from the score
        err = err[20:]
        rms = float(np.sqrt(np.mean(err ** 2)))
        fs_span = 3.3
        pct = rms / fs_span * 100
        ok = pct < 1.0
        print("    %-26s %7.0fHz %7.2f%%FS  %s"
              % (label, f3, pct, "ok" if ok else
                 "noisy logs -- average in firmware or accept"))
        if pct >= 2.0:
            fails.append("%s mode logs %.1f%% FS of noise+alias error"
                         % (label.split()[0], pct))
    print()
    print("    The error is dominated by in-band noise the filter passes,")
    print("    not by aliasing artifacts: oversample-and-average in firmware")
    print("    (the ADS1115 at 860 SPS averaged 4:1 gives an effective")
    print("    215 Hz rate with half the noise) if the logs look hairy.")
    return fails


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["analog", "crank", "canbus",
                                       "budgets", "fidelity"])
    ap.add_argument("--no-plots", action="store_true")
    args = ap.parse_args()
    plots = not args.no_plots

    print("ngspice     : %s" % ngspice())
    print("decks + data: %s" % SIM)
    fails = []
    if args.only in (None, "analog"):
        fails += sim_analog(plots)
    if args.only in (None, "crank"):
        fails += sim_crank(plots)
    if args.only in (None, "canbus"):
        fails += sim_canbus(plots)
    if args.only in (None, "budgets"):
        fails += sim_budgets(plots)
    if args.only in (None, "fidelity"):
        fails += sim_fidelity(plots)

    head("Summary")
    if not fails:
        print("    Nothing flagged.")
    for f in dict.fromkeys(fails):
        print("  - " + f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
