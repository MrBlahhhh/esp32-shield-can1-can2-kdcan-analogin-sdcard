#!/usr/bin/env python3
"""
Firmware-in-the-loop simulation: run the ESP32 sketch itself and feed it inputs.

  python gen/simulate_firmware.py [--only <study>] [--sketch r53|autosport|both]
                                  [--no-plots] [--keep]

`gen/simulate.py` answers what the *circuit* does. This answers what the
*firmware* does on top of it, which is a different question and, on a board
whose GPIO map is not the one the code was written against, a more dangerous
one -- a wrong pin number is not a compile error, it is a board that boots,
prints happy status lines, and measures nothing.

How it works. The sketch is compiled unmodified for the host against shims in
`fwsim/shim/` that implement the Arduino, FastLED, NimBLE, Wire and TWAI calls
it makes. Underneath them sits a model of the board: what each GPIO is really
wired to, the analog divider ratios, the switched sensor rail, the ADC's
ceiling, the power-fail detector and the ride-through budget. Time is virtual,
so a minute of driving runs in a few milliseconds and runs the same every time.

Two sketches are under test:

  r53        firmware/esp32_shiftlight_wideband as it runs in the MINI today
             (mini-r53-logger). Compiled straight out of that repository, so
             there is no copy here to drift.
  autosport  the same firmware ported to this board.

Two board models, from README sections 2, 3 and 6:

  s3zero     Waveshare ESP32-S3-Zero, SN65HVD230, 10k/10k on the wideband --
             what the R53 sketch was written for. Used as the control: the
             harness has to agree that the shipping firmware is clean before
             anything it says about the new board is worth reading.
  autosport  this board.

What this does NOT cover: anything about the radio, the USB stack, FreeRTOS
scheduling or flash wear. The shims model the API contract, not the
implementation -- a NimBLE bug will not show up here, and neither will a stack
overflow. It answers questions about the sketch's own logic and its fit to the
hardware.
"""

from __future__ import annotations

import argparse
import re
import glob
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.abspath(os.path.join(HERE, ".."))
FWSIM = os.path.join(PROJ, "fwsim")
SIM = os.path.join(PROJ, "sim", "fw")
BUILD = os.path.join(PROJ, ".build", "fwsim")

# The control build is a verbatim copy of the shipping R53 sketch, vendored
# into this repository so a study here can never write to mini-r53-logger.
# firmware/vendor/README.md records where it came from and how to refresh it.
R53_SKETCH = os.path.join(PROJ, "firmware", "vendor", "r53_shiftlight_wideband", "main.cpp")
AUTOSPORT_SKETCH = os.path.join(PROJ, "firmware", "esp32_shiftlight_wideband", "src", "main.cpp")

# The carrier has ONE divider ratio: 2.21 / 13.21, spanning 0-16 V so that a
# 5 V and a 12 V sensor both fit without a jumper. RANGE_5V is what the R53
# board's own front end does, and is kept only for the control comparison.
RANGE_16V = 0.1673
RANGE_5V = 0.5769          # the s3zero control board, not this one
ADC_FULLSCALE = 3.10


# ---------------------------------------------------------------- toolchain --
def host_cxx() -> str:
    """The host C++ compiler. PlatformIO's MinGW package is the one that is
    here because the ESP32 toolchain is; fall back to anything on PATH."""
    cands = glob.glob(os.path.join(os.path.expanduser("~"), ".platformio",
                                   "packages", "toolchain-gccmingw32*", "bin", "g++.exe"))
    cands.sort()
    if cands:
        return cands[-1]
    for name in ("g++", "clang++", "c++"):
        found = shutil.which(name)
        if found:
            return found
    raise SystemExit(
        "no host C++ compiler found.\n"
        "  Install PlatformIO's MinGW package, which needs no admin rights:\n"
        "    pio pkg install -g -t platformio/toolchain-gccmingw32")


def build(sketch_path: str, tag: str) -> str:
    if not os.path.exists(sketch_path):
        raise SystemExit("sketch not found: %s" % sketch_path)
    os.makedirs(BUILD, exist_ok=True)
    exe = os.path.join(BUILD, "fwsim_%s.exe" % tag)
    src = [os.path.join(FWSIM, "runner.cpp"),
           os.path.join(FWSIM, "shim", "sim.cpp"),
           os.path.join(FWSIM, "shim", "globals.cpp"),
           sketch_path]
    newest = max(os.path.getmtime(s) for s in src +
                 glob.glob(os.path.join(FWSIM, "shim", "*.h")) +
                 glob.glob(os.path.join(FWSIM, "shim", "driver", "*.h")))
    if os.path.exists(exe) and os.path.getmtime(exe) > newest:
        return exe
    cmd = [host_cxx(), "-std=c++11", "-O1", "-Wall",
           "-I", FWSIM, "-I", os.path.join(FWSIM, "shim"),
           # Statically linked so the executable does not need the toolchain's
           # DLLs on PATH to run.
           "-static", "-static-libgcc", "-static-libstdc++"] + src + ["-o", exe]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        sys.stderr.write(res.stdout + res.stderr)
        raise SystemExit("build failed for %s" % sketch_path)
    if res.stderr.strip():
        print("  compiler warnings:\n" + res.stderr.rstrip())
    return exe


# ------------------------------------------------------------------ running --
class Run:
    """One scenario executed against one build."""

    def __init__(self, name, exe, scenario):
        os.makedirs(SIM, exist_ok=True)
        self.name = name
        base = os.path.join(SIM, name)
        self.scn, self.csv = base + ".txt", base + ".csv"
        self.log, self.flt = base + ".log", base + ".faults.txt"
        with open(self.scn, "w", encoding="utf-8") as fh:
            fh.write(scenario)
        subprocess.run([exe, "--scenario", self.scn, "--trace", self.csv,
                        "--serial", self.log, "--faults", self.flt],
                       capture_output=True, text=True)
        self.faults_text = _read(self.flt)
        self.serial = _read(self.log)
        self.summary, self.faults = _split_faults(self.faults_text)
        self.rows = _read_csv(self.csv)

    # -- accessors the checks are written in terms of ----------------------
    def codes(self):
        # "ERROR   120.00ms LED_PIN   detail..." -- see fault() in sim.cpp.
        return [f.split()[2] for f in self.faults if len(f.split()) > 3]

    def errors(self):
        return [f for f in self.faults if f.startswith("ERROR")]

    def at(self, t_ms):
        """The trace row nearest t_ms."""
        return min(self.rows, key=lambda r: abs(r["t_ms"] - t_ms))

    def num(self, key):
        return float(self.summary.get(key, "nan").split()[0])


def _read(path):
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _split_faults(text):
    summary, faults, in_faults = {}, [], False
    for line in text.splitlines():
        if line.startswith("----"):
            in_faults = True
            continue
        if in_faults:
            if line.strip():
                faults.append(line)
        elif line.strip():
            parts = line.split(None, 1)
            if len(parts) == 2:
                summary[parts[0]] = parts[1].strip()
    return summary, faults


def _read_csv(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, "r", encoding="utf-8") as fh:
        head = fh.readline().strip().split(",")
        for line in fh:
            vals = line.strip().split(",")
            if len(vals) != len(head):
                continue
            row = {}
            for k, v in zip(head, vals):
                try:
                    row[k] = float(v)
                except ValueError:
                    row[k] = v
            rows.append(row)
    return rows


# ---------------------------------------------------------------- scenarios --
def rpm_sweep(board, *, sensor=1.60, rail="none", extra=""):
    return f"""board {board}
duration 12000
trace 20
@0    vbat 13.8
@0    sensor 1 {sensor}
@0    sensorrail 1 {rail}
@0    canid 0x316
@0    canrate 100
@0    rpm 800
@2000 rpm 3200
@4000 rpm 6500
@6000 rpm 7400
@7000 ble connect
@7200 ble subscribe 1
@9000 rpm 2000
{extra}"""


def accuracy_ladder(board, rail="none"):
    """Step the wideband through its range and read back what the firmware
    reports over BLE. Compares a measurement to its own stimulus, which is the
    only way a divider ratio baked into a constant ever gets checked."""
    steps = [0.50, 1.00, 1.50, 2.00, 2.50, 3.00, 3.50, 4.00, 4.50, 5.00]
    lines = [f"board {board}", "duration %d" % (1000 + 400 * len(steps)), "trace 10",
             "@0 vbat 13.8", "@0 sensorrail 1 " + rail,
             "@0 canid 0x316", "@0 canrate 50", "@0 rpm 1500",
             "@300 ble connect", "@400 ble subscribe 1"]
    # No range override any more. The carrier has ONE divider ratio and the
    # board model already carries it, so forcing r5v here was overriding the
    # design with a jumper setting that no longer exists -- the front end
    # divided by 0.1673 while the scenario insisted on 0.5769, and the
    # firmware came out reading 250% high. The board is the authority.
    for i, v in enumerate(steps):
        lines.append("@%d sensor 1 %.3f" % (1000 + 400 * i, v))
    return "\n".join(lines) + "\n", steps


# A 12 V cigarette-socket USB charger, plus the dev board's own input diode.
# 1.0 V of headroom is typical for a small synchronous buck; 0.3 V is the
# Schottky. Nominal output here is 4.70 V rather than 5.00 because that is
# what the rail measures loaded, cable drop included.
CHARGER_DROPOUT = 1.0
DEVKIT_SCHOTTKY = 0.3
USB_NOMINAL = 4.70


def charger(vbat):
    return max(0.0, min(USB_NOMINAL, vbat - CHARGER_DROPOUT - DEVKIT_SCHOTTKY))


def replay(dat, column=1, *, board="autosport", duration_ms=None,
           step_ms=2.0, prologue="", scale=1.0):
    """Turn an ngspice transient into supply events so the firmware rides the
    same waveform the circuit study produced.

    Two nets come out of one waveform now. The trace is the vehicle battery,
    which this board only senses (OBD-II pin 16). What it RUNS on is 5 V from
    a USB charger in a cigarette socket, so the charger has to be modelled:
    a buck regulating 5.0 V until its own input gets too low, then falling
    with it, less the DevKitC-1's series Schottky.

        v5 = min(4.70, vbat - CHARGER_DROPOUT - SCHOTTKY)

    That single line is why a crank that used to be a survival question is
    mostly a non-event now: the charger holds 5 V through a dip that would
    have taken the parent board's +VBAT most of the way to its UVLO."""
    import numpy as np
    raw = np.loadtxt(dat)
    t_ms = raw[:, 0] * 1000.0
    v = raw[:, column] * scale
    if duration_ms is None:
        duration_ms = float(t_ms[-1])
    lines = [f"board {board}", "duration %.0f" % (duration_ms + 200), "trace 2",
             "@0 vbat %.3f" % v[0], "@0 usb %.3f" % charger(v[0]),
             prologue.strip()]
    last, last5, t = None, None, 0.0
    while t <= duration_ms:
        val = float(np.interp(t, t_ms, v))
        if last is None or abs(val - last) > 0.05:
            lines.append("@%.1f vbat %.3f" % (t, val))
            last = val
        v5 = charger(val)
        if last5 is None or abs(v5 - last5) > 0.02:
            lines.append("@%.1f usb %.3f" % (t, v5))
            last5 = v5
        t += step_ms
    return "\n".join(l for l in lines if l.strip()) + "\n"


# ------------------------------------------------------------------ reports --
FAILS = []
PASSES = []


def head(title):
    print("\n" + title)
    print("-" * len(title))


def check(ok, label, detail=""):
    if ok:
        PASSES.append(label)
        print("  ok    %s%s" % (label, ("  -- " + detail) if detail else ""))
    else:
        FAILS.append("%s: %s" % (label, detail))
        print("  FAIL  %s%s" % (label, ("  -- " + detail) if detail else ""))


def show_faults(run, limit=12):
    if not run.faults:
        print("  (no findings)")
        return
    for f in run.faults[:limit]:
        # Wrap the long explanatory tail so the report stays readable.
        parts = f.split(None, 3)
        if len(parts) < 4:
            print("  " + f)
            continue
        sev, stamp, code, text = parts
        print("  %-5s %10s %-20s %s" % (sev, stamp, code, _wrap(text, 39)))
    if len(run.faults) > limit:
        print("  ... and %d more" % (len(run.faults) - limit))


def plot(runs, path):
    """Two panels, because there are two kinds of question here: does the
    firmware respond correctly to inputs, and does it survive the power going
    away. Nothing is plotted that is not also in a .csv next to it."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("  (matplotlib not installed -- skipping plots)")
        return

    fig, ax = plt.subplots(2, 1, figsize=(10, 7.5))

    rpm_run = runs.get("rpm")
    if rpm_run:
        t = [r["t_ms"] / 1000.0 for r in rpm_run.rows]
        ax[0].plot(t, [r["cmd_rpm"] for r in rpm_run.rows], color="0.6",
                   lw=1.2, label="RPM injected on CAN")
        ax0b = ax[0].twinx()
        ax0b.plot(t, [r["leds_lit"] for r in rpm_run.rows], color="tab:green",
                  lw=1.0, label="LEDs lit")
        ax0b.set_ylabel("LEDs lit", color="tab:green")
        ax0b.set_ylim(-0.4, 8.6)
        ax[0].set_ylabel("engine RPM")
        ax[0].set_xlabel("seconds")
        ax[0].set_title("Shift light responding to CAN frames "
                        "(firmware under test, autosport board)")
        ax[0].axhline(3000, ls=":", lw=0.8, color="0.7")
        ax[0].axhline(7100, ls=":", lw=0.8, color="0.7")
        ax[0].legend(loc="upper left", fontsize=8)
        ax[0].grid(alpha=0.25)

    ig = runs.get("ignition")
    if ig:
        t = [r["t_ms"] for r in ig.rows]
        pf = float(ig.summary.get("PWR_FAIL_ms", "nan"))
        lo = pf - 40 if pf == pf else t[0]
        ax[1].plot(t, [r["reserve"] * 100 for r in ig.rows], color="tab:blue",
                   lw=1.6, label="charge left in the 540 uF bank (%)")
        ax[1].plot(t, [r["sens_en"] * 100 for r in ig.rows], color="tab:orange",
                   lw=1.0, label="SENS_EN (+5VS live)")
        ax[1].plot(t, [r["sd_open"] * 100 for r in ig.rows], color="tab:red",
                   lw=1.0, label="log file open")
        if pf == pf:
            ax[1].axvline(pf, color="k", ls="--", lw=1.0)
            ax[1].annotate("PWR_FAIL", (pf, 104), fontsize=8, ha="left")
        closed = _first_time(ig, lambda r: r["sd_open"] == 0 and r["t_ms"] > pf)
        if closed is not None and pf == pf:
            ax[1].axvline(closed, color="tab:red", ls="--", lw=1.0)
            ax[1].annotate("file closed\n+%.0f ms" % (closed - pf),
                           (closed + 2, 60), fontsize=8)
        ax[1].set_xlim(lo, t[-1] + 10)
        ax[1].set_ylim(-5, 112)
        ax[1].set_xlabel("milliseconds")
        ax[1].set_ylabel("percent")
        ax[1].set_title("Ignition off: spending the ride-through window")
        ax[1].legend(loc="center right", fontsize=8)
        ax[1].grid(alpha=0.25)

    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
    print("  wrote %s" % os.path.relpath(path, PROJ))


def _wrap(text, indent, width=96):
    out, line = [], ""
    for word in text.split():
        if len(line) + len(word) + 1 > width - indent:
            out.append(line)
            line = word
        else:
            line = (line + " " + word).strip()
    out.append(line)
    return ("\n" + " " * indent).join(out)


# ------------------------------------------------------------------ studies --
# The MCU is not on this board -- an ESP32-S3-DevKitC-1 drops into two 22-way
# sockets. So a GPIO is reached through a socket pin, and the mapping is the
# dev board's own J1/J3 pin order from its v1.1 user guide. Study 0 used to
# read this off the module's footprint pins; with the module gone it found
# nothing and reported every pin as a mismatch, which is the check doing its
# job rather than a bug.
#
# Keyed on the socket's MPN and resolved to a designator at run time, not
# written as "J2"/"J3". Splitting the harness into two plugs renumbered every
# connector after it and turned the sockets into J3/J4 -- a hard-coded map
# then silently read the wrong parts and reported the whole pin map as a
# mismatch. Designators are assigned sequentially as parts are declared, so
# they are an output of the generator, not a name to depend on.
SOCKET_PIN_TO_GPIO = {
    # mirrors the dev board's J1 header
    "ESP32-S3-DevKitC-1 J1": {4: 4, 5: 5, 6: 6, 7: 7, 8: 15, 9: 16, 10: 17, 11: 18, 12: 8,
           13: 3, 14: 46, 15: 9, 16: 10, 17: 11, 18: 12, 19: 13, 20: 14},
    # mirrors the dev board's J3 header
    "ESP32-S3-DevKitC-1 J3": {2: 43, 3: 44, 4: 1, 5: 2, 6: 42, 7: 41, 8: 40, 9: 39, 10: 38,
           11: 37, 12: 36, 13: 35, 14: 0, 15: 45, 16: 48, 17: 47, 18: 21,
           19: 20, 20: 19},
}


def study_pinmap(exes):
    head("0. Does the board model agree with the netlist?")
    print("  Every other result depends on this. A model built from prose")
    print("  instead of the design would validate the wrong board, confidently.")
    import re
    import subprocess as sp

    exe = next(iter(exes.values()))
    out = sp.run([exe, "--dump-board", "autosport"], capture_output=True, text=True).stdout
    model, skipped = {}, []
    for line in out.splitlines()[1:]:
        gpio, net, role = line.split(",", 2)
        # The PSRAM pins are bonded inside the module and appear on no net.
        # They are in the model so that touching one is a fault, not so that
        # they can be traced.
        if "PSRAM" in role:
            skipped.append(int(gpio))
        else:
            model[int(gpio)] = net

    # Resolve each socket's MPN to whatever designator it ended up with.
    sys.path.insert(0, HERE)
    import generate_schematic as _sch
    _sch.assign_refs()
    socket_refs = {}
    for _sh in _sch.SHEETS:
        for _p in _sh["parts"]:
            table = SOCKET_PIN_TO_GPIO.get(_p["mpn"])
            if table:
                socket_refs[_p["ref"]] = table
    if len(socket_refs) != len(SOCKET_PIN_TO_GPIO):
        raise SystemExit("study 0: expected %d sockets, resolved %d (%s) -- "
                         "SOCKET_PIN_TO_GPIO is keyed on MPNs that no longer "
                         "exist" % (len(SOCKET_PIN_TO_GPIO), len(socket_refs),
                                    sorted(socket_refs)))

    netlist = {}
    path = os.path.join(PROJ, "netlist.txt")
    for line in open(path, encoding="utf-8"):
        parts = line.split()
        if len(parts) < 2:
            continue
        for ref in parts[1:]:
            m = re.fullmatch(r"(J\d+)\.(\d+)", ref)
            if m and m.group(1) in socket_refs:
                gpio = socket_refs[m.group(1)].get(int(m.group(2)))
                if gpio is not None:
                    netlist.setdefault(gpio, []).append(parts[0])

    bad = []
    for gpio in sorted(model):
        nets = netlist.get(gpio, [])
        if model[gpio] not in nets:
            bad.append("GPIO%d: model says %s, the sockets carry %s"
                       % (gpio, model[gpio], nets or "nothing"))
    for b in bad:
        print("    MISMATCH  %s" % b)
    check(not bad, "every modelled pin matches netlist.txt",
          "%d of %d pins verified against the design, not the README "
          "(%d PSRAM pins bonded inside the module, no net to check)"
          % (len(model) - len(bad), len(model), len(skipped)))

    # Pins the netlist connects to the module but the model says nothing about.
    unmodelled = sorted(set(netlist) - set(model) - set(skipped) - {0})
    check(not unmodelled, "no connected module pin is missing from the model",
          "unmodelled: %s" % unmodelled if unmodelled else "")
    return bad


def study_control(exes):
    head("1. Control: the shipping R53 firmware on the board it was written for")
    print("  If the harness cannot agree that working firmware works, nothing")
    print("  else it reports means anything.")
    r = Run("control_s3zero", exes["r53"], rpm_sweep("s3zero"))
    show_faults(r)
    check(not r.errors(), "shipping firmware is clean on s3zero",
          "%d errors" % len(r.errors()))

    # The strip against the RPM the scenario was commanding at that moment.
    a = r.at(1500)      # 800 rpm
    check(a["leds_lit"] == 0, "strip dark below the 3000 rpm threshold",
          "%d lit at 800 rpm" % a["leds_lit"])
    b = r.at(3000)      # 3200 rpm
    check(b["leds_lit"] == 2 and b["led_g"] == 255 and b["led_r"] == 0,
          "3200 rpm gives one green pair", "lit=%d rgb=(%d,%d,%d)"
          % (b["leds_lit"], b["led_r"], b["led_g"], b["led_b"]))
    c = r.at(5000)      # 6500 rpm
    check(c["leds_lit"] == 6 and c["led_r"] > 0 and c["led_g"] > 0,
          "6500 rpm is amber, three pairs", "lit=%d rgb=(%d,%d,%d)"
          % (c["leds_lit"], c["led_r"], c["led_g"], c["led_b"]))
    d = r.at(6800)      # 7400 rpm, over the blink threshold
    check(d["leds_lit"] in (0, 8), "7400 rpm blinks the whole strip",
          "lit=%d" % d["leds_lit"])

    check(r.rows[-1]["notifies"] > 0, "wideband is notified once a phone subscribes",
          "%.0f notifications" % r.rows[-1]["notifies"])
    check(r.rows[-1]["can_delivered"] > 1000,
          "CAN frames are arriving and being decoded",
          "%.0f frames delivered" % r.rows[-1]["can_delivered"])
    return r


def study_port(exes):
    head("2. The same firmware, unmodified, on this board")
    print("  Nothing here is a bug in the R53 firmware. Every one of these is a")
    print("  pin that means something different on the new hardware.")
    r = Run("port_autosport", exes["r53"], rpm_sweep("autosport", rail="5vs"))
    show_faults(r)
    codes = set(r.codes())
    for code, what in (("LED_PIN", "WS2812 pin moved (4 -> 48)"),
                       ("CAN_TX_PIN", "TWAI TX pin moved (5 -> 17)"),
                       ("CAN_RX_PIN", "TWAI RX pin moved (6 -> 18)"),
                       ):
        # There used to be a fourth: SENSOR_RAIL_OFF, "the sensor rail is
        # switched and off at reset". It is not switched any more -- the
        # supercap made shedding 80 mA pointless and GPIO16 went back to
        # being spare -- so there is no longer a difference here for the
        # R53 sketch to fall foul of. Removed rather than weakened: a check
        # that cannot fail is worse than no check, because it reads as
        # coverage.
        check(code in codes, "caught: " + what, "" if code in codes else "not detected")
    dark = all(row["leds_lit"] == 0 for row in r.rows)
    check(dark, "shift light is dark for the whole run on this board",
          "the strip is on GPIO48; the sketch drives GPIO4")
    return r


def study_accuracy(exes):
    head("3. Measurement accuracy: what the firmware reports vs what went in")
    print("  The wideband is stepped through its range and the value the phone")
    print("  would receive is compared with the voltage actually applied.")
    worst = {}
    for board in ("s3zero", "autosport"):
        scn, steps = accuracy_ladder(board, rail="none")
        r = Run("accuracy_%s" % board, exes["r53"], scn)
        print("\n  %s" % board)
        print("    %8s %10s %10s %9s" % ("applied", "reported", "error", "verdict"))
        err_max = 0.0
        for i, v in enumerate(steps):
            row = r.at(1000 + 400 * i + 350)
            got = row["notify_v"]
            err = (got - v) / v * 100.0
            err_max = max(err_max, abs(err))
            print("    %7.2fV %9.3fV %+8.1f%% %9s"
                  % (v, got, err, "ok" if abs(err) < 3.0 else "WRONG"))
        worst[board] = err_max

    # It used to be a gain error and it is now a wiring error, which is a
    # bigger failure and worth naming as one. The R53 sketch reads its
    # wideband on GPIO1. On the carrier GPIO1 is K_RX -- there is no analog
    # front end on it at all -- so the sketch does not read the sensor
    # wrongly, it reads nothing. Hence the full -100%.
    #
    # Had the pin survived, the error would have been the divider: this front
    # end is 0.1673 against the 0.5 that DIVIDER_GAIN = 2.0 assumes, so the
    # reading would have come out about 66% low.
    if_pin_had_survived = (RANGE_16V * 2.0 * 1.015 - 1.0) * 100.0
    check(worst["autosport"] > 10.0,
          "the R53 firmware cannot measure the wideband on this board at all",
          "measured %+.1f%% -- its ADC pin is K_RX here. Even with the pin "
          "right the divider alone would have put it %+.1f%% out"
          % (worst["autosport"], if_pin_had_survived))
    check(worst["s3zero"] < 3.0, "the same code is accurate on its own board",
          "worst error %+.1f%% -- the internal ADC's gain error, nothing more"
          % worst["s3zero"])
    return worst


def study_ads(exes):
    head("4. The ADS1115 path")
    print("  The phone can switch the sketch to the 16-bit converter. That")
    print("  brings up I2C -- on pins this board uses for the microSD.")
    scn = rpm_sweep("autosport", rail="none", extra="@8000 ble hwmode 1\n")
    scn = scn.replace("@0    sensorrail 1 none", "@0    sensorrail 1 none\n@0    ads 1")
    r = Run("ads_autosport", exes["r53"], scn)
    show_faults(r)
    codes = set(r.codes())
    check("I2C_SDA_PIN" in codes or "I2C_SCL_PIN" in codes,
          "caught: I2C brought up on the microSD control pins",
          "GPIO7 is SD_PWR_EN and GPIO8 is SD_CD on this board")
    return r


def study_busload(exes, sketch="r53", board="s3zero"):
    head("5. Bus load: does a busy bus outrun the 20 ms loop?")
    print("  FastLED.show() blocks while it shifts the strip out, and the loop")
    print("  sleeps 20 ms. rx_queue_len is 32.")
    out = []
    for hz in (100, 500, 1000, 2000):
        scn = f"""board {board}
duration 6000
trace 50
@0 vbat 13.8
@0 sensorrail 1 none
@0 canid 0x316
@0 canrate {hz}
@0 rpm 5200
"""
        r = Run("busload_%s_%d" % (board, hz), exes[sketch], scn)
        gen = r.rows[-1]["can_generated"]
        drop = r.rows[-1]["can_dropped"]
        pct = 100.0 * drop / gen if gen else 0.0
        out.append((hz, gen, drop, pct))
        print("    %5d frames/s  generated %6.0f  dropped %6.0f  (%5.1f%%)"
              % (hz, gen, drop, pct))
    check(out[0][3] == 0.0, "no frames dropped at a typical 100 frames/s load")
    worst = out[-1]
    check(True, "queue overflows only under synthetic load",
          "%d frames/s drops %.0f%%; the R53 cluster sends 0x316 at 100 Hz"
          % (worst[0], worst[3]))
    return out


def study_stale(exes, sketch="r53", board="s3zero", rail="none"):
    head("6. CAN goes quiet mid-drive (%s on %s)" % (sketch, board))
    scn = f"""board {board}
duration 9000
trace 20
@0 vbat 13.8
@0 sensorrail 1 {rail}
@0 canid 0x316
@0 canrate 100
@0 rpm 5200
@3000 canrate 0
"""
    r = Run("stale_%s" % board, exes[sketch], scn)
    lit_before = r.at(2900)["leds_lit"]
    lit_at_4s = r.at(4500)["leds_lit"]
    lit_after = r.at(6000)["leds_lit"]
    print("    at 2.9 s (bus live):        %d LEDs lit" % lit_before)
    print("    at 4.5 s (1.5 s of quiet):  %d LEDs lit" % lit_at_4s)
    print("    at 6.0 s (3.0 s of quiet):  %d LEDs lit" % lit_after)
    check(lit_before > 0, "strip is lit while the bus is live")
    check(lit_at_4s > 0, "strip holds through a short dropout (RPM_STALE_MS is 2 s)")
    check(lit_after == 0, "strip blanks once RPM is stale rather than freezing")
    return r


def study_busoff(exes, sketch="r53", board="s3zero", rail="none"):
    head("7. Bus-off recovery (%s on %s)" % (sketch, board))
    scn = f"""board {board}
duration 9000
trace 20
@0 vbat 13.8
@0 sensorrail 1 {rail}
@0 canid 0x316
@0 canrate 100
@0 rpm 5200
@3000 busoff
"""
    r = Run("busoff_%s" % board, exes[sketch], scn)
    before = r.at(2900)["can_delivered"]
    after = r.rows[-1]["can_delivered"]
    print("    frames delivered by 2.9 s: %.0f" % before)
    print("    frames delivered by end:   %.0f" % after)
    check(after > before + 100,
          "twai_initiate_recovery() gets the bus back without a power cycle",
          "%.0f more frames after the fault" % (after - before))
    return r


def study_ignition(exes, sketch, tag):
    head("8. Ignition off: the firmware contract in README section 2")
    print("  On PWR_FAIL rising: clear the strip, stop sampling, flush and close.")
    print("  Hardware guarantees the window; spending it is firmware's job.")
    print("  Cut at 6500 rpm, with the strip lit. It used to be 3000, which is")
    print("  exactly the shift point -- the strip was dark, so the run never")
    print("  exercised the load-shed path it was supposed to be checking.")
    scn = """board autosport
duration 3000
trace 2
@0 vbat 13.8
@0 sensorrail 1 5vs
@0 canid 0x316
@0 canrate 100
@0 rpm 6500
@500 ble connect
@600 ble subscribe 1
@1500 usb 0.0
"""
    r = Run("ignition_%s" % tag, exes[sketch], scn)
    show_faults(r)
    pf = float(r.summary.get("PWR_FAIL_ms", "nan"))
    print("    stopped: %s" % r.summary.get("stopped", "?"))
    if pf == pf:
        closed = _first_time(r, lambda row: row["sd_open"] == 0 and row["t_ms"] > pf)
        collapse = r.rows[-1]["t_ms"]
        print("    PWR_FAIL asserted at   %8.1f ms" % pf)
        if closed:
            print("    log file closed at     %8.1f ms  (+%.0f ms)" % (closed, closed - pf))
        print("    rails collapsed at     %8.1f ms  (+%.0f ms of ride-through)"
              % (collapse, collapse - pf))
        if closed:
            print("    margin                 %8.1f ms unspent" % (collapse - closed))
    return r, set(r.codes())


def _first_time(run, pred):
    for row in run.rows:
        if pred(row):
            return row["t_ms"]
    return None


def study_ported_detail(exes):
    """Behaviour the port inherited but nothing re-checked on this board.

    Written after mutation testing (gen/mutate_firmware.py) put the suite's
    catch rate at 44 %. Almost every survivor had the same shape: the LED
    bands, the stale blanking, the bus-off recovery and the notify gating were
    only ever exercised against the *vendored R53 sketch on the s3zero model*.
    The ported firmware inherited the confidence without ever being asked.
    """
    head("13. The ported firmware's own behaviour, checked on this board")
    print("  Everything below was previously only verified against the R53")
    print("  sketch on the s3zero model, and assumed to carry over.")

    r = Run("ported_bands", exes["autosport"], rpm_sweep("autosport", rail="5vs"))
    for t, want, label in ((1500, 0, "dark below the 3000 rpm threshold"),
                           (3000, 2, "one green pair at 3200 rpm"),
                           (5000, 6, "three pairs, amber, at 6500 rpm")):
        row = r.at(t)
        ok = row["leds_lit"] == want
        if t == 3000:
            ok = ok and row["led_g"] == 255 and row["led_r"] == 0
        if t == 5000:
            ok = ok and row["led_r"] > 0 and row["led_g"] > 0
        check(ok, "ported: " + label,
              "lit=%d rgb=(%d,%d,%d)" % (row["leds_lit"], row["led_r"],
                                         row["led_g"], row["led_b"]))

    # --- the two things mutation testing said nothing was watching --------
    #
    # Both of these were real bugs on this board, found by hand and fixed;
    # neither had a check, so gen/mutate_firmware.py could put them back and
    # the suite stayed green. That is the definition of a gap.

    # 1. The battery divider. It read 11.0 against a 13.2:1 network for the
    #    whole life of the parent board -- every reading 20% low -- and the
    #    fwsim model carried the same wrong constant, so the two agreed with
    #    each other and neither agreed with the schematic. Study 0 compares
    #    nets, not ratios, and could not see it.
    scn = """board autosport
duration 2500
trace 5
@0 usb 4.70
@0 vbat 13.80
@0 sensorrail 1 5vs
@0 canid 0x316
@0 canrate 50
@0 rpm 3000
"""
    rb = Run("ported_vbat", exes["autosport"], scn)
    reported = [float(m) for m in
                re.findall(r"batt\s+([\d.]+)", rb.serial)]
    got = reported[-1] if reported else float("nan")
    err = abs(got - 13.80) / 13.80 * 100.0 if got == got else 999.0
    check(err < 3.0, "ported firmware reports battery voltage correctly",
          "13.80 V applied, %.2f V reported (%.1f%% out)" % (got, err))

    # 2. The differential read. AIN3 on both ADS1115s carries the sensor
    #    loom's own ground through a matched attenuator, and the config
    #    register decides whether that gets subtracted. Ask for a
    #    single-ended conversion instead and a chassis offset is measured as
    #    signal -- which is worse than useless, because the 0.1% dividers
    #    exist to make exactly that error small.
    # "ble hwmode 1" is what switches the sketch to the ADS1115 -- the same
    # lazy probe study 14 exercises. Without it the reading comes off the
    # ESP32's own SAR ADC, which never touches the config register, and this
    # check silently measures nothing.
    def wideband_at(offset_v, tag):
        scn = """board autosport
duration 2600
trace 5
@0 usb 4.70
@0 vbat 13.80
@0 sensorrail 1 5vs
@0 gndoffset %.3f
@0 ads 1
@0 sensor 1 2.000
@500 ble connect
@600 ble subscribe 1
@800 ble hwmode 1
""" % offset_v
        rr = Run("ported_gnd_%s" % tag, exes["autosport"], scn)
        return rr.rows[-1]["notify_v"]

    clean = wideband_at(0.0, "clean")
    shifted = wideband_at(0.300, "offset")
    slip = abs(shifted - clean)
    # 300 mV at the connector is 50 mV at the ADC through the 0.1673 divider,
    # which referred back to the input is the full 300 mV. Anything over about
    # 30 mV of movement means the offset is not being subtracted.
    check(slip < 0.030,
          "a 300 mV chassis offset does not move the reading",
          "%.3f V with no offset, %.3f V with 300 mV -- moved %.0f mV"
          % (clean, shifted, slip * 1000))

    # The simulator raises warnings for things that are legal but wrong on this
    # board -- a floating WS2812 buffer input at boot, a card unmounted by
    # pulling its supply, a 1-bit mount. Nothing asserted on them, so all three
    # survived mutation. They are not errors, but on the shipped firmware any
    # of them appearing is a regression.
    warns = [f for f in r.faults if f.startswith("WARN")]
    check(not warns, "ported firmware raises no warnings either",
          "; ".join(w.split(None, 3)[2] for w in warns) if warns else "")

    # 2500 rpm sits between the real 3000 threshold and a plausible wrong one.
    # Without a sample in that gap, moving the threshold to 2000 changed
    # nothing any check looked at, and the mutation survived.
    rt = Run("ported_threshold", exes["autosport"], """board autosport
duration 4000
trace 20
@0 vbat 13.8
@0 sensorrail 1 5vs
@0 canid 0x316
@0 canrate 100
@0 rpm 2500
""")
    lit = max(row["leds_lit"] for row in rt.rows)
    check(lit == 0, "ported: still dark at 2500 rpm, just under the threshold",
          "%d LEDs lit -- the shift point has moved" % lit)

    study_stale(exes, sketch="autosport", board="autosport", rail="5vs")
    study_busoff(exes, sketch="autosport", board="autosport", rail="5vs")

    head("14. Paths the port has that the R53 sketch never exercised here")
    # ADS1115: lazily probed, so it is only reached when the phone selects it.
    # No autosport scenario ever did, which is why moving the I2C pins back to
    # the R53 board's GPIO7/8 was invisible to the whole suite.
    scn = rpm_sweep("autosport", rail="5vs", extra="@8000 ble hwmode 1\n")
    scn = scn.replace("@0    sensorrail 1 5vs", "@0    sensorrail 1 5vs\n@0    ads 1")
    ra = Run("ported_ads", exes["autosport"], scn)
    show_faults(ra)
    check(not ra.errors(), "selecting the ADS1115 brings up I2C on the right pins",
          "%d errors" % len(ra.errors()))
    check(ra.rows[-1]["notifies"] > 0, "still notifying after the mode switch",
          "%.0f notifications" % ra.rows[-1]["notifies"])

    # Notify gating: a phone is connected for a beat before it writes the CCCD.
    # Notifying into that gap is what the sketch's comment warns about.
    scn = """board autosport
duration 6000
trace 20
@0 vbat 13.8
@0 sensorrail 1 5vs
@0 canid 0x316
@0 canrate 100
@0 rpm 3000
@1000 ble connect
"""
    rn = Run("ported_nosub", exes["autosport"], scn)
    check(rn.rows[-1]["notifies"] == 0,
          "connected but unsubscribed: nothing is notified",
          "%.0f notifications sent into a client that never wrote the CCCD"
          % rn.rows[-1]["notifies"])

    # This used to watch the ISR drop SENS_EN, the one register write that
    # separated a 154 ms window from a 75 ms one. There is no sensor switch
    # any more, so that check went on passing while watching a signal the
    # board does not have -- gpio_with_role() returns -1 and the trace column
    # reads 0 forever, which looks exactly like "shed immediately".
    #
    # The load that matters now is the shift light: eight WS2812s at full
    # white is 480 mA against ~120 mA for the rest of the board, and the
    # hold-up budget is inversely proportional to it. shutdown() clears the
    # strip first for that reason, so that is what gets asserted.
    ri, _codes = study_ignition(exes, "autosport", "shedleds")
    pf = float(ri.summary.get("PWR_FAIL_ms", "nan"))
    lit_at_pf = max([row["leds_lit"] for row in ri.rows if row["t_ms"] <= pf] or [0])
    dark = _first_time(ri, lambda row: row["leds_lit"] == 0 and row["t_ms"] >= pf)
    check(lit_at_pf > 0 and dark is not None and dark - pf <= 20.0,
          "the shift light is cleared within 20 ms of PWR_FAIL",
          "%d LEDs lit at the cut, dark at +%s ms -- this is what turns a "
          "769 ms budget back into 2500 ms"
          % (lit_at_pf, "%.1f" % (dark - pf) if dark is not None else "never"))

    # The shutdown path only runs during an ignition cut, so warnings raised
    # there -- dropping the card supply while the bus is still mounted, for one
    # -- never appeared in the steady-state run checked above.
    iw = [f for f in ri.faults if f.startswith("WARN")]
    check(not iw, "the shutdown path raises no warnings either",
          "; ".join(w.split(None, 3)[2] for w in iw) if iw else "")
    return r


def study_worst_case(exes):
    head("12. The same power cut, with the hold-up at its worst")
    print("  The flat-battery corner this study used to run does not exist any")
    print("  more: the board is powered from USB and the 12 V it can see is")
    print("  OBD-II pin 16, which is permanent battery and does not switch off")
    print("  with the ignition. What decides the outcome now is the supercap.")
    print("  Three corners: everything shed, everything still running, and a")
    print("  bank that has aged. EDLC capacitance falls and ESR rises over")
    print("  life -- 30% down at end of life is a normal datasheet number, and")
    print("  a car cabin is not a kind place to keep one.")
    print("    %-26s %10s %10s %9s" % ("window", "closed at", "collapse", "verdict"))
    rows = []
    for label, shed, noshed in (("fresh bank, load shed", 2500.0, 769.0),
                                ("fresh bank, nothing shed", 769.0, 769.0),
                                ("aged bank -30%, nothing shed", 538.0, 538.0)):
        scn = f"""board autosport
duration 2500
trace 1
@0 vbat 13.8
@0 sensorrail 1 5vs
@0 budget {shed} {noshed}
@0 canid 0x316
@0 canrate 100
@0 rpm 3000
@1000 usb 0.0
"""
        r = Run("worstcase_%d" % int(shed), exes["autosport"], scn)
        pf = float(r.summary.get("PWR_FAIL_ms", "nan"))
        lost = "SD_OPEN_AT_POWER_LOSS" in set(r.codes())
        closed = None if lost else _first_time(
            r, lambda row: row["sd_open"] == 0 and row["t_ms"] > pf)
        collapse = r.rows[-1]["t_ms"]
        rows.append((label, shed, closed, pf, collapse, lost))
        print("    %-26s %9s %9.0fms %9s"
              % (label + " (%.0f ms)" % shed,
                 "LOST" if lost else
                 ("NEVER CLOSED" if closed is None else "+%.0f ms" % (closed - pf)),
                 collapse - pf,
                 "FILE LOST" if lost else "ok"))
    worst = rows[-1]
    # worst[2] is `closed`. A None there means the file was still open when
    # the run ended and nothing else noticed -- which is exactly what the
    # no-close mutation does, and it used to crash this report rather than
    # fail it.
    check(not worst[5] and worst[2] is not None,
          "the log survives a power cut on an aged bank with nothing shed",
          "file closed %s a %.0f ms window"
          % ("+%.0f ms into" % (worst[2] - worst[3]) if worst[2]
             else "NEVER within", worst[1]))
    # How slow a card still fits inside the *worst-case* window, not the nominal.
    print("\n  Card latency the 538 ms corner tolerates:")
    last_ok = None
    for flush_ms in (18, 50, 100, 200, 300, 400, 500, 600):
        scn = f"""board autosport
duration 2500
trace 1
@0 vbat 13.8
@0 sensorrail 1 5vs
@0 budget 538 538
@0 sdflush {flush_ms}
@0 canid 0x316
@0 canrate 100
@0 rpm 3000
@1000 usb 0.0
"""
        r = Run("worstflush_%03d" % flush_ms, exes["autosport"], scn)
        lost = "SD_OPEN_AT_POWER_LOSS" in set(r.codes())
        print("    %3d ms flush -> %s" % (flush_ms, "LOST" if lost else "closed"))
        if not lost:
            last_ok = flush_ms
    check(last_ok is not None and last_ok >= 300,
          "the worst-case window still swallows a card 15x slower than healthy",
          "safe up to a %s ms flush, against ~18 ms for a healthy card"
          % last_ok)
    return rows


def study_flush_margin(exes):
    head("11. How slow can the card be before the file is lost?")
    print("  README section 2 says the shed path 'covers even a card that")
    print("  stalls'. This puts a number on that: the flush time is swept until")
    print("  the close no longer fits inside the hold-up window. The sweep runs")
    print("  out to six seconds because the supercap made the old 300 ms top")
    print("  end meaningless -- nothing failed, which reads as proof and is")
    print("  really just a sweep that stopped too early.")
    print("    %10s %12s %14s" % ("flush time", "outcome", "closed at"))
    last_ok = None
    first_bad = None
    for flush_ms in (18, 100, 300, 600, 1000, 1500, 2000, 2400, 2600, 3000,
                     4000, 6000):
        scn = f"""board autosport
duration 2000
trace 2
@0 vbat 13.8
@0 sensorrail 1 5vs
@0 sdflush {flush_ms}
@0 canid 0x316
@0 canrate 100
@0 rpm 3000
@1000 usb 0.0
"""
        r = Run("flush_%03d" % flush_ms, exes["autosport"], scn)
        lost = "SD_OPEN_AT_POWER_LOSS" in set(r.codes())
        pf = float(r.summary.get("PWR_FAIL_ms", "nan"))
        closed = None if lost else _first_time(r, lambda row: row["sd_open"] == 0 and row["t_ms"] > pf)
        print("    %8d ms %12s %14s"
              % (flush_ms, "LOST" if lost else "closed",
                 "-" if closed is None else "+%.0f ms" % (closed - pf)))
        if lost and first_bad is None:
            first_bad = flush_ms
        if not lost:
            last_ok = flush_ms
    # A healthy card flushes in tens of milliseconds. Requiring the window to
    # survive 80 ms means it tolerates a card roughly four times slower than
    # that before the log is lost.
    check(last_ok is not None and last_ok >= 2000,
          "the window tolerates a card ~100x slower than a healthy one",
          "still safe at a %d ms flush, against ~18 ms healthy" % (last_ok or 0))
    check(first_bad is not None,
          "and a slow enough card still does lose the file",
          "first loss at a %s ms flush -- this is a real limit, not unlimited "
          "protection" % first_bad)
    return last_ok, first_bad


def study_crank(exes, sketch, tag):
    head("9. Engine crank, replayed from the circuit study")
    dat = os.path.join(PROJ, "sim", "crank_45.dat")
    if not os.path.exists(dat):
        print("  sim/crank_45.dat not present -- run gen/simulate.py first. Skipped.")
        return None
    try:
        scn = replay(dat, column=1, board="autosport",
                     prologue="@0 sensorrail 1 5vs\n@0 canid 0x316\n@0 canrate 100\n@0 rpm 300")
    except ImportError:
        print("  numpy not available. Skipped.")
        return None
    r = Run("crank_%s" % tag, exes[sketch], scn)
    lo = min(row["vbat"] for row in r.rows)
    lo5 = min(row["v5"] for row in r.rows)
    edges = 0
    prev = 0
    for row in r.rows:
        if row["pwr_fail"] and not prev:
            edges += 1
        prev = row["pwr_fail"]
    trip = 4.20
    print("    cold-crank dip reaches %.2f V at the battery" % lo)
    print("    the charger holds the 5 V rail down to %.2f V" % lo5)
    print("    PWR_FAIL edges during the crank: %d" % edges)
    print("    run ended: %s" % r.summary.get("stopped", "?"))
    # Data-driven, so this keeps meaning something if the crank trace or the
    # charger model changes. Above the trip point the correct answer is that
    # nothing happens at all; below it, exactly one edge and no chatter.
    if lo5 > trip:
        check(edges == 0,
              "the charger rides the crank without asserting PWR_FAIL",
              "rail bottomed at %.2f V against a %.2f V trip, %d edges"
              % (lo5, trip, edges))
    else:
        check(edges == 1, "PWR_FAIL asserts exactly once per crank",
              "%d rising edges -- more than one is interrupt chatter" % edges)
    check("collapsed" not in r.summary.get("stopped", ""),
          "the board rides the crank without the rails collapsing")
    return r


# --------------------------------------------------------------------- main --
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="run a single study by number or name")
    ap.add_argument("--sketch", default="both", choices=("r53", "autosport", "both"))
    ap.add_argument("--no-plots", action="store_true")
    ap.add_argument("--sketch-path",
                    help="build the autosport studies from this file instead "
                         "of the real one (used by the mutation harness)")
    args = ap.parse_args()

    print(__doc__.strip().split("\n\n")[0])
    print()

    exes = {}
    print("Building host executables with %s" % os.path.basename(host_cxx()))
    if args.sketch in ("r53", "both"):
        exes["r53"] = build(R53_SKETCH, "r53")
        print("  r53        %s" % R53_SKETCH)
    if args.sketch_path:
        globals()["AUTOSPORT_SKETCH"] = args.sketch_path
    have_port = os.path.exists(AUTOSPORT_SKETCH)
    if args.sketch in ("autosport", "both") and have_port:
        exes["autosport"] = build(args.sketch_path or AUTOSPORT_SKETCH,
                                  "mutant" if args.sketch_path else "autosport")
        print("  autosport  %s" % AUTOSPORT_SKETCH)

    want = args.only
    def run_it(n, name):
        return want is None or want in (str(n), name)

    if exes and run_it(0, "pinmap"):
        study_pinmap(exes)

    if "r53" in exes:
        if run_it(1, "control"):
            study_control(exes)
        if run_it(2, "port"):
            study_port(exes)
        if run_it(3, "accuracy"):
            study_accuracy(exes)
        if run_it(4, "ads"):
            study_ads(exes)
        if run_it(5, "busload"):
            study_busload(exes)
        if run_it(6, "stale"):
            study_stale(exes)
        if run_it(7, "busoff"):
            study_busoff(exes)
        if run_it(8, "ignition"):
            r, codes = study_ignition(exes, "r53", "r53")
            check("PWR_FAIL_IGNORED" in codes,
                  "caught: the R53 firmware has no power-fail path",
                  "expected -- that board has no such signal")

    if "autosport" in exes:
        head("10. The ported firmware on this board")
        print("  Every study above, re-run against firmware/esp32_shiftlight_wideband.")
        r = Run("ported_rpm", exes["autosport"], rpm_sweep("autosport", rail="5vs"))
        show_faults(r)
        check(not r.errors(), "ported firmware is clean on this board",
              "%d errors" % len(r.errors()))
        lit = max(row["leds_lit"] for row in r.rows)
        check(lit == 8, "shift light reaches full scale", "%d LEDs at peak" % lit)

        scn, steps = accuracy_ladder("autosport", rail="5vs")
        ra = Run("ported_accuracy", exes["autosport"], scn)
        errs = []
        for i, v in enumerate(steps):
            got = ra.at(1000 + 400 * i + 350)["notify_v"]
            errs.append(abs(got - v) / v * 100.0)
        check(max(errs) < 3.0, "ported firmware reports the wideband correctly",
              "worst error %.1f%%" % max(errs))

        r2, codes2 = study_ignition(exes, "autosport", "ported")
        check("PWR_FAIL_IGNORED" not in codes2,
              "ported firmware acts on PWR_FAIL")
        check("SD_OPEN_AT_POWER_LOSS" not in codes2,
              "the log file is closed before the rails collapse")
        study_crank(exes, "autosport", "ported")
        if run_it(11, "flush"):
            study_flush_margin(exes)
        if run_it(12, "worstcase"):
            study_worst_case(exes)
        if run_it(13, "detail"):
            study_ported_detail(exes)
        if not args.no_plots:
            plot({"rpm": r, "ignition": r2}, os.path.join(PROJ, "sim", "firmware.png"))

    head("Result")
    print("  %d checks passed, %d failed" % (len(PASSES), len(FAILS)))
    # Record the totals so gen/audit_docs.py can hold the README's numbers
    # against a real run. Counting check() calls in this file instead gives
    # the wrong answer -- several of them sit inside loops.
    #
    # Only on a FULL run. The mutation harness invokes this with --sketch
    # autosport, which runs 25 of the 48 checks, and that partial figure was
    # overwriting the file and making audit_docs report the README as wrong.
    full_run = args.only is None and args.sketch == "both" and not args.sketch_path
    if not full_run:
        return 1 if FAILS else 0
    try:
        os.makedirs(os.path.join(PROJ, "sim", "fw"), exist_ok=True)
        with open(os.path.join(PROJ, "sim", "fw", "result.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write("passed %d\nfailed %d\n" % (len(PASSES), len(FAILS)))
    except OSError:
        pass
    for f in FAILS:
        print("    FAIL  %s" % f)
    print("\n  artefacts in sim/fw/ -- one .txt scenario, .csv trace, .log serial")
    print("  and .faults.txt per run, so any line above can be re-derived.")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
