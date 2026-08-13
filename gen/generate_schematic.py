#!/usr/bin/env python3
"""
Generate the KiCad schematic for the ESP32-S3 CAN + microSD automotive logger.

The netlist below is the single source of truth: every component lists its
pins and the net each pin lands on.  Symbol geometry and -- critically --
pin numbering come from the official KiCad symbol libraries, so pin numbers
are never hand-typed here.

Output: <project>/*.kicad_sch (hierarchical, one sheet per functional block),
        <project>/bom.csv, <project>/netlist.txt

Usage:  python3 gen/generate_schematic.py [--symbol-dir /usr/share/kicad/symbols]
"""

import argparse
import csv
import math
import os
import re
import uuid

# --------------------------------------------------------------------------
# S-expression reading (only what we need: pin geometry + raw symbol text)
# --------------------------------------------------------------------------

TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|\(|\)|[^\s()]+')


def parse_sexp(text):
    toks = TOKEN.findall(text)
    pos = 0

    def read():
        nonlocal pos
        tok = toks[pos]
        pos += 1
        if tok == "(":
            out = []
            while toks[pos] != ")":
                out.append(read())
            pos += 1
            return out
        return tok

    out = []
    while pos < len(toks):
        out.append(read())
    return out


def unquote(s):
    if s.startswith('"'):
        return s[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return s


def children(node, tag):
    return [c for c in node if isinstance(c, list) and c and c[0] == tag]


def _property_spans(text):
    """[(start, end)] of each top-level (property ...) block in a symbol."""
    spans = []
    depth, i, in_str = 0, 0, False
    start = None
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "(":
            if depth == 1 and start is None and text.startswith("(property ", i):
                start = i
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 1 and start is not None:
                spans.append((start, i + 1))
                start = None
        i += 1
    return spans


# The schematic format this generator emits, and the symbol-library format its
# embedded definitions were validated against.  KiCad opens older schematics and
# upgrades them on load, so emitting the 7.0 format is safe on 7, 8 and 9 alike.
SCH_FORMAT_VERSION = "20230121"          # KiCad 7.0
VALIDATED_SYMBOL_VERSION = "20241209"    # KiCad 9.0 symbol libraries


def find_symbol_dir():
    """Locate the stock KiCad symbol libraries on Windows, macOS or Linux."""
    import glob as _glob

    for var in ("KICAD9_SYMBOL_DIR", "KICAD8_SYMBOL_DIR", "KICAD7_SYMBOL_DIR",
                "KICAD6_SYMBOL_DIR"):
        path = os.environ.get(var)
        if path and os.path.isdir(path):
            return path

    patterns = [
        r"C:\Program Files\KiCad\*\share\kicad\symbols",
        r"C:\Program Files (x86)\KiCad\*\share\kicad\symbols",
        "/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols",
        "/usr/share/kicad/symbols",
        "/usr/local/share/kicad/symbols",
        os.path.expanduser("~/.local/share/kicad/*/symbols"),
    ]
    found = []
    for pat in patterns:
        found.extend(p for p in _glob.glob(pat) if os.path.isdir(p))
    if found:
        # Highest KiCad version wins when several are installed.
        return sorted(found)[-1]
    return None


class SymbolLibs:
    """Reads the installed KiCad symbol libraries."""

    def __init__(self, symbol_dir):
        self.symbol_dir = symbol_dir
        self._parsed = {}
        self._raw = {}
        self.lib_version = None

    def _load(self, lib):
        if lib in self._parsed:
            return
        path = os.path.join(self.symbol_dir, lib + ".kicad_sym")
        if not os.path.exists(path):
            raise SystemExit("symbol library not found: " + path)
        text = open(path, encoding="utf-8").read().replace("\r\n", "\n")
        root = parse_sexp(text)[0]
        ver = children(root, "version")
        if ver and self.lib_version is None:
            self.lib_version = unquote(ver[0][1])
        self._parsed[lib] = {unquote(s[1]): s for s in children(root, "symbol")}

        # Byte-exact source text for each top-level symbol, so the definitions
        # we embed in the schematic are identical to the library's.
        # One indent level is two spaces (7.0 libraries) or one tab (9.0).
        raw = {}
        for m in re.finditer(r'^(?:  |\t)\(symbol "([^"]+)"', text, re.M):
            start = m.start()
            depth, i, in_str = 0, start, False
            while i < len(text):
                ch = text[i]
                if in_str:
                    if ch == "\\":
                        i += 2
                        continue
                    if ch == '"':
                        in_str = False
                elif ch == '"':
                    in_str = True
                elif ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                i += 1
            raw[m.group(1)] = text[start:i]
        self._raw[lib] = raw

    def symbol(self, lib_id):
        lib, name = lib_id.split(":", 1)
        self._load(lib)
        if name not in self._parsed[lib]:
            raise SystemExit("symbol not found: " + lib_id)
        return self._parsed[lib][name]

    def has(self, lib_id):
        lib, name = lib_id.split(":", 1)
        if not os.path.exists(os.path.join(self.symbol_dir, lib + ".kicad_sym")):
            return False
        self._load(lib)
        return name in self._parsed[lib]

    def extends(self, lib_id):
        ext = children(self.symbol(lib_id), "extends")
        return unquote(ext[0][1]) if ext else None

    def properties(self, lib_id):
        out = {}
        for prop in children(self.symbol(lib_id), "property"):
            out[unquote(prop[1])] = unquote(prop[2])
        return out

    def raw(self, lib_id):
        """Library source for the symbol, renamed to its full lib_id.

        A schematic's lib_symbols cache cannot express `extends`: KiCad stores
        derived symbols flattened.  KiCad's own flattening keeps the parent's
        geometry but the child's property fields verbatim (positions and text
        effects included), so the splice must swap whole property blocks or
        ERC flags the cached copy as differing from the library's.
        """
        lib, name = lib_id.split(":", 1)
        self._load(lib)
        parent = self.extends(lib_id)
        if parent is None:
            text = self._raw[lib][name]
            return text.replace('(symbol "%s"' % name,
                                '(symbol "%s:%s"' % (lib, name), 1)

        text = self._raw[lib][parent]
        child = self._raw[lib][name]
        pspans = _property_spans(text)
        child_props = [child[a:b] for a, b in _property_spans(child)]
        if pspans and child_props:
            first, last = pspans[0][0], pspans[-1][1]
            indent = text[:first].rsplit("\n", 1)[-1]
            text = text[:first] + ("\n" + indent).join(child_props) + text[last:]
        # Sub-symbol names must follow the derived symbol, e.g. FOO_1_1.
        text = text.replace('(symbol "%s_' % parent, '(symbol "%s_' % name)
        text = text.replace('(symbol "%s"' % parent,
                            '(symbol "%s:%s"' % (lib, name), 1)
        return text

    def pins(self, lib_id):
        """[(number, name, local_x, local_y, angle, hidden)] resolving `extends`.

        Hidden pins matter: the ESP32 module symbol carries its GND pin 40 and
        exposed pad 41 as hidden pins that KiCad ties to GND by name.  We record
        them so the netlist documents the connection, but never draw a stub to
        an invisible pin.
        """
        parent = self.extends(lib_id)
        if parent:
            lib = lib_id.split(":", 1)[0]
            return self.pins(lib + ":" + parent)
        out = []
        for unit in children(self.symbol(lib_id), "symbol"):
            for pin in children(unit, "pin"):
                at = children(pin, "at")[0]
                num = unquote(children(pin, "number")[0][1])
                nam = unquote(children(pin, "name")[0][1])
                out.append((num, nam, float(at[1]), float(at[2]),
                            int(float(at[3])), "hide" in pin))
        return out


# --------------------------------------------------------------------------
# Geometry helpers
# --------------------------------------------------------------------------

def rotate(theta, vx, vy):
    """Rotate a sheet-space vector by the symbol placement angle (CCW, Y down)."""
    theta %= 360
    if theta == 0:
        return (vx, vy)
    if theta == 90:
        return (vy, -vx)
    if theta == 180:
        return (-vx, -vy)
    if theta == 270:
        return (-vy, vx)
    raise ValueError("unsupported rotation %r" % theta)


def pin_geometry(local_x, local_y, angle, theta):
    """Return (offset_from_origin, outward_unit_vector) in sheet space."""
    # Symbol space has Y up; the sheet has Y down.
    off = rotate(theta, local_x, -local_y)
    # A pin's `angle` points from its connection end *into* the body, so the
    # free (wire) direction is the opposite.
    out_ang = math.radians(angle + 180)
    d = rotate(theta, round(math.cos(out_ang)), -round(math.sin(out_ang)))
    return off, d


# Rails get a power symbol rather than a net-name label. It is what every
# schematic does, it makes a rail readable at a glance, and it removes the
# single largest source of labels on the page -- GND alone accounted for
# hundreds. Where KiCad has no stock symbol for a rail (+5VS, SD_VDD,
# OBD_VBAT)
# a generic one is instantiated and its Value overridden: for a power symbol
# KiCad takes the net name from the Value field, so the rail is named
# correctly and drawn correctly. gen/validate.py re-extracts the netlist
# through KiCad and compares it node-for-node, so if that were wrong the
# build would fail rather than quietly merge two rails.
RAILS = {
    "GND":      ("power:GND",   "GND"),
    "+3V3":     ("power:+3V3",  "+3V3"),
    "+5V":      ("power:+5V",   "+5V"),
    "+5VS":     ("power:+BATT", "+5VS"),
    # Not a rail the board runs on -- OBD-II pin 16, sense only. It is drawn
    # as a power symbol anyway because it behaves like one on the sheet:
    # one source, several consumers, no point wiring it.
    "OBD_VBAT": ("power:+BATT", "OBD_VBAT"),
    "SD_VDD":   ("power:+BATT", "SD_VDD"),
}


def power_placement(libs, lib_id, ex, ey, d):
    """Where to put a power symbol so its pin lands at (ex, ey) facing -d."""
    angle = libs.pins(lib_id)[0][4]
    want = (-d[0], -d[1])
    for theta in (0, 90, 180, 270):
        off, ds = pin_geometry(0.0, 0.0, angle, theta)
        if ds == want:
            return snap(ex - off[0]), snap(ey - off[1]), theta
    return snap(ex), snap(ey), 0


# --------------------------------------------------------------- wiring ----
# A net drawn as two name labels is correct and unreadable. Where a net has
# exactly two pins in the whole design and both are on one sheet, the two
# parts can simply be placed next to each other and joined with a wire --
# which is what the connection actually is. Everything else (rails, shared
# nodes, anything crossing a sheet) keeps a symbol or a label, because those
# genuinely are one-to-many.

def pin_count(net):
    n = 0
    for sh in SHEETS:
        for p in sh["parts"]:
            n += sum(1 for v in p["pins"].values() if v == net)
    return n


def wire_pairs(sh):
    """[(partA, pinA, partB, pinB, net)] for this sheet's two-pin nets."""
    here = [p for p in sh["parts"] if not p["prefix"].startswith("#")]
    out, used = [], set()
    for net in sorted({v for p in here for v in p["pins"].values()}):
        if net in RAILS or pin_count(net) != 2:
            continue
        ends = [(p, num) for p in here for num, v in p["pins"].items() if v == net]
        if len(ends) != 2:
            continue                      # the other end is on another sheet
        (pa, na), (pb, nb) = ends
        if pa["ref"] in used or pb["ref"] in used:
            continue                      # one cluster per part keeps it simple
        used.add(pa["ref"])
        used.add(pb["ref"])
        out.append((pa, na, pb, nb, net))
    return out


def label_rotation(direction):
    return {(1, 0): 0, (-1, 0): 180, (0, -1): 90, (0, 1): 270}[direction]


def label_justify(direction):
    """Which way the text runs from the anchor.

    Every label used to be justified left, so one pointing back at its own
    component ran its text straight over the body -- RON_5V across R4,
    SW_5V across L1. Text has to run away from the part, not into it.
    """
    return "right" if direction in ((-1, 0), (0, 1)) else "left"


def mm(v):
    v = round(v, 4)
    return ("%f" % v).rstrip("0").rstrip(".") or "0"


NS = uuid.UUID("6f1d7d1e-6d3e-5a2b-9c44-2b6f0f0a1c77")


def det_uuid(key):
    return str(uuid.uuid5(NS, key))


# --------------------------------------------------------------------------
# Board description
# --------------------------------------------------------------------------

PROJECT = "esp32s3-can-sd-logger"
TITLE = "ESP32-S3 CAN + microSD Automotive Logger"
REV = "B"
DATE = "2026-08-11"   # the date of the last electrical change, not of the
                      # last regeneration -- update it when the netlist moves
COMPANY = "geekopolis"

R0805 = "Resistor_SMD:R_0805_2012Metric"
C0805 = "Capacitor_SMD:C_0805_2012Metric"
C1206 = "Capacitor_SMD:C_1206_3216Metric"
SOT23 = "Package_TO_SOT_SMD:SOT-23"
SOT236 = "Package_TO_SOT_SMD:SOT-23-6"
# NOTE: confirm the exposed-pad size against TI's DDA package drawing before layout.
# TI DDA0008B (SO PowerPAD-8): pad max 2.71x3.4mm, TI land 2.95x4.9mm copper
# with a 2.71x3.4mm solder-mask-defined opening -- this footprint matches it.
SO8EP = ("Package_SO:SOIC-8-1EP_3.9x4.9mm_P1.27mm_"
         "EP2.95x4.9mm_Mask2.71x3.4mm_ThermalVias")
SOIC8 = "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"
SMC = "Diode_SMD:D_SMC"
SMA = "Diode_SMD:D_SMA"
SOD123 = "Diode_SMD:D_SOD-123"
LED0805 = "LED_SMD:LED_0805_2012Metric"
JST4 = "Connector_JST:JST_PH_B4B-PH-K_1x04_P2.00mm_Vertical"
JST10 = "Connector_JST:JST_PH_B10B-PH-K_1x10_P2.00mm_Vertical"
SOIC14 = "Package_SO:SOIC-14_3.9x8.7mm_P1.27mm"
XTAL4 = "Crystal:Crystal_SMD_3225-4Pin_3.2x2.5mm"
HDR3 = "Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical"
HDR4 = "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical"
HDR6 = "Connector_PinHeader_2.54mm:PinHeader_1x06_P2.54mm_Vertical"
# Sockets, not headers: the DevKitC-1 ships with male pins soldered pointing
# down, so the shield presents receptacles facing up and the dev board drops in.
SOCK22 = "Connector_PinSocket_2.54mm:PinSocket_1x22_P2.54mm_Vertical"
HDR8 = "Connector_PinHeader_2.54mm:PinHeader_1x08_P2.54mm_Vertical"
SOT235 = "Package_TO_SOT_SMD:SOT-23-5"
SOT233 = "Package_TO_SOT_SMD:SOT-23"   # TLV431 is a 3-pin SOT-23
SJ2 = "Jumper:SolderJumper-2_P1.3mm_Open_Pad1.0x1.5mm"
SJ2B = "Jumper:SolderJumper-2_P1.3mm_Bridged_Pad1.0x1.5mm"
SJ3 = "Jumper:SolderJumper-3_P1.3mm_Open_Pad1.0x1.5mm"
SJ3B12 = "Jumper:SolderJumper-3_P1.3mm_Bridged12_Pad1.0x1.5mm"
TP = "TestPoint:TestPoint_Pad_D1.5mm"
MH = "MountingHole:MountingHole_3.2mm_M3"

# Each entry: (lib_id, value, footprint, {pin: net}, mpn, note)
SHEETS = []


def sheet(name, filename, description):
    s = {"name": name, "file": filename, "desc": description, "parts": []}
    SHEETS.append(s)
    return s


_TOL_RE = re.compile(r"\s+(\d+(?:\.\d+)?\s*%)$")
_VOLT_RE = re.compile(r"\s+(\d+(?:\.\d+)?\s*V)$")


def split_value(value):
    """'100nF 16V' -> ('100nF', '16V', ''), '1k 0.1%' -> ('1k', '', '0.1%').

    A rating packed into the VALUE field is invisible to anything that
    parses the schematic: a review tool reading "1k 0.1%" sees no usable
    resistance and reports the part as unvalued, which is what happened to
    34 capacitors and 16 resistors here. Only a trailing voltage or
    tolerance token is peeled off, so "600R" and
    "0.2A PTC" are left exactly as they are.
    """
    tol = volt = ""
    m = _TOL_RE.search(value)
    if m:
        tol = m.group(1).replace(" ", "")
        value = value[:m.start()]
    m = _VOLT_RE.search(value)
    if m:
        volt = m.group(1).replace(" ", "")
        value = value[:m.start()]
    return value.strip(), volt, tol


# Part numbers for the generic passives and the two ferrites.
#
# These used to be left blank on the theory that JLC's order flow auto-matches
# an 0805 resistor from value + package, which it does. That stopped being good
# enough the moment the boards were going to be hand-assembled: buying the
# parts loose needs a real number for every line, not just the interesting
# ones. Every entry below is the part JLCPCB's own matcher chose on a live
# order run (2026-08-13), so ordering from LCSC gets the same component the
# assembly house would have fitted.
#
# Keyed on the Comment string export_fab.py writes, which is value + voltage +
# tolerance -- so 100nF100V and 100nF16V stay distinct. That distinction is
# load-bearing: the OBD 12 V decoupler must be the 100 V part, not the 16 V
# one that shares its value.
GENERIC_LCSC = {
    # Verified against JLCPCB's basic-parts list, 2026-08-12. Package is part
    # of the key -- see generic_lcsc() for why that is not optional.
    ("100", "1206"): "C17901",      # 1/4 W, the supercap charge resistor
    ("15pF50V", "0805"): "C1794",
    ("470nF50V", "0805"): "C13967",  # CL21B474KBFNNNE, X7R
    ("18k1%", "0805"): "C17506",
    ("20", "1206"): "C17955",       # 1/4 W, K-line series
    ("22k", "0805"): "C17560",
    ("43k1%", "0805"): "C17695",
    ("750", "1206"): "C17985",      # 1/4 W, K-line tester pull-up
    ("0466002.NR", "1206"): "C187595",
    ("100", "0805"): "C17408",
    ("100k", "0805"): "C149504",
    ("100k1%", "0805"): "C5713386",
    ("100nF100V", "0805"): "C28233",
    ("100nF16V", "0805"): "C49678",
    ("10k", "0805"): "C17414",
    ("10k0.1%", "0805"): "C856630",
    ("10nF100V", "0805"): "C128805",
    ("10uF100V", "1206"): "C6872041",
    ("10uF16V", "1206"): "C13585",
    ("12.7k1%", "0805"): "C2933300",
    ("121k", "0805"): "C17438",
    ("15k0.1%", "0805"): "C728642",
    ("1M", "0805"): "C17514",
    ("1k", "0805"): "C17513",
    ("1k0.1%", "0805"): "C864177",
    ("1nF50V", "0805"): "C46653",
    ("1uF16V", "0805"): "C28323",
    ("1uF50V", "0805"): "C28323",
    ("2.2k", "0805"): "C17520",   # 1%, and 2.2/13.2 is exactly 1/6
    ("2.2nF50V", "0805"): "C28260",
    ("2.49k", "0805"): "C2930178",
    ("20.5k", "0805"): "C2933365",
    ("22uF16V", "1206"): "C12891",
    ("27.4k1%", "0805"): "C2930188",
    ("270pF50V", "0805"): "C1732",
    ("31.6k", "0805"): "C49254250",
    ("33", "0805"): "C17634",
    ("4.7k", "0805"): "C17673",
    ("4.7nF50V", "0805"): "C1744",
    ("44.2k", "0805"): "C2960780",
    ("47k", "0805"): "C17713",
    ("5.1k", "0805"): "C27834",
    ("57.6k", "0805"): "C163405",
    ("60.4", "0805"): "C72998",
    ("742792022", "0805"): "C2661452",
    ("742792625", "1206"): "C1533835",
    ("8.2k", "0805"): "C17828",
    ("95.3k", "0805"): "C2930435",
}


_SIZE_RE = re.compile(r"_(0402|0603|0805|1206|1210|1812|2010|2512)_")


def package_size(footprint):
    """'Resistor_SMD:R_1206_3216Metric' -> '1206'. Empty if not a chip part."""
    m = _SIZE_RE.search(footprint)
    return m.group(1) if m else ""


def generic_lcsc(comment, mpn, footprint):
    """Look up a jellybean part number by value AND package.

    Keyed on package as well as value, which it was not. A 33 ohm resistor is
    a 33 ohm resistor, but C17634 is specifically an 0805 one -- and this
    board has 33 ohm on a 1206 land as well, for the K-line driver's fault
    current. Keyed on value alone, the 1206 position ordered the 0805 part:
    the right resistance, on a footprint it does not fit, on every board.
    Nothing downstream could have caught it. The fab BOM would have looked
    complete, JLC would have shipped, and it would have shown up at reflow.

    Returning "" is the safe failure. An unmatched line has no part number,
    export_order.py refuses to write it, and somebody has to go and look.
    """
    size = package_size(footprint)
    if not size:
        return ""
    for key in (comment, mpn, mpn.split()[-1] if mpn else ""):
        if key:
            hit = GENERIC_LCSC.get((key, size))
            if hit:
                return hit
    return ""


def part(sh, prefix, lib_id, value, footprint, pins, mpn="", note="", nc=(),
         lcsc=""):
    base, volt, tol = split_value(value)
    sh["parts"].append(
        {
            "prefix": prefix,
            "lib_id": lib_id,
            "value": base,
            "voltage": volt,
            "tolerance": tol,
            "footprint": footprint,
            "pins": pins,
            "mpn": mpn,
            "note": note,
            "nc": set(nc),
            # Fall back to the generic table, keyed the same way export_fab.py
            # builds its Comment column (value + voltage + tolerance).
            # ...or by bare MPN. export_fab.py drops the manufacturer from
            # the Comment ("Wurth 742792022" -> "742792022"), so match the
            # last token too.
            "lcsc": lcsc or generic_lcsc(base + volt + tol, mpn, footprint),
        }
    )


def R(sh, value, a, b, mpn="", note="", fp=R0805):
    part(sh, "R", "Device:R", value, fp, {"1": a, "2": b}, mpn, note)


def C(sh, value, a, b, fp=C0805, mpn="", note="", polarized=False):
    """`a` is pin 1. For a polarized part that is the + terminal, and the
    symbol must say so: a plain Device:C on an electrolytic land is right
    by luck rather than by construction, and neither ERC nor a reader can
    tell it from a reversed one."""
    lib = "Device:C_Polarized" if polarized else "Device:C"
    part(sh, "C", lib, value, fp, {"1": a, "2": b}, mpn, note)


def flag(sh, net):
    part(sh, "#FLG", "power:PWR_FLAG", "PWR_FLAG", "", {"1": net}, note="ERC power-source flag")


# ---------------------------------------------------------------- power ----
pw = sheet(
    "Rails + harness",
    "power.kicad_sch",
    "USB-derived rails from the dev board, supercap hold-up, OBD-II harness",
)

# ---------------------------------------------------------------------------
# There is no power conversion on this board any more.
#
# The parent generated its own +5V and +3V3 from the vehicle's 12 V with two
# LM5164 bucks behind an ideal-diode front end. On a shield that is 57 % of
# the component area (1746 mm^2 of 3052) for something the dev board already
# has: the DevKitC-1 takes 5 V from its USB-C socket and makes 3.3 V with its
# own LDO. So the shield consumes both rails and generates neither.
#
# Deleted with the converters: F1, FB1, D1 SMCJ40CA, U1 LM74700, Q1
# IPD068N10, C3/C6/C7 (the three electrolytic cans), U3/U4 LM5164 and every
# RON / FB / ripple-injection / bootstrap part around them, L1/L2, the +3V3
# zener, and the Q2/Q3 sensor-rail load switch.
#
# CURRENT BUDGET on the dev board's 5 V pin, which is USB VBUS behind the
# DevKitC-1's own Schottky:
#     TJA1051 transmitting dominant        ~50 mA
#     sensor excitation, 4 x 20 mA          ~80 mA
#     WS2812 shift light, one lit segment   ~60 mA  (all 8 white is ~480 mA)
#     dev board with the radio up          ~200 mA
# A 1.5 A charger covers that with room. All eight LEDs at full white does
# not -- PF1 below is the backstop, and the shift light only ever lights a
# few LEDs at a time in practice.
# ---------------------------------------------------------------------------

# OBD-II harness. The parent's 4-way carried +12V/GND/CAN_H/CAN_L because it
# was also the board's power feed. It is not any more, so the pins it frees
# go to the two things that need the vehicle side: the K-line and a real
# battery-voltage reading.
#
#   J1  OBD-II   what
#   1   16       battery + (permanent). SENSE ONLY -- see the note below
#   2   4/5      chassis / signal ground
#   3   6        CAN_H     bus 1, D-CAN on a BMW/MINI
#   4   14       CAN_L
#
#   J10 OBD-II   what
#   1   7        AUX_A     K-line, OR the second bus's CAN_H
#   2   -        AUX_B     unused in K-line mode, CAN_L for the second bus
#   3   4/5      ground
#   4   4/5      ground, so the aux pair can be twisted with a return
# TWO connectors, not one, and the reason is layout rather than wiring
# taste. CAN 1 hangs off the J1 socket row on the left of the board; the
# second bus and the K-line hang off J3 on the right. A single harness plug
# had to sit on one side or the other, and gen/audit_routes.py measured what
# that cost: 163 mm of CAN_H and 168 mm of CAN_L snaking the length of the
# board to reach a transceiver at the far corner. Two plugs, each beside the
# circuit it feeds, and both runs are short.
part(pw, "J", "Connector_Generic:Conn_01x04", "CAN1 + power harness", JST4,
     {"1": "OBD_VBAT", "2": "GND", "3": "CAN_H", "4": "CAN_L"},
     "JST B4B-PH-K-S(LF)(SN)",
     "OBD-II pins 16/4/6/14. Pin 1 is sense only -- the board is powered "
     "from the dev board's USB-C, not from here", lcsc="C131334")
part(pw, "J", "Connector_Generic:Conn_01x04", "Aux bus harness", JST4,
     {"1": "AUX_A", "2": "AUX_B", "3": "GND", "4": "GND"},
     "JST B4B-PH-K-S(LF)(SN)",
     "The second port. What these two pins carry is a solder-jumper choice: "
     "K-line on AUX_A (the default, which is what an R53 wants), or the "
     "second CAN pair. See AUXSEL and AUXCL on the CAN sheet", lcsc="C131334")

# OBD pin 16 is permanent battery, so it is live whenever the harness is
# plugged in, ignition or not. Nothing on this board is powered from it: it
# feeds a 100k-topped divider on the analog sheet (VBAT_SNS) and, if stuffed,
# the optional K-line pull-up. A 100k series resistor into a clamped node is
# inherently safe -- 36 V gives 0.36 mA and even a 100 V load dump gives 1 mA
# -- so the divider needs no fuse of its own. The parts below exist for the
# pull-up option, which is a 510 ohm load and does need protecting.
part(pw, "PF", "Device:Polyfuse", "0.2A PTC", "Resistor_SMD:R_1812_4532Metric",
     {"1": "OBD_VBAT", "2": "OBD_VBAT_F"}, "Bourns MF-MSMF020/60-2",
     "OBD-II pin 16 is PERMANENT battery -- live with the ignition off and "
     "the car parked -- so the thing being protected against is this board "
     "failing short across it, which without protection is a fire. A "
     "resettable PTC rather than a fuse: same job, and it is the same part "
     "as PF2 so it costs no extra BOM line. 60 V rated, which covers the "
     "42 V the TVS beside it clamps to. Load is at most 16 mA through the "
     "optional pull-up against a 200 mA hold",
     lcsc="C719178")
part(pw, "D", "Device:D_TVS", "SMAJ26CA", SMA, {"1": "OBD_VBAT_F", "2": "GND"},
     "Diodes Inc SMAJ26CA-13-F",
     "Clamps the OBD 12 V node. 400 W is enough here, not the parent's "
     "1500 W SMC: nothing downstream draws more than 25 mA, so there is no "
     "load-dump energy to carry into a converter -- only a clamp to hold",
     lcsc="C134976")
C(pw, "100nF 100V", "OBD_VBAT_F", "GND")

# ---- rails in from the dev board -----------------------------------------
# Both come UP through the sockets on the Dev board sheet, so this sheet only
# decouples and flags them. Nothing here drives either net; the PWR_FLAGs at
# the bottom are what stops ERC reporting them as undriven.
C(pw, "22uF 16V", "+5V", "GND", fp=C1206, note="Bulk at the 5 V socket pin")
C(pw, "100nF 16V", "+5V", "GND")
C(pw, "22uF 16V", "+3V3", "GND", fp=C1206, note="Bulk at the 3V3 socket pin")
C(pw, "100nF 16V", "+3V3", "GND")

# ---- hold-up, so the file still closes -----------------------------------
# The parent held 760 uF at 12 V and rode out 127 ms with the load shed. The
# same trick does not work down here and the arithmetic is worth writing
# down, because it is not obvious:
#
#   usable window   12.0 -> 7.0 V   (converter dropout)      = 5.0 V
#   usable window    4.2 -> 3.6 V   (DevKitC-1 LDO dropout)  = 0.6 V
#
# t = C dV / I, so at a 120 mA load 2000 uF of electrolytic buys 12.5 ms --
# less than one slow SD block write, which is the whole thing this has to
# outlast. Getting 50 ms would take 8000 uF, four cans, and we would be back
# to the footprint that made the parent board too big to be a shield.
#
# A supercapacitor gets there in less area than one of those cans. 0.165 F
# over the same 0.6 V window at 120 mA is 825 ms -- six times what the parent
# board shipped with, and still 65 % clear of the worst card stall the
# firmware studies model.
#
# LOW-ESR CELLS, NOT COIN TYPE. This is the part that bites: a 5.5 V coin
# EDLC has 30-200 ohm of ESR and physically cannot source 120 mA -- the rail
# would collapse the instant it was asked to. Cylindrical cells are tens of
# milliohms. Two 2.7 V cells in series, because 5.5 V single-package parts
# are all coin type.
for _ in range(2):
    part(pw, "C", "Device:C_Polarized", "0.33F 2.7V",
         "Capacitor_THT:CP_Radial_D8.0mm_P3.50mm",
         {"1": "SCAP_MID" if _ else "SCAP_TOP", "2": "GND" if _ else "SCAP_MID"},
         "Eaton HB1030-2R5105-R",
         "Hold-up cell %d of 2 in series -> 0.165 F at 5.4 V, which is "
         "825 ms with the strip shed. 1 F was the first pick and bought "
         "2500 ms that nothing needs: the parent board shipped 127 ms and "
         "that was judged adequate, a healthy card flushes in 18 ms, and the "
         "worst stall gen/simulate_firmware.py models is 500 ms. This is "
         "still six times the parent's margin and it is the most expensive "
         "line on the board -- see docs/COST.md. ESR must be under about "
         "1 ohm; cylindrical cells are around 200 mohm, coin-type EDLCs are "
         "30-200 OHM and will not work here" % (_ + 1))
# Series cells do not share voltage by themselves -- leakage current differs
# part to part and the leakier one ends up with less of the total, which
# means the other one ends up over its 2.7 V rating and dies slowly. 100k
# each swamps the leakage and forces a 50/50 split, at 27 uA standing.
R(pw, "100k", "SCAP_TOP", "SCAP_MID", note="Cell balancing, upper")
R(pw, "100k", "SCAP_MID", "GND", note="Cell balancing, lower")
# Charge through the resistor, discharge through the diode. Straight across
# the rail the bank looks like a dead short at plug-in: 0.5 F is 4 A into a
# USB port, which trips the charger and may well be read as a fault by it.
# 22 ohm holds the inrush to 210 mA and charges in about half a minute.
R(pw, "100", "+5V", "SCAP_TOP", fp="Resistor_SMD:R_1206_3216Metric",
  note="Inrush limit, and the value is set by the resistor's own rating "
       "rather than by the charge time. Charging 0.5 F to 4.7 V dissipates "
       "0.5*C*V^2 = 5.5 J in here whatever the resistance is; what changes "
       "is how fast. At 22 ohm the first instant is V^2/R = 1.0 W into a "
       "1206 rated 0.25 W, and a 1206's thermal time constant is far shorter "
       "than the 11 s the pulse would last -- it would cook. 100 ohm makes "
       "the peak 0.22 W, inside the rating, at the cost of a 50 s time "
       "constant: the hold-up is not fully armed for the first few minutes "
       "of a drive. A 2010 would buy back the speed if that ever matters")
# PIN 1 IS THE CATHODE on Device:D_Schottky, and this was the other way round
# until gen/audit_polarity.py said so. Reversed, it does both of its jobs
# backwards at once: it shorts out the 100 ohm charge resistor, putting 4 A of
# inrush into the USB port at plug-in, and it BLOCKS the discharge, so the
# bank can never hold the rail up and the entire hold-up circuit is inert.
# Nothing else on this board would have noticed -- ERC, DRC and the netlist
# compare are all happy with a diode pointing either way.
part(pw, "D", "Device:D_Schottky", "SS14", SMA, {"1": "+5V", "2": "SCAP_TOP"},
     "SS14", "Discharge path, bypassing the charge resistor. Anode on the "
     "bank, cathode on the rail: it conducts only once the rail has sagged a "
     "diode drop below the bank, which is exactly when the supply has gone "
     "away", lcsc="C2480")

# ---- power-fail detect ---------------------------------------------------
# Sensed on the 5 V rail itself. The parent could do better -- it sensed
# ahead of its ideal diode, so it saw the harness open before the bank had
# given up anything at all -- but there is no equivalent node here: USB VBUS
# is not brought out on the DevKitC-1, so the rail is the only thing to look
# at. That is affordable now only because the budget is seconds rather than
# milliseconds. Losing the first 100 ms of a 2500 ms window costs nothing.
#
# A bare divider into a GPIO still will not do, for the same reason as on the
# parent: the ESP32's input threshold is only specified between 0.25*VDD and
# 0.75*VDD, which is a 1.7 V window of "could be either". The shunt
# reference makes the trip point +/-1 %.
# 43k/18k, not the 28.7k/12.0k this started as. Same trip point to within a
# couple of millivolts -- 1.24 * 61/18 = 4.202 V against 4.204 -- but both of
# these are stocked 1% parts and 28.7k is not, so the pair costs nothing to
# buy and nothing to substitute later.
R(pw, "43k 1%", "+5V", "PFD_SENSE", note="Power-fail divider, upper leg")
R(pw, "18k 1%", "PFD_SENSE", "GND",
  note="Lower leg: trips at 1.24V * 61/18 = 4.202V. The rail sits at "
       "4.5-4.7V loaded (USB 5 V less the dev board's Schottky and the cable "
       "drop), and the LDO above holds 3.3 V down to about 3.6 V in, so 4.20 "
       "is inside the margin at both ends")
part(pw, "U", "Reference_Voltage:TL431DBZ", "TLV431A", SOT233,
     {"1": "PWR_FAIL", "2": "PFD_SENSE", "3": "GND"},
     "TLV431ASN1T1G",
     "Power-fail comparator. Below the trip point it stops conducting and "
     "R_pu takes PWR_FAIL high, so the interrupt is a rising edge and an "
     "absent or dead part reads as 'failing' rather than as 'fine'. "
     "PINOUT: this uses KiCad's TL431DBZ numbering (1 K, 2 REF, 3 A) on a "
     "plain SOT-23. Shunt references are NOT consistent between vendors in "
     "this package -- CONFIRM against the datasheet for the exact part "
     "ordered. If REF and A are swapped the part simply never conducts and "
     "PWR_FAIL sits permanently asserted: loud, harmless, and obvious on the "
     "bench, but it will not be caught by ERC or DRC", lcsc="C127592")
R(pw, "10k", "+3V3", "PWR_FAIL",
  note="Cathode pull-up. 10k gives the part 330uA, over the TLV431's 100uA "
       "minimum cathode current")
R(pw, "1M", "PWR_FAIL", "PFD_SENSE",
  note="Hysteresis. The sense node sees 43k||18k = 12.7k, so 3.3 V through "
       "1M moves it 42 mV, which is 142 mV referred to the rail. A USB rail is noisier than a "
       "battery and the trip point is only 300 mV below the working level, "
       "so this matters more here than it did on the parent")
C(pw, "1nF 50V", "PFD_SENSE", "GND",
  note="Keeps switching noise off the reference")

# ---- sensor excitation ---------------------------------------------------
# No load switch any more. Its job on the parent was to shed 80 mA so the
# ride-through lasted; with 2.5 s of budget there is nothing to shed, and
# dropping it frees GPIO16 as well as six parts. The protection stays --
# these wires run into an engine bay whatever powers them.
part(pw, "PF", "Device:Polyfuse", "0.2A PTC", "Resistor_SMD:R_1812_4532Metric",
     {"1": "+5V", "2": "VSENS_F"}, "Bourns MF-MSMF020/60-2",
     "Resettable: a shorted sensor wire trips this, not the dev board's USB "
     "port. 1812, NOT 1206: Bourns' MSMF series is an 1812 part and this "
     "footprint said 1206 all the way from the parent board -- a 4.5 x 3.2 mm "
     "body on a 3.2 x 1.6 mm land, which does not solder. Caught by checking "
     "the package field on the part number, not by any DRC",
     lcsc="C719178")
part(pw, "FB", "Device:L", "600R", "Inductor_SMD:L_0805_2012Metric",
     {"1": "VSENS_F", "2": "+5VS"}, "Wurth 742792022")
C(pw, "10uF 16V", "+5VS", "GND", fp=C1206)
part(pw, "D", "Device:D_Zener", "SMAJ6.0A", SMA, {"1": "+5VS", "2": "GND"},
     "Littelfuse SMAJ6.0A",
     "Clamps harness-injected transients on the sensor 5V. 6.0V standoff, "
     "not 5.0V: a 5.0V part on a 5.0V rail leaks up to 800uA continuously",
     lcsc="C223993")

part(pw, "D", "Device:LED", "green", LED0805, {"1": "PWR_LED_K", "2": "+3V3"},
     lcsc="C2297",  # KT-0805G, 525 nm emerald green, verified 2026-08-13
     note="Shield 3V3 is up, which means the dev board is in its socket the "
          "right way round and its LDO is running")
R(pw, "1k", "PWR_LED_K", "GND")

for net in ["+5V", "+3V3", "+5VS", "OBD_VBAT", "GND"]:
    part(pw, "TP", "Connector:TestPoint", net, TP, {"1": net})
# +5V and +3V3 arrive from the dev board through a connector, and OBD_VBAT
# from a connector as well. Connector pins are passive as far as ERC is
# concerned, so without these every rail on the board reads as undriven.
for net in ["GND", "+5V", "+3V3", "+5VS", "OBD_VBAT", "SD_VDD", "AGND_SENSE"]:
    flag(pw, net)
for _ in range(4):
    part(pw, "H", "Mechanical:MountingHole", "M3", MH, {})

# ------------------------------------------------------------------ MCU ----
mc = sheet("Dev board", "mcu.kicad_sch",
           "ESP32-S3-DevKitC-1 sockets, I2C/Qwiic, WS2812 buffer, rail break-out")

# ---------------------------------------------------------------------------
# The MCU is not on this board. An ESP32-S3-DevKitC-1 drops into these two
# 22-way sockets, which mirror its J1 and J3 headers pin for pin (v1.1 user
# guide). It carries the same ESP32-S3-WROOM-1 module the parent board had on
# it, so every GPIO assignment came across unchanged -- see
# docs/SHIELD-PLAN.md.
#
# PIN ASSIGNMENT IS A LAYOUT DECISION HERE, NOT A FREE CHOICE.
#
# Each socket is 22 through-holes on a 2.54 mm pitch. At a 1.3 mm pad the
# inner-layer voids leave about half a millimetre of copper between them, so
# each row cuts a 53 mm slot through every plane. A signal routed across that
# slot has no return path underneath it and has to go 100 mm round the end
# instead -- irrelevant for a card-detect switch, ruinous for a 40 MHz SD bus.
#
# So the pins are grouped by which SIDE of the board their circuit lives on:
#
#   J1 row, left of the board   analog front end + both ADS1115s (pins 4-7,
#                               12), power-fail (8), CAN 1 (9-11), I2C
#                               (15,16), microSD (17-20)
#   J3 row, right of the board  K-line (4,5), CAN 2's SPI bus (6-9,18),
#                               WS2812 (16), SD supply-enable (17)
#
# One net has to get from one side to the other: SD_PWR_EN, which is a load-
# switch gate and therefore DC. It routes AROUND the end of the row rather
# than through it, so nothing crosses a plane slot at all. The SD bus, both
# CAN pairs, the SPI bus and the four analog channels each stay on their own
# side by construction.
#
# Two constraints made this land with nothing spare. ADC1 is GPIO1-10 and
# ADC2 is unusable with the radio up, so the five analog pins must come from
# there; the SD bus eats IO9/IO10 and IO3 is a strapping pin, which leaves
# exactly IO4, IO5, IO6, IO7 and IO8 -- all on J1. That in turn pushes
# SD_CD and SD_PWR_EN onto J3, and they are the right two to exile: a
# mechanical switch and a load-switch gate.
#
# There is nothing spare left. All 24 usable pins are allocated: 15 on J1 and
# 9 on J3. IO1 and IO2 were the last two free and the second CAN's interrupt
# and the K-line took them.
#
# POWER: the shield consumes both rails and generates neither. 5 V comes UP
# from the dev board on J1 pin 21 (its USB VBUS, behind its own Schottky) and
# 3.3 V comes up on J1 pins 1-2, from its onboard LDO.
#
# This reverses the earlier decision to feed 5 V only and leave 3V3 open. That
# rule existed because the shield had its own +3V3 buck and tying the two
# would have paralleled two regulators onto one net. The buck is gone, so
# there is nothing to parallel -- both pins are now inputs to the shield.
#
# The sockets are receptacles facing up, because a DevKitC-1 ships with male
# headers already soldered pointing down. The dev board drops in; nothing has
# to be soldered to it.
part(mc, "J", "Connector_Generic:Conn_01x22", "DevKit J1", SOCK22,
     {
         "1": "+3V3", "2": "+3V3",   # its LDO output, feeding the shield
         # --- analog block, top of the row, all five ADC1 pins together ---
         "4": "AIN1", "5": "AIN2", "6": "AIN3", "7": "AIN4",
         "8": "PWR_FAIL",
         # --- CAN block ---
         "9": "CAN_S", "10": "CAN_TX", "11": "CAN_RX",
         "12": "VBAT_SNS",           # IO8, the fifth ADC1 pin
         # 13 (IO3) and 14 (IO46) are strapping pins and stay unconnected.
         # --- I2C, then the microSD bus, at the bottom of the row ---
         "15": "I2C_SCL", "16": "I2C_SDA",
         "17": "SD_CD",
         # 1-BIT SDMMC, not 4-bit. D1/D2/D3 no longer reach the MCU: their
         # three pins went to the second CAN controller's SPI bus, which
         # needed five and had two. 1-bit still moves about 1.5 MB/s against
         # this logger's sub-100 kB/s, and it does not touch the flush margin
         # -- that is the card's internal write time, not the bus width.
         # The card's own D1/D2/D3 keep their pull-ups; see the SD sheet.
         "18": "SD_D0", "19": "SD_CMD", "20": "SD_CLK",
         "21": "+5V", "22": "GND",
     },
     mpn="ESP32-S3-DevKitC-1 J1",
     note="Mirrors DevKitC-1 J1. Pins 1-2 (3V3) and 21 (5V) are both INPUTS "
          "to the shield. Analog on 4-7 and 12, CAN on 9-11, SD bus on 15-20 "
          "-- grouped so each block routes away from the row rather than "
          "across it. Pin 3 is RST, pins 13/14 are IO3/IO46, all left open.",
     nc=("3", "13", "14"))

part(mc, "J", "Connector_Generic:Conn_01x22", "DevKit J3", SOCK22,
     {
         "1": "GND",
         # 2, 3 are IO43/IO44, the dev board's own UART0 console -- left to it.
         # 4, 5 are IO1/IO2, the last two pins that were spare. K-line takes
         # them, and adjacent, so its FET and divider sit together.
         "4": "K_RX", "5": "K_TX",
         # 6, 7, 8, 9 are IO42/IO41/IO40/IO39 -- four contiguous pins for the
         # second CAN controller's SPI bus, which is the whole reason they
         # were kept together. All four are the ESP32-S3's EXTERNAL JTAG pins
         # (MTMS/MTDI/MTDO/MTCK), which sounds worse than it is: the S3
         # defaults to the USB-Serial-JTAG bridge on IO19/20 for debugging,
         # so these are ordinary GPIO unless an eFuse says otherwise.
         "6": "CAN2_CS", "7": "CAN2_MISO", "8": "CAN2_MOSI", "9": "CAN2_SCK",
         # 10 is IO38, which drives the DevKitC-1 v1.1's onboard addressable
         # RGB LED -- using it for I2C would flicker the LED on every
         # transaction and hang its input capacitance on the bus.
         # 11-13 are IO37/36/35, consumed by the module's octal PSRAM.
         # 14 is IO0 (BOOT) and 15 is IO45, both the dev board's business.
         "16": "LED_DIN_MCU",
         "17": "SD_PWR_EN",          # a load-switch gate; DC, so it may cross
         "18": "CAN2_INT",           # IO21
         # 19, 20 are IO20/IO19, wired to the dev board's native USB port.
         "21": "GND", "22": "GND",
     },
     mpn="ESP32-S3-DevKitC-1 J3",
     note="Mirrors DevKitC-1 J3. K-line on 4/5, the second CAN's SPI on "
          "6-9 with its interrupt on 18, WS2812 on 16. Pin 17 is SD_PWR_EN, "
          "the one net that crosses to the other side of the board, and it "
          "goes round the end of the row. Pin 10 (IO38) is left open because it drives the "
          "dev board's RGB LED on v1.1; check the revision, some put it on "
          "IO48, which is the WS2812 output here.",
     nc=("2", "3", "10", "11", "12", "13", "14", "15", "19", "20"))

# Decoupling for the shield's own 3V3 loads. The module's decoupling went with
# the module; this is what the ADS1115, the analog dividers and the SD
# pull-ups sit behind.
C(mc, "10uF 16V", "+3V3", "GND", fp=C1206)
C(mc, "100nF 16V", "+3V3", "GND")

part(mc, "J", "Connector_Generic:Conn_01x04", "I2C / Qwiic", HDR4,
     {"1": "GND", "2": "+3V3", "3": "I2C_SDA", "4": "I2C_SCL"},
     note="External I2C sensors. Kept because the dev board has no Qwiic "
          "connector of its own")
R(mc, "4.7k", "+3V3", "I2C_SDA")
R(mc, "4.7k", "+3V3", "I2C_SCL")

# WS2812 shift-light header: true 5 V data via AHCT buffer (3.3 V TTL-friendly
# input, 5 V rail). IO48 is RMT-capable and not a strapping pin.
R(mc, "33", "LED_DIN_MCU", "LED_DIN_A",
  note="Edge-rate limit into the level shifter")
part(mc, "U", "74xGxx:74AHCT1G125", "74AHCT1G125", SOT235,
     {"1": "GND", "2": "LED_DIN_A", "3": "GND", "4": "LED_DIN", "5": "+5V"},
     "SN74AHCT1G125DBVR", lcsc="C7484",
     note="5 V buffer so WS2812 DIN is a real 5 V rail, not 3.3 V hoping")
C(mc, "100nF 16V", "+5V", "GND", note="AHCT decoupling")
R(mc, "100", "LED_DIN", "LED_DIN_J",
  note="Series termination into the strip. The buffer drove the connector "
       "directly, and a WS2812 strip is metres of unterminated lead")
part(mc, "PF", "Device:Polyfuse", "0.5A hold", "Resistor_SMD:R_1812_4532Metric",
     {"1": "+5V", "2": "LED_5V"}, "Bourns MF-MSMF050-2",
     "Fused tap for the shift-light strip (8x WS2812 ~0.5 A worst case). "
     "1812, same correction as PF2 -- see the note there",
     lcsc="C17313")
part(mc, "J", "Connector_Generic:Conn_01x03", "WS2812", HDR3,
     {"1": "LED_5V", "2": "LED_DIN_J", "3": "GND"},
     note="Shift-light header: +5V / 5V-logic DIN / GND")

part(mc, "J", "Connector_Generic:Conn_01x04", "Rail break-out", HDR4,
     {"1": "+5V", "2": "+3V3", "3": "GND", "4": "GND"})


# ------------------------------------------------------------- SD card ----
sd = sheet("SD Card", "sdcard.kicad_sch",
           "microSD in 4-bit SDMMC mode with a switchable card supply")

part(sd, "J", "Connector:Micro_SD_Card_Det1", "microSD push-pull",
     "Connector_Card:microSD_HC_Hirose_DM3D-SF",
     {"1": "SD_D2_C", "2": "SD_D3_C", "3": "SD_CMD_C", "4": "SD_VDD",
      "5": "SD_CLK_C", "6": "GND", "7": "SD_D0_C", "8": "SD_D1_C",
      "9": "SD_CD", "10": "GND"},
     "Hirose DM3D-SF", "Push-pull socket; DET is the card-present switch",
     lcsc="C719027")

# The card contacts are touched at every swap -- by hand, in a paddock, in
# whatever the weather is doing -- and until now the only thing between an
# insertion-day static discharge and the ESP32's GPIO was the ESP32's own
# ~2 kV pin diodes. Two quad arrays clamp every card contact. VP goes to
# +3V3 rather than the switched SD_VDD so the clamps reference a rail that
# is alive even while the card power is cycled off.
part(sd, "U", "Power_Protection:SRV05-4", "SRV05-4",
     "Package_TO_SOT_SMD:SOT-23-6",
     {"1": "SD_CLK_C", "3": "SD_CMD_C", "4": "SD_D0_C", "6": "SD_D1_C",
      "2": "GND", "5": "+3V3"},
     "Semtech SRV05-4.TCT", "Card-slot ESD clamp, CLK/CMD/D0/D1",
     lcsc="C13612")
part(sd, "U", "Power_Protection:SRV05-4", "SRV05-4",
     "Package_TO_SOT_SMD:SOT-23-6",
     {"1": "SD_D2_C", "3": "SD_D3_C", "4": "SD_CD",
      "2": "GND", "5": "+3V3"},
     "Semtech SRV05-4.TCT", "Card-slot ESD clamp, D2/D3/CD",
     lcsc="C13612", nc=("6",))

# The SD bus is the most likely thing to need a scope at bring-up, and both
# of these nets otherwise exist only as fine-pitch SMD pads.
part(sd, "TP", "Connector:TestPoint", "SD_CLK", TP, {"1": "SD_CLK_C"})
part(sd, "TP", "Connector:TestPoint", "SD_CMD", TP, {"1": "SD_CMD_C"})

part(sd, "Q", "Device:Q_PMOS_GSD", "DMG2301L", SOT23,
     {"1": "SD_PG", "2": "+3V3", "3": "SD_VDD"}, "DMG2301L",
     "High-side switch so firmware can power-cycle a wedged card",
     lcsc="C7472914")
R(sd, "100k", "+3V3", "SD_PG", note="Default off")
part(sd, "Q", "Device:Q_NMOS_GSD", "2N7002", SOT23,
     {"1": "SD_EN_G", "2": "GND", "3": "SD_PG"}, "2N7002",
     "Level shift for the P-ch gate. Prefer an AEC-Q101 equivalent: the "
     "standard 2N7002 is not automotive qualified", lcsc="C8545")
R(sd, "1k", "SD_PWR_EN", "SD_EN_G",
  note="Series gate resistor. 10k here divided against R27 100k and left "
       "only 0.24V over the 2N7002 cold-end threshold")
R(sd, "100k", "SD_EN_G", "GND")
C(sd, "10uF 16V", "SD_VDD", "GND", fp=C1206)
C(sd, "100nF 16V", "SD_VDD", "GND")

# 1-BIT SDMMC. Only CLK, CMD and D0 reach the MCU -- the three pins D1/D2/D3
# used to take went to the second CAN controller's SPI bus.
for sig in ["CLK", "CMD", "D0"]:
    R(sd, "33", "SD_" + sig, "SD_%s_C" % sig, note="Series damping")
# The card's unused data lines still need pulling up, and this is not
# optional tidiness: an SD card samples DAT3 at power-up and drops into SPI
# mode if it finds it low. DAT1 and DAT2 float into the ESD array and the
# card's own input if left alone. So all three keep their pull-ups and their
# clamps, and simply stop at the card -- there is no net from here to the
# socket rows at all.
for sig in ["CMD", "D0", "D1", "D2", "D3"]:
    R(sd, "10k", "SD_VDD", "SD_%s_C" % sig,
      note="Espressif's recommended value; pulled to the switched rail so "
           "nothing back-feeds a powered-down card. For D1/D2/D3 this is now "
           "the ONLY thing on the net besides the card and its ESD clamp")
# Card detect is a slow mechanical contact, not a bus line, so it keeps the
# weaker pull-up -- less standing current with a card inserted.
R(sd, "47k", "+3V3", "SD_CD", note="Card-detect pull-up")

# ----------------------------------------------------------------- CAN ----
cn = sheet("CAN + K-line", "can.kicad_sch",
           "Two independent CAN nodes and an ISO 9141 K-line interface")

part(cn, "U", "Interface_CAN_LIN:TJA1051T-3", "TJA1051T/3", SOIC8,
     {"1": "CAN_TX", "2": "GND", "3": "+5V", "4": "CAN_RX", "5": "+3V3",
      "6": "CANL_T", "7": "CANH_T", "8": "CAN_S"},
     "TJA1051T/3,118", lcsc="C58988",
     note="5V bus drive with a 3.3V VIO pin, so no level shifting to the ESP32")
C(cn, "100nF 16V", "+5V", "GND")
C(cn, "100nF 16V", "+3V3", "GND")
R(cn, "10k", "CAN_S", "GND", note="Default to normal (non-silent) mode")

part(cn, "L", "Device:L_Coupled", "51uH", "esp32autosport:L_CommonMode_TDK_ACT45B",
     {"1": "CANH_T", "2": "CAN_H", "3": "CANL_T", "4": "CAN_L"},
     "TDK ACT45B-510-2P-TL003",
     "AEC-Q200 CAN choke; footprint pads renumbered so symbol winding 1-2 is "
     "the package's top (1-4) winding", lcsc="C76584")
part(cn, "JP", "Jumper:SolderJumper_2_Open", "TERM (default OFF)", SJ2,
     {"1": "CAN_H", "2": "TERM_A"},
     note="Ships OPEN: unterminated. Bridge the pads only when this board is "
          "an END node on its own bus. A vehicle's bus -- OBD-II diagnostics "
          "included -- is already terminated at both ends, and a third 120 "
          "ohm across the pair takes it to about 40 ohm and can stop it "
          "working. Defaulting terminated made that an easy mistake to make")
R(cn, "60.4", "TERM_A", "CAN_SPLIT", note="Split termination upper half")
R(cn, "60.4", "CAN_SPLIT", "CAN_L", note="Split termination lower half")
C(cn, "4.7nF 50V", "CAN_SPLIT", "GND", note="Split-termination common-mode stabiliser")
part(cn, "D", "Device:D_TVS", "SMAJ26CA", SMA, {"1": "CAN_H", "2": "GND"},
     "Diodes Inc SMAJ26CA-13-F", "Bidirectional bus clamp", lcsc="C134976")
part(cn, "D", "Device:D_TVS", "SMAJ26CA", SMA, {"1": "CAN_L", "2": "GND"},
     "Diodes Inc SMAJ26CA-13-F", lcsc="C134976")
part(cn, "TP", "Connector:TestPoint", "CAN_H", TP, {"1": "CAN_H"})
part(cn, "TP", "Connector:TestPoint", "CAN_L", TP, {"1": "CAN_L"})

# ------------------------------------------------------------- K-line ----
# The other half of K+DCAN. On a BMW/MINI cable "D-CAN" is just ISO 15765-4
# at 500 kbit/s, which the TJA1051 above already does -- bringing OBD-II pins
# 6 and 14 to the harness is the whole of it. K-line (ISO 9141-2 / KWP2000,
# OBD-II pin 7) is the part that needs hardware, and it is what an R53
# actually talks.
#
# WHY DISCRETE AND NOT AN L9637D. The L9637D is the textbook part, but it is
# a 5 V device: its TXD input threshold is 0.7*VCC = 3.5 V, which a 3.3 V
# GPIO cannot reliably meet, and its RXD output swings to 5 V into a pin
# rated 3.3 V. Fixing both ends costs more parts than the whole discrete
# interface below, and the discrete version has no baud ceiling -- a LIN
# transceiver, the other obvious substitute, slew-limits for 20 kbit/s and
# would not pass the 115200 baud modes the BMW tools use for flashing.
#
# K-line is open-collector at battery level: the ECU holds it high through
# its own pull-up and either end pulls it down. So TX is a low-side FET and
# RX is a divider, and the two are independently inverted -- TX is (GPIO high
# = bus low), RX is not. The ESP32-S3 UART inverts each signal separately in
# hardware, so uart_set_line_inverse(port, UART_SIGNAL_TXD_INV) is the entire
# software cost.
# --- transmit: low-side switch -------------------------------------------
part(cn, "Q", "Device:Q_NMOS_GSD", "2N7002", SOT23,
     {"1": "K_TX_G", "2": "GND", "3": "K_TX_D"}, "onsemi 2N7002",
     "K-line low-side driver. 60 V, so it stands off the clamped transient. "
     "Drain current in normal use is ~45 mA into the ECU's pull-up",
     lcsc="C8545")
R(cn, "10k", "K_TX", "K_TX_G", note="Gate series resistor")
R(cn, "100k", "K_TX_G", "GND",
  note="Holds the FET off while the MCU is in reset or the dev board is out "
       "of its socket. Without it a floating gate can sit the K-line "
       "permanently dominant, which jams diagnostics for every other tool "
       "on the bus -- a failure that looks like a dead car, not a dead "
       "shield")
R(cn, "20", "K_TX_D", "K_LINE", fp="Resistor_SMD:R_1206_3216Metric",
  note="Series limit, and the value is set by the worst of the two pull-up "
       "cases. ISO 9141-2 wants the tester's dominant below 0.2*Vb = 2.4 V. "
       "Including the FET's roughly 18 ohm Rds(on) at a 3.3 V gate the leg "
       "is about 38 ohm, so against the ECU's 510 ohm alone the dominant is "
       "12*38/548 = 0.83 V, and with the optional 750 ohm below also stuffed "
       "(304 ohm combined) it is 1.33 V. Both clear. 33 ohm would also have "
       "worked and 100 ohm would not, but 20 is what exists in 1206. A "
       "sustained short of K-line to battery while transmitting puts about "
       "316 mA through the FET, over its 115 mA continuous rating -- it "
       "survives the pulse, not the fault. 1206 carries the pulse energy")

# --- receive: divider, clamped -------------------------------------------
# The ratio cannot avoid clamping and it is worth writing down why. The GPIO
# needs > 0.75*3V3 = 2.48 V to read high for certain, and the bus recessive
# level is whatever the battery is doing -- 9 V on crank, 16 V on a charging
# fault. Satisfying 9*r > 2.48 needs r > 0.276; keeping 16*r under 3.3 needs
# r < 0.206. There is no ratio that does both, so the divider is sized for
# the low end and the Schottky pair carries the top.
R(cn, "22k", "K_LINE", "K_RX", note="RX divider, upper leg")
R(cn, "10k", "K_RX", "GND",
  note="Lower leg: 9 V recessive gives 2.81 V, clear of the 2.48 V worst-"
       "case input-high threshold. Above 11.5 V the clamp takes over and "
       "the upper leg carries about 0.47 mA at 14 V -- 6 mW, continuous, "
       "and the reason this is 22k rather than something stiffer")
part(cn, "D", "Device:D_Schottky_Dual_Series_AKC", "BAT54S", SOT23,
     {"1": "GND", "3": "K_RX", "2": "+3V3"}, "MDD BAT54S",
     "RX clamp. This one is not optional -- it conducts on every recessive "
     "bit above 11.5 V of battery, which is most of them", lcsc="C408389")

# --- optional tester pull-up ---------------------------------------------
# ISO 9141-2 has the ECU provide the bus pull-up, and it does, so this is
# unstuffed by default and the board works without it. A few modules are
# happier seeing a tester pull-up as well; the jumper is here so that is a
# soldering iron rather than a respin. It hangs off the fused OBD 12 V.
part(cn, "JP", "Jumper:SolderJumper_2_Open", "KPU (default OFF)", SJ2,
     {"1": "OBD_VBAT_F", "2": "K_PU"},
     note="Ships OPEN. Bridge only if a module will not answer without a "
          "tester pull-up on the K-line")
R(cn, "750", "K_PU", "K_LINE", fp="Resistor_SMD:R_1206_3216Metric",
  note="The tester pull-up. ISO 9141-2 names 510 ohm; 750 is what exists in "
       "1206 and it is the better number here anyway -- the value is not "
       "critical, the ECU provides the real pull-up, and 750 dissipates "
       "12^2/750 = 190 mW while the line is dominant against 510's 280 mW, "
       "which a 1206 rated 250 mW could not have carried")
part(cn, "TP", "Connector:TestPoint", "K_LINE", TP, {"1": "K_LINE"})

# --------------------------------------------------------- second CAN ----
# TWO BUSES AT ONCE NEEDS A SECOND CONTROLLER, NOT A SECOND TRANSCEIVER.
#
# The ESP32-S3 has exactly one TWAI peripheral. Two transceivers hung off it
# would give a choice of bus, selectable at runtime through the GPIO matrix,
# but never both live -- and the point of a second bus is watching the
# powertrain and the body bus at the same time. So the second channel is a
# whole CAN controller on SPI, and the ESP32 only sees registers.
#
# MCP2517FD in SOIC-14. The MCP2518FD is the same pinout with newer silicon
# and fits this footprint -- KiCad only ships the 2517 symbol, and the two
# are interchangeable here. Either does CAN FD, which nothing on this car
# needs today and costs nothing to keep.
#
# THE PIN COST, PAID FROM THE SD CARD. SPI needs SCK, SDI, SDO, nCS and an
# interrupt: five pins, against two spare. The three came from dropping the
# microSD from 4-bit to 1-bit mode, which is a real trade and a cheap one --
# 1-bit SDMMC still moves about 1.5 MB/s and this logger writes under
# 100 kB/s. It does not touch the flush-latency margin either, because that
# is the card's internal write time, not the bus width.
part(cn, "U", "Interface_CAN_LIN:MCP2517FD-xSL", "MCP2518FD", SOIC14,
     {"1": "CAN2_TXD", "2": "CAN2_RXD", "4": "CAN2_INT",
      "5": "XTAL2", "6": "XTAL1", "7": "GND",
      "10": "CAN2_SCK", "11": "CAN2_MOSI", "12": "CAN2_MISO",
      "13": "CAN2_CS", "14": "+3V3"},
     "MCP2518FDT-H/SL", lcsc="C626759",
     note="Second CAN controller, on SPI. Pin 11 SDI is the controller's "
          "input, so it lands on the MCU's MOSI; pin 12 SDO is its output "
          "and lands on MISO -- naming them CAN2_MOSI/CAN2_MISO here keeps "
          "that straight, because SDI/SDO on a peripheral means the "
          "opposite of what it means on a host. VERIFY THE PART NUMBER "
          "against the live catalogue before ordering, as with every other "
          "line on this board",
     nc=("3", "8", "9"))
C(cn, "100nF 16V", "+3V3", "GND", note="MCP2518FD decoupling, at pin 14")
C(cn, "1uF 16V", "+3V3", "GND", note="MCP2518FD bulk")
part(cn, "Y", "Device:Crystal_GND24", "40MHz", XTAL4,
     {"1": "XTAL1", "3": "XTAL2", "2": "GND", "4": "GND"},
     "TX322540M4FBCE2T", lcsc="C5186937",
     note="40 MHz, the top of the three rates the part accepts (4/20/40). "
          "Classic 500 kbit/s would be happy on 20, but 40 is what CAN FD "
          "data rates need and the crystals cost the same. This one is "
          "-40/+85 C and 12 pF CL; the first part picked was -20/+70, which "
          "a car cabin exceeds in a summer car park")
C(cn, "15pF 50V", "XTAL1", "GND",
  note="Crystal load cap, sized for Y1's 12 pF CL: C1=C2=2*(CL - Cstray) = "
       "2*(12 - 5) = 14 pF, and 15 pF is the nearest E-series value. Re-check "
       "this if the crystal is substituted -- CL varies part to part, and an "
       "oscillator that starts on the bench but not at -20 C is the classic "
       "way to get it wrong")
C(cn, "15pF 50V", "XTAL2", "GND", note="Crystal load cap")

part(cn, "U", "Interface_CAN_LIN:TJA1051T-3", "TJA1051T/3", SOIC8,
     {"1": "CAN2_TXD", "2": "GND", "3": "+5V", "4": "CAN2_RXD", "5": "+3V3",
      "6": "CAN2L_T", "7": "CAN2H_T", "8": "CAN2_S"},
     "TJA1051T/3,118", lcsc="C58988",
     note="Second bus transceiver. Same part as U5, so two positions on one "
          "BOM line")
C(cn, "100nF 16V", "+5V", "GND", note="U-CAN2 5 V decoupling")
C(cn, "100nF 16V", "+3V3", "GND", note="U-CAN2 VIO decoupling")
R(cn, "10k", "CAN2_S", "GND", note="Default to normal (non-silent) mode")

part(cn, "L", "Device:L_Coupled", "51uH", "esp32autosport:L_CommonMode_TDK_ACT45B",
     {"1": "CAN2H_T", "2": "CAN2_H_C", "3": "CAN2L_T", "4": "CAN2_L_C"},
     "TDK ACT45B-510-2P-TL003", "Second bus choke, same part as L1",
     lcsc="C76584")
part(cn, "JP", "Jumper:SolderJumper_2_Open", "TERM2 (default OFF)", SJ2,
     {"1": "CAN2_H_C", "2": "TERM2_A"},
     note="Ships OPEN, for the same reason TERM does: a vehicle bus is "
          "already terminated at both ends")
R(cn, "60.4", "TERM2_A", "CAN2_SPLIT", note="Split termination upper half")
R(cn, "60.4", "CAN2_SPLIT", "CAN2_L_C", note="Split termination lower half")
C(cn, "4.7nF 50V", "CAN2_SPLIT", "GND", note="Split-termination common-mode stabiliser")

# ---- which one reaches the harness --------------------------------------
# AUX_A and AUX_B are harness pins 5 and 6. They carry either the K-line or
# the second CAN pair, never both -- K-line idles at battery voltage and a
# CAN transceiver biases its bus to 2.5 V, so the two cannot share a wire.
#
# The clamps sit at the connector, AHEAD of the jumpers, so the protection is
# the same whichever way they are set. That is also why the K-line lost its
# own SMAJ26CA: this one already covers it, and 26 V bidirectional is the
# right standoff for both jobs.
part(cn, "D", "Device:D_TVS", "SMAJ26CA", SMA, {"1": "AUX_A", "2": "GND"},
     "Diodes Inc SMAJ26CA-13-F", "Aux port clamp, either mode", lcsc="C134976")
part(cn, "D", "Device:D_TVS", "SMAJ26CA", SMA, {"1": "AUX_B", "2": "GND"},
     "Diodes Inc SMAJ26CA-13-F", "Aux port clamp, either mode", lcsc="C134976")
part(cn, "JP", "Jumper:SolderJumper_3_Bridged12", "AUXSEL (default K-line)", SJ3B12,
     {"2": "AUX_A", "1": "K_LINE", "3": "CAN2_H_C"},
     note="Ships BRIDGED 1-2 = K-line on harness pin 5, which is what an "
          "R53 wants. For the second CAN instead: cut 1-2, bridge 2-3, and "
          "close AUXCL below. Both, or neither, is a bus that does not work")
part(cn, "JP", "Jumper:SolderJumper_2_Open", "AUXCL (default OFF)", SJ2,
     {"1": "AUX_B", "2": "CAN2_L_C"},
     note="The second half of AUXSEL. Open in K-line mode, which also leaves "
          "the second transceiver looking at an open circuit -- harmless, "
          "but firmware must not enable CAN2 there or it will bus-off "
          "waiting for an ACK that cannot come")
part(cn, "TP", "Connector:TestPoint", "AUX_A", TP, {"1": "AUX_A"})


# -------------------------------------------------------------- analog ----
an = sheet("Analog Inputs", "analog.kicad_sch",
           "4 sensor channels, jumper-selected dividers, differential return")

# ---------------------------------------------------------------------------
# DIFFERENTIAL GROUND.
#
# The parent board was the only thing in the car it was grounded to: harness
# ground came in on the power connector and everything referenced it. This one
# is grounded twice over -- through the sensor loom, and through USB, which
# reaches chassis by way of a charger in a cigarette socket somewhere else
# entirely. A few hundred millivolts of chassis IR drop between those two
# points lands directly on every single-ended reading, and it makes the 0.1 %
# divider resistors below a waste of money: 300 mV on a 0-5 V channel is 6 %.
#
# The fix has to be at the DIVIDER, not just at the ADC. A differential ADC
# fed from a divider whose bottom leg goes to shield ground measures an error
# that was already baked in one stage earlier. So the sensor's own ground
# comes back as a Kelvin sense wire, through an IDENTICAL attenuator, and the
# ADS1115 subtracts the two:
#
#     AINn      = (Vsig + Voff) * 2.2/13.2
#     AGND_SENSE= (       Voff) * 2.2/13.2
#     AINn - AGND_SENSE = Vsig * 2.2/13.2     <- Voff gone, exactly
#
# SENS_RTN is a sense wire and carries ~12 uA; GND on the connector is the
# excitation return and carries the sensors' 80 mA. They are separate pins on
# purpose -- if they shared one, that wire's own IR drop would be the offset
# we are trying to remove.
#
# There is one divider ratio on this board, and that is what makes the
# subtraction exact -- see the note above the channel loop.
#
# The ESP32's own ADC path stays single-ended. It is the fast-and-rough
# channel at +/-1-2 % anyway, so a ground offset is not what limits it.
# ---------------------------------------------------------------------------

part(an, "J", "Connector_Generic:Conn_01x10", "Sensor harness", JST10,
     {"1": "+5VS", "2": "+5VS", "3": "AIN1_IN", "4": "AIN2_IN",
      "5": "SENS_RTN", "6": "SENS_RTN", "7": "AIN3_IN", "8": "AIN4_IN",
      "9": "GND", "10": "GND"},
     "JST B10B-PH-K-S(LF)(SN)",
     "Two 5V excitation pins, two Kelvin sense-ground pins, two current-"
     "carrying grounds, four signals. SENS_RTN must land on the SENSOR's "
     "ground stud, not on the same stud as pins 9/10", lcsc="C158038")

# ONE RANGE, NOT THREE.
#
# The channels used to carry three solder jumpers each: a 0-3.3 V bypass, a
# 0-5 V / 0-16 V range select, and a pull-up bias. Only 5 V and 12 V sensors
# are actually wanted, and both fit inside the 0-16 V divider, so the range
# select and the bypass are gone -- twelve jumpers and four resistors.
#
# The resolution argument for a narrower range died with the second ADS1115.
# A 5 V sensor through the 0-16 V divider lands at 0.836 V, which on a 16-bit
# part at +/-4.096 V is 6688 counts and 0.75 mV referred to the input. Even
# the ESP32's own 12-bit ADC still gets 1104 counts out of it.
#
# The real reason is the differential ground, and it is not a nicety. A
# channel switched to 0-16 V while the return attenuator stayed at the 0-5 V
# ratio subtracts 0.5769*Voff from a signal scaled by 0.1673 -- so a 300 mV
# chassis offset arrives as 735 mV of error, worse than not correcting at
# all. A per-channel range select would need a per-channel matched return
# attenuator and a jumper that switches both together, and a jumper you can
# half-set is a silent wrong reading. One fixed ratio makes the match
# structural instead of a thing to remember.
for n in range(1, 5):
    inp, node, out = "AIN%d_IN" % n, "AIN%d_A" % n, "AIN%d" % n
    # Transient clamp at the connector, ahead of everything else. The BAT54S
    # pairs downstream are 200mA signal Schottkys with no pulse energy rating,
    # so on an unshielded harness they were the only thing standing between an
    # ISO 7637 pulse and the ADC. Bidirectional because pulse 1 is negative.
    # 40V standoff clears a sustained short to the 36V top of the input window,
    # which a lower standoff part would sit in conduction on until it failed.
    part(an, "D", "Device:D_TVS", "SMAJ40CA", SMA, {"1": inp, "2": "GND"},
         "Littelfuse SMAJ40CA",
         "Ch%d harness transient clamp (bidirectional, 400W)" % n, lcsc="C223989")
    R(an, "1k", inp, node,
      note="Ch%d series/fault-current limit. 1%%, not the 0.1%% thin film this "
           "started as -- see docs/COST.md. Fifteen 0.1%% parts cost $2.68, "
           "more than the CAN controller, to tighten a divider whose error "
           "the ADS1115's own +/-0.30%% gain spec already dominates. Absolute "
           "scale is a firmware calibration constant either way; what "
           "tolerance actually buys here is the MATCH between this chain and "
           "the return attenuator, and at 1%% a 300 mV chassis offset leaves "
           "6 mV instead of 0.6 mV" % n)
    part(an, "JP", "Jumper:SolderJumper_2_Open", "PULLUP%d" % n, SJ2,
         {"1": "+5VS", "2": "AIN%d_PU" % n},
         note="Close for 2-wire NTC / open-collector sensors. Nothing to do "
              "with the input range -- it is about what kind of sensor is on "
              "the other end, not how many volts it swings")
    R(an, "2.49k", "AIN%d_PU" % n, node, note="Ch%d bias resistor" % n)
    R(an, "10k", node, out,
      note="Ch%d divider upper leg" % n)
    R(an, "2.2k", out, "GND",
      note="Ch%d divider lower leg. 2.2/13.2 = exactly 1/6, so 16.0 V in "
           "gives 2.67 V at the ADC and 5.0 V gives 0.833 V -- both inside "
           "the 3.1 V the ESP32 ADC can actually use, and the firmware's "
           "DIVIDER_GAIN is a round 6.000. 2.2k rather than 2.21k because "
           "E96 values do not exist in the 1%% jellybean library, and the "
           "0.37%% it moves the ratio is a calibration constant, not an "
           "error. This value must match the return attenuator below or the "
           "ground correction is worse than useless" % n)
    C(an, "470nF 50V", out, "GND",
      note="Ch%d anti-alias. 470nF, not the 100nF this inherited: source "
           "impedance here is (1k+10k)||2.21k = 1.84k, so 100nF puts the "
           "corner at 865 Hz -- above the 430 Hz Nyquist of the ADS1115 at "
           "860 SPS, and well above anything the ESP32's own SAR ADC can "
           "sample cleanly. That was survivable while 0-5 V was the default "
           "range (261 Hz) and became the only behaviour when the range "
           "jumpers went. 470nF gives 184 Hz, which still passes anything a "
           "wideband does and stops engine noise folding into the log" % n)
    # One SOT-23 series pair: GND -> signal -> +3V3, so the node is clamped a
    # Schottky drop either side of the rails.
    part(an, "D", "Device:D_Schottky_Dual_Series_AKC", "BAT54S", SOT23,
         {"1": "GND", "3": out, "2": "+3V3"}, "MDD BAT54S",
         "Ch%d rail clamp (both polarities)" % n, lcsc="C408389")

# ---- the shared return attenuator ----------------------------------------
# Component-for-component identical to one signal channel's default path:
# 1k series, 10k upper, 15k lower, same 0.1 % thin film, same 100nF. It has
# to be, or the subtraction leaves a residue proportional to the mismatch.
# Same part numbers as the channels above, so they track over temperature.
part(an, "D", "Device:D_TVS", "SMAJ40CA", SMA, {"1": "SENS_RTN", "2": "GND"},
     "Littelfuse SMAJ40CA",
     "Return-sense harness clamp. This wire is as exposed as the signals",
     lcsc="C223989")
R(an, "1k", "SENS_RTN", "AGND_A", note="Matches the channels' 1k series")
R(an, "10k", "AGND_A", "AGND_SENSE", note="Matches the channels' upper leg")
R(an, "2.2k", "AGND_SENSE", "GND",
  note="Matches the channels' lower leg. Same value, same part, same reel if "
       "possible -- the whole ground correction is the difference of two "
       "attenuators, so it is only as good as they match, and parts from one "
       "reel match far better than their tolerance band suggests. Also what "
       "holds AGND_SENSE at 0 V when nothing is plugged in, so an absent "
       "loom reads as zero offset rather than as a floating reference")
C(an, "470nF 50V", "AGND_SENSE", "GND",
  note="Matches the channels' filter, and this one is not cosmetic: the "
       "ground correction is the difference of two attenuators, so if their "
       "corner frequencies differ the subtraction leaves a phase residue on "
       "every transient. Same value, same dielectric, same part number")
part(an, "D", "Device:D_Schottky_Dual_Series_AKC", "BAT54S", SOT23,
     {"1": "GND", "3": "AGND_SENSE", "2": "+3V3"}, "MDD BAT54S",
     "Return-sense rail clamp. +/-0.5 V of chassis offset arrives here as "
     "+/-0.29 V, inside the ADS1115's GND-0.3V absolute minimum; beyond that "
     "this is what holds the pin legal", lcsc="C408389")

# ---- precision path ------------------------------------------------------
# The ESP32-S3 ADC is only good for +/-1-2% even calibrated, which is +/-0.2
# AFR on a 0-5V wideband output. The ADS1115s (16-bit delta-sigma) share the
# conditioned AINx nodes, so firmware chooses per channel: fast-and-rough on
# the internal ADC, or slow-and-accurate here.
#
# TWO of them, because the differential MUX is the constraint. An ADS1115
# offers AIN0-AIN1, AIN0-AIN3, AIN1-AIN3, AIN2-AIN3 -- so AIN3 can be a
# COMMON negative for three channels, but only three. The second part carries
# the fourth and leaves two differential inputs spare. At about $1.50 and
# 20 mm^2 that is a great deal cheaper than giving every channel its own
# return wire and its own matched attenuator, which is 20 more parts.
part(an, "U", "Analog_ADC:ADS1115IDGS", "ADS1115 (0x48)",
     "Package_SO:VSSOP-10_3x3mm_P0.5mm",
     {"1": "GND", "3": "GND", "4": "AIN1", "5": "AIN2", "6": "AIN3",
      "7": "AGND_SENSE", "8": "+3V3", "9": "I2C_SDA", "10": "I2C_SCL"},
     "ADS1115IDGSR", lcsc="C37593",
     note="Channels 1-3. ADDR to GND = 0x48. Read AIN0-AIN3, AIN1-AIN3 and "
          "AIN2-AIN3: every one of them is a channel minus the shared "
          "sensor-ground reference",
     nc=("2",))
C(an, "100nF 16V", "+3V3", "GND", note="ADS1115 (0x48) decoupling")

part(an, "U", "Analog_ADC:ADS1115IDGS", "ADS1115 (0x49)",
     "Package_SO:VSSOP-10_3x3mm_P0.5mm",
     {"1": "+3V3", "3": "GND", "4": "AIN4", "5": "AIN_SP1", "6": "AIN_SP2",
      "7": "AGND_SENSE", "8": "+3V3", "9": "I2C_SDA", "10": "I2C_SCL"},
     "ADS1115IDGSR", lcsc="C37593",
     note="Channel 4, plus two spare differential inputs on the pads below. "
          "ADDR to +3V3 = 0x49",
     nc=("2",))
C(an, "100nF 16V", "+3V3", "GND", note="ADS1115 (0x49) decoupling")
part(an, "J", "Connector_Generic:Conn_01x04", "Spare diff in", HDR4,
     {"1": "AIN_SP1", "2": "AIN_SP2", "3": "AGND_SENSE", "4": "GND"},
     note="The second ADC's unused pair, brought out raw -- 0-3.3V only, no "
          "divider and no clamp in front of them. For a bench sensor, not "
          "for the loom")
R(an, "100k", "AIN_SP1", "GND", note="Bleed: an open input is not a reading")
R(an, "100k", "AIN_SP2", "GND")

# ---- battery monitor -----------------------------------------------------
# OBD-II pin 16, which is permanent battery. It is the only 12 V on the board
# and it is sense-only: 100k in series means a load dump arrives as 1 mA.
R(an, "100k", "OBD_VBAT", "VBAT_SNS", note="Battery monitor, upper leg")
R(an, "8.2k", "VBAT_SNS", "GND",
  note="Divide by 13.2, not 11: at 36V the 11:1 divider put 3.27V on the "
       "pin, above the ADC's usable 3.1V, so the reading saturated near "
       "the top of the declared input window")
C(an, "100nF 16V", "VBAT_SNS", "GND")
part(an, "D", "Device:D_Schottky_Dual_Series_AKC", "BAT54S", SOT23,
     {"1": "GND", "3": "VBAT_SNS", "2": "+3V3"}, "MDD BAT54S",
     "Battery-monitor clamp", lcsc="C408389")


# --------------------------------------------------------------------------
# Reference designators
# --------------------------------------------------------------------------

def assign_refs():
    counters = {}
    for sh in SHEETS:
        for p in sh["parts"]:
            pre = p["prefix"]
            counters[pre] = counters.get(pre, 0) + 1
            p["ref"] = "%s%d" % (pre, counters[pre])


# --------------------------------------------------------------------------
# Placement
# --------------------------------------------------------------------------

PAGE_W, PAGE_H = 594.0, 297.0   # A2 width, A3 height: sheets grow sideways
MARGIN_X, MARGIN_TOP, MARGIN_BOT = 12.0, 18.0, 22.0
STUB = 5.08
LABEL_ALLOWANCE = 15.0   # rails are power symbols and pairs are wired now
# Breathing room. The packer used to fill each column to the bottom of the
# page before starting the next, which reads as a dense stripe down the left
# with the rest of an A3 sheet empty. ROW_PAD and COL_GAP set the minimum,
# and any height a column does not need is then shared out between its parts.
ROW_PAD, ROW_MIN, COL_GAP, ROW_SLACK_MAX = 10.16, 17.78, 12.7, 15.24
# Sheets are packed against A3 and then given the smallest standard page the
# drawing actually fits on. A CAN transceiver and its bus is a 130 x 70 mm
# drawing; left on A3 it is a stamp in the corner of an empty page, and
# "fit to window" then renders it too small to read.
PAPERS = [("A5", 210.0, 148.0), ("A4", 297.0, 210.0), ("A3", 420.0, 297.0),
          ("A2", 594.0, 420.0)]
# A2 is here because the Power sheet outgrew A3 when the ride-through bank,
# the power-fail detector and the sensor-rail switch went on it. The
# alternative was splitting Power in two, which would put the front end and
# the rails it feeds on different pages -- worse to read than a wide page.
GRID = 1.27


def snap(v):
    return round(v / GRID) * GRID


def symbol_extent(libs, lib_id, theta):
    xs, ys = [0.0], [0.0]
    for _, _, lx, ly, ang, hidden in libs.pins(lib_id):
        if hidden:
            continue
        off, d = pin_geometry(lx, ly, ang, theta)
        xs += [off[0], off[0] + d[0] * STUB]
        ys += [off[1], off[1] + d[1] * STUB]
    return min(xs), max(xs), min(ys), max(ys)


WIRE_GAP = 8.89          # host pin to satellite pin, with room for the
                         # net name to sit on the wire between them


def match_part(sh, value, nets, taken):
    """Find a part on this sheet by value and the exact nets it touches.

    Same identification the PCB placement tables use, and for the same
    reason: it survives reference renumbering, which happens on every run.
    """
    for p in sh["parts"]:
        if p["ref"] in taken or p["value"] != value:
            continue
        if nets is not None and set(p["pins"].values()) != set(nets):
            continue
        taken.add(p["ref"])
        return p
    return None


def pin_clearance(libs, part):
    """How far the text has to stand off, so it clears any vertical stub."""
    up = down = 0.0
    for _n, _nm, lx, ly, ang, hid in libs.pins(part["lib_id"]):
        if hid:
            continue
        _o, dv = pin_geometry(lx, ly, ang, part["theta"])
        if dv == (0, -1):
            up = STUB + 3.81
        elif dv == (0, 1):
            down = STUB + 3.81
    part["_bup"], part["_bdown"] = up, down


def apply_blocks(libs, sh):
    """Place the hand-drawn blocks that live on this sheet.

    Returns the parts the blocks own, so the column packer leaves them
    alone -- a block travels as one drawing, not as loose symbols.
    """
    import sch_blocks
    owned, taken, placed = {}, set(), []
    for blk in sch_blocks.BLOCKS:
        if blk["sheet"] != sh["name"]:
            continue
        anchor = match_part(sh, blk["anchor"][0], blk["anchor"][1], taken)
        if anchor is None:
            continue
        BLOCKS_PLACED.add(id(blk))
        anchor["_block"] = True
        anchor["_own_ext"] = anchor["ext"]
        pin_clearance(libs, anchor)
        members = []
        for value, nets, dx, dy, rot in blk["parts"]:
            part = match_part(sh, value, nets, taken)
            if part is None:
                raise SystemExit(
                    "block %r wants a %r on %s and this sheet has none left"
                    % (blk["anchor"][0], value, sorted(nets or [])))
            part["theta"] = rot
            part["ext"] = symbol_extent(libs, part["lib_id"], rot)
            part["_up"] = part["_down"] = 0.0
            part["_block"] = True
            pin_clearance(libs, part)
            members.append((part, dx, dy))
        placed.append({"blk": blk, "anchor": anchor, "members": members})
        for part, dx, dy in members:
            owned[part["ref"]] = (anchor, dx, dy)
    sh["_blocks"] = placed
    return owned


# Which BLOCKS entries actually got drawn. A block whose sheet was renamed or
# whose anchor part was deleted used to be skipped in silence, and the only
# symptom was a schematic that quietly got worse -- five of them had stopped
# drawing before anyone noticed. The hand-drawn layout is the whole reason
# this file is 2000 lines, so an orphan is a build failure now.
BLOCKS_PLACED = set()


def check_all_blocks_placed():
    import sch_blocks
    names = {sh["name"] for sh in SHEETS}
    orphans = []
    for blk in sch_blocks.BLOCKS:
        if id(blk) in BLOCKS_PLACED:
            continue
        why = ("no sheet named %r" % blk["sheet"] if blk["sheet"] not in names
               else "no unclaimed %r on that sheet" % (blk["anchor"][0],))
        orphans.append("  %-18s %s" % (blk["anchor"][0], why))
    if orphans:
        raise SystemExit(
            "sch_blocks: %d block(s) were never drawn --\n%s\n"
            "Either repoint them or take them out of BLOCKS."
            % (len(orphans), "\n".join(orphans)))


def orient_two_pin(libs, p, num, want):
    """Rotation that makes pin `num` of a two-pin part face `want`."""
    for theta in (0, 90, 180, 270):
        for n, _nm, lx, ly, ang, hid in libs.pins(p["lib_id"]):
            if hid or n != num:
                continue
            _off, d = pin_geometry(lx, ly, ang, theta)
            if d == want:
                return theta
    return p.get("theta", 0)


def attach_satellites(libs, sh, owned=()):
    """Park each two-pin partner right on the pin it serves.

    The satellite sits WIRE_GAP beyond the host pin, facing back at it, so
    the connection is a single straight wire rather than a pair of labels.

    Parts a hand-drawn block already owns are off limits. This runs after
    apply_blocks and writes position and rotation last, so without the
    guard it quietly re-parked block members -- the 5 V bootstrap cap ended
    up rotated and 6 mm from where the block drew its wire, and the only
    symptom was two nets KiCad could no longer name.
    """
    sats = {}
    for pa, na, pb, nb, net in wire_pairs(sh):
        if pa["ref"] in owned or pb["ref"] in owned:
            continue
        host, hnum, sat, snum = pa, na, pb, nb
        if len(pa["pins"]) < len(pb["pins"]):
            host, hnum, sat, snum = pb, nb, pa, na
        if len(sat["pins"]) != 2:
            continue                       # only two-pin parts travel
        hoff = hd = None
        for n, _nm, lx, ly, ang, hid in libs.pins(host["lib_id"]):
            if not hid and n == hnum:
                hoff, hd = pin_geometry(lx, ly, ang, host["theta"])
        if hd is None:
            continue
        theta = orient_two_pin(libs, sat, snum, (-hd[0], -hd[1]))
        soff = None
        for n, _nm, lx, ly, ang, hid in libs.pins(sat["lib_id"]):
            if not hid and n == snum:
                soff, _ = pin_geometry(lx, ly, ang, theta)
        if soff is None:
            continue
        sat["theta"] = theta
        sat["ext"] = symbol_extent(libs, sat["lib_id"], theta)
        sats[sat["ref"]] = {
            "host": host["ref"], "theta": theta,
            "dx": hoff[0] + hd[0] * WIRE_GAP - soff[0],
            "dy": hoff[1] + hd[1] * WIRE_GAP - soff[1],
            "wire": (hoff, hd, soff),
            "host_pin": hnum, "sat_pin": snum, "net": net,
        }
    sh["_sats"] = sats
    return sats


def place(libs):
    """Lay parts out in columns, sized to the widest symbol in each column."""
    for sh in SHEETS:
        for p in sh["parts"]:
            # Two-pin parts read best lying horizontally, but KiCad draws some
            # of them vertically and others horizontally, so ask the symbol
            # which way its pins point rather than hard-coding a list.
            visible = [pin for pin in libs.pins(p["lib_id"]) if not pin[5]]
            p["theta"] = 0
            if len(visible) == 2:
                dirs = {pin_geometry(x, y, a, 0)[1] for _, _, x, y, a, _ in visible}
                if dirs <= {(0, 1), (0, -1)}:
                    p["theta"] = 90
            p["ext"] = symbol_extent(libs, p["lib_id"], p["theta"])
            up = down = 0.0
            for _n, _nm, lx, ly, ang, hid in libs.pins(p["lib_id"]):
                if hid:
                    continue
                _o, dv = pin_geometry(lx, ly, ang, p["theta"])
                if dv == (0, -1):
                    up = STUB + 3.81
                elif dv == (0, 1):
                    down = STUB + 3.81
            p["_up"], p["_down"] = up, down

        blocks = apply_blocks(libs, sh)
        # Grow the anchor so the packer reserves room for the whole drawing.
        for ref, (anchor, dx, dy) in blocks.items():
            part = next(q for q in sh["parts"] if q["ref"] == ref)
            px0, px1, py0, py1 = part["ext"]
            ax0, ax1, ay0, ay1 = anchor["ext"]
            anchor["ext"] = (min(ax0, px0 + dx), max(ax1, px1 + dx),
                             min(ay0, py0 + dy), max(ay1, py1 + dy))
        # ...and for the wires, which reach past the parts they connect.
        for entry in sh.get("_blocks", []):
            anchor, blk = entry["anchor"], entry["blk"]
            pts = [q for poly in blk["wires"] for q in poly]
            if not pts:
                continue
            ax0, ax1, ay0, ay1 = anchor["ext"]
            anchor["ext"] = (min([ax0] + [q[0] for q in pts]),
                             max([ax1] + [q[0] for q in pts]),
                             min([ay0] + [q[1] for q in pts]),
                             max([ay1] + [q[1] for q in pts]))

        # Satellites ride with their host, so they are not packed separately.
        sats = attach_satellites(libs, sh, blocks)
        for ref, info in sats.items():
            host = next(q for q in sh["parts"] if q["ref"] == info["host"])
            sx0, sx1, sy0, sy1 = next(q for q in sh["parts"]
                                      if q["ref"] == ref)["ext"]
            hx0, hx1, hy0, hy1 = host["ext"]
            host["ext"] = (min(hx0, sx0 + info["dx"]), max(hx1, sx1 + info["dx"]),
                           min(hy0, sy0 + info["dy"]), max(hy1, sy1 + info["dy"]))

        columns, col, y = [], [], MARGIN_TOP
        usable = PAGE_H - MARGIN_TOP - MARGIN_BOT
        for p in sh["parts"]:
            if p["ref"] in sats or p["ref"] in blocks:
                continue
            x0, x1, y0, y1 = p["ext"]
            # Leave room for the reference and value text above and below the
            # body, otherwise adjacent rows of passives print on top of each
            # other.
            h = max((y1 - y0) + ROW_PAD + p["_up"] + p["_down"], ROW_MIN)
            if col and y + h > MARGIN_TOP + usable:
                columns.append(col)
                col, y = [], MARGIN_TOP
            p["_h"], p["_y"] = h, y
            col.append(p)
            y += h
        if col:
            columns.append(col)
        # Share out whatever height the column did not need.
        for col in columns:
            spare = usable - sum(p["_h"] for p in col)
            slack = min(spare / max(len(col), 1), ROW_SLACK_MAX)
            if slack <= 0:
                continue
            for i, p in enumerate(col):
                p["_y"] += slack * i
        x = MARGIN_X
        for col in columns:
            left = min(p["ext"][0] for p in col)
            right = max(p["ext"][1] for p in col)
            cx = x - left + LABEL_ALLOWANCE
            for p in col:
                p["x"] = snap(cx)
                p["y"] = snap(p["_y"] - p["ext"][2])
            x = cx + right + LABEL_ALLOWANCE + COL_GAP
        for ref, (anchor, dx, dy) in blocks.items():
            part = next(q for q in sh["parts"] if q["ref"] == ref)
            part["x"], part["y"] = snap(anchor["x"] + dx), snap(anchor["y"] + dy)
        # Satellites take their position from the host they hang off.
        for ref, info in sats.items():
            host = next(q for q in sh["parts"] if q["ref"] == info["host"])
            sat = next(q for q in sh["parts"] if q["ref"] == ref)
            sat["x"] = snap(host["x"] + info["dx"])
            sat["y"] = snap(host["y"] + info["dy"])
        sh["width_used"] = x
        # The title block eats the bottom right corner, so leave it clear.
        need_h = max([p["y"] + p["ext"][3] for p in sh["parts"]] or [0]) + 30.0
        sh["paper"] = next((nm for nm, pw, ph in PAPERS
                            if x <= pw and need_h <= ph), PAPERS[-1][0])
        # The packer wraps on height but never on width, so a sheet that grew
        # wide simply ran off the page: two symbols on the power sheet landed
        # at x = 422.9 on a 420 mm page, where ERC reported them unconnected
        # and nothing else complained. Fail loudly instead.
        if x > PAGE_W:
            raise SystemExit(
                "sheet %r needs %.0f mm of width but the page is %.0f mm -- "
                "%d columns. Widen the page or split the sheet."
                % (sh["name"], x, PAGE_W, len(columns)))


# --------------------------------------------------------------------------
# Serialisation
# --------------------------------------------------------------------------

FONT = "(effects (font (size 1.27 1.27)) %s)"


def emit_symbol(libs, sh, p, sheet_uuid):
    lines = []
    su = det_uuid("sym:%s:%s" % (sh["file"], p["ref"]))
    x0, x1, y0, y1 = p["ext"]
    lines.append(
        '  (symbol (lib_id "%s") (at %s %s %d) (unit 1)\n'
        "    (in_bom %s) (on_board yes) (dnp no)\n"
        '    (uuid %s)'
        % (p["lib_id"], mm(p["x"]), mm(p["y"]), p["theta"],
           "no" if p["prefix"].startswith("#") else "yes", su)
    )
    # Reference sits above the body and value below -- but a pin leaving the
    # top or bottom takes a stub and then a label or power symbol with it, and
    # the text landed straight on top of that: "LM5164 (5V)" printed through
    # the GND symbol under U2. Clear the whole stub where a pin goes that way.
    up, down = p.get("_up", 0.0), p.get("_down", 0.0)
    ref_y = p["y"] + y0 - 2.0 - up
    val_y = p["y"] + y1 + 2.0 + down
    ref_x = val_x = p["x"]
    # Inside a hand-drawn block the parts sit a few millimetres apart, and
    # the packer's generous text offsets then print one part's value through
    # the next part's body. Tuck the fields alongside instead, the way they
    # sit in a schematic drawn by hand: beside a vertical part, above and
    # below one lying on its side.
    if p.get("_block"):
        # An anchor's extent has been grown to reserve room for the whole
        # block, so field placement has to use the part's own body -- else
        # the anchor's reference floats off at the top of the drawing.
        x0, x1, y0, y1 = p.get("_own_ext", p["ext"])
        # Beside only for a part decidedly taller than it is wide. An IC that
        # is square-ish has pins out of both sides, and its fields then print
        # over the very labels those pins carry -- "USBLC6-2SC6" straight
        # through USB_DM. Above and below is where the room is.
        if y1 - y0 <= 1.6 * (x1 - x0):
            # A pin leaving the top or bottom takes a stub and then a label
            # or a power symbol with it, and the text lands on that: the
            # buffer printed "74AHCT1G125" straight through the ground
            # symbol under it. Clear the whole stub, as the packer does.
            ref_y = p["y"] + y0 - 1.27 - p.get("_bup", 0.0)
            val_y = p["y"] + y1 + 2.03 + p.get("_bdown", 0.0)
        else:
            ref_x = val_x = p["x"] + x1 + 1.27
            ref_y, val_y = p["y"] - 1.27, p["y"] + 1.78
    # A power symbol's name belongs on the far side of its body from the pin
    # it hangs off, or it lands on whatever that pin belongs to: "+3V3"
    # printed over "R16" on the very resistor it was feeding.
    if p.get("_pwr_dir"):
        dx, dy = p["_pwr_dir"]
        if dy < 0:
            val_y = p["y"] + y0 - 2.0
        elif dy > 0:
            val_y = p["y"] + y1 + 2.0
        else:
            val_y = p["y"] + 1.27
            val_x = p["x"] + (x1 + 2.0 if dx > 0 else x0 - 2.0)
    # A power symbol's reference (#PWR003) is noise -- the rail name in the
    # Value field is the label. KiCad hides these by convention and so do we,
    # along with the flags'.
    anon = p["prefix"].startswith("#")
    props = [("Reference", p["ref"], ref_x, ref_y, anon),
             ("Value", p["value"], val_x, val_y, p["lib_id"] == "power:PWR_FLAG"),
             ("Footprint", p["footprint"], val_x, val_y, True),
             ("Datasheet", "~", val_x, val_y, True)]
    if p.get("voltage"):
        props.append(("Voltage", p["voltage"], val_x, val_y, True))
    if p.get("tolerance"):
        props.append(("Tolerance", p["tolerance"], val_x, val_y, True))
    if p["mpn"]:
        props.append(("MPN", p["mpn"], val_x, val_y, True))
    if p["note"]:
        props.append(("Note", p["note"], val_x, val_y, True))
    # A property's angle is applied *on top of* the symbol's rotation, so a
    # rotated part needs its text counter-rotated to stay horizontal.
    # ...except at 180, where counter-rotating would print the text upside
    # down. KiCad only ever writes field angles of 0 or 90; a part flipped
    # end for end keeps its text the right way up.
    text_angle = 0 if p["theta"] == 180 else (360 - p["theta"]) % 360
    just = " (justify left)" if ref_x != p["x"] else ""
    for name, value, px, py, hide in props:
        # No control characters in a property string, ever. A literal
        # newline inside one is accepted by KiCad's loader but silently
        # breaks its connectivity pass: every symbol after it in the file
        # drops out of the netlist and ERC reports hundreds of dangling
        # wires with no hint of the cause. One "\n" in a part note cost an
        # evening of bisecting to find.
        clean = " ".join(value.replace('"', "'").split())
        lines.append(
            '    (property "%s" "%s" (at %s %s %d)\n      %s\n    )'
            % (name, clean, mm(px), mm(py), text_angle,
               "(effects (font (size 1.27 1.27))%s %s)"
               % (just, "hide" if hide else ""))
        )
    for num, _, _, _, _, _ in libs.pins(p["lib_id"]):
        lines.append('    (pin "%s" (uuid %s))'
                     % (num, det_uuid("pin:%s:%s:%s" % (sh["file"], p["ref"], num))))
    lines.append(
        '    (instances\n      (project "%s"\n        (path "/%s/%s" (reference "%s") (unit 1))\n      )\n    )'
        % (PROJECT, ROOT_UUID, sheet_uuid, p["ref"])
    )
    lines.append("  )")
    return "\n".join(lines)


def on_segment(px, py, a, b, eps=0.01):
    """Is (px, py) on the axis-aligned segment a--b?

    Block wires are drawn on the 1.27 mm grid and so are the pins they
    land on, so exact arithmetic would nearly do; eps covers the halves
    and thirds that creep in from symbol geometry.
    """
    (x1, y1), (x2, y2) = a, b
    if abs(y1 - y2) < eps:
        return (abs(py - y1) < eps
                and min(x1, x2) - eps <= px <= max(x1, x2) + eps)
    if abs(x1 - x2) < eps:
        return (abs(px - x1) < eps
                and min(y1, y2) - eps <= py <= max(y1, y2) + eps)
    dx, dy = x2 - x1, y2 - y1
    t = ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)
    return (0 <= t <= 1
            and abs(x1 + t * dx - px) < eps and abs(y1 + t * dy - py) < eps)


def wire_seg(x1, y1, x2, y2, uid):
    return ("  (wire (pts (xy %s %s) (xy %s %s))\n"
            "    (stroke (width 0) (type default))\n"
            "    (uuid %s)\n  )" % (mm(x1), mm(y1), mm(x2), mm(y2), uid))


def junction(x, y, uid):
    return ("  (junction (at %s %s) (diameter 0) (color 0 0 0 0)\n"
            "    (uuid %s)\n  )" % (mm(x), mm(y), uid))


HIER_LABEL = (
    '  (hierarchical_label "%s" (shape %s) (at %s %s %d)\n'
    "    (effects (font (size 1.27 1.27)) (justify %s))\n"
    "    (uuid %s)\n  )"
)

SHEET_PIN = (
    '    (pin "%s" %s (at %s %s 180)\n'
    "      (effects (font (size 1.27 1.27)) (justify right))\n"
    "      (uuid %s)\n    )"
)


def crossing_nets(sh):
    """Nets this sheet shares with another one, so they are its interface.

    Rails are left out: a power symbol is global by definition and does not
    belong in a sheet's signal interface. Everything else that leaves the
    sheet becomes a hierarchical label here and a pin on the sheet symbol,
    which is what makes six pages an actual hierarchy rather than six
    drawings that happen to share net names.
    """
    mine = {v for p in sh["parts"] for v in p["pins"].values()}
    others = set()
    for o in SHEETS:
        if o is sh:
            continue
        for p in o["parts"]:
            others.update(p["pins"].values())
    return sorted(n for n in mine & others if n not in RAILS)


LOCAL_LABEL = (
    '  (label "%s" (at %s %s %d)\n'
    "    (effects (font (size 1.27 1.27)) (justify left bottom))\n"
    "    (uuid %s)\n  )"
)


PWR_COUNTER = [0]


def emit_sheet(libs, sh, sheet_uuid, page):
    used = []
    for p in sh["parts"]:
        if p["lib_id"] not in used:
            used.append(p["lib_id"])
    for p in sh["parts"]:
        for net in p["pins"].values():
            lid = RAILS.get(net, (None,))[0]
            if lid and lid not in used:
                used.append(lid)
    lib_block = [libs.raw(lib_id) for lib_id in used]

    out = [
        "(kicad_sch (version %s) (generator eeschema)" % SCH_FORMAT_VERSION,
        "",
        "  (uuid %s)" % sheet_uuid,
        "",
        '  (paper "%s")' % sh.get("paper", "A3"),
        "",
        "  (title_block",
        '    (title "%s")' % TITLE,
        '    (date "%s")' % DATE,
        '    (rev "%s")' % REV,
        '    (company "%s")' % COMPANY,
        '    (comment 1 "%s -- %s")' % (sh["name"], sh["desc"]),
        "  )",
        "",
        "  (lib_symbols",
        "\n".join(lib_block),
        "  )",
        "",
    ]

    wires, labels, syms, ncs = [], [], [], []
    crossing = set(crossing_nets(sh))
    pwr_n = [PWR_COUNTER[0]]

    # Hand-drawn blocks: a pin the block's own wires reach is already
    # connected, so it drops its label. Deciding that per pin rather than per
    # net is what lets a block share a net with the rest of the sheet -- an
    # analog channel ends on AIN2, and the ADC over in the next column still
    # needs that name on its own pin.
    # Two passes: every block has to know which pins every other block has
    # wired before any of them can decide whether a name is private.
    blk_joined = set()
    for entry in sh.get("_blocks", []):
        blk = entry["blk"]
        for part, dx, dy in [(entry["anchor"], 0.0, 0.0)] + entry["members"]:
            for num, _nm, lx, ly, ang, hid in libs.pins(part["lib_id"]):
                if hid:
                    continue
                off, _d = pin_geometry(lx, ly, ang, part["theta"])
                px, py = dx + off[0], dy + off[1]
                if any(on_segment(px, py, a, b)
                       for poly in blk["wires"]
                       for a, b in zip(poly, poly[1:])):
                    blk_joined.add((part["ref"], num))
    for entry in sh.get("_blocks", []):
        blk, anchor = entry["blk"], entry["anchor"]
        ax, ay = anchor["x"], anchor["y"]
        # A wire must END at every junction on it. KiCad's own editor splits
        # wires when a junction is dropped, and its netlister relies on that:
        # a junction sitting mid-segment left the far half of the segment on
        # a net of its own -- SW stopped at the bootstrap cap and never
        # reached the inductor. Split here so the block tables stay readable.
        for i, poly in enumerate(blk["wires"]):
            for j, ((x1, y1), (x2, y2)) in enumerate(zip(poly, poly[1:])):
                cuts = [(jx, jy) for jx, jy in blk.get("junctions", [])
                        if min(x1, x2) <= jx <= max(x1, x2)
                        and min(y1, y2) <= jy <= max(y1, y2)
                        and (jx, jy) not in ((x1, y1), (x2, y2))]
                cuts.sort(key=lambda q: (q[0] - x1) ** 2 + (q[1] - y1) ** 2)
                pts = [(x1, y1)] + cuts + [(x2, y2)]
                for k, ((sx, sy), (ex, ey)) in enumerate(zip(pts, pts[1:])):
                    wires.append(wire_seg(ax + sx, ay + sy, ax + ex, ay + ey,
                                          det_uuid("bw:%s:%s:%d:%d:%d"
                                                   % (sh["file"], anchor["ref"],
                                                      i, j, k))))
        for k, (jx, jy) in enumerate(blk.get("junctions", [])):
            wires.append(junction(ax + jx, ay + jy,
                                  det_uuid("bj:%s:%s:%d"
                                           % (sh["file"], anchor["ref"], k))))
        # A name the block invents for its own wiring is a local label. A name
        # the rest of the design also uses has to be written the way the rest
        # of the design writes it, or ERC quite rightly complains that a local
        # and a global label share a name and mean different things.
        for net, spec in blk.get("labels", {}).items():
            lx, ly, ang = spec if len(spec) == 3 else spec + (0,)
            # Owning the net means the block's wires reach every pin on it --
            # not merely that every part is a member. The microSD connector
            # belongs to the SD block, but its VDD pin is fanned out to a
            # label like its eight neighbours, so SD_VDD is still a name the
            # sheet shares and has to be written as a global.
            elsewhere = {(p["ref"], num) for s2 in SHEETS for p in s2["parts"]
                         for num, v in p["pins"].items()
                         if v == net} - blk_joined
            uid = det_uuid("bl:%s:%s" % (sh["file"], net))
            if not elsewhere:
                labels.append(LOCAL_LABEL
                              % (net, mm(ax + lx), mm(ay + ly), ang, uid))
            elif net in crossing:
                labels.append(HIER_LABEL % (net, "bidirectional", mm(ax + lx),
                                            mm(ay + ly), ang, "left", uid))
            else:
                labels.append(
                    '  (global_label "%s" (shape input) (at %s %s %d) (fields_autoplaced)\n'
                    "    (effects (font (size 1.27 1.27)) (justify left))\n"
                    "    (uuid %s)\n"
                    '    (property "Intersheet References" "${INTERSHEET_REFS}" (at %s %s 0)\n'
                    "      (effects (font (size 1.27 1.27)) (justify left) hide)\n"
                    "    )\n  )"
                    % (net, mm(ax + lx), mm(ay + ly), ang, uid,
                       mm(ax + lx), mm(ay + ly)))
        # A rail pin the block wired up has lost its own power symbol, so the
        # block says where the rail enters instead: the inductor and the
        # feedback divider share one +5 V symbol on the output node, which is
        # what the node is.
        for net, lx, ly, facing in blk.get("rails", []):
            lib_id, shown = RAILS[net]
            sx, sy, stheta = power_placement(libs, lib_id, ax + lx, ay + ly,
                                             facing)
            pwr_n[0] += 1
            syms.append(emit_symbol(libs, sh, {
                "lib_id": lib_id, "value": shown, "voltage": "",
                "tolerance": "", "footprint": "", "mpn": "", "note": "",
                "prefix": "#PWR", "ref": "#PWR%03d" % pwr_n[0],
                "x": sx, "y": sy, "theta": stheta,
                "ext": symbol_extent(libs, lib_id, stheta),
                "_pwr_dir": facing,
            }, sheet_uuid))

    # Satellite links become a drawn wire, and both ends lose their label:
    # the connection is on the page now, so naming it twice adds nothing.
    sats = sh.get("_sats", {})
    joined, by_ref = set(blk_joined), {q["ref"]: q for q in sh["parts"]}
    for ref, info in sats.items():
        host, sat = by_ref[info["host"]], by_ref[ref]
        hoff, _hd, soff = info["wire"]
        hx, hy = host["x"] + hoff[0], host["y"] + hoff[1]
        sx, sy = sat["x"] + soff[0], sat["y"] + soff[1]
        wires.append(
            "  (wire (pts (xy %s %s) (xy %s %s))\n"
            "    (stroke (width 0) (type default))\n"
            "    (uuid %s)\n  )"
            % (mm(hx), mm(hy), mm(sx), mm(sy),
               det_uuid("link:%s:%s:%s" % (sh["file"], info["host"], ref))))
        # Both pin labels go and the name moves onto the wire, which is where
        # a schematic puts it. Dropping both without naming the wire would let
        # KiCad autoname the net Net-(U2-BST) and lose the meaning.
        labels.append(
            # The anchor has to sit exactly on the wire or KiCad treats the
            # label as floating; "left bottom" lifts the text clear instead.
            LOCAL_LABEL % (info["net"], mm(hx + (sx - hx) * 0.18),
                           mm(hy + (sy - hy) * 0.18), 0,
                           det_uuid("wlbl:%s:%s" % (sh["file"], ref))))
        joined.add((info["host"], info["host_pin"]))
        joined.add((ref, info["sat_pin"]))
    for p in sh["parts"]:
        syms.append(emit_symbol(libs, sh, p, sheet_uuid))
        for num, _, lx, ly, ang, hidden in libs.pins(p["lib_id"]):
            if hidden:
                continue  # KiCad bonds hidden power pins by name
            off, d = pin_geometry(lx, ly, ang, p["theta"])
            px, py = p["x"] + off[0], p["y"] + off[1]
            if num in p["nc"]:
                ncs.append("  (no_connect (at %s %s) (uuid %s))"
                           % (mm(px), mm(py), det_uuid("nc:%s:%s:%s" % (sh["file"], p["ref"], num))))
                continue
            net = p["pins"].get(num)
            if net is None:
                continue
            if (p["ref"], num) in joined:
                continue                   # already drawn as a wire
            ex, ey = px + d[0] * STUB, py + d[1] * STUB
            wires.append(
                "  (wire (pts (xy %s %s) (xy %s %s))\n"
                "    (stroke (width 0) (type default))\n"
                "    (uuid %s)\n  )"
                % (mm(px), mm(py), mm(ex), mm(ey),
                   det_uuid("wire:%s:%s:%s" % (sh["file"], p["ref"], num)))
            )
            # PWR_FLAG keeps a label. Giving it a power symbol instead pairs
            # the two into an island of their own that declares a rail and
            # connects to nothing you can see; a named label reads better and
            # joins the rail the same way.
            rail = None if p["prefix"].startswith("#") else RAILS.get(net)
            if rail:
                lib_id, shown = rail
                sx, sy, stheta = power_placement(libs, lib_id, ex, ey, d)
                pwr_n[0] += 1
                syms.append(emit_symbol(libs, sh, {
                    "lib_id": lib_id, "value": shown, "voltage": "",
                    "tolerance": "", "footprint": "", "mpn": "", "note": "",
                    "prefix": "#PWR", "ref": "#PWR%03d" % pwr_n[0],
                    "x": sx, "y": sy, "theta": stheta,
                    "ext": symbol_extent(libs, lib_id, stheta),
                    "_pwr_dir": d,
                }, sheet_uuid))
                continue
            if net in crossing:
                labels.append(
                    HIER_LABEL % (net, "bidirectional", mm(ex), mm(ey),
                                  label_rotation(d), label_justify(d),
                                  det_uuid("hlbl:%s:%s:%s"
                                           % (sh["file"], p["ref"], num))))
                continue
            labels.append(
                '  (global_label "%s" (shape input) (at %s %s %d) (fields_autoplaced)\n'
                "    (effects (font (size 1.27 1.27)) (justify %s))\n"
                "    (uuid %s)\n"
                '    (property "Intersheet References" "${INTERSHEET_REFS}" (at %s %s 0)\n'
                "      (effects (font (size 1.27 1.27)) (justify left) hide)\n"
                "    )\n  )"
                % (net, mm(ex), mm(ey), label_rotation(d), label_justify(d),
                   det_uuid("lbl:%s:%s:%s" % (sh["file"], p["ref"], num)), mm(ex), mm(ey))
            )

    PWR_COUNTER[0] = pwr_n[0]
    out += ncs + wires + labels + syms
    out.append("")
    out.append('  (sheet_instances\n    (path "/" (page "%d"))\n  )' % page)
    out.append(")")
    return "\n".join(out) + "\n"


def emit_root(sheet_uuids):
    out = [
        "(kicad_sch (version %s) (generator eeschema)" % SCH_FORMAT_VERSION,
        "",
        "  (uuid %s)" % ROOT_UUID,
        "",
        '  (paper "A3")',
        "",
        "  (title_block",
        '    (title "%s")' % TITLE,
        '    (date "%s")' % DATE,
        '    (rev "%s")' % REV,
        '    (company "%s")' % COMPANY,
        '    (comment 1 "Root sheet -- see the block sheets below")',
        "  )",
        "",
        "  (lib_symbols\n  )",
        "",
    ]
    y = 20.32
    root_wires, root_labels = [], []
    for i, sh in enumerate(SHEETS):
        su = sheet_uuids[sh["file"]]
        iface = crossing_nets(sh)
        height = max(25.0, 2.54 * (len(iface) + 2))
        pins = []
        for k, net in enumerate(iface):
            py = snap(y + 2.54 * (k + 1))
            pins.append(SHEET_PIN % (net, "bidirectional", mm(40.64), mm(py),
                                     det_uuid("spin:%s:%s" % (sh["file"], net))))
            # A named stub off each pin, so the top level joins the sheets by
            # name while every sheet still declares what it needs.
            root_wires.append(
                "  (wire (pts (xy %s %s) (xy %s %s))\n"
                "    (stroke (width 0) (type default))\n"
                "    (uuid %s)\n  )"
                % (mm(40.64), mm(py), mm(31.75), mm(py),
                   det_uuid("swire:%s:%s" % (sh["file"], net))))
            root_labels.append(
                '  (global_label "%s" (shape bidirectional) (at %s %s 180)'
                " (fields_autoplaced)\n"
                "    (effects (font (size 1.27 1.27)) (justify right))\n"
                "    (uuid %s)\n  )"
                % (net, mm(31.75), mm(py),
                   det_uuid("slbl:%s:%s" % (sh["file"], net))))
        block = (
            "  (sheet (at 40.64 %s) (size 120.65 %s) (fields_autoplaced)\n"
            "    (stroke (width 0.1524) (type solid))\n"
            "    (fill (color 0 0 0 0.0000))\n"
            "    (uuid %s)\n"
            '    (property "Sheetname" "%s" (at 40.64 %s 0)\n'
            "      (effects (font (size 1.27 1.27)) (justify left bottom))\n    )\n"
            '    (property "Sheetfile" "%s" (at 40.64 %s 0)\n'
            "      (effects (font (size 1.27 1.27)) (justify left top))\n    )\n"
            "    (instances\n"
            '      (project "%s"\n'
            '        (path "/%s" (page "%d"))\n'
            "      )\n    )"
            % (mm(y), mm(height), su, sh["name"], mm(y - 0.6), sh["file"],
               mm(y + height + 0.6), PROJECT, ROOT_UUID, i + 2)
        )
        if pins:
            block += "\n" + "\n".join(pins)
        out.append(block + "\n  )")
        y += height + 12.0
    out += root_wires + root_labels
    out.append("")
    out.append('  (sheet_instances\n    (path "/" (page "1"))\n  )')
    out.append(")")
    return "\n".join(out) + "\n"


PRO_TEMPLATE = """{
  "board": {"design_settings": {"rules": {"min_through_hole_diameter": 0.2}}},
  "boards": [],
  "cvpcb": {"equivalence_files": []},
  "libraries": {"pinned_footprint_libs": [], "pinned_symbol_libs": []},
  "meta": {"filename": "%s.kicad_pro", "version": 1},
  "net_settings": {
    "classes": [
      {"bus_width": 12, "clearance": 0.2, "diff_pair_gap": 0.25, "diff_pair_via_gap": 0.25,
       "diff_pair_width": 0.2, "line_style": 0, "microvia_diameter": 0.3, "microvia_drill": 0.1,
       "name": "Default", "pcb_color": "rgba(0, 0, 0, 0.000)", "schematic_color": "rgba(0, 0, 0, 0.000)",
       "track_width": 0.25, "via_diameter": 0.6, "via_drill": 0.3, "wire_width": 6},
      {"bus_width": 12, "clearance": 0.2, "diff_pair_gap": 0.25, "diff_pair_via_gap": 0.25,
       "diff_pair_width": 0.2, "line_style": 0, "microvia_diameter": 0.3, "microvia_drill": 0.1,
       "name": "Power", "pcb_color": "rgba(0, 0, 0, 0.000)", "schematic_color": "rgba(0, 0, 0, 0.000)",
       "track_width": 0.55, "via_diameter": 0.8, "via_drill": 0.4, "wire_width": 6},
      {"bus_width": 12, "clearance": 0.2, "diff_pair_gap": 0.2, "diff_pair_via_gap": 0.25,
       "diff_pair_width": 0.25, "line_style": 0, "microvia_diameter": 0.3, "microvia_drill": 0.1,
       "name": "CAN", "pcb_color": "rgba(0, 0, 0, 0.000)", "schematic_color": "rgba(0, 0, 0, 0.000)",
       "track_width": 0.25, "via_diameter": 0.6, "via_drill": 0.3, "wire_width": 6},
      {"bus_width": 12, "clearance": 0.2, "diff_pair_gap": 0.25, "diff_pair_via_gap": 0.25,
       "diff_pair_width": 0.2, "line_style": 0, "microvia_diameter": 0.3, "microvia_drill": 0.1,
       "name": "Fine", "pcb_color": "rgba(0, 0, 0, 0.000)", "schematic_color": "rgba(0, 0, 0, 0.000)",
       "track_width": 0.2, "via_diameter": 0.5, "via_drill": 0.25, "wire_width": 6}
    ],
    "meta": {"version": 3},
    "net_colors": null,
    "netclass_assignments": null,
    "netclass_patterns": [
      {"netclass": "Power", "pattern": "+5V"},
      {"netclass": "Power", "pattern": "+3V3"},
      {"netclass": "Power", "pattern": "GND"},
      {"netclass": "Power", "pattern": "+5VS"},
      {"netclass": "Power", "pattern": "OBD_VBAT*"},
      {"netclass": "Power", "pattern": "SCAP_*"},
      {"netclass": "Power", "pattern": "SD_VDD"},
      {"netclass": "Power", "pattern": "LED_5V"},
      {"netclass": "Power", "pattern": "VSENS_F"},
      {"netclass": "Power", "pattern": "VSENS_SW"},
      {"netclass": "Fine", "pattern": "USB_CC1"},
      {"netclass": "Fine", "pattern": "USB_CC2"},
      {"netclass": "CAN", "pattern": "CAN_H"},
      {"netclass": "CAN", "pattern": "CAN_L"},
      {"netclass": "CAN", "pattern": "CANH_T"},
      {"netclass": "CAN", "pattern": "CANL_T"}
    ]
  },
  "pcbnew": {"last_paths": {}, "page_layout_descr_file": ""},
  "schematic": {
    "annotate_start_num": 0,
    "legacy_lib_dir": "",
    "legacy_lib_list": [],
    "meta": {"version": 1},
    "page_layout_descr_file": "",
    "spice_current_sheet_as_root": false,
    "spice_external_command": "spice \\"%%I\\"",
    "spice_model_current_sheet_as_root": true,
    "spice_save_all_currents": false,
    "spice_save_all_voltages": false,
    "subpart_first_id": 65,
    "subpart_id_separator": 0
  },
  "sheets": [],
  "text_variables": {}
}
"""


def write_bom(path):
    rows = []
    for sh in SHEETS:
        for p in sh["parts"]:
            if p["prefix"].startswith("#") or p["lib_id"] == "Connector:TestPoint":
                continue
            rows.append(p)
    groups = {}
    for p in rows:
        key = (p["value"], p.get("voltage", ""), p.get("tolerance", ""),
               p["footprint"], p["mpn"], p["lcsc"])
        groups.setdefault(key, []).append(p["ref"])
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Qty (1 board)", "Qty (10 boards)", "References", "Value",
                    "Voltage", "Tolerance", "Footprint",
                    "Manufacturer part number", "LCSC", "Notes"])
        for (value, volt, tol, fp, mpn, lcsc), refs in sorted(
                groups.items(), key=lambda kv: kv[1][0]):
            note = next((p["note"] for p in rows if p["ref"] == refs[0] and p["note"]), "")
            w.writerow([len(refs), len(refs) * 10, " ".join(sorted(refs)),
                        value, volt, tol, fp, mpn, lcsc, note])
    return len(rows), len(groups)


def write_netlist(path, libs):
    nets = {}
    for sh in SHEETS:
        for p in sh["parts"]:
            for num, net in p["pins"].items():
                nets.setdefault(net, []).append("%s.%s" % (p["ref"], num))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("Net connectivity for %s rev %s\n" % (TITLE, REV))
        fh.write("(%d nets)\n\n" % len(nets))
        for net in sorted(nets):
            fh.write("%-14s %s\n" % (net, " ".join(sorted(nets[net]))))
    return nets


ROOT_UUID = det_uuid("root:" + PROJECT)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol-dir", default=None,
                    help="KiCad symbol library directory "
                         "(auto-detected if omitted)")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), ".."))
    args = ap.parse_args()

    if args.symbol_dir is None:
        args.symbol_dir = find_symbol_dir()
        if args.symbol_dir is None:
            raise SystemExit(
                "Could not find the KiCad symbol libraries.\n"
                "Pass --symbol-dir explicitly, e.g.\n"
                '  Windows: --symbol-dir "C:\\Program Files\\KiCad\\9.0'
                '\\share\\kicad\\symbols"\n'
                "  macOS:   --symbol-dir /Applications/KiCad/KiCad.app"
                "/Contents/SharedSupport/symbols\n"
                "  Linux:   --symbol-dir /usr/share/kicad/symbols")
        print("symbol libs : %s" % args.symbol_dir)

    libs = SymbolLibs(args.symbol_dir)
    libs.symbol("Device:R")  # force one library load so the format is known
    if libs.lib_version != VALIDATED_SYMBOL_VERSION:
        print(
            "WARNING: symbol libraries in %s are format %s, but the embedded\n"
            "         definitions were last validated against %s (KiCad 9.0).\n"
            "         A different library generation may use syntax the emitted\n"
            "         schematic format (%s) does not accept. Run gen/validate.py\n"
            "         before trusting the output."
            % (args.symbol_dir, libs.lib_version, VALIDATED_SYMBOL_VERSION,
               SCH_FORMAT_VERSION))

    # The KiCad 9 libraries moved the generic MOSFET symbols out of Device.
    # Resolve each part against whichever library the local install has; the
    # symbols are pin-identical, so this only changes the recorded lib_id.
    alternates = {
        "Device:Q_NMOS_GSD": ("Transistor_FET:Q_NMOS_GSD",),
        "Device:Q_PMOS_GSD": ("Transistor_FET:Q_PMOS_GSD",),
        "Device:Q_PNP_BEC": ("Transistor_BJT:Q_PNP_BEC",),
    }
    resolved = {}
    for sh in SHEETS:
        for p in sh["parts"]:
            lid = p["lib_id"]
            if lid not in resolved:
                resolved[lid] = lid
                if not libs.has(lid):
                    for alt in alternates.get(lid, ()):
                        if libs.has(alt):
                            print("symbol %s not in these libraries; using %s"
                                  % (lid, alt))
                            resolved[lid] = alt
                            break
            p["lib_id"] = resolved[lid]

    assign_refs()
    place(libs)

    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)

    sheet_uuids = {sh["file"]: det_uuid("sheet:" + sh["file"]) for sh in SHEETS}
    open(os.path.join(out, PROJECT + ".kicad_sch"), "w", encoding="utf-8").write(
        emit_root(sheet_uuids))
    for i, sh in enumerate(SHEETS):
        open(os.path.join(out, sh["file"]), "w", encoding="utf-8").write(
            emit_sheet(libs, sh, sheet_uuids[sh["file"]], i + 2))
    check_all_blocks_placed()
    open(os.path.join(out, PROJECT + ".kicad_pro"), "w", encoding="utf-8").write(
        PRO_TEMPLATE % PROJECT)

    n_parts, n_lines = write_bom(os.path.join(out, "bom.csv"))
    nets = write_netlist(os.path.join(out, "netlist.txt"), libs)

    print("sheets      : %d" % (len(SHEETS) + 1))
    print("components  : %d (%d distinct BOM lines)" % (n_parts, n_lines))
    print("nets        : %d" % len(nets))
    singles = [n for n, v in nets.items() if len(v) < 2]
    if singles:
        print("single-node nets (check these): %s" % ", ".join(sorted(singles)))


if __name__ == "__main__":
    main()
