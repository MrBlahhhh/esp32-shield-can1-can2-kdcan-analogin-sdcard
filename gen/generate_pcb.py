#!/usr/bin/env python3
"""
Generate the KiCad board for the ESP32-S3 CAN + microSD automotive logger.

Run with KiCad's bundled Python so the pcbnew API is available:

  "C:\\Program Files\\KiCad\\9.0\\bin\\python.exe" gen/generate_pcb.py

The part and net tables in gen/generate_schematic.py are the single source
of truth: this script imports them, loads each part's real footprint from the
installed KiCad libraries (or the project library), assigns every pad its
net, and places parts into functional zones:

  - left edge: sensor harness (J10) and power/CAN harness (J1)
  - top: analog front end + ADS1115, ESP32 module with the antenna over a
    copper keepout at the top edge
  - right edge: USB-C and microSD for bench access, buttons and LEDs
  - middle: the two stacked buck islands
  - bottom edge: battery front end, and the UART0 / I2C / rail / WS2812
    headers
  - inner layers: solid GND plane (In1) and 3V3 plane (In2)

Output is placed but unrouted; gen/build_board.py runs this as stage 1 and
carries on through routing. The netclasses live in the .kicad_pro and are
preserved across this script's save -- see read_net_settings() below.
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

import pcbnew  # noqa: E402  (needs KiCad's python)

import generate_schematic as sch  # noqa: E402

# ------------------------------------------------------------------ setup ----

BOARD_W = 98.0
BOARD_H = 100.0
# The carrier is organised around the two sockets the dev board drops into,
# and they dominate everything. Each is 22 through-holes on a 2.54 mm pitch,
# so each cuts a 53.3 mm slot through every inner layer -- see the socket map
# in gen/generate_schematic.py for why the pin assignment follows from that.
#
# Layout follows the same split. The sockets sit right of centre, and the
# board is deliberately ASYMMETRIC: the J1 row carries 15 usable pins against
# J3's 9, so the circuits hanging off it need about twice the room.
#
#   left strip    x  2.0 .. 37.0   analog front end, both ADS1115s, CAN 1,
#                                  microSD -- everything on the J1 row
#   middle strip  x 42.5 .. 60.4   under the dev board. Short parts only --
#                                  about 7 mm over the socket -- and no test
#                                  points, which would be unreachable there
#   right strip   x 66.0 .. 90.0   the second CAN and its controller, the
#                                  K-line, the WS2812 buffer, OBD harness
#   header column x 91.3 .. 94.8   the three edge headers plus the spare
#   bottom band   y 72.0 .. 88.0   below the dev board: the test-point row
#                                  and the two hold-up cells
#
# ROW_PITCH IS CONFIRMED, from Espressif's own DXF -- the actual file, not a
# rendering of it: DXF_ESP32S3DevKitC1_V1_20210312CB.dxf. The pad columns
# come out of the vertex list as three x values each, the pad's left edge,
# centre and right edge:
#
#   left  row   0.640   1.270   1.910
#   right row  23.500  24.130  24.770
#
# 22 pads per column, y 7.960 .. 61.300, which is 53.34 mm = 21 * 2.54 and
# confirms the pitch at the same time.
#
#   ROW SPACING = 24.130 - 1.270 = 22.860 mm, exactly.
#
# It closes against the board too: the drawing labels a 1.27 mm inset each
# side and a 25.40 mm width, and 1.270 + 22.860 + 1.270 = 25.400.
#
# Worth stating because a plausible-looking figure of 25.4 mm row spacing on
# a 27.9 mm body circulates for this part. That is the ORIGINAL ESP32-DevKitC
# with the WROOM-32 module, which really is wider. On the S3, 25.40 mm is the
# BOARD WIDTH, not the row spacing -- and a 25.5 mm body with 25.4 mm rows
# would put the pin centres 0.05 mm from the edge, which is the tell.
ROW_PITCH = 22.86
SOCK_X1 = 40.0                 # J2, mirrors the dev board's J1 header
SOCK_X2 = SOCK_X1 + ROW_PITCH  # J3
SOCK_Y = 14.0                  # top pin; the row runs 21 * 2.54 = 53.34 down
# Body size, same drawing: 25.40 x 62.74 mm. These are deliberately left a
# touch generous -- a keepout that is 0.1 mm too big costs nothing, one that
# is 0.1 mm too small fouls the module.
DEVKIT_W = 25.5                # drawing says 25.40
DEVKIT_L = 63.0                # drawing says 62.74
# The DevKitC-1 v1.1 has a USB-C at BOTH ends -- one to the native USB
# peripheral, one to the UART bridge -- and both have to stay pluggable. The
# dev board sits about 8.5 mm up on the sockets, so a plug's overmould sweeps
# the band in front of each end at roughly that height. Anything under ~6 mm
# tall is fine there; a supercapacitor can or a pin header is not.
#
# This caught two parts on the first pass: the second hold-up cell and the
# spare-input header were both parked in the bottom band, where they would
# have made one of the two USB ports unusable. Nothing in the netlist or the
# DRC can see that -- it is a 3D clearance against a part that is not on this
# board.
USB_BAND = 12.0                # keep tall parts this far off each dev-board end
FILLET = 3.0
OUT = os.path.join(PROJ, "esp32s3-can-sd-logger.kicad_pcb")
PRO = os.path.join(PROJ, "esp32s3-can-sd-logger.kicad_pro")


# Netclasses live in the .kicad_pro, and gen/generate_schematic.py is what
# writes them.  This module builds its board with CreateEmptyBoard(), which
# knows only the Default class, and saving that board rewrites the project
# file from the board's own settings -- silently throwing Power and CAN
# away.  Everything downstream then routes at the default 0.2 mm, including
# the 1 A rails, and nothing complains: the widths are legal, just wrong.
#
# So the classes are lifted out before the save and put back after it.

def read_net_settings():
    try:
        with open(PRO, encoding="utf-8") as fh:
            return json.load(fh).get("net_settings")
    except Exception:
        return None


def restore_net_settings(keep):
    if not keep:
        return "none to restore -- run gen/generate_schematic.py first"
    with open(PRO, encoding="utf-8") as fh:
        pro = json.load(fh)
    if pro.get("net_settings") == keep:
        return "unchanged"
    pro["net_settings"] = keep
    with open(PRO, "w", encoding="utf-8") as fh:
        json.dump(pro, fh, indent=2)
    names = [c.get("name") for c in keep.get("classes", [])]
    return "restored %s (%d patterns)" % (
        ", ".join(n for n in names if n), len(keep.get("netclass_patterns", [])))

KICAD_FP = None
for pat in (r"C:\Program Files\KiCad\9.0\share\kicad\footprints",
            "/usr/share/kicad/footprints",
            "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints"):
    if os.path.isdir(pat):
        KICAD_FP = pat
        break

PROJECT_FP = os.path.join(PROJ, "footprints")


def full_value(p):
    """'100nF' + '100V' -> '100nF 100V'.

    The schematic tables split ratings out of VALUE so review tools can
    parse them, but the placement tables below are keyed on the rating as
    written, which reads better and disambiguates (there are 16 V and
    100 V 100nF parts). Reassemble for lookup only; the board's own VALUE
    field stays bare, matching the schematic.
    """
    return " ".join(x for x in (p["value"], p.get("voltage", ""),
                                p.get("tolerance", "")) if x)


def mm(v):
    return pcbnew.FromMM(v)


def pt(x, y):
    return pcbnew.VECTOR2I(mm(x), mm(y))


def lib_path(lib):
    if os.path.isdir(os.path.join(PROJECT_FP, lib + ".pretty")):
        return os.path.join(PROJECT_FP, lib + ".pretty")
    return os.path.join(KICAD_FP, lib + ".pretty")


# ------------------------------------------------------- part -> zone map ----

# Fixed placements for the structural parts: identified by MPN or value so the
# mapping survives reference renumbering.  (x, y, rotation_degrees)
FIXED = {
    # The two sockets the dev board drops into. Everything else on this
    # board is placed relative to them.
    #
    # Rotation 0, not 270: PinSocket_1x22 is drawn with its pins already
    # running down +Y, so a 270 here lays the row ACROSS the board instead of
    # down it -- 57 mm of socket through the middle of the analog front end,
    # which is what the fixed-part clash check caught.
    #
    # Pin 1 is the anchor, so the row runs from SOCK_Y to SOCK_Y + 53.34.
    "ESP32-S3-DevKitC-1 J1":    (SOCK_X1, SOCK_Y, 0),
    "ESP32-S3-DevKitC-1 J3":    (SOCK_X2, SOCK_Y, 0),

    # Pin 1 is the footprint origin and the pins run +X, so the anchor is
    # roughly 2.5 mm right of the body's left edge, not its centre. All three
    # of these were placed as if it were the centre and landed on a corner
    # mounting hole -- the clash check now includes the holes, which is what
    # finally caught it.
    #
    # These keys are the MPN. When the OBD harness went from 6 to 8 pins the
    # key changed with it, the old B6B entry stopped matching anything, and
    # the connector quietly fell through to the zone packer and was placed
    # in the middle of the board. Renaming a part renames its FIXED key.
    "JST B10B-PH-K-S(LF)(SN)":  (12.0, 6.5, 0),     # sensor harness, top left
    # Two 4-way plugs, keyed by value because they share an MPN. Each sits
    # beside the circuit it feeds rather than next to the other one -- see
    # the note on the connectors in gen/generate_schematic.py.
    "value:CAN1 + power harness": (4.0, 66.0, 90),   # left edge, by U5
    "value:Aux bus harness":      (68.0, 6.5, 0),    # top right, by U6/U7
    # 180 degrees, and the tell is the solder joints. A microSD card is
    # inserted contacts-first, so the socket's spring contacts -- and the
    # tails that solder to these pads -- are at the BACK of the slot, and the
    # mouth is at the opposite end. Pads 1-8 sit at footprint y +5.35, so at
    # 0 degrees the mouth faces -y, which on this board pointed INTO the
    # laminate: the card could only have been inserted from inside the PCB.
    #
    # I got this wrong first time by reading the U-shaped outline on the fab
    # layer at +y as the card. It is not the card, it is the contact block.
    # The pads are the evidence.
    "Hirose DM3D-SF":           (17.0, 91.5, 180),  # mouth to the bottom edge

    # Headers on the free edges. The dev board covers the middle of this
    # board end to end, so nothing can present a connector there.
    # Rotation 0, not 90: PinHeader_1x0n is drawn with its pins running down
    # +Y, so these three sit as a column against the right edge at 0. At 90
    # they lay across it and overhung the board by 8 mm.
    "value:WS2812":             (93.0, 20.0, 0),
    "value:Rail break-out":     (93.0, 33.0, 0),
    "value:I2C / Qwiic":        (93.0, 46.0, 0),
    "value:Spare diff in":      (93.0, 59.0, 0),
}

# The supercapacitors. Two 8 mm cans, the tallest parts on the board by a
# long way and the only through-hole electrolytics, so they are placed by
# hand at the right edge rather than let loose on a shelf packer -- and away
# from the dev board, which sits about 7 mm above the laminate.
PIN_FIXED = [
    # The bypass capacitors used to be pinned here by hand. They are not any
    # more -- zone_for() places a rail-only capacitor with whatever it was
    # declared under, which puts each bypass beside its own IC without any
    # coordinates that go stale when the packer moves something.

    # sheet,             value,   nets,                     x,    y,  rot
    # Bottom-right corner, below the dev board and as far from it as the
    # outline allows -- 8 mm cans beside a board sitting 8.5 mm up on sockets
    # is asking for one to foul the other during assembly.
    ("Rails + harness", "1F 2.7V", {"SCAP_TOP", "SCAP_MID"}, (72.0, 92.0, 0)),
    ("Rails + harness", "1F 2.7V", {"SCAP_MID", "GND"},      (83.0, 92.0, 0)),
]

# Nothing left to hand-shape: the two LM5164 islands went with the power
# section, and gen/route_bucks.py has nothing to route. Kept as an empty
# table rather than deleted so the placement machinery below is unchanged.
BUCK_FIXED = []


# Mounting holes: one per corner, 4 mm in from each, so the four of them
# form a square.  They used to sit inboard and at three different insets --
# a fixing beside the module, another halfway down the right edge, and the
# bottom pair 6 mm out of line with the top -- which is neither symmetric
# nor useful.  4 mm is set by H1: any further in and its keepout ring eats
# into J7, and the top header row has no slack to give.
HOLES = [(4.0, 4.0), (4.0, 96.0), (94.0, 4.0), (94.0, 96.0)]

# How much of a zone's spare height may go between its rows.  Silkscreen
# reference text is 0.8 mm, so a millimetre on top of the 0.4 mm packing gap
# is the difference between text that fits and text that collides.
ROW_SLACK = 1.0

# Auto-packed zones: (x, y, w, h) shelves filled left-to-right, top-to-bottom.
# Order matters: the first predicate that matches a part claims it.
ZONES = [
    # (name, rect, predicate). Shelves filled left-to-right, top-to-bottom;
    # the first predicate that matches a part claims it.

    # A test point under the dev board is a test point you cannot reach: the
    # DevKitC-1 covers x 38.7..64.2 from one end of the board to the other,
    # and it is not coming off with the loom plugged in. They all go in one
    # row along the bottom edge instead, between the microSD socket and the
    # hold-up cells. This predicate is FIRST so it wins over whatever zone
    # the probed net would otherwise have put them in.
    ("testpoints", (26.0, 82.0, 38.0,  7.0), lambda p, n, s: p["prefix"] == "TP"),

    # ---- left strip: everything that hangs off the J1 socket row ----------
    # The four channels are 8.7 mm wide on an 8.9 mm pitch, which leaves
    # only 0.2 mm of board between one channel's parts and the next one's --
    # and the closest two parts on this board are a ch1 diode and a ch2
    # capacitor 0.34 mm apart, across that seam.
    #
    # It cannot be widened here. Narrowing the channels to buy the gap was
    # tried at 8.5, 8.4 and 7.9: below 8.7 a resistor and a diode stop
    # fitting side by side, the column wraps to an extra row, and 30 mm of
    # height becomes 35.7. There is no more height either -- the harness is
    # above and the ADC shelf below. Four channels properly spaced needs the
    # board wider than 100 mm, which is the JLCPCB price step.
    ("ch1",       ( 2.0, 13.0,  8.7, 30.0), lambda p, n, s: n & {"AIN1_A", "AIN1_PU", "AIN1_IN", "AIN1"}),
    ("ch2",       (10.9, 13.0,  8.7, 30.0), lambda p, n, s: n & {"AIN2_A", "AIN2_PU", "AIN2_IN", "AIN2"}),
    ("ch3",       (19.8, 13.0,  8.7, 30.0), lambda p, n, s: n & {"AIN3_A", "AIN3_PU", "AIN3_IN", "AIN3"}),
    ("ch4",       (28.7, 13.0,  8.7, 30.0), lambda p, n, s: n & {"AIN4_A", "AIN4_PU", "AIN4_IN", "AIN4"}),
    # The return attenuator is a fifth channel in everything but name, and it
    # only works if it MATCHES the four above -- same values, same package,
    # same thermal environment. Placed beside them for that reason.
    ("agnd",      ( 2.0, 44.0, 23.0,  8.0), lambda p, n, s: n & {"SENS_RTN", "AGND_A", "AGND_SENSE"}),
    ("adc",       (26.0, 44.0, 11.4,  8.0), lambda p, n, s: p["mpn"] == "ADS1115IDGSR" or n & {"AIN_SP1", "AIN_SP2"} or (s == "Analog Inputs" and n <= {"+3V3", "GND"})),
    # Battery sense moved under the dev board: four flat parts on a DC
    # line, and the left edge they used to hold is where the CAN1 plug
    # has to sit to be near its transceiver.
    ("vbatsns",   (42.5, 66.0, 17.9,  6.0), lambda p, n, s: "VBAT_SNS" in n),
    ("can1",      (10.0, 54.0, 27.0, 14.0), lambda p, n, s: s == "CAN + K-line" and n & {"CAN_H", "CAN_L", "CANH_T", "CANL_T", "CAN_TX", "CAN_RX", "CAN_S", "TERM_A", "CAN_SPLIT"}),
    # One millimetre clear of can1 above and the test-point row below:
    # abutting exactly, the packer put Q1 and R27 courtyard-to-courtyard.
    ("sd",        ( 8.0, 69.0, 30.0, 12.0), lambda p, n, s: s == "SD Card"),

    # ---- right strip: the J3 row -----------------------------------------
    # The second CAN gets the most room of anything here: a controller, its
    # crystal, a transceiver, a choke, split termination and the two aux
    # jumpers. It sits nearest the J3 pins carrying its SPI bus.
    ("can2",      (66.0, 12.0, 24.0, 30.0), lambda p, n, s: n & {"CAN2_TXD", "CAN2_RXD", "CAN2_INT", "CAN2_SCK", "CAN2_MOSI", "CAN2_MISO", "CAN2_CS", "XTAL1", "XTAL2", "CAN2H_T", "CAN2L_T", "CAN2_H_C", "CAN2_L_C", "CAN2_S", "TERM2_A", "CAN2_SPLIT", "AUX_A", "AUX_B"}),
    ("kline",     (66.0, 43.0, 24.0, 15.0), lambda p, n, s: n & {"K_LINE", "K_TX", "K_RX", "K_TX_G", "K_TX_D", "K_PU"}),
    ("ws2812",    (66.0, 59.0, 24.0,  8.0), lambda p, n, s: n & {"LED_DIN_MCU", "LED_DIN_A", "LED_DIN", "LED_5V"}),
    # Right of the J3 socket, which runs down x 61..65 to y 69.
    ("holdup",    (66.0, 68.0, 24.0, 15.0), lambda p, n, s: n & {"SCAP_TOP", "SCAP_MID"}),

    # ---- middle strip: under the dev board, short parts only -------------
    ("pfd",       (42.5, 14.0, 17.9, 12.0), lambda p, n, s: n & {"PFD_SENSE", "PWR_FAIL"}),
    ("sens5v",    (42.5, 27.0, 17.9, 10.0), lambda p, n, s: n & {"VSENS_F", "+5VS"}),
    ("decoup",    (42.5, 38.0, 17.9, 30.0), lambda p, n, s: True),
]



# Nets that carry no information -- a part touching only these has no
# signal to place it near, so its position has to come from somewhere else.
RAILS_ONLY = {"GND", "+5V", "+3V3", "+5VS", "SD_VDD", "OBD_VBAT"}


def zone_for(part, sheet_name, previous=None):
    """Which shelf a part belongs on.

    A decoupling capacitor touches nothing but a rail and ground, so no
    net-based predicate can tell where it goes, and every one of them fell
    through to the catch-all shelf in the middle of the board. audit_routes
    measured the result: three 100 nF bypasses between 28 and 42 mm from the
    pin each was supposed to be bypassing, which is not a bypass -- the loop
    it closes is longer than the one it exists to shorten.

    So a rail-only capacitor inherits the zone of the part declared before
    it. In the schematic every bypass is written directly under the IC it
    serves, and the shelf packer fills in declaration order, which puts the
    two side by side. Pinning each one by hand was the other option and it is
    worse: the coordinates are relative to an IC the packer is free to move.
    """
    nets = set(part["pins"].values())
    if previous and part["prefix"] == "C" and nets <= RAILS_ONLY:
        return previous
    for name, rect, pred in ZONES:
        if pred(part, nets, sheet_name):
            return name
    return "pwr_misc"


# ------------------------------------------------------------------ build ----

# Footprints whose own 3D model KiCad does not ship, and a part of the same
# class to stand in.  This is cosmetic only -- no 3D model reaches fab -- but
# without one the part is simply absent from a render, and a USB-C port you
# cannot see in the board plot is a USB-C port nobody checks the direction of.
MODEL_SUBS = {
    "USB_C_Receptacle_HRO_TYPE-C-31-M-12":
        ("Connector_USB.3dshapes/"
         "USB_C_Receptacle_GCT_USB4105-xx-A_16P_TopMnt_Horizontal.step"),
    # The buck names an exposed pad KiCad has no model for; the 2.41x3.81
    # variant is the same SOIC-8 body.
    "SOIC-8-1EP_3.9x4.9mm_P1.27mm_EP2.95x4.9mm_Mask2.71x3.4mm_ThermalVias":
        ("Package_SO.3dshapes/"
         "SOIC-8-1EP_3.9x4.9mm_P1.27mm_EP2.41x3.81mm.step"),
    # The CAN choke is a project footprint with no model at all. Its body is
    # 4.5 x 3.2 mm, which is an 1812.
    "L_CommonMode_TDK_ACT45B":
        "Inductor_SMD.3dshapes/L_1812_4532Metric.step",
}


def substitute_model(fp, name):
    """Point a modelless footprint at a stand-in of the same class."""
    path = MODEL_SUBS.get(name)
    if path is None:
        return
    models = fp.Models()
    while len(models):
        models.pop()
    m = pcbnew.FP_3DMODEL()
    m.m_Filename = "${KICAD9_3DMODEL_DIR}/" + path
    m.m_Show = True
    models.push_back(m)


def load_footprint(fpid):
    lib, name = fpid.split(":", 1)
    fp = pcbnew.FootprintLoad(lib_path(lib), name)
    if fp is None:
        raise SystemExit("footprint not found: " + fpid)
    substitute_model(fp, name)
    return fp


def courtyard_box(fp):
    """Board-coordinate bounding box of a footprint's courtyard.

    Built from the courtyard graphics directly rather than from
    GetCourtyard(). That call returns a cached polygon which pcbnew does not
    reliably re-transform after a script rotates a footprint, so for the one
    rotated connector on this board it kept describing an unrotated outline
    a few millimetres from where the part actually is -- which showed up as
    a zone-overlap warning against a shelf eleven millimetres away, and, more
    quietly, as a fixed-part clash check that could miss a real collision.
    Each graphic item's own GetBoundingBox() is already in board coordinates
    and already rotated, so summing them needs no transform of our own.
    """
    xs, ys = [], []
    for g in fp.GraphicalItems():
        if g.GetLayer() in (pcbnew.F_CrtYd, pcbnew.B_CrtYd):
            bb = g.GetBoundingBox()
            xs += [bb.GetLeft(), bb.GetRight()]
            ys += [bb.GetTop(), bb.GetBottom()]
    if not xs:
        return fp.GetBoundingBox(False, False)
    return pcbnew.BOX2I(pcbnew.VECTOR2I(min(xs), min(ys)),
                        pcbnew.VECTOR2I(max(xs) - min(xs), max(ys) - min(ys)))

def main():
    sch.assign_refs()

    board = pcbnew.CreateEmptyBoard()
    board.SetCopperLayerCount(4)
    bds = board.GetDesignSettings()
    # JLCPCB 4-layer (JLC04161H-7628) allows 0.2mm via drills; the LM5164
    # thermal-via footprints use exactly that.
    bds.m_MinThroughDrill = mm(0.2)

    # --- nets -------------------------------------------------------------
    netnames = set()
    for sh in sch.SHEETS:
        for p in sh["parts"]:
            netnames.update(p["pins"].values())
    nets = {}
    for name in sorted(netnames):
        item = pcbnew.NETINFO_ITEM(board, name)
        board.Add(item)
        nets[name] = item

    # --- board outline (rounded rectangle) --------------------------------
    def edge_line(x1, y1, x2, y2):
        s = pcbnew.PCB_SHAPE(board, pcbnew.SHAPE_T_SEGMENT)
        s.SetStart(pt(x1, y1))
        s.SetEnd(pt(x2, y2))
        s.SetLayer(pcbnew.Edge_Cuts)
        s.SetWidth(mm(0.1))
        board.Add(s)

    def edge_arc(cx, cy, sx, sy, angle):
        s = pcbnew.PCB_SHAPE(board, pcbnew.SHAPE_T_ARC)
        s.SetCenter(pt(cx, cy))
        s.SetStart(pt(sx, sy))
        s.SetArcAngleAndEnd(pcbnew.EDA_ANGLE(angle, pcbnew.DEGREES_T), False)
        s.SetLayer(pcbnew.Edge_Cuts)
        s.SetWidth(mm(0.1))
        board.Add(s)

    # The DevKitC-1's own outline, on SILKSCREEN rather than the fabrication
    # layer. Nothing inside it may be taller than about 7 mm, the USB sockets
    # at each end have to stay reachable, and on the finished board it is the
    # difference between "why is there empty laminate here" and "that is where
    # the dev board goes, this way round". Silk costs nothing and it is under
    # the dev board anyway, so it is only ever read at assembly time.
    def devkit_outline():
        w, l = DEVKIT_W, DEVKIT_L
        cx = (SOCK_X1 + SOCK_X2) / 2.0
        y0 = SOCK_Y + 21 * 2.54 / 2.0 - l / 2.0
        for x1, y1, x2, y2 in ((cx - w / 2, y0, cx + w / 2, y0),
                               (cx + w / 2, y0, cx + w / 2, y0 + l),
                               (cx + w / 2, y0 + l, cx - w / 2, y0 + l),
                               (cx - w / 2, y0 + l, cx - w / 2, y0)):
            sh = pcbnew.PCB_SHAPE(board, pcbnew.SHAPE_T_SEGMENT)
            sh.SetStart(pt(x1, y1))
            sh.SetEnd(pt(x2, y2))
            sh.SetLayer(pcbnew.F_SilkS)
            sh.SetWidth(mm(0.15))
            board.Add(sh)
        t = pcbnew.PCB_TEXT(board)
        t.SetText("ESP32-S3-DevKitC-1")
        # Just inside the top edge of the outline, horizontal. Centred and
        # rotated it ran the length of the board straight through the
        # middle-strip parts -- legible in a plot, unreadable on a board.
        t.SetPosition(pt(cx, y0 + 2.2))
        t.SetLayer(pcbnew.F_SilkS)
        t.SetTextSize(pcbnew.VECTOR2I(mm(1.0), mm(1.0)))
        t.SetTextThickness(mm(0.15))
        board.Add(t)
    devkit_outline()

    def usb_bands():
        """The two keep-low strips in front of the dev board's USB sockets."""
        cx = (SOCK_X1 + SOCK_X2) / 2.0
        y0 = SOCK_Y + 21 * 2.54 / 2.0 - DEVKIT_L / 2.0
        for ya, yb in ((y0 - USB_BAND, y0), (y0 + DEVKIT_L, y0 + DEVKIT_L + USB_BAND)):
            ya, yb = max(0.0, ya), min(BOARD_H, yb)
            if yb - ya < 0.5:
                continue
            for x1, y1, x2, y2 in ((cx - DEVKIT_W / 2, ya, cx + DEVKIT_W / 2, ya),
                                   (cx + DEVKIT_W / 2, ya, cx + DEVKIT_W / 2, yb),
                                   (cx + DEVKIT_W / 2, yb, cx - DEVKIT_W / 2, yb),
                                   (cx - DEVKIT_W / 2, yb, cx - DEVKIT_W / 2, ya)):
                sh = pcbnew.PCB_SHAPE(board, pcbnew.SHAPE_T_SEGMENT)
                sh.SetStart(pt(x1, y1))
                sh.SetEnd(pt(x2, y2))
                sh.SetLayer(pcbnew.F_Fab)
                sh.SetWidth(mm(0.1))
                board.Add(sh)
    usb_bands()

    f, W, H = FILLET, BOARD_W, BOARD_H
    edge_line(f, 0, W - f, 0)
    edge_line(W, f, W, H - f)
    edge_line(W - f, H, f, H)
    edge_line(0, H - f, 0, f)
    edge_arc(f, f, 0, f, 90)
    edge_arc(W - f, f, W - f, 0, 90)
    edge_arc(W - f, H - f, W, H - f, 90)
    edge_arc(f, H - f, f, H, 90)

    # --- load, net, and bucket every part ---------------------------------
    buckets = {name: [] for name, _, _ in ZONES}
    fixed_parts, hole_parts = [], []
    buck_index = {}
    for sheet_name, value, netset, pos in BUCK_FIXED + PIN_FIXED:
        key = (sheet_name, value,
               frozenset(netset) if netset is not None else None)
        buck_index.setdefault(key, []).append(pos)

    last_zone, last_fp = None, None
    decoup_anchor = {}
    for sh in sch.SHEETS:
        last_zone, last_fp = None, None
        for p in sh["parts"]:
            if p["prefix"].startswith("#"):
                continue
            fp = load_footprint(p["footprint"])
            fp.SetReference(p["ref"])
            fp.SetValue(p["value"])
            for pad in fp.Pads():
                net = p["pins"].get(pad.GetNumber())
                if net is not None:
                    pad.SetNet(nets[net])
            # Solid plane connections rather than thermal spokes for the
            # LM5164 exposed pads (they are the part's heatsink) and for
            # through-hole pads on a plane net, whose spokes get starved by
            # the surrounding via field.  The board is machine-assembled, so
            # the easier-to-hand-solder thermal relief buys nothing.
            full = getattr(pcbnew, "ZONE_CONNECTION_FULL", None)
            if full is not None:
                for pad in fp.Pads():
                    thru = pad.GetAttribute() == pcbnew.PAD_ATTRIB_PTH
                    ep = p["value"].startswith("LM5164") and pad.GetNumber() == "9"
                    if ep or (thru and p["pins"].get(pad.GetNumber()) in
                              ("GND", "+3V3")):
                        pad.SetLocalZoneConnection(full)

            board.Add(fp)

            if p["footprint"].startswith("MountingHole"):
                hole_parts.append(fp)
                continue
            netset = frozenset(p["pins"].values())
            hit = None
            fv = full_value(p)
            for key in ((sh["name"], fv, netset),
                        (sh["name"], fv, None)):
                lst = buck_index.get(key)
                if lst:
                    hit = lst.pop(0)
                    break
            if hit is not None:
                x, y, rot = hit
                fp.SetOrientationDegrees(rot)
                fp.SetPosition(pt(x, y))
                fixed_parts.append(fp)
                continue
            key_m, key_v = p["mpn"], "value:" + p["value"]
            if key_m in FIXED or key_v in FIXED:
                x, y, rot = FIXED[key_m if key_m in FIXED else key_v]
                fp.SetOrientationDegrees(rot)
                fp.SetPosition(pt(x, y))
                fixed_parts.append(fp)
                continue
            zone = zone_for(p, sh["name"], last_zone)
            if (p["prefix"] == "C" and set(p["pins"].values()) <= RAILS_ONLY
                    and last_fp is not None):
                # Remember what this bypass was declared under, so the shelf
                # packer can keep the two together. Being in the right zone is
                # not enough on its own -- see the sort below.
                decoup_anchor[fp.GetReference()] = last_fp.GetReference()
            else:
                last_zone, last_fp = zone, fp
            buckets[zone].append(fp)

    for fp, (x, y) in zip(hole_parts, HOLES):
        fp.SetPosition(pt(x, y))

    unused = sum(len(v) for v in buck_index.values())
    if unused:
        print("WARNING: %d BUCK_FIXED entries matched no part" % unused)

    # The shelf packer does not know fixed parts exist. It fills each ZONES
    # rectangle as though the board underneath were empty, so a rectangle
    # drawn over a connector, a mounting hole or a pinned capacitor quietly
    # stacks parts on top of it -- and that only ever surfaced as a DRC
    # courtyard error after a twenty-minute route. Report it here instead.
    #
    # A note on trusting this, because I got it wrong once: when it first
    # flagged a rotated connector sitting in a shelf, I "verified" the
    # geometry by parsing the .kicad_pcb myself, saw no overlap, and deleted
    # the check as a false positive. My parser was what was broken -- KiCad
    # stores a footprint's graphics in FOOTPRINT-LOCAL coordinates, and I was
    # adding the placement offset without applying the rotation, so every
    # rotated part came out with its width and height swapped. DRC agreed
    # with the check. If this and a hand-rolled reading of the file ever
    # disagree again, the hand-rolled reading is the suspect.
    fixed_boxes = []
    for fp in list(fixed_parts) + list(hole_parts):
        bb = courtyard_box(fp)
        fixed_boxes.append((fp.GetReference(),
                            pcbnew.ToMM(bb.GetLeft()), pcbnew.ToMM(bb.GetTop()),
                            pcbnew.ToMM(bb.GetRight()), pcbnew.ToMM(bb.GetBottom())))
    for name, (zx, zy, zw, zh), _pred in ZONES:
        for ref, bx1, by1, bx2, by2 in fixed_boxes:
            ox = min(zx + zw, bx2) - max(zx, bx1)
            oy = min(zy + zh, by2) - max(zy, by1)
            if ox > 0.01 and oy > 0.01:
                print("     ZONE OVERLAPS FIXED  zone %-10s x %-4s %.2f x %.2f mm"
                      % (name, ref, ox, oy))

    # Report courtyard overlaps between fixed parts so the tables above can
    # be tuned against real numbers instead of guesses.
    #
    # The mounting holes are in this list too, and they were not. That gap
    # let the microSD socket, the sensor harness and the OBD harness all sit
    # on top of a hole -- three of the eleven fixed parts, none of them
    # flagged, because the check only ever compared fixed parts to each
    # other. A hole is a part: it has a courtyard, it has a keepout ring, and
    # a screw through it does not care that the netlist is clean.
    boxes = []
    for fp in list(fixed_parts) + list(hole_parts):
        bb = courtyard_box(fp)
        boxes.append((fp.GetReference(), fp.GetValue()[:14],
                      pcbnew.ToMM(bb.GetLeft()), pcbnew.ToMM(bb.GetTop()),
                      pcbnew.ToMM(bb.GetRight()), pcbnew.ToMM(bb.GetBottom())))
    clashes = 0
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            r1, v1, ax1, ay1, ax2, ay2 = boxes[i]
            r2, v2, bx1, by1, bx2, by2 = boxes[j]
            ox = min(ax2, bx2) - max(ax1, bx1)
            oy = min(ay2, by2) - max(ay1, by1)
            if ox > 0.01 and oy > 0.01:
                print("  FIXED CLASH %s(%s) x %s(%s): dx=%.2f dy=%.2f" %
                      (r1, v1, r2, v2, ox, oy))
                clashes += 1
    if clashes:
        print("  %d fixed-part clashes" % clashes)

    # --- shelf-pack each zone ---------------------------------------------
    # 1.2 mm, three times the 0.4 this board was laid out with, because it
    # is being built by hand now rather than sent to an assembly house.
    #
    # Not the 2.5 mm the gate controller uses. That board carries half the
    # parts on the same area; here 2.5 wants roughly 1400 mm2 more copper,
    # which means growing past 100 mm and out of JLCPCB's cheap tier, and
    # stretching the CAN, SPI and microSD runs to buy it. 1.2 mm fits inside
    # the outline that already exists and still triples the elbow room.
    GAP = 1.2
    ZONE_GAP = {}
    # A note on what this does NOT guarantee. The gap holds between parts in
    # the SAME zone; two zones whose rectangles touch can still put a part
    # from each against the boundary. Insetting every zone by half a gap
    # fixes that in one line and was tried -- but it costs 1.2 mm of width
    # per zone, and the four analogue channels are 8.7 mm single-file
    # columns that stop being able to pair a resistor with a diode. Making
    # that work needs the board wider than 100 mm, which is the price step.
    #
    # So the guarantee is per-zone, the median nearest-neighbour distance is
    # 1.2 mm against 0.4 before, and the handful of pairs that end up closer
    # are all across a zone boundary. audit_pcb's courtyard check is what
    # catches any that actually touch.
    report = []
    for name, (zx, zy, zw, zh), _ in ZONES:
        gap = ZONE_GAP.get(name, GAP)
        parts = buckets[name]
        # tallest first packs shelves tightly
        sized = []
        for fp in parts:
            bb = courtyard_box(fp)
            sized.append((pcbnew.ToMM(bb.GetHeight()), pcbnew.ToMM(bb.GetWidth()), fp))
        sized.sort(key=lambda t: (-t[0], -t[1]))
        # Tallest-first packs shelves tightly, and it also tears every bypass
        # capacitor away from the IC it was declared under -- an 0805 and a
        # SOIC-8 sort nowhere near each other. gen/audit_routes.py measured
        # the result at 12 to 42 mm, which is not a bypass: the loop it closes
        # is longer than the one it exists to shorten, and for a CAN
        # transceiver that loop carries a 1 Mbit/s driver.
        #
        # So pull each one back to just behind its anchor after sorting. It
        # costs a little shelf efficiency and buys a decoupling capacitor that
        # is actually decoupling something.
        anchored = [t for t in sized if t[2].GetReference() in decoup_anchor]
        if anchored:
            rest = [t for t in sized if t[2].GetReference() not in decoup_anchor]
            out = []
            for t in rest:
                out.append(t)
                for a in anchored:
                    if decoup_anchor[a[2].GetReference()] == t[2].GetReference():
                        out.append(a)
            # Any whose anchor landed in a different zone keep their sorted
            # position rather than being dropped.
            out += [a for a in anchored if a not in out]
            sized = out
        # Break into shelves first, place second.  A zone that does not need
        # its full height shares what is left between its rows: the analogue
        # channels were packing 20.7 mm of parts into a 28 mm shelf and then
        # sitting them 0.4 mm apart, which leaves the silkscreen nowhere to
        # go and is why the reference designators ran into each other.
        rows, row, x, row_h = [], [], zx, 0.0
        # Width a part needs INCLUDING the bypass that has to stay beside it.
        # Reordering the list is not enough on its own: if the IC lands at the
        # end of a row its capacitor wraps to the start of the next one, which
        # on a 24 mm shelf puts them 17 mm apart -- further than before the
        # reordering. Break the row before the pair, not between them.
        need = []
        for i, (h, w, fp) in enumerate(sized):
            extra = 0.0
            if (i + 1 < len(sized)
                    and decoup_anchor.get(sized[i + 1][2].GetReference())
                    == fp.GetReference()):
                extra = sized[i + 1][1] + gap
            need.append(w + extra)
        for i, (h, w, fp) in enumerate(sized):
            if x + need[i] > zx + zw and x > zx:
                rows.append((row, row_h))
                row, x, row_h = [], zx, 0.0
            row.append((h, w, fp))
            x += w + gap
            row_h = max(row_h, h)
        if row:
            rows.append((row, row_h))
        packed = sum(rh for _r, rh in rows) + gap * max(len(rows) - 1, 0)
        slack = 0.0
        if len(rows) > 1:
            slack = min(max(zh - packed, 0.0) / (len(rows) - 1), ROW_SLACK)
        y, bottom = zy, zy
        for members, rh in rows:
            x = zx
            for h, w, fp in members:
                # position so the courtyard's top-left lands at (x, y)
                fp.SetPosition(pt(x + w / 2.0, y + h / 2.0))
                bb = courtyard_box(fp)
                fp.Move(pt(x, y) - pcbnew.VECTOR2I(bb.GetLeft(), bb.GetTop()))
                x += w + gap
            bottom = y + rh
            y += rh + gap + slack
        used_h = bottom - zy
        report.append((name, len(parts), used_h, zh, used_h > zh))

    # --- perimeter keepout -------------------------------------------------
    # Copper-to-edge clearance is 0.5 mm; an autorouter reading the Specctra
    # export does not know that rule, so the band is made an explicit rule
    # area.  It also pulls the plane pours back from the routed edge.
    band = 0.6
    for x1, y1, x2, y2 in ((0, 0, W, band), (0, H - band, W, H),
                           (0, 0, band, H), (W - band, 0, W, H)):
        ka = pcbnew.ZONE(board)
        ka.SetIsRuleArea(True)
        ka.SetDoNotAllowCopperPour(True)
        ka.SetDoNotAllowTracks(True)
        ka.SetDoNotAllowVias(True)
        ka.SetLayerSet(pcbnew.LSET.AllCuMask(4))
        olk = ka.Outline()
        olk.NewOutline()
        for x, y in ((x1, y1), (x2, y1), (x2, y2), (x1, y2)):
            olk.Append(mm(x), mm(y))
        board.Add(ka)

    # --- footprint keepouts, promoted to board level ------------------------
    # The microSD footprint carries keepouts for the card slot and the eject
    # mechanism, and the module carries its RF area. Those live inside the
    # footprint, where the Specctra export cannot see them -- so the
    # autorouter happily ran a track through the middle of J9's card slot.
    # Board-level rule areas do get exported, and gen/maze_route.py reads
    # them too, so copying them out makes both routers aware.
    promoted = 0
    for fp in board.GetFootprints():
        for z in list(fp.Zones()):
            if not z.GetIsRuleArea():
                continue
            ka = pcbnew.ZONE(board)
            ka.SetIsRuleArea(True)
            ka.SetDoNotAllowCopperPour(True)
            ka.SetDoNotAllowTracks(True)
            ka.SetDoNotAllowVias(True)
            ka.SetLayerSet(z.GetLayerSet())
            src, dst = z.Outline(), ka.Outline()
            for i in range(src.OutlineCount()):
                dst.NewOutline()
                oc = src.Outline(i)
                for j in range(oc.PointCount()):
                    pt_ = oc.CPoint(j)
                    dst.Append(pt_.x, pt_.y)
            board.Add(ka)
            promoted += 1

    # --- inner-layer planes ------------------------------------------------
    def plane(layer, netname):
        z = pcbnew.ZONE(board)
        z.SetLayer(layer)
        z.SetNet(nets[netname])
        z.SetLocalClearance(mm(0.3))
        z.SetMinThickness(mm(0.25))
        ol2 = z.Outline()
        ol2.NewOutline()
        for x, y in ((0, 0), (W, 0), (W, H), (0, H)):
            ol2.Append(mm(x), mm(y))
        board.Add(z)
        return z

    plane(pcbnew.In1_Cu, "GND")
    plane(pcbnew.In2_Cu, "+3V3")

    # ZONE_FILLER segfaults in headless KiCad 9.0.5; the zones are saved
    # unfilled and KiCad regenerates the fill on demand (press B).

    board.SetFileName(OUT)
    keep = read_net_settings()
    pcbnew.SaveBoard(OUT, board)
    restored = restore_net_settings(keep)

    print("board       : %s" % OUT)
    print("netclasses  : %s" % restored)
    print("size        : %.0f x %.0f mm, 4 layer" % (W, H))
    n_fp = len(list(board.GetFootprints()))
    print("footprints  : %d (%d fixed, %d holes)" % (n_fp, len(fixed_parts), len(hole_parts)))
    print("nets        : %d" % len(nets))
    print("keepouts    : %d promoted from footprints" % promoted)
    for name, count, used, zh, overflow in report:
        flag = "  OVERFLOW" if overflow else ""
        print("  zone %-9s %2d parts, %5.1f/%4.1f mm used%s" % (name, count, used, zh, flag))


if __name__ == "__main__":
    main()
