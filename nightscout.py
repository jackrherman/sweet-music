#!/usr/bin/env python3
"""Nightscout glucose screen for the 64x64 matrix.

Nightscout always stores glucose as mg/dL in the `sgv` field regardless of the
display units configured in the web UI, so the mmol/L conversion happens here.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import BdfFontFile, Image, ImageDraw, ImageFont

MG_DL_PER_MMOL_L = 18.0

# Standard time-in-range thresholds in mmol/L.
LOW_MMOL = 3.9
HIGH_MMOL = 10.0
URGENT_LOW_MMOL = 3.0
URGENT_HIGH_MMOL = 13.9

# A CGM reports every ~5 minutes; past 15 the number on screen is not actionable.
STALE_SECONDS = 15 * 60

# Fully saturated primaries throughout. An LED panel at pwm_bits=8 has very few
# PWM steps near the bottom, so dimmed colours quantise badly and drift off-hue -
# (89,15,12) reads as a muddy pink rather than a deep red. Staying at or near
# full channel values keeps every colour clean on the hardware.
COLOR_IN_RANGE = (0, 255, 65)
COLOR_LOW = (255, 60, 0)
COLOR_HIGH = (255, 170, 0)
COLOR_URGENT = (255, 0, 0)
COLOR_STALE = (170, 170, 170)
COLOR_DIM = (200, 200, 200)
COLOR_BAND = (0, 150, 60)
COLOR_TRACE = (255, 255, 255)

DIRECTION_ARROWS = {
    "DoubleUp": "double_up",
    "SingleUp": "up",
    "FortyFiveUp": "up45",
    "Flat": "flat",
    "FortyFiveDown": "down45",
    "SingleDown": "down",
    "DoubleDown": "double_down",
}


@dataclass
class Reading:
    sgv: int
    mmol: float
    direction: str
    epoch_ms: int

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.epoch_ms / 1000.0)

    @property
    def is_stale(self) -> bool:
        return self.age_seconds > STALE_SECONDS


def to_mmol(sgv: float) -> float:
    return sgv / MG_DL_PER_MMOL_L


def parse_entries(payload: list[dict[str, Any]]) -> list[Reading]:
    readings: list[Reading] = []
    for entry in payload:
        sgv = entry.get("sgv")
        epoch = entry.get("date") or entry.get("mills")
        if sgv is None or epoch is None:
            continue
        readings.append(
            Reading(
                sgv=int(sgv),
                mmol=to_mmol(float(sgv)),
                direction=str(entry.get("direction") or "NONE"),
                epoch_ms=int(epoch),
            )
        )
    readings.sort(key=lambda r: r.epoch_ms, reverse=True)
    return readings


_SESSION = None


def _session():
    """One keep-alive session for the life of the process.

    A fresh TLS handshake per poll is expensive enough on an ARMv6 core to
    stall the panel, and it is pure waste when polling the same host every
    minute.
    """
    global _SESSION
    if _SESSION is None:
        import requests

        _SESSION = requests.Session()
        _SESSION.headers.update({"Accept": "application/json"})
    return _SESSION


# Free Nightscout hosting (Render and similar) idles the instance out and
# cold-starts it on the next request, which regularly takes 30-60s. A short
# read timeout means the very request that would wake the site is the one
# that gets abandoned, so the screen never recovers on its own.
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 75.0
ATTEMPTS = 2


def fetch_entries(base_url: str, count: int = 24, timeout: float | None = None) -> list[Reading]:
    url = f"{base_url.rstrip('/')}/api/v1/entries.json"
    deadline = timeout if timeout is not None else (CONNECT_TIMEOUT, READ_TIMEOUT)

    last_error: Exception | None = None
    for attempt in range(ATTEMPTS):
        try:
            response = _session().get(url, params={"count": count}, timeout=deadline)
            response.raise_for_status()
            return parse_entries(response.json())
        except Exception as exc:  # retry once: a cold start often fails then works
            last_error = exc
            if attempt + 1 < ATTEMPTS:
                # Drop the session; a half-open connection to a sleeping
                # instance would otherwise fail identically on the retry.
                global _SESSION
                _SESSION = None
    raise last_error


_FONT_CACHE: dict[str, ImageFont.ImageFont] = {}


def load_font(name: str, font_dir: Path, cache_dir: Path) -> ImageFont.ImageFont:
    """Convert one of the rpi-rgb-led-matrix BDF fonts into a PIL bitmap font.

    Pixel fonts matter at 64x64: anti-aliased TTF glyphs smear across the few
    pixels available and become unreadable.
    """
    cached = _FONT_CACHE.get(name)
    if cached is not None:
        return cached

    cache_dir.mkdir(parents=True, exist_ok=True)
    pil_path = cache_dir / f"{name}.pil"
    if not pil_path.exists():
        with (font_dir / f"{name}.bdf").open("rb") as handle:
            BdfFontFile.BdfFontFile(handle).save(str(cache_dir / name))

    font = ImageFont.load(str(pil_path))
    _FONT_CACHE[name] = font
    return font


def value_color(mmol: float, stale: bool) -> tuple[int, int, int]:
    if stale:
        return COLOR_STALE
    if mmol <= URGENT_LOW_MMOL or mmol >= URGENT_HIGH_MMOL:
        return COLOR_URGENT
    if mmol < LOW_MMOL:
        return COLOR_LOW
    if mmol > HIGH_MMOL:
        return COLOR_HIGH
    return COLOR_IN_RANGE


def format_mmol(mmol: float) -> str:
    return f"{mmol:.1f}"


def format_delta(delta: float | None) -> str:
    if delta is None:
        return ""
    return f"{delta:+.1f}"


def format_age(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 1:
        return "now"
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h{minutes % 60:02d}"


def _draw_arrow(draw: ImageDraw.ImageDraw, kind: str, x: int, y: int, color) -> None:
    """Trend arrows drawn as polygons - the BDF fonts have no arrow glyphs."""
    if kind == "flat":
        draw.line((x, y + 6, x + 10, y + 6), fill=color, width=2)
        draw.polygon([(x + 8, y + 2), (x + 12, y + 6), (x + 8, y + 10)], fill=color)
        return

    if kind in ("up", "double_up"):
        draw.line((x + 5, y + 12, x + 5, y + 2), fill=color, width=2)
        draw.polygon([(x + 1, y + 5), (x + 5, y), (x + 9, y + 5)], fill=color)
        if kind == "double_up":
            draw.polygon([(x + 1, y + 10), (x + 5, y + 5), (x + 9, y + 10)], fill=color)
        return

    if kind in ("down", "double_down"):
        draw.line((x + 5, y, x + 5, y + 10), fill=color, width=2)
        draw.polygon([(x + 1, y + 7), (x + 5, y + 12), (x + 9, y + 7)], fill=color)
        if kind == "double_down":
            draw.polygon([(x + 1, y + 2), (x + 5, y + 7), (x + 9, y + 2)], fill=color)
        return

    if kind == "up45":
        draw.line((x + 1, y + 11, x + 10, y + 2), fill=color, width=2)
        draw.polygon([(x + 5, y + 1), (x + 11, y + 1), (x + 11, y + 7)], fill=color)
        return

    if kind == "down45":
        draw.line((x + 1, y + 1, x + 10, y + 10), fill=color, width=2)
        draw.polygon([(x + 5, y + 11), (x + 11, y + 11), (x + 11, y + 5)], fill=color)
        return

    # Unknown / NOT COMPUTABLE - a question mark is more honest than a wrong arrow.
    draw.line((x + 2, y + 3, x + 8, y + 3), fill=color, width=2)
    draw.line((x + 8, y + 3, x + 5, y + 7), fill=color, width=2)
    draw.point((x + 5, y + 10), fill=color)


def _draw_sparkline(
    draw: ImageDraw.ImageDraw,
    readings: list[Reading],
    top: int,
    bottom: int,
    width: int,
) -> None:
    if len(readings) < 2:
        return

    series = list(reversed(readings))  # oldest first
    values = [r.mmol for r in series]
    lo = min(min(values), LOW_MMOL) - 0.5
    hi = max(max(values), HIGH_MMOL) + 0.5
    span = max(hi - lo, 1.0)

    def y_for(mmol: float) -> int:
        frac = (mmol - lo) / span
        return int(bottom - frac * (bottom - top))

    # Target range as two thin rules rather than a filled slab, which at 64x64
    # swamped everything else on the panel.
    left, right = 4, width - 5
    draw.line((left, y_for(HIGH_MMOL), right, y_for(HIGH_MMOL)), fill=COLOR_BAND)
    draw.line((left, y_for(LOW_MMOL), right, y_for(LOW_MMOL)), fill=COLOR_BAND)

    step = (right - left) / (len(series) - 1)
    points = [(int(left + i * step), y_for(value)) for i, value in enumerate(values)]
    draw.line(points, fill=COLOR_TRACE, width=1)
    draw.ellipse(
        (points[-1][0] - 1, points[-1][1] - 1, points[-1][0] + 1, points[-1][1] + 1),
        fill=COLOR_TRACE,
    )


def _text_width(font: ImageFont.ImageFont, text: str) -> int:
    box = font.getbbox(text)
    return box[2] - box[0]


def render_glucose(
    readings: list[Reading],
    size: int,
    font_dir: Path,
    cache_dir: Path,
) -> Image.Image:
    """Framed card layout: status-coloured border, centred value, chart beneath."""
    frame = Image.new("RGB", (size, size), (0, 0, 0))
    draw = ImageDraw.Draw(frame)

    value_font = load_font("9x18B", font_dir, cache_dir)
    small = load_font("5x7", font_dir, cache_dir)

    if not readings:
        draw.rectangle((0, 0, size - 1, size - 1), outline=COLOR_DIM, width=2)
        message = "no data"
        draw.text(
            ((size - _text_width(small, message)) // 2, size // 2 - 3),
            message,
            font=small,
            fill=COLOR_STALE,
        )
        return frame

    latest = readings[0]
    color = value_color(latest.mmol, latest.is_stale)

    draw.rectangle((0, 0, size - 1, size - 1), outline=color, width=2)

    value = format_mmol(latest.mmol)
    value_width = _text_width(value_font, value)
    # Reserve 14px to the right of the value so value+arrow sit centred as a pair.
    value_x = (size - value_width - 14) // 2
    draw.text((value_x, 10), value, font=value_font, fill=color)
    _draw_arrow(
        draw,
        DIRECTION_ARROWS.get(latest.direction, "unknown"),
        value_x + value_width + 3,
        11,
        color,
    )

    delta = format_delta(latest.mmol - readings[1].mmol) if len(readings) > 1 else ""
    age_text = format_age(latest.age_seconds)
    caption = f"{delta}   {age_text}" if delta else age_text
    caption_color = COLOR_URGENT if latest.is_stale else COLOR_DIM
    draw.text(
        ((size - _text_width(small, caption)) // 2, 31),
        caption,
        font=small,
        fill=caption_color,
    )

    _draw_sparkline(draw, readings, top=42, bottom=size - 5, width=size)
    return frame
