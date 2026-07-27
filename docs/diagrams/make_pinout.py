#!/usr/bin/env python3
"""Generate the GPIO / HUB75 signal mapping diagram.

Pin assignments are taken from the `adafruit-hat` entry in
hzeller/rpi-rgb-led-matrix lib/hardware-mapping.c
"""
from pathlib import Path

# physical pin -> label, for a 40-pin Raspberry Pi header
HEADER = [
    "3V3", "5V", "GPIO2", "5V", "GPIO3", "GND", "GPIO4", "GPIO14",
    "GND", "GPIO15", "GPIO17", "GPIO18", "GPIO27", "GND", "GPIO22", "GPIO23",
    "3V3", "GPIO24", "GPIO10", "GND", "GPIO9", "GPIO25", "GPIO11", "GPIO8",
    "GND", "GPIO7", "ID_SD", "ID_SC", "GPIO5", "GND", "GPIO6", "GPIO12",
    "GPIO13", "GND", "GPIO19", "GPIO16", "GPIO26", "GPIO20", "GND", "GPIO21",
]

# GPIO -> HUB75 signal, from the adafruit-hat mapping
MATRIX = {
    "GPIO4": "OE", "GPIO17": "CLK", "GPIO21": "LAT",
    "GPIO22": "A", "GPIO26": "B", "GPIO27": "C", "GPIO20": "D", "GPIO24": "E",
    "GPIO5": "R1", "GPIO13": "G1", "GPIO6": "B1",
    "GPIO12": "R2", "GPIO16": "G2", "GPIO23": "B2",
}

HUB75 = [
    ("1", "R1"), ("2", "G1"), ("3", "B1"), ("4", "GND"),
    ("5", "R2"), ("6", "G2"), ("7", "B2"), ("8", "GND/E"),
    ("9", "A"), ("10", "B"), ("11", "C"), ("12", "D"),
    ("13", "CLK"), ("14", "LAT"), ("15", "OE"), ("16", "GND"),
]

W, H = 1080, 900
ROW = 30
TOP = 150
LEFT_X, RIGHT_X = 250, 420   # header column edges

out = [
    f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
    f'font-family="DejaVu Sans Mono, Consolas, monospace">',
    f'<rect width="{W}" height="{H}" fill="#ffffff"/>',
    '<text x="40" y="56" font-size="26" fill="#000">Raspberry Pi header -&gt; HUB75</text>',
    '<text x="40" y="86" font-size="13" fill="#000">Mapping used by the "adafruit-hat" profile. '
    'The bonnet makes these connections for you.</text>',
    '<text x="40" y="108" font-size="13" fill="#000">Filled pins are driven by the matrix. '
    'E is the one signal you must add by hand.</text>',
]

# header outline
out.append(f'<rect x="{LEFT_X}" y="{TOP - 20}" width="{RIGHT_X - LEFT_X}" height="{20 * ROW + 28}" '
           f'fill="#fff" stroke="#000" stroke-width="1.6"/>')
out.append(f'<text x="{(LEFT_X + RIGHT_X) / 2}" y="{TOP - 2}" font-size="12" text-anchor="middle" '
           f'fill="#000">40-PIN HEADER</text>')

for row in range(20):
    y = TOP + 20 + row * ROW
    left_pin, right_pin = row * 2 + 1, row * 2 + 2
    left_label, right_label = HEADER[row * 2], HEADER[row * 2 + 1]

    for pin, label, side in ((left_pin, left_label, "L"), (right_pin, right_label, "R")):
        used = label in MATRIX
        px = LEFT_X + 22 if side == "L" else RIGHT_X - 22
        fill = "#000" if used else "#fff"
        out.append(f'<rect x="{px - 8}" y="{y - 8}" width="16" height="16" fill="{fill}" '
                   f'stroke="#000" stroke-width="1.3"/>')
        if used:
            out.append(f'<text x="{px}" y="{y + 4}" font-size="9" text-anchor="middle" fill="#fff">{pin}</text>')
        else:
            out.append(f'<text x="{px}" y="{y + 4}" font-size="9" text-anchor="middle" fill="#000">{pin}</text>')

        weight = "bold" if used else "normal"
        if side == "L":
            out.append(f'<text x="{LEFT_X - 12}" y="{y + 4}" font-size="12" text-anchor="end" '
                       f'fill="#000" font-weight="{weight}">{label}</text>')
            if used:
                out.append(f'<text x="{LEFT_X - 96}" y="{y + 4}" font-size="12" text-anchor="end" '
                           f'fill="#000">{MATRIX[label]}</text>')
        else:
            out.append(f'<text x="{RIGHT_X + 12}" y="{y + 4}" font-size="12" fill="#000" '
                       f'font-weight="{weight}">{label}</text>')
            if used:
                out.append(f'<text x="{RIGHT_X + 96}" y="{y + 4}" font-size="12" fill="#000">{MATRIX[label]}</text>')

# HUB75 connector
HX, HY = 760, TOP + 40
out.append(f'<rect x="{HX}" y="{HY - 26}" width="215" height="{8 * 34 + 40}" fill="#fff" '
           f'stroke="#000" stroke-width="1.6"/>')
out.append(f'<text x="{HX + 107}" y="{HY - 8}" font-size="12" text-anchor="middle" fill="#000">HUB75 CONNECTOR</text>')
for index in range(8):
    y = HY + 20 + index * 34
    for col in range(2):
        pin, name = HUB75[index * 2 + col]
        px = HX + 34 + col * 110
        out.append(f'<rect x="{px - 9}" y="{y - 9}" width="18" height="18" fill="#fff" '
                   f'stroke="#000" stroke-width="1.3"/>')
        out.append(f'<text x="{px}" y="{y + 4}" font-size="9" text-anchor="middle" fill="#000">{pin}</text>')
        out.append(f'<text x="{px + 14}" y="{y + 4}" font-size="11" fill="#000">{name}</text>')

# E-line note
NY = HY + 8 * 34 + 60
out.append(f'<rect x="{HX - 10}" y="{NY}" width="230" height="120" fill="#fff" stroke="#000" stroke-width="1.4"/>')
out.append(f'<text x="{HX + 4}" y="{NY + 24}" font-size="12" fill="#000">THE E LINE</text>')
out.append(f'<text x="{HX + 4}" y="{NY + 46}" font-size="11" fill="#000">64x64 = 1/32 scan = needs E.</text>')
out.append(f'<text x="{HX + 4}" y="{NY + 66}" font-size="11" fill="#000">Bonnet leaves it unconnected.</text>')
out.append(f'<text x="{HX + 4}" y="{NY + 86}" font-size="11" fill="#000">Solder pad E to pad 8.</text>')
out.append(f'<text x="{HX + 4}" y="{NY + 106}" font-size="11" fill="#000">Garbled top half -&gt; use pad 16.</text>')

# config.txt note
out.append(f'<text x="40" y="{H - 96}" font-size="12" fill="#000">config.txt parks exactly these 14 pins '
           f'before the kernel starts, so the</text>')
out.append(f'<text x="40" y="{H - 78}" font-size="12" fill="#000">bonnet\'s 74AHCT245 buffers never see '
           f'floating inputs:</text>')
out.append(f'<text x="40" y="{H - 50}" font-size="12.5" fill="#000">gpio=4=op,dh</text>')
out.append(f'<text x="40" y="{H - 30}" font-size="12.5" fill="#000">'
           f'gpio=5,6,12,13,16,17,20,21,22,23,24,26,27=op,dl</text>')

out.append('</svg>')

Path(__file__).with_name("pinout.svg").write_text("\n".join(out))
print("wrote pinout.svg")
