#!/usr/bin/env python3
"""
Mutation test: break the firmware on purpose and see what the suite notices.

  python gen/mutate_firmware.py [--only <name>] [--list]

`gen/simulate_firmware.py` passing means the firmware and the harness agree.
It does not mean the harness would object if the firmware were wrong -- a
suite of tautologies passes just as convincingly. The only honest measure is
to introduce defects and count how many come back.

Each mutation below is a real mistake somebody could make: a constant carried
over from the other board, a line deleted during a refactor, a pin transposed.
For each one the sketch is copied, the substitution applied, and the autosport
studies re-run against the mutant. A mutation that still passes is a hole in
the tests, not a harmless change.

A caught mutation prints which checks failed, so the output doubles as a map
of which test covers which behaviour.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.abspath(os.path.join(HERE, ".."))
SKETCH = os.path.join(PROJ, "firmware", "esp32_shiftlight_wideband", "src", "main.cpp")

# (name, what a human would call the mistake, find, replace)
MUTATIONS = [
    ("gain-from-other-board", "DIVIDER_GAIN left at the R53 board's 2.0",
     "static const float DIVIDER_GAIN = 5.9774f;",
     "static const float DIVIDER_GAIN = 2.0f;"),
    ("led-pin", "WS2812 pin left on GPIO4",
     "#define LED_PIN     48", "#define LED_PIN     4"),
    ("can-tx-pin", "TWAI TX left on GPIO5",
     "#define CAN_TX_PIN  GPIO_NUM_17", "#define CAN_TX_PIN  GPIO_NUM_5"),
    ("i2c-pins", "ADS1115 bus left on the R53 board's GPIO7/8",
     "static const int   ADS_SDA_PIN  = 10;",
     "static const int   ADS_SDA_PIN  = 7;"),
    ("ads-single-ended", "ADS1115 read single-ended, so the chassis offset "
     "is measured as signal",
     'if (!adsWriteReg(0x01, 0x1283)) return;',
     'if (!adsWriteReg(0x01, 0x4283)) return;'),
    ("sd-4bit", "card mounted in 4-bit mode, but only D0 is wired",
     'if (!SD_MMC.begin("/sdcard", true)) {',
     'if (!SD_MMC.begin("/sdcard", false)) {'),
    ("vbat-divider", "battery monitor left at the inherited 11.0 divisor",
     "static const float VBAT_DIVIDER = 13.195f;",
     "static const float VBAT_DIVIDER = 11.0f;"),
    ("no-pwrfail-isr", "power-fail interrupt never attached",
     "  attachInterrupt(digitalPinToInterrupt(PWR_FAIL_PIN), onPowerFail, RISING);",
     "  (void)0;  // attachInterrupt removed by mutation"),
    ("no-close", "shutdown() never closes the log file",
     "    logFile.close();\n    SD_MMC.end();\n    sdUp = false;",
     "    SD_MMC.end();\n    sdUp = false;"),
    ("no-led-shed", "shutdown() leaves the strip lit, so the hold-up budget "
     "stays at its 769 ms worst case instead of 2500 ms",
     "  FastLED.clear(true);", "  // strip left lit by mutation"),
    ("rpm-byte-swap", "RPM decoded big-endian instead of little",
     "uint16_t raw = msg.data[2] | ((uint16_t)msg.data[3] << 8);",
     "uint16_t raw = msg.data[3] | ((uint16_t)msg.data[2] << 8);"),
    ("shift-threshold", "shift light starts at 2000 rpm instead of 3000",
     "  if (rpm < 3000) {", "  if (rpm < 2000) {"),
    ("no-busoff-recovery", "bus-off is never recovered from",
     "    twai_initiate_recovery();", "    /* recovery removed by mutation */;"),
    ("no-stale-blank", "a dead bus leaves the last RPM on the strip forever",
     "#define RPM_STALE_MS 2000", "#define RPM_STALE_MS 200000"),
    ("notify-unsubscribed", "notifies without waiting for the CCCD write",
     "  if (bleReady && voltsCharacteristic != nullptr && voltsSubscribed &&",
     "  if (bleReady && voltsCharacteristic != nullptr &&"),
    ("sd-backfeed", "card supply dropped while the bus is still mounted",
     "    logFile.close();\n    SD_MMC.end();\n    sdUp = false;",
     "    logFile.close();\n    sdUp = false;"),
    ("led-boot-float", "GPIO48 not driven low before FastLED claims it",
     "  pinMode(LED_PIN, OUTPUT);\n  digitalWrite(LED_PIN, LOW);",
     "  // boot-low removed by mutation"),
]



def run_suite(sketch_path):
    """Run the autosport studies against one sketch. Returns (passed, failed, names)."""
    res = subprocess.run(
        [sys.executable, os.path.join(HERE, "simulate_firmware.py"),
         "--sketch", "autosport", "--sketch-path", sketch_path, "--no-plots"],
        capture_output=True, text=True, cwd=PROJ)
    out = res.stdout + res.stderr
    failed = re.findall(r"^  FAIL  (.+?)(?:  --|$)", out, re.M)
    m = re.search(r"(\d+) checks passed, (\d+) failed", out)
    if not m:
        return 0, -1, ["suite did not run: %s" % out.strip().splitlines()[-1:]]
    return int(m.group(1)), int(m.group(2)), [f.strip() for f in failed]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    if args.list:
        for name, desc, _f, _r in MUTATIONS:
            print("  %-22s %s" % (name, desc))
        return 0

    base_src = open(SKETCH, encoding="utf-8").read()
    tmpdir = tempfile.mkdtemp(prefix="fwmutate_")
    mutant = os.path.join(tmpdir, "main.cpp")

    print("Baseline (unmutated firmware)")
    shutil.copy(SKETCH, mutant)
    p0, f0, _ = run_suite(mutant)
    print("  %d passed, %d failed\n" % (p0, f0))
    if f0 != 0:
        print("  baseline is not clean -- fix that before reading anything below")
        return 1

    caught, survived, skipped = [], [], []
    print("%-24s %-8s %s" % ("mutation", "verdict", "checks that failed"))
    print("-" * 100)
    for name, desc, find, repl in MUTATIONS:
        if args.only and args.only != name:
            continue
        if find not in base_src:
            skipped.append((name, "pattern not found -- firmware changed?"))
            print("%-24s %-8s %s" % (name, "SKIP", "pattern not present in the sketch"))
            continue
        with open(mutant, "w", encoding="utf-8") as fh:
            fh.write(base_src.replace(find, repl, 1))
        p, f, names = run_suite(mutant)
        if f < 0:
            skipped.append((name, "did not build"))
            print("%-24s %-8s %s" % (name, "NOBUILD", "mutant did not compile"))
            continue
        if f > 0:
            caught.append((name, desc, names))
            print("%-24s %-8s %s" % (name, "caught", "; ".join(names)[:66]))
        else:
            survived.append((name, desc))
            print("%-24s %-8s %s" % (name, "SURVIVED", "-- nothing failed"))

    shutil.rmtree(tmpdir, ignore_errors=True)
    total = len(caught) + len(survived)
    print("\n%d of %d mutations caught (%.0f%%)"
          % (len(caught), total, 100.0 * len(caught) / total if total else 0))
    if survived:
        print("\nSURVIVING MUTATIONS -- each one is a gap in the suite:")
        for name, desc in survived:
            print("  %-24s %s" % (name, desc))
    for name, why in skipped:
        print("  skipped %-20s %s" % (name, why))
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
