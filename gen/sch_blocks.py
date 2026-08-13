#!/usr/bin/env python3
"""
Hand-drawn schematic blocks.

The generic problem -- lay out any netlist so it reads well -- is
schematic auto-layout, and it is hard. This side-steps it the same way
gen/generate_pcb.py side-steps auto-placement for the buck islands: the
recurring blocks are drawn once, by hand, as explicit coordinates and
wires, and the generator stamps them out. A person laying out this
schematic would not solve the general problem either; they would draw a
buck converter, and then draw the second one the same way.

Coordinates are millimetres relative to the block's anchor part, on the
1.27 mm grid KiCad connects on. Parts are identified the way the PCB
tables identify them -- by value and the exact set of nets they touch --
so the mapping survives reference renumbering.

Each block declares:
  anchor    (value, netset) of the part everything else is placed around
  parts     [(value, netset, dx, dy, rot)]
  wires     [[(x, y), ...]] polylines, block-relative
  junctions [(x, y)] where three or more wires meet
  labels    {net: (x, y)} one name per net, anchored on its wire
  rails     [(net, x, y)] power symbols, at the wire end they terminate
"""

from __future__ import annotations


def buck(anchor, rail, sw, en, ron, fb, bst, ramp, pg, ind, ron_r, fb_lo,
         ramp_r):
    """One LM5164 step-down, drawn the way the datasheet draws it.

    VIN and the enable divider on the left, RON below them, the bootstrap
    cap and the inductor on the right, feedback dividing back from the
    output, ramp injection under it. Both converters on this board are
    the same circuit, so both get this same drawing.
    """
    return {
        "sheet": "Power",
        "anchor": (anchor, None),
        "parts": [
            # value        nets                       dx      dy   rot
            ("100k",       {"+VBAT", en},          -22.86,  -2.54,  90),
            ("10nF",       {en, "GND"},            -19.05,   1.27,   0),
            (ron_r,        {ron, "GND"},           -15.24,  19.05,   0),
            ("2.2nF",      {bst, sw},               19.05,  -5.08,   0),
            (ind,          {sw, rail},              29.21,  -2.54,  90),
            ("100k",       {rail, fb},              45.72,   5.08,   0),
            (fb_lo,        {fb, "GND"},             45.72,  16.51,   0),
            (ramp_r,       {sw, ramp},              25.40,   3.81,   0),
            ("2.2nF",      {ramp, rail},            30.48,  11.43,   0),
            ("270pF",      {ramp, fb},              36.83,  11.43,   0),
            ("100k",       {pg, "+3V3"},            16.51,   8.89,   0),
        ],
        "wires": [
            [(-19.05, -2.54), (-12.70, -2.54)],                  # EN divider
            [(-12.70, 5.08), (-15.24, 5.08), (-15.24, 15.24)],   # RON leg
            [(12.70, -7.62), (19.05, -7.62), (19.05, -8.89)],    # BST cap
            [(12.70, -2.54), (25.40, -2.54)],                    # SW rail
            [(19.05, -2.54), (19.05, -1.27)],                    # BST cap foot
            [(25.40, -2.54), (25.40, 0.00)],                     # SW to ramp R
            [(33.02, -2.54), (45.72, -2.54), (45.72, 1.27)],     # out to divider
            [(39.37, -2.54), (39.37, -6.35)],                    # out rail stub
            [(12.70, 2.54), (41.91, 2.54), (41.91, 15.24)],      # FB sense
            [(41.91, 10.16), (45.72, 10.16)],                    # FB tap
            [(45.72, 8.89), (45.72, 12.70)],                     # divider mid
            [(36.83, 15.24), (41.91, 15.24)],                    # ramp cap to FB
            [(25.40, 7.62), (36.83, 7.62)],                      # ramp node
            [(12.70, 5.08), (16.51, 5.08)],                      # PGOOD
        ],
        # Only where a wire genuinely branches. A junction dot dropped on a
        # point that is merely a wire end -- even one two pins share -- does
        # not help and does break things: KiCad stopped carrying SW past the
        # inductor and FB past the divider tap until these came out.
        "junctions": [(19.05, -2.54), (30.48, 7.62), (39.37, -2.54),
                      (41.91, 10.16), (45.72, 10.16)],
        "rails": [(rail, 39.37, -6.35, (0, -1))],
        # Angles keep the name off the wire it names. A horizontal net label
        # printed on a short horizontal wire lands on the pin name behind it;
        # turned 90 degrees it stands clear.
        "labels": {
            sw: (13.97, -2.54, 0),
            en: (-19.05, -2.54, 0),
            fb: (20.32, 2.54, 0),
            ramp: (31.75, 7.62, 0),
            bst: (13.97, -7.62, 0),
            ron: (-15.24, 8.89, 0),
            pg: (13.97, 5.08, 0),
        },
    }


def channel(n):
    """One protected, scaled analogue input, drawn left to right.

    Signal in from the harness on the left, transient clamp straight to
    ground, series resistor, then the optional pull-up for a switch or a
    two-wire sender. The 10k/2.21k divider sets the scale, and the filter
    cap and Schottky clamp sit on the ADC node at the right. Four of these,
    identical.

    The bypass and range jumpers that used to sit between the divider and
    the ADC node are gone with the second and third input ranges -- see the
    note above the channel loop in gen/generate_schematic.py. The drawing is
    shorter by two parts and a whole column of wire.
    """
    inn, a, pu = "AIN%d_IN" % n, "AIN%d_A" % n, "AIN%d_PU" % n
    ain = "AIN%d" % n
    return {
        "sheet": "Analog Inputs",
        "anchor": ("1k", {inn, a}),
        "parts": [
            # value            nets            dx      dy   rot
            ("SMAJ40CA",   {inn, "GND"},    -12.70,   3.81, 270),
            ("PULLUP%d" % n, {"+5VS", pu},    7.62, -15.24, 270),
            ("2.49k",      {pu, a},           7.62,  -3.81,   0),
            ("10k",        {a, ain},         26.67,   0.00,  90),
            # Rotation 0 is VERTICAL for Device:R and Device:C -- 90 lays
            # them down. Both of these are legs to ground, so they stand up.
            ("2.21k",      {ain, "GND"},     35.56,   7.62,   0),
            ("470nF",      {ain, "GND"},     45.72,   7.62,   0),
            ("BAT54S", {"GND", "+3V3", ain}, 40.64, -16.51,   0),
        ],
        "wires": [
            [(-20.32, 0.00), (-3.81, 0.00)],      # harness in, clamped
            [(3.81, 0.00), (22.86, 0.00)],        # after the series resistor
            [(7.62, -11.43), (7.62, -7.62)],      # pull-up jumper to its R
            [(30.48, 0.00), (45.72, 0.00)],       # the ADC node
            [(35.56, 0.00), (35.56, 3.81)],       # divider lower leg
            [(45.72, 0.00), (45.72, 3.81)],       # filter cap
            [(40.64, -11.43), (40.64, 0.00)],     # clamp
        ],
        "junctions": [(-12.70, 0.00), (7.62, 0.00),
                      (35.56, 0.00), (40.64, 0.00), (45.72, 0.00)],
        "labels": {
            inn: (-20.32, 0.00, 0),
            a: (16.51, 0.00, 0),
            pu: (7.62, -9.53, 0),
            ain: (31.75, 0.00, 0),
        },
    }


MODULE = {
    # The module and the parts that only exist to serve it: its own
    # decoupling on a 3V3 rail across the top, and the two strapping pins
    # taken out sideways -- EN up and to the left with its pull-up, delay
    # cap and reset button, IO0 down and to the left with its pull-up and
    # the boot button. Everything else on this sheet reaches the module by
    # name, which is right: those are signals going elsewhere.
    "sheet": "Dev board",
    "anchor": ("ESP32-S3-WROOM-1-N16R8", None),
    "parts": [
        # value      nets                       dx      dy   rot
        ("10uF",  {"+3V3", "GND"},           -20.32, -41.91,   0),
        ("100nF", {"+3V3", "GND"},           -12.70, -41.91,   0),
        ("100nF", {"+3V3", "GND"},            -5.08, -41.91,   0),
        ("10k",   {"+3V3", "MCU_EN"},        -43.18, -54.61,   0),
        ("1uF",   {"MCU_EN", "GND"},         -50.80, -46.99,   0),
        ("RESET", {"MCU_EN", "GND"},         -66.04, -50.80, 180),
        ("10k",   {"+3V3", "MCU_BOOT"},      -50.80,  -8.89,   0),
        ("BOOT",  {"MCU_BOOT", "GND"},       -68.58,  -5.08, 180),
    ],
    # Every other pin down the module's left edge takes a 5.08 mm stub to a
    # label, so the stub ends all sit on x = -20.32 and the label text runs
    # out to about -32. A wire crossing that band shorts whatever it lands
    # on -- the first attempt tied IO0 to four of them -- so EN and IO0 both
    # travel out on their own pin row and only turn once they are clear.
    "wires": [
        [(-20.32, -45.72), (0.00, -45.72), (0.00, -27.94)],     # 3V3 in
        [(-16.51, -45.72), (-16.51, -50.80)],                   # rail symbol
        [(-15.24, -22.86), (-38.10, -22.86), (-38.10, -50.80),
         (-60.96, -50.80)],                                     # EN
        [(-15.24, -17.78), (-45.72, -17.78), (-45.72, -5.08),
         (-63.50, -5.08)],                                      # IO0 / boot
    ],
    "junctions": [(-12.70, -45.72), (-5.08, -45.72), (-16.51, -45.72),
                  (-43.18, -50.80), (-50.80, -50.80), (-50.80, -5.08)],
    "rails": [("+3V3", -16.51, -50.80, (0, -1))],
    "labels": {
        "MCU_EN": (-38.10, -35.56, 0),
        "MCU_BOOT": (-45.72, -11.43, 0),
    },

}


WS2812 = {
    # The shift-light output, in signal order: series damping into the level
    # shifter that lifts 3.3 V to the 5 V the LEDs actually want, its bypass
    # cap on the supply pin, another series resistor on the way out, and the
    # strip's own 5 V through a resettable fuse so a shorted strip does not
    # take the rail down with it.
    "sheet": "Dev board",
    "anchor": ("74AHCT1G125", None),
    "parts": [
        # value        nets                             dx      dy   rot
        ("33",     {"LED_DIN_MCU", "LED_DIN_A"},     -27.94,   0.00,  90),
        ("100nF",  {"+5V", "GND"},                   -12.70, -17.78, 270),
        ("100",    {"LED_DIN", "LED_DIN_J"},          27.94,   0.00,  90),
        ("0.5A hold", {"+5V", "LED_5V"},              25.40, -10.16,  90),
        ("WS2812", {"LED_5V", "LED_DIN_J", "GND"},    50.80,   0.00,   0),
    ],
    "wires": [
        [(-24.13, 0.00), (-15.24, 0.00)],                       # into the gate
        [(-5.08, -10.16), (-5.08, -17.78)],                     # VCC
        [(-8.89, -17.78), (-5.08, -17.78)],                     # bypass cap
        [(-5.08, -17.78), (-5.08, -22.86)],                     # rail symbol
        [(12.70, 0.00), (24.13, 0.00)],                         # out of the gate
        [(31.75, 0.00), (45.72, 0.00)],                         # to the strip
        [(29.21, -10.16), (36.83, -10.16), (36.83, -2.54),
         (45.72, -2.54)],                                       # fused 5V
    ],
    "junctions": [(-5.08, -17.78)],
    "rails": [("+5V", -5.08, -22.86, (0, -1))],
    "labels": {
        "LED_DIN_A": (-21.59, 0.00, 0),
        "LED_DIN": (13.97, 0.00, 0),
        "LED_DIN_J": (32.39, 0.00, 0),
        "LED_5V": (31.75, -10.16, 0),
    },
}


USB = {
    # Receptacle on the left with everything leaving to the right, in the
    # order the port uses it: VBUS through the polyfuse to the OR-ing diode
    # and its bulk, the CC pull-downs below it, then the two data pairs --
    # each duplicated on the A and B rows and tied here -- into the ESD
    # array. D- has to cross D+ once on the way: the connector presents them
    # in the opposite order to the protection device, and no arrangement of
    # the two avoids it.
    "sheet": "Dev board",
    "anchor": ("USB-C", None),
    "parts": [
        # value          nets                          dx      dy   rot
        # The polyfuse now feeds VBUS_R; the OVP switch between VBUS_R and
        # VBUS is packed near the SD socket, so in this drawing the two
        # rails just hand over through labels with a gap between them.
        ("0.5A hold", {"VBUS_IN", "VBUS_R"},         33.02, -15.24,  90),
        ("40V 1A",    {"+5V", "VBUS"},               45.72, -19.05, 270),
        ("10uF",      {"VBUS", "GND"},               57.15, -11.43,   0),
        ("100nF",     {"VBUS", "GND"},               66.04, -11.43,   0),
        ("5.1k",      {"USB_CC1", "GND"},            30.48,  -6.35,   0),
        ("5.1k",      {"USB_CC2", "GND"},            38.10,  -3.81,   0),
        ("USBLC6-2SC6", {"USB_DP_CON", "USB_DM_CON", "USB_DP", "USB_DM",
                         "VBUS", "GND"},             76.20,   5.08,   0),
    ],
    "wires": [
        [(15.24, -15.24), (29.21, -15.24)],                     # VBUS in
        [(36.83, -15.24), (40.64, -15.24)],                     # VBUS_R out
        [(43.18, -15.24), (76.20, -15.24), (76.20, 0.00)],      # VBUS
        [(15.24, -10.16), (30.48, -10.16)],                     # CC1
        [(15.24, -7.62), (38.10, -7.62)],                       # CC2
        [(15.24, 5.08), (71.12, 5.08)],                         # D+
        [(15.24, 2.54), (20.32, 2.54), (20.32, 5.08)],          # D+ A row
        [(15.24, -2.54), (25.40, -2.54), (25.40, 7.62),
         (71.12, 7.62)],                                        # D-
        [(15.24, 0.00), (25.40, 0.00)],                         # D- B row
    ],
    "junctions": [(45.72, -15.24), (57.15, -15.24), (66.04, -15.24),
                  (20.32, 5.08), (25.40, 0.00)],
    "labels": {
        "VBUS_IN": (20.32, -15.24, 0),
        "VBUS_R": (40.64, -15.24, 0),
        "VBUS": (43.18, -15.24, 0),
        "USB_CC1": (20.32, -10.16, 0),
        "USB_CC2": (17.78, -7.62, 0),
        "USB_DP_CON": (55.88, 5.08, 0),
        "USB_DM_CON": (55.88, 7.62, 0),
    },

}


FRONTEND = {
    # Battery in from the left along the top: fuse, transient clamp, ferrite,
    # then the reverse-battery FET, then the bulk on +VBAT. The LM74700 sits
    # underneath it with its three pins reaching up to the FET it drives,
    # which is how the datasheet draws it; the UVLO divider and the charge
    # pump reservoir hang off the anode side on the left.
    #
    # The gate drive crosses the source rail on its way up and around the
    # FET. That crossing carries no junction and so is not a connection --
    # the alternative was a longer way round the outside of the whole block.
    "sheet": "Power",
    "anchor": ("LM74700-Q1", None),
    "parts": [
        # value                 nets                        dx      dy   rot
        ("2A slow",  {"VBAT_IN", "VBAT_F"},              -44.45, -35.56,  90),
        ("SMCJ40CA", {"VBAT_F", "GND"},                  -40.64, -31.75, 270),
        ("600R", {"VBAT_F", "VBAT_FB"},      -26.67, -35.56,  90),
        ("IPD068N10", {"GATE_RB", "VBAT_FB", "+VBAT"},
                                                           0.00, -38.10, 270),
        ("1uF",      {"VCAP", "VBAT_FB"},                -25.40,   8.89, 270),
        ("100nF",    {"VBAT_FB", "GND"},               -12.70, -26.67,   0),
        ("100k",     {"VBAT_FB", "VBAT_UVLO"},           -31.75,  -7.62,   0),
        ("44.2k",    {"VBAT_UVLO", "GND"},               -31.75,   3.81,   0),
        ("100uF",    {"+VBAT", "GND"},                   12.70, -31.75,   0),
        ("10uF",     {"+VBAT", "GND"},                   20.32, -31.75,   0),
        ("100nF",    {"+VBAT", "GND"},                   27.94, -31.75,   0),
        # The ride-through bank lives on this same rail; drawn anywhere
        # else the two cans read as orphans.
        ("330uF",    {"+VBAT", "GND"},                   40.64, -31.75,   0),
        ("330uF",    {"+VBAT", "GND"},                   48.26, -31.75,   0),
        ("+VBAT",    {"+VBAT"},                           55.88, -35.56,   0),
    ],
    "wires": [
        [(-40.64, -35.56), (-30.48, -35.56)],                    # fused input
        [(-22.86, -35.56), (-5.08, -35.56)],                     # FET source
        [(5.08, -35.56), (55.88, -35.56)],                       # +VBAT
        [(33.02, -35.56), (33.02, -41.91)],                      # rail symbol
        [(-7.62, -10.16), (-7.62, -13.97), (-16.51, -13.97),
         (-16.51, -35.56)],                                      # ANODE sense
        [(7.62, -10.16), (7.62, -17.78), (35.56, -17.78),
         (35.56, -35.56)],                                       # CATHODE sense
        [(0.00, -10.16), (0.00, -16.51), (-8.89, -16.51),
         (-8.89, -45.72), (0.00, -45.72), (0.00, -43.18)],       # gate drive
        [(-12.70, 2.54), (-17.78, 2.54), (-17.78, 8.89),
         (-21.59, 8.89)],                                        # charge pump
        [(-29.21, 8.89), (-35.56, 8.89), (-35.56, -20.32),
         (-31.75, -20.32)],
        [(-31.75, -11.43), (-31.75, -20.32), (-16.51, -20.32)],  # UVLO top
        [(-31.75, -3.81), (-31.75, 0.00)],                       # UVLO mid
        [(-31.75, -1.27), (-27.94, -1.27), (-27.94, 5.08),
         (-12.70, 5.08)],                                        # to EN
        [(-16.51, -30.48), (-12.70, -30.48)],                    # anode cap
    ],
    "junctions": [(-16.51, -35.56),
                  (-16.51, -20.32),
                  (-16.51, -30.48), (-31.75, -1.27),
                  (12.70, -35.56), (20.32, -35.56), (27.94, -35.56),
                  (33.02, -35.56), (35.56, -35.56),
                  (40.64, -35.56), (48.26, -35.56)],
    "rails": [("+VBAT", 33.02, -41.91, (0, -1))],
    "labels": {
        "VBAT_F": (-36.83, -35.56, 0),
        "VBAT_FB": (-21.59, -35.56, 0),
        "GATE_RB": (-8.89, -30.48, 0),
        "VBAT_UVLO": (-31.75, -2.54, 0),
        "VCAP": (-20.32, 8.89, 0),
    },
}


CAN = {
    # The transceiver, then the bus, drawn as a bus: CANH along the top and
    # CANL along the bottom, with the common-mode choke, the transient
    # clamps, the split termination and the test points hanging between them
    # in the order the signal meets them.
    "sheet": "CAN + K-line",
    "anchor": ("TJA1051T/3", None),
    "parts": [
        # value                nets                        dx      dy   rot
        ("100nF",  {"+5V", "GND"},                      -10.16, -20.32,   0),
        ("100nF",  {"+3V3", "GND"},                     -20.32, -20.32,   0),
        ("10k",    {"CAN_S", "GND"},                    -17.78,   8.89,   0),
        ("51uH", {"CANH_T", "CAN_H", "CANL_T", "CAN_L"},
                                                         25.40,   0.00,   0),
        ("SMAJ26CA", {"CAN_H", "GND"},                   43.18,  -8.89, 270),
        ("SMAJ26CA", {"CAN_L", "GND"},                   43.18,  24.13, 270),
        ("TERM", {"CAN_H", "TERM_A"},       53.34,  -8.89, 270),
        ("60.4",   {"TERM_A", "CAN_SPLIT"},              53.34,   1.27,   0),
        ("60.4",   {"CAN_SPLIT", "CAN_L"},               53.34,  12.70,   0),
        ("4.7nF",  {"CAN_SPLIT", "GND"},                 60.96,   8.89,   0),
        ("CAN_H",  {"CAN_H"},                            68.58, -12.70,   0),
        ("CAN_L",  {"CAN_L"},                            68.58,  20.32, 180),
    ],
    "wires": [
        [(12.70, -2.54), (20.32, -2.54)],                     # to the choke
        [(12.70, 2.54), (20.32, 2.54)],
        [(30.48, -2.54), (35.56, -2.54), (35.56, -12.70),
         (68.58, -12.70)],                                    # CANH out
        [(30.48, 2.54), (35.56, 2.54), (35.56, 20.32),
         (68.58, 20.32)],                                     # CANL out
        [(-12.70, 5.08), (-17.78, 5.08)],                     # mode select
        [(53.34, -5.08), (53.34, -2.54)],                     # jumper to 60R
        [(53.34, 5.08), (53.34, 8.89)],                       # split node
        [(53.34, 5.08), (60.96, 5.08)],                       # split cap
        [(53.34, 16.51), (53.34, 20.32)],                     # 60R to CANL
    ],
    "junctions": [(43.18, -12.70), (53.34, -12.70),
                  (43.18, 20.32), (53.34, 20.32)],
    "labels": {
        "CANH_T": (14.61, -2.54, 0),
        "CANL_T": (14.61, 2.54, 0),
        "CAN_S": (-16.51, 5.08, 0),
        "TERM_A": (53.34, -3.81, 0),
        "CAN_SPLIT": (55.88, 5.08, 0),
        "CAN_H": (38.10, -12.70, 0),
        "CAN_L": (38.10, 20.32, 0),
    },
}


SDCARD = {
    # The card sits on the right and everything feeding it reads right to
    # left: the switched supply and its gate drive top left, the pull-up bank
    # on the switched rail beneath it, then the series terminators in line
    # with the card pins they damp.
    "sheet": "SD Card",
    "anchor": ("microSD push-pull", None),
    "parts": [
        # value      nets                          dx      dy   rot
        ("DMG2301L", {"SD_PG", "+3V3", "SD_VDD"},
                                                -72.39, -73.66, 180),
        ("100k",     {"+3V3", "SD_PG"},         -60.96, -78.74,   0),
        ("2N7002", {"SD_EN_G", "GND", "SD_PG"},
                                                -57.15, -63.50,   0),
        ("1k",       {"SD_PWR_EN", "SD_EN_G"},  -73.66, -63.50,  90),
        ("100k",     {"SD_EN_G", "GND"},        -66.04, -59.69,   0),
        # The switched rail runs down the left; the bulk and bypass caps sit
        # on it at the top, then the five pull-ups, each one reaching right
        # to the card line it holds up.
        # 10.16 apart, not 5.08. A horizontal capacitor shows its reference
        # above and its value below, and once both were moved clear of the
        # plates that is about 4 mm each way -- so at 5.08 pitch they printed
        # through the plates, and at 7.62 C13's value met C14's reference in
        # the gap between them.
        ("10uF",     {"SD_VDD", "GND"},        -100.33, -67.31, 270),
        ("100nF",    {"SD_VDD", "GND"},        -100.33, -57.15, 270),
        ("10k",      {"SD_VDD", "SD_CMD_C"},    -92.71, -50.80,  90),
        ("10k",      {"SD_VDD", "SD_D0_C"},     -92.71, -40.64,  90),
        ("10k",      {"SD_VDD", "SD_D1_C"},     -92.71, -30.48,  90),
        ("10k",      {"SD_VDD", "SD_D2_C"},     -92.71, -20.32,  90),
        ("10k",      {"SD_VDD", "SD_D3_C"},     -92.71, -10.16,  90),
        # Only the three lines that still reach the MCU get a damping
        # resistor. D1/D2/D3 stop at the card in 1-bit mode -- they keep
        # their pull-ups above, because a card samples DAT3 at power-up and
        # falls into SPI mode if it finds it low, but there is no transmission
        # line left to damp.
        ("33",       {"SD_CMD", "SD_CMD_C"},    -64.77, -10.16,  90),
        ("33",       {"SD_CLK", "SD_CLK_C"},    -64.77,   0.00,  90),
        ("33",       {"SD_D0", "SD_D0_C"},      -64.77,  10.16,  90),
        ("47k",      {"+3V3", "SD_CD"},         -64.77,  25.40,   0),
    ],
    "wires": [
        [(-74.93, -68.58), (-96.52, -68.58), (-96.52, -10.16)],  # switched rail
        [(-67.31, -73.66), (-60.96, -73.66), (-60.96, -74.93)],  # gate to R
        [(-60.96, -73.66), (-54.61, -73.66), (-54.61, -68.58)],  # gate to FET
        # The enable pull-down sits with its top pin on this wire, so the
        # junction is the whole connection -- no stub to draw.
        [(-69.85, -63.50), (-62.23, -63.50)],                    # enable
    ],
    "junctions": [(-60.96, -73.66), (-66.04, -63.50),
                  (-96.52, -67.31), (-96.52, -57.15), (-96.52, -50.80),
                  (-96.52, -40.64), (-96.52, -30.48), (-96.52, -20.32)],
    "labels": {
        "SD_PG": (-58.42, -73.66, 0),
        "SD_EN_G": (-68.58, -63.50, 0),
        "SD_VDD": (-96.52, -55.88, 0),
    },
}


RIDETHRU = {
    # The power-fail detector, read left to right the way the signal flows:
    # the sense divider drops the 5 V rail to the TLV431's REF, the 1nF
    # keeps switching noise off it, and when REF falls below 1.24 V the
    # cathode releases PWR_FAIL to its pull-up -- a rising edge into the
    # MCU. The 1M from PWR_FAIL back into the sense node is the hysteresis
    # that keeps a rail hovering at the trip from chattering it.
    "sheet": "Rails + harness",
    "anchor": ("TLV431A", {"PWR_FAIL", "PFD_SENSE", "GND"}),
    "parts": [
        # value    nets                            dx      dy   rot
        ("43k",   {"+5V", "PFD_SENSE"},         -16.51, -17.78,   0),
        ("18k",   {"PFD_SENSE", "GND"},         -16.51,  -5.08,   0),
        ("1nF",   {"PFD_SENSE", "GND"},          -7.62,  -5.08,   0),
        ("10k",   {"+3V3", "PWR_FAIL"},          10.16,  -6.35,   0),
        ("1M",    {"PWR_FAIL", "PFD_SENSE"},     -3.81,   5.08, 270),
    ],
    "wires": [
        [(-16.51, -21.59), (-16.51, -24.13), (-21.59, -24.13)],  # VBAT_F in
        [(-16.51, -13.97), (-16.51, -11.43)],                    # divider top
        [(-16.51, -11.43), (-16.51, -8.89)],                     # divider bot
        [(-16.51, -11.43), (-7.62, -11.43)],                     # sense run
        [(-7.62, -11.43), (0.00, -11.43), (0.00, -2.54)],        # into REF
        [(-7.62, -11.43), (-7.62, -8.89)],                       # filter cap
        [(2.54, 0.00), (10.16, 0.00)],                           # cathode out
        [(10.16, 0.00), (12.70, 0.00)],
        [(12.70, 0.00), (15.24, 0.00)],                          # PWR_FAIL
        [(10.16, -2.54), (10.16, 0.00)],                         # pull-up
        [(0.00, 5.08), (12.70, 5.08), (12.70, 0.00)],            # hysteresis
        [(-7.62, 5.08), (-20.32, 5.08), (-20.32, -11.43),
         (-16.51, -11.43)],                                      # back to sense
    ],
    "junctions": [(-16.51, -11.43), (-7.62, -11.43),
                  (10.16, 0.00), (12.70, 0.00)],
    "labels": {
        "+5V": (-21.59, -24.13, 180),
        "PFD_SENSE": (-20.32, -2.54, 90),
        "PWR_FAIL": (15.24, 0.00, 0),
    },
}


SENSW = {
    # The switched sensor rail. Only SENS_EN gets a label -- every other
    # net here is completely wired inside the block, and a label on a
    # fully-wired private net is ink on top of the circuit, not
    # information. Pitches leave a full symbol's worth of air between the
    # text of one part and the body of the next, and every GND symbol has
    # 10 mm of empty sheet under its pin.
    "sheet": "Power",
    "anchor": ("2N7002", {"SENS_EN_G", "GND", "SENS_G"}),
    "parts": [
        # value                   nets                       dx      dy   rot
        ("AO3401A", {"SENS_G", "+5V", "VSENS_SW"},        -10.16,  29.21, 270),
        ("100k",  {"+5V", "SENS_G"},                      -19.05,  20.32,  90),
        ("10k",   {"SENS_EN", "SENS_EN_G"},               -19.05,   0.00,  90),
        ("100k",  {"SENS_EN_G", "GND"},                   -12.70,   3.81,   0),
        ("0.2A PTC", {"VSENS_SW", "VSENS_F"},               7.62,  31.75,  90),
        ("600R",  {"VSENS_F", "+5VS"},                     25.40,  31.75,  90),
        ("10uF",  {"+5VS", "GND"},                         34.29,  35.56,   0),
        ("SMAJ6.0A", {"+5VS", "GND"},                      41.91,  35.56, 270),
    ],
    "wires": [
        [(-22.86, 0.00), (-27.94, 0.00)],                        # SENS_EN in
        [(-15.24, 0.00), (-12.70, 0.00)],
        [(-12.70, 0.00), (-5.08, 0.00)],                         # to the gate
        [(2.54, -5.08), (8.89, -5.08), (8.89, 20.32)],           # drain rise
        [(-10.16, 20.32), (8.89, 20.32)],                        # gate bus
        [(-15.24, 20.32), (-10.16, 20.32)],                      # from the pull
        [(-10.16, 20.32), (-10.16, 24.13)],                      # into the gate
        [(-22.86, 20.32), (-26.67, 20.32), (-26.67, 31.75)],     # pull to +5V
        [(-15.24, 31.75), (-26.67, 31.75)],                      # source rail
        [(-26.67, 31.75), (-29.21, 31.75), (-29.21, 27.94)],     # +5V symbol
        [(-5.08, 31.75), (-1.27, 31.75)],                        # VSENS_SW
        [(-1.27, 31.75), (3.81, 31.75)],
        [(-1.27, 31.75), (-1.27, 27.94)],                        # name spur
        [(11.43, 31.75), (16.51, 31.75)],                        # VSENS_F
        [(16.51, 31.75), (21.59, 31.75)],
        [(16.51, 31.75), (16.51, 27.94)],                        # name spur
        [(29.21, 31.75), (34.29, 31.75)],                        # +5VS
        [(34.29, 31.75), (41.91, 31.75)],
        [(41.91, 31.75), (46.99, 31.75), (46.99, 27.94)],        # rail symbol
    ],
    "junctions": [(-12.70, 0.00), (-10.16, 20.32), (-26.67, 31.75),
                  (-1.27, 31.75), (16.51, 31.75),
                  (34.29, 31.75), (41.91, 31.75)],
    "rails": [("+5V", -29.21, 27.94, (0, -1)),
              ("+5VS", 46.99, 27.94, (0, -1))],
    "labels": {
        "SENS_EN": (-27.94, 0.00, 180),
        "SENS_EN_G": (-11.43, 0.00, 0),
        "SENS_G": (-2.54, 20.32, 0),
        "VSENS_SW": (-1.27, 27.94, 90),
        "VSENS_F": (16.51, 27.94, 90),
    },
}


USBOVP = {
    # The USB overvoltage cutoff. The VBUS_R rail from the polyfuse crosses
    # the P-FET into VBUS; the divider below watches the raw side, and above
    # the trip the TLV431 sinks the PNP's base so the gate is yanked up to
    # the source and the switch opens. Gate pull-down keeps it on in normal
    # life; the 10k keeps the PNP off while the TLV431 is not conducting.
    "sheet": "Dev board",
    "anchor": ("TLV431A", {"VBUS_OV", "VBUS_OVS", "GND"}),
    "parts": [
        # value    nets                             dx      dy   rot
        ("AO3401A", {"VBUS_G", "VBUS_R", "VBUS"},  5.08, -30.48, 270),
        ("MMBT3906",   {"VBUS_OV", "VBUS_R", "VBUS_G"},    -5.08, -38.10,   0),
        ("100k",  {"VBUS_G", "GND"},                  17.78, -35.56,   0),
        ("10k",   {"VBUS_R", "VBUS_OV"},               7.62,  -7.62,   0),
        ("100k",  {"VBUS_R", "VBUS_OVS"},            -16.51, -16.51,   0),
        ("27.4k", {"VBUS_OVS", "GND"},               -16.51,  -6.35,   0),
    ],
    "wires": [
        [(-16.51, -20.32), (-16.51, -27.94)],                    # divider feed
        [(-21.59, -27.94), (-16.51, -27.94)],                    # VBUS_R in
        [(-16.51, -27.94), (-2.54, -27.94)],                     # VBUS_R rail
        [(-2.54, -27.94), (0.00, -27.94)],                       # into source
        [(-2.54, -33.02), (-2.54, -27.94)],                      # PNP emitter
        [(-2.54, -43.18), (5.08, -43.18), (5.08, -39.37)],       # PNP collector
        [(5.08, -39.37), (5.08, -35.56)],                        # to the gate
        [(5.08, -39.37), (17.78, -39.37)],                       # gate pull-down
        [(7.62, -11.43), (7.62, -27.94), (0.00, -27.94)],        # 10k to VBUS_R
        [(2.54, 0.00), (7.62, 0.00), (7.62, -3.81)],             # cathode node
        [(-16.51, -12.70), (-16.51, -11.43)],                    # divider mid
        [(-16.51, -11.43), (-16.51, -10.16)],
        [(-16.51, -11.43), (0.00, -11.43), (0.00, -2.54)],       # into REF
        [(-10.16, -38.10), (-12.70, -38.10), (-12.70, -19.05),
         (11.43, -19.05), (11.43, 0.00), (7.62, 0.00)],          # PNP base
        [(10.16, -27.94), (12.70, -27.94), (12.70, -31.75)],     # VBUS symbol
    ],
    "junctions": [(-2.54, -27.94), (0.00, -27.94), (5.08, -39.37),
                  (-16.51, -11.43), (-16.51, -27.94), (7.62, 0.00)],
    "rails": [("VBUS", 12.70, -31.75, (0, -1))],
    "labels": {
        "VBUS_R": (-21.59, -27.94, 180),
        "VBUS_OV": (11.43, -7.62, 90),
        "VBUS_OVS": (-7.62, -11.43, 0),
        "VBUS_G": (10.16, -39.37, 0),
    },
}


SPAREIO = {
    # The spare-IO header with its strap pull-downs attached, instead of
    # three resistors floating elsewhere on the sheet. The drops cross the
    # lower stubs without junctions -- crossings are not connections.
    "sheet": "Dev board",
    "anchor": ("Spare IO", None),
    "parts": [
        # value   nets              dx      dy   rot
        ("10k",  {"IO3", "GND"},   -8.89,  12.70,  0),
        ("10k",  {"IO45", "GND"}, -13.97,  12.70,  0),
        ("10k",  {"IO46", "GND"}, -19.05,  12.70,  0),
    ],
    "wires": [
        [(-5.08, -5.08), (-8.89, -5.08)],
        [(-8.89, -5.08), (-16.51, -5.08)],                       # IO3
        [(-5.08, -2.54), (-13.97, -2.54)],
        [(-13.97, -2.54), (-16.51, -2.54)],                      # IO45
        [(-5.08, 0.00), (-19.05, 0.00)],
        [(-19.05, 0.00), (-21.59, 0.00)],                        # IO46
        [(-5.08, 2.54), (-16.51, 2.54)],                         # PWR_FAIL
        [(-5.08, 5.08), (-16.51, 5.08)],                         # SENS_EN
        [(-8.89, -5.08), (-8.89, 8.89)],                         # drop 1
        [(-13.97, -2.54), (-13.97, 8.89)],                       # drop 2
        [(-19.05, 0.00), (-19.05, 8.89)],                        # drop 3
    ],
    "junctions": [(-8.89, -5.08), (-13.97, -2.54), (-19.05, 0.00)],
    "labels": {
        "IO3": (-16.51, -5.08, 180),
        "IO45": (-16.51, -2.54, 180),
        "IO46": (-21.59, 0.00, 180),
        "PWR_FAIL": (-16.51, 2.54, 180),
        "SENS_EN": (-16.51, 5.08, 180),
    },
}


QWIIC = {
    # The Qwiic header with the bus pull-ups it owns drawn on it.
    "sheet": "Dev board",
    "anchor": ("I2C / Qwiic", None),
    "parts": [
        # value    nets                    dx      dy   rot
        ("4.7k",  {"+3V3", "I2C_SDA"},   -8.89, -10.16,  0),
        ("4.7k",  {"+3V3", "I2C_SCL"},  -13.97, -10.16,  0),
    ],
    "wires": [
        [(-5.08, 2.54), (-8.89, 2.54)],
        [(-8.89, 2.54), (-16.51, 2.54)],                         # SDA
        [(-5.08, 5.08), (-13.97, 5.08)],
        [(-13.97, 5.08), (-16.51, 5.08)],                        # SCL
        [(-8.89, 2.54), (-8.89, -6.35)],                         # SDA pull-up
        [(-13.97, 5.08), (-13.97, -6.35)],                       # SCL pull-up
    ],
    "junctions": [(-8.89, 2.54), (-13.97, 5.08)],
    "labels": {
        "I2C_SDA": (-16.51, 2.54, 180),
        "I2C_SCL": (-16.51, 5.08, 180),
    },
}


UTILITY = {
    # The sheet's single-pin utility parts, gathered into one panel instead
    # of smeared one-per-row down a column: a row of test points, a row of
    # the ERC power-source flags, the mounting holes, and the power LED.
    # None of these need wires -- each pin picks up its net through the
    # label or power symbol the emitter attaches -- the block exists purely
    # so they read as a deliberate group.
    "sheet": "Rails + harness",
    "anchor": ("+5V", {"+5V"}),
    "parts": [
        # value       nets            dx      dy   rot
        # The PG_5V and PG_3V3 test points went with the two bucks; the
        # rails they reported no longer exist to report on.
        ("+3V3",     {"+3V3"},      12.70,   0.00,  0),
        ("+5VS",     {"+5VS"},      25.40,   0.00,  0),
        ("OBD_VBAT", {"OBD_VBAT"},  38.10,   0.00,  0),
        ("GND",      {"GND"},       50.80,   0.00,  0),
        ("PWR_FLAG", {"GND"},        0.00,  25.40,  0),
        ("PWR_FLAG", {"+5V"},       10.16,  25.40,  0),
        ("PWR_FLAG", {"+3V3"},      20.32,  25.40,  0),
        ("PWR_FLAG", {"+5VS"},      30.48,  25.40,  0),
        ("PWR_FLAG", {"OBD_VBAT"},  40.64,  25.40,  0),
        ("PWR_FLAG", {"SD_VDD"},    50.80,  25.40,  0),
        ("PWR_FLAG", {"AGND_SENSE"}, 60.96, 25.40,  0),
        ("M3",       set(),          0.00,  45.72,  0),
        ("M3",       set(),         10.16,  45.72,  0),
        ("M3",       set(),         20.32,  45.72,  0),
        ("M3",       set(),         30.48,  45.72,  0),
        ("1k",       {"PWR_LED_K", "GND"},   50.80, 45.72, 270),
        ("green",    {"PWR_LED_K", "+3V3"},  63.50, 45.72,   0),
    ],
    "wires": [
        [(54.61, 45.72), (57.15, 45.72)],        # 1k into the LED cathode
        [(57.15, 45.72), (59.69, 45.72)],
        [(57.15, 45.72), (57.15, 41.91)],        # name spur
    ],
    "junctions": [(57.15, 45.72)],
    "labels": {"PWR_LED_K": (57.15, 41.91, 90)},
}


BLOCKS = ([channel(n) for n in (1, 2, 3, 4)]
          + [CAN, SDCARD, WS2812, RIDETHRU, QWIIC, UTILITY])
# Gone, with the parts they drew:
#
#   buck x2, FRONTEND, SENSW   the whole 12 V power section. The dev board
#                              makes both rails from USB, so there is no
#                              converter, no ideal diode and no sensor-rail
#                              switch left to draw.
#   USB, MODULE, USBOVP,       the MCU and its surroundings. The dev board
#   SPAREIO                    brings its own USB, reset and boot buttons,
#                              and exposes every spare GPIO on the same
#                              headers this shield plugs into.
#
# Their layout tables are left in the file rather than deleted -- if this
# ever grows back into a standalone board, they are the drawing that goes
# with it. sanity_check_blocks() below is what stops that leniency turning
# into a block that silently stopped being drawn.
