#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gc
import base64
from io import BytesIO
import json
import math
import os
import secrets
import threading
import time
import urllib.parse
import urllib.request
from email.message import Message
from urllib.error import HTTPError
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageOps

import nightscout

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv() -> None:
        return None


AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
CURRENTLY_PLAYING_URL = "https://api.spotify.com/v1/me/player/currently-playing"
SCOPE = "user-read-currently-playing"


@dataclass
class PlaybackArt:
    key: str
    image_url: str
    is_playing: bool
    progress_ms: int = 0
    duration_ms: int = 0


@dataclass
class SharedPlaybackState:
    art_key: str | None = None
    image_url: str | None = None
    image: Image.Image | None = None
    is_playing: bool = False
    progress_ms: int = 0
    duration_ms: int = 0
    # Monotonic stamp of the last poll, so playback position can be advanced
    # locally between polls instead of polling often enough to animate the bar.
    sampled_at: float = 0.0

    def current_progress_ms(self) -> int:
        if not self.duration_ms:
            return 0
        progress = self.progress_ms
        if self.is_playing and self.sampled_at:
            progress += int((time.monotonic() - self.sampled_at) * 1000)
        return max(0, min(progress, self.duration_ms))


@dataclass
class HttpResponse:
    status: int
    headers: Message
    body: bytes

    def json(self) -> dict[str, Any]:
        return json.loads(self.body.decode("utf-8"))


def http_request(
    method: str,
    url: str,
    *,
    params: dict[str, str] | None = None,
    data: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10,
) -> HttpResponse:
    if params:
        separator = "&" if urllib.parse.urlparse(url).query else "?"
        url = f"{url}{separator}{urllib.parse.urlencode(params)}"

    encoded_data = urllib.parse.urlencode(data).encode("utf-8") if data else None
    request = urllib.request.Request(
        url,
        data=encoded_data,
        headers=headers or {},
        method=method,
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return HttpResponse(response.status, response.headers, response.read())
    except HTTPError as exc:
        return HttpResponse(exc.code, exc.headers, exc.read())


def raise_http_error(response: HttpResponse, context: str) -> None:
    body = response.body.decode("utf-8", errors="replace")
    raise RuntimeError(f"{context} failed with HTTP {response.status}: {body}")


class SpotifyClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        token_cache: Path,
        open_browser: bool,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.token_cache = token_cache
        self.open_browser = open_browser
        self.token = self._load_token()
        self._session = None

    @property
    def session(self):
        """Keep-alive session for the polling endpoint.

        A fresh TLS handshake every poll costs enough CPU on an ARMv6 core to
        visibly stall the matrix render loop, so the connection is reused.
        """
        if self._session is None:
            import requests

            self._session = requests.Session()
        return self._session

    def get_currently_playing(self) -> dict[str, Any] | None:
        token = self._valid_access_token()
        response = self.session.get(
            CURRENTLY_PLAYING_URL,
            params={"additional_types": "track,episode"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )

        if response.status_code == 204:
            return None
        if response.status_code == 401:
            self._refresh_access_token()
            return self.get_currently_playing()
        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", "5"))
            time.sleep(max(retry_after, 1))
            return None
        if response.status_code != 200:
            raise RuntimeError(
                f"Spotify currently-playing request failed with HTTP "
                f"{response.status_code}: {response.text}"
            )

        return response.json()

    def authorize(self) -> None:
        self._valid_access_token()

    def _valid_access_token(self) -> str:
        if not self.token:
            self.token = self._authorize()

        if time.time() >= float(self.token.get("expires_at", 0)):
            self._refresh_access_token()

        return str(self.token["access_token"])

    def _load_token(self) -> dict[str, Any] | None:
        if not self.token_cache.exists():
            return None

        # A truncated or empty cache must not be fatal: this runs unattended as a
        # boot service, so crashing here means the panel stays dark until someone
        # notices. Treat a corrupt cache as no cache and re-authorize instead.
        try:
            with self.token_cache.open("r", encoding="utf-8") as token_file:
                token = json.load(token_file)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"Ignoring unreadable token cache ({exc}); re-authorizing.", flush=True)
            return None

        if not isinstance(token, dict) or "access_token" not in token:
            print("Token cache missing access_token; re-authorizing.", flush=True)
            return None
        return token

    def _save_token(self, token: dict[str, Any]) -> None:
        self.token_cache.parent.mkdir(parents=True, exist_ok=True)
        token["expires_at"] = time.time() + int(token.get("expires_in", 3600)) - 60

        previous_refresh_token = self.token.get("refresh_token") if self.token else None
        if previous_refresh_token and "refresh_token" not in token:
            token["refresh_token"] = previous_refresh_token

        # Write-then-rename so a power cut can never leave a half-written cache.
        # Opening the real path with "w" truncates it first, which is how the
        # previous cache ended up 0 bytes after the panel was unplugged.
        temp_path = self.token_cache.with_suffix(".json.tmp")
        with temp_path.open("w", encoding="utf-8") as token_file:
            json.dump(token, token_file, indent=2)
            token_file.flush()
            os.fsync(token_file.fileno())
        os.replace(temp_path, self.token_cache)

        self.token = token

    def _authorize(self) -> dict[str, Any]:
        state = secrets.token_urlsafe(18)
        parsed_redirect = urllib.parse.urlparse(self.redirect_uri)
        if parsed_redirect.hostname not in {"127.0.0.1", "localhost"}:
            raise RuntimeError("This script expects a localhost Spotify redirect URI.")

        callback = LocalCallbackServer(
            host=parsed_redirect.hostname or "127.0.0.1",
            port=parsed_redirect.port or 80,
            path=parsed_redirect.path or "/callback",
            expected_state=state,
        )

        query = urllib.parse.urlencode(
            {
                "client_id": self.client_id,
                "response_type": "code",
                "redirect_uri": self.redirect_uri,
                "scope": SCOPE,
                "state": state,
            }
        )
        auth_url = f"{AUTH_URL}?{query}"

        print("Authorize Spotify in your browser:")
        print(auth_url)
        if self.open_browser:
            webbrowser.open(auth_url)

        code = callback.wait_for_code()
        token = self._post_token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
            }
        )
        self._save_token(token)
        return token

    def _refresh_access_token(self) -> None:
        refresh_token = self.token.get("refresh_token") if self.token else None
        if not refresh_token:
            self.token = self._authorize()
            return

        token = self._post_token(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        )
        self._save_token(token)

    def _post_token(self, data: dict[str, str]) -> dict[str, Any]:
        credentials = f"{self.client_id}:{self.client_secret}".encode("utf-8")
        basic_auth = base64.b64encode(credentials).decode("ascii")
        response = http_request(
            "POST",
            TOKEN_URL,
            data=data,
            headers={
                "Authorization": f"Basic {basic_auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=10,
        )
        if response.status != 200:
            raise_http_error(response, "Spotify token request")
        return response.json()


class LocalCallbackServer:
    def __init__(self, host: str, port: int, path: str, expected_state: str) -> None:
        self.code: str | None = None
        self.error: str | None = None
        self.state_error: str | None = None
        self.path = path
        self.expected_state = expected_state

        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parsed = urllib.parse.urlparse(self.path)
                params = urllib.parse.parse_qs(parsed.query)

                if parsed.path != parent.path:
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b"Wrong callback path.")
                    return

                returned_state = params.get("state", [""])[0]
                if returned_state != parent.expected_state:
                    parent.state_error = "Spotify callback state did not match."
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b"State mismatch.")
                    return

                if "error" in params:
                    parent.error = params["error"][0]
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b"Spotify authorization failed.")
                    return

                parent.code = params.get("code", [None])[0]
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"Spotify authorization complete. You can close this tab.")

            def log_message(self, format: str, *args: Any) -> None:
                return

        self.server = HTTPServer((host, port), Handler)

    def wait_for_code(self) -> str:
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        try:
            while not self.code and not self.error and not self.state_error:
                time.sleep(0.1)
        finally:
            self.server.shutdown()
            self.server.server_close()

        if self.state_error:
            raise RuntimeError(self.state_error)
        if self.error:
            raise RuntimeError(f"Spotify authorization failed: {self.error}")
        if not self.code:
            raise RuntimeError("Spotify authorization did not return a code.")
        return self.code


class MatrixDisplay:
    def __init__(self, args: argparse.Namespace) -> None:
        try:
            from rgbmatrix import RGBMatrix, RGBMatrixOptions
        except ImportError as exc:
            raise RuntimeError(
                "The rgbmatrix Python bindings are not installed. "
                "Install hzeller/rpi-rgb-led-matrix on the Pi, or run with --mock-output."
            ) from exc

        options = RGBMatrixOptions()
        options.rows = args.rows
        options.cols = args.cols
        options.chain_length = args.chain_length
        options.parallel = args.parallel
        options.brightness = args.brightness
        options.gpio_slowdown = args.gpio_slowdown
        options.hardware_mapping = args.hardware_mapping
        options.pwm_bits = args.pwm_bits
        options.pwm_lsb_nanoseconds = args.pwm_lsb_nanoseconds
        options.limit_refresh_rate_hz = args.limit_refresh_rate_hz
        options.disable_hardware_pulsing = args.no_hardware_pulse
        # The library drops to the `daemon` user after init by default. A 0700 home
        # directory then makes the token cache and nightscout.py unreadable.
        options.drop_privileges = False

        self.matrix = RGBMatrix(options=options)
        self.canvas = self.matrix.CreateFrameCanvas()

    def show(self, image: Image.Image) -> None:
        self.canvas.SetImage(image.convert("RGB"))
        self.canvas = self.matrix.SwapOnVSync(self.canvas)

    def clear(self) -> None:
        self.matrix.Clear()


class MockDisplay:
    def __init__(self, output: Path) -> None:
        self.output = output
        self.output.parent.mkdir(parents=True, exist_ok=True)

    def show(self, image: Image.Image) -> None:
        image.save(self.output)

    def clear(self) -> None:
        return


def demo_album_art(size: int) -> Image.Image:
    image = Image.new("RGB", (size, size), (18, 18, 18))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, size // 2, size // 2), fill=(238, 70, 60))
    draw.rectangle((size // 2, 0, size, size // 2), fill=(245, 180, 40))
    draw.rectangle((0, size // 2, size // 2, size), fill=(35, 150, 235))
    draw.rectangle((size // 2, size // 2, size, size), fill=(65, 185, 95))
    draw.line((0, 0, size, size), fill=(255, 255, 255), width=max(2, size // 18))
    draw.line((size, 0, 0, size), fill=(0, 0, 0), width=max(2, size // 22))
    return image


def playback_art_from_response(playback: dict[str, Any] | None) -> PlaybackArt | None:
    if not playback:
        return None

    item = playback.get("item")
    if not item:
        return None

    item_type = item.get("type")
    if item_type == "track":
        images = item.get("album", {}).get("images", [])
    else:
        images = item.get("images", [])

    if not images:
        return None

    # Spotify offers several sizes (typically 640/300/64). Downscaling the 640px
    # version to 64x64 with LANCZOS costs ~3s on a Pi Zero and stalls the panel on
    # every track change, so take the smallest image that still covers the panel.
    usable = [candidate for candidate in images if (candidate.get("width") or 0) >= 64]
    if usable:
        image = min(usable, key=lambda candidate: candidate["width"])
    else:
        image = max(images, key=lambda candidate: candidate.get("width") or 0)
    item_id = item.get("id") or item.get("uri") or image["url"]
    return PlaybackArt(
        key=str(item_id),
        image_url=image["url"],
        is_playing=bool(playback.get("is_playing")),
        progress_ms=int(playback.get("progress_ms") or 0),
        duration_ms=int(item.get("duration_ms") or 0),
    )


def download_image(url: str) -> Image.Image:
    import requests

    response = requests.get(url, timeout=15)
    response.raise_for_status()
    return Image.open(BytesIO(response.content)).convert("RGB")


_DISC_MASKS: dict[int, Image.Image] = {}


def _disc_mask(disc_size: int) -> Image.Image:
    mask = _DISC_MASKS.get(disc_size)
    if mask is None:
        mask = Image.new("L", (disc_size, disc_size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, disc_size - 1, disc_size - 1), fill=255)
        _DISC_MASKS[disc_size] = mask
    return mask


def _fitted_art(art: Image.Image, disc_size: int) -> Image.Image:
    # Downscaling 640x640 album art with LANCZOS costs ~300ms on a Pi Zero, which
    # dominates the frame budget. The fitted square only changes when the track
    # does, so cache it on the source image and rotate the small square instead.
    cached = getattr(art, "_fitted_square", None)
    if cached is not None and cached.size == (disc_size, disc_size):
        return cached

    fitted = ImageOps.fit(
        art, (disc_size, disc_size), method=Image.Resampling.LANCZOS
    ).convert("RGBA")
    try:
        art._fitted_square = fitted  # type: ignore[attr-defined]
    except AttributeError:
        pass
    return fitted


def render_record(art: Image.Image | None, angle: float, size: int) -> Image.Image:
    frame = Image.new("RGBA", (size, size), (0, 0, 0, 255))
    if art is None:
        return frame.convert("RGB")

    margin = max(2, size // 32)
    disc_size = size - margin * 2
    # The album art is the record surface: rotate it first, then cut it into a circular disk.
    art_square = _fitted_art(art, disc_size)
    rotated = art_square.rotate(angle, resample=Image.Resampling.BICUBIC)

    frame.paste(rotated, (margin, margin), _disc_mask(disc_size))

    draw = ImageDraw.Draw(frame, "RGBA")
    outer = (margin, margin, size - margin - 1, size - margin - 1)
    draw.ellipse(outer, outline=(6, 6, 6, 255), width=max(1, size // 32))

    center = size // 2
    label_radius = max(5, size // 11)
    hole_radius = max(2, size // 25)
    draw.ellipse(
        (
            center - label_radius,
            center - label_radius,
            center + label_radius,
            center + label_radius,
        ),
        fill=(16, 16, 16, 210),
        outline=(220, 220, 220, 90),
    )
    draw.ellipse(
        (
            center - hole_radius,
            center - hole_radius,
            center + hole_radius,
            center + hole_radius,
        ),
        fill=(0, 0, 0, 255),
    )
    return frame.convert("RGB")


_GLUCOSE_CACHE: dict[tuple[int, int, int], Image.Image] = {}


def render_glucose_screen(
    args: argparse.Namespace,
    state: "SharedGlucoseState",
    state_lock: threading.Lock,
    size: int,
) -> Image.Image | None:
    if not args.nightscout_url:
        return None

    with state_lock:
        readings = state.readings

    if not readings:
        return None

    latest = readings[0]
    # The picture only changes when a new reading lands or the age ticks over a
    # minute, so cache instead of re-rendering text and a sparkline every frame.
    key = (latest.epoch_ms, int(latest.age_seconds // 60), size)
    cached = _GLUCOSE_CACHE.get(key)
    if cached is not None:
        return cached

    image = nightscout.render_glucose(
        readings, size, args.font_dir, args.font_cache_dir
    )
    _GLUCOSE_CACHE.clear()
    _GLUCOSE_CACHE[key] = image
    return image


PROGRESS_HEIGHT = 2
PROGRESS_MARGIN = 3
PROGRESS_TRACK = (90, 90, 90)
PROGRESS_FILL = (255, 255, 255)
# Bottom rows are darkened before the bar is drawn so it stays readable over
# light artwork, the same trick Spotify's own overlay uses.
SCRIM_ROWS = 8
SCRIM_STRENGTH = 0.55


def fit_art(art: Image.Image, size: int) -> Image.Image:
    """Album art cropped to fill the whole panel, cached on the source image."""
    cached = getattr(art, "_full_panel", None)
    if cached is not None and cached.size == (size, size):
        return cached

    fitted = ImageOps.fit(
        art, (size, size), method=Image.Resampling.LANCZOS
    ).convert("RGB")
    try:
        art._full_panel = fitted  # type: ignore[attr-defined]
    except AttributeError:
        pass
    return fitted


def render_now_playing(
    art: Image.Image, size: int, progress_ms: int, duration_ms: int
) -> tuple[Image.Image, int]:
    """Full-bleed art with a progress bar along the bottom.

    Returns the frame and the filled pixel width, which the caller uses as a
    cache key: the bar only moves one pixel every duration/size seconds (~2.8s
    for a 3 minute track), so almost every frame is identical to the last.
    """
    left = PROGRESS_MARGIN
    right = size - 1 - PROGRESS_MARGIN
    span = right - left
    fraction = 0.0
    if duration_ms > 0:
        fraction = min(1.0, max(0.0, progress_ms / duration_ms))
    filled = int(span * fraction)

    # The composed frame only changes when the bar advances a pixel - roughly
    # every 3 seconds on a normal track - so keep one per bar position.
    cache = getattr(art, "_now_playing_frames", None)
    if cache is None or cache.get("size") != size:
        cache = {"size": size, "frames": {}}
        try:
            art._now_playing_frames = cache  # type: ignore[attr-defined]
        except AttributeError:
            pass

    existing = cache["frames"].get(filled)
    if existing is not None:
        return existing, filled

    fill_color, track_color = bar_colors(art, size)
    frame = _scrimmed_art(art, size).copy()
    draw = ImageDraw.Draw(frame)
    top = size - PROGRESS_MARGIN - PROGRESS_HEIGHT
    bottom = top + PROGRESS_HEIGHT - 1
    draw.rectangle((left, top, right, bottom), fill=track_color)
    if filled > 0:
        draw.rectangle((left, top, left + filled, bottom), fill=fill_color)
    cache["frames"][filled] = frame
    return frame, filled


def _luminance(color: tuple[int, int, int]) -> float:
    red, green, blue = color
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


HUE_BINS = 36
# Pixels below these thresholds carry no usable hue - near-black, near-white and
# washed-out greys would otherwise vote for arbitrary hues and skew the gap search.
HUE_MIN_SATURATION = 0.25
HUE_MIN_VALUE = 0.15


def _unused_hue(art: Image.Image) -> float | None:
    """Find the hue least used by the whole artwork.

    Builds a saturation-weighted hue histogram over the entire image, then
    returns the middle of the widest unoccupied arc. A bar in that hue contrasts
    with every colour actually present, not just whatever sits directly behind
    it.
    """
    import colorsys

    sample = art.convert("RGB").resize((32, 32), Image.Resampling.BILINEAR)
    weights = [0.0] * HUE_BINS
    total = 0.0

    for red, green, blue in sample.getdata():
        hue, saturation, value = colorsys.rgb_to_hsv(red / 255.0, green / 255.0, blue / 255.0)
        if saturation < HUE_MIN_SATURATION or value < HUE_MIN_VALUE:
            continue
        weight = saturation * value
        weights[int(hue * HUE_BINS) % HUE_BINS] += weight
        total += weight

    if total < 20.0:  # effectively greyscale artwork - no hue to avoid
        return None

    # Spread each bin's weight into its neighbours so a hue adjacent to a heavily
    # used one is not treated as free.
    smeared = [
        weights[(index - 1) % HUE_BINS] * 0.5
        + weights[index]
        + weights[(index + 1) % HUE_BINS] * 0.5
        for index in range(HUE_BINS)
    ]
    threshold = total * 0.01
    occupied = [value > threshold for value in smeared]

    if not any(occupied):
        return None
    if all(occupied):
        # Every hue is in use, so take the least-used bin instead of a gap.
        return (smeared.index(min(smeared)) + 0.5) / HUE_BINS

    best_start, best_length = 0, 0
    index = 0
    while index < HUE_BINS * 2:
        if not occupied[index % HUE_BINS]:
            start = index
            while index < HUE_BINS * 2 and not occupied[index % HUE_BINS]:
                index += 1
            length = index - start
            if length > best_length and length <= HUE_BINS:
                best_start, best_length = start, length
        else:
            index += 1

    return ((best_start + best_length / 2.0) % HUE_BINS) / HUE_BINS


def bar_colors(art: Image.Image, size: int) -> tuple[tuple, tuple]:
    """Pick a progress bar colour that contrasts with the whole artwork."""
    cached = getattr(art, "_bar_colors", None)
    if cached is not None and cached[0] == size:
        return cached[1], cached[2]

    import colorsys

    backdrop = _scrimmed_art(art, size)
    top = size - PROGRESS_MARGIN - PROGRESS_HEIGHT
    strip = backdrop.crop(
        (PROGRESS_MARGIN, top, size - PROGRESS_MARGIN, size - PROGRESS_MARGIN)
    )
    average = strip.resize((1, 1), Image.Resampling.BILINEAR).getpixel((0, 0))
    backdrop_luminance = _luminance(average)

    hue = _unused_hue(art)
    if hue is None:
        # Greyscale artwork: hue is meaningless, so go on brightness alone.
        fill = (255, 255, 255) if backdrop_luminance < 128 else (0, 0, 0)
    else:
        fill = tuple(
            int(channel * 255) for channel in colorsys.hsv_to_rgb(hue, 1.0, 1.0)
        )
        # An unused hue can still match the backdrop's brightness, which reads as
        # mush on a 2px bar. Brighten or darken it before giving up on hue.
        if abs(_luminance(fill) - backdrop_luminance) < 55:
            lighter = tuple(min(255, int(channel * 1.6) + 60) for channel in fill)
            darker = tuple(int(channel * 0.35) for channel in fill)
            candidate = darker if backdrop_luminance > 128 else lighter
            if abs(_luminance(candidate) - backdrop_luminance) >= 55:
                fill = candidate
            else:
                fill = (255, 255, 255) if backdrop_luminance < 128 else (0, 0, 0)

    track = (30, 30, 30) if _luminance(fill) > 128 else (225, 225, 225)

    try:
        art._bar_colors = (size, fill, track)  # type: ignore[attr-defined]
    except AttributeError:
        pass
    return fill, track


_SCRIM_MASKS: dict[int, Image.Image] = {}


def _scrim_mask(size: int) -> Image.Image:
    """Alpha mask that ramps to SCRIM_STRENGTH over the bottom SCRIM_ROWS."""
    mask = _SCRIM_MASKS.get(size)
    if mask is not None:
        return mask

    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    for row in range(size - SCRIM_ROWS, size):
        depth = (row - (size - SCRIM_ROWS)) / max(SCRIM_ROWS - 1, 1)
        draw.line((0, row, size - 1, row), fill=int(255 * SCRIM_STRENGTH * depth))
    _SCRIM_MASKS[size] = mask
    return mask


def _scrimmed_art(art: Image.Image, size: int) -> Image.Image:
    """Album art with the bottom rows darkened, cached per source image.

    Done as a masked paste rather than a per-pixel loop: the Python version cost
    ~4 seconds on the Pi, stalling the panel on every track change.
    """
    cached = getattr(art, "_scrimmed", None)
    if cached is not None and cached.size == (size, size):
        return cached

    frame = fit_art(art, size).copy()
    frame.paste(Image.new("RGB", (size, size), (0, 0, 0)), (0, 0), _scrim_mask(size))

    try:
        art._scrimmed = frame  # type: ignore[attr-defined]
    except AttributeError:
        pass
    return frame


def render_swipe(
    outgoing: Image.Image,
    incoming: Image.Image,
    progress: float,
    size: int,
    direction: int = 1,
) -> Image.Image:
    """Incoming screen slides in while crossfading over the outgoing one.

    direction 1 brings the new screen in from the right, -1 from the left.
    """
    eased = 1.0 - (1.0 - progress) ** 3  # ease-out so it settles rather than stops
    offset = int(size * eased) * (1 if direction >= 0 else -1)

    slid = Image.new("RGB", (size, size), (0, 0, 0))
    slid.paste(outgoing, (-offset, 0))
    slid.paste(incoming, (size - offset if direction >= 0 else -size - offset, 0))

    # Dip the whole frame toward black around the midpoint. Crossfading the slid
    # composite against the incoming image instead would double-expose it - the
    # incoming art appears at both its slid and final positions at once.
    dip = math.sin(math.pi * min(1.0, max(0.0, progress))) * 0.4
    if dip <= 0.01:
        return slid
    return Image.blend(slid, Image.new("RGB", (size, size), (0, 0, 0)), dip)


def render_record_cached(
    art: Image.Image, angle: float, size: int, steps: int
) -> Image.Image:
    """Return a rotation of the record, reusing frames across revolutions.

    Rendering a frame costs ~44ms on a Pi Zero once the matrix refresh thread is
    competing for the single core, which alone caps the display around 12fps and
    makes the spin visibly step. The disc only ever shows `steps` distinct
    rotations, so each is built once and then replayed for free.
    """
    index = int(round(angle / (360.0 / steps))) % steps
    cache = getattr(art, "_rotation_cache", None)
    if cache is None or cache.get("size") != size or cache.get("steps") != steps:
        cache = {"size": size, "steps": steps, "frames": {}}
        try:
            art._rotation_cache = cache  # type: ignore[attr-defined]
        except AttributeError:
            pass

    frames = cache["frames"]
    frame = frames.get(index)
    if frame is None:
        frame = render_record(art, index * (360.0 / steps), size)
        frames[index] = frame
    return frame


def render_idle(size: int) -> Image.Image:
    frame = Image.new("RGB", (size, size), (0, 0, 0))
    draw = ImageDraw.Draw(frame)
    margin = max(2, size // 32)
    draw.ellipse((margin, margin, size - margin - 1, size - margin - 1), outline=(55, 55, 55), width=2)
    center = size // 2
    radius = max(3, size // 18)
    draw.ellipse((center - radius, center - radius, center + radius, center + radius), fill=(18, 18, 18))
    return frame


def render_test_pattern(size: int, offset: int) -> Image.Image:
    frame = Image.new("RGB", (size, size), (0, 0, 0))
    draw = ImageDraw.Draw(frame)
    colors = (
        (255, 0, 0),
        (255, 160, 0),
        (255, 255, 0),
        (0, 255, 0),
        (0, 120, 255),
        (80, 0, 255),
        (255, 255, 255),
        (0, 0, 0),
    )
    stripe_width = max(1, size // len(colors))
    for index, color in enumerate(colors):
        x0 = (index * stripe_width + offset) % size
        draw.rectangle((x0, 0, min(size - 1, x0 + stripe_width - 1), size - 1), fill=color)
        if x0 + stripe_width > size:
            draw.rectangle((0, 0, (x0 + stripe_width) % size, size - 1), fill=color)
    draw.rectangle((0, 0, size - 1, size - 1), outline=(255, 255, 255))
    return frame


@dataclass
class SharedGlucoseState:
    readings: list[Any] | None = None
    error: str | None = None


def poll_nightscout(
    base_url: str,
    state: SharedGlucoseState,
    state_lock: threading.Lock,
    stop_event: threading.Event,
    poll_seconds: float,
) -> None:
    last_status: str | None = None

    while not stop_event.is_set():
        try:
            readings = nightscout.fetch_entries(base_url, count=24)
            with state_lock:
                state.readings = readings
                state.error = None
            status = (
                f"{readings[0].mmol:.1f} mmol/L {readings[0].direction}"
                if readings
                else "no readings"
            )
        except Exception as exc:
            with state_lock:
                state.error = str(exc)
            status = f"fetch failed: {exc}"

        if status != last_status:
            print(f"Nightscout: {status}", flush=True)
            last_status = status

        stop_event.wait(poll_seconds)


def poll_spotify(
    spotify: SpotifyClient,
    state: SharedPlaybackState,
    state_lock: threading.Lock,
    stop_event: threading.Event,
    poll_seconds: float,
) -> None:
    last_status: str | None = None

    while not stop_event.is_set():
        try:
            playback = spotify.get_currently_playing()
            art = playback_art_from_response(playback)

            if art:
                with state_lock:
                    needs_download = art.key != state.art_key or art.image_url != state.image_url

                image = download_image(art.image_url) if needs_download else None

                with state_lock:
                    state.art_key = art.key
                    state.image_url = art.image_url
                    state.is_playing = art.is_playing
                    state.progress_ms = art.progress_ms
                    state.duration_ms = art.duration_ms
                    state.sampled_at = time.monotonic()
                    if image is not None:
                        state.image = image

                status = f"art found, is_playing={art.is_playing}"
            else:
                with state_lock:
                    state.art_key = None
                    state.image_url = None
                    state.image = None
                    state.is_playing = False
                status = "no currently playing item"

            if status != last_status:
                print(f"Spotify: {status}", flush=True)
                last_status = status
        except Exception as exc:
            print(f"Spotify poll failed: {exc}", flush=True)

        stop_event.wait(poll_seconds)


def run(args: argparse.Namespace) -> None:
    if args.preview_frames:
        render_preview_frames(args.preview_frames)
        return

    load_dotenv()

    # parse_args ran before load_dotenv, so .env values land here.
    if not args.nightscout_url:
        args.nightscout_url = os.environ.get("NIGHTSCOUT_URL", "")

    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    redirect_uri = os.environ.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")

    missing = [
        name
        for name, value in (
            ("SPOTIFY_CLIENT_ID", client_id),
            ("SPOTIFY_CLIENT_SECRET", client_secret),
            ("SPOTIFY_REDIRECT_URI", redirect_uri),
        )
        if not value
    ]
    if missing:
        raise SystemExit(f"Missing required environment values: {', '.join(missing)}")

    spotify = SpotifyClient(
        client_id=client_id or "",
        client_secret=client_secret or "",
        redirect_uri=redirect_uri,
        token_cache=args.token_cache,
        open_browser=not args.no_browser,
    )

    if args.auth_only:
        spotify.authorize()
        print(f"Spotify token cached at {args.token_cache}")
        return

    display: MatrixDisplay | MockDisplay
    if args.mock_output:
        display = MockDisplay(args.mock_output)
    else:
        display = MatrixDisplay(args)

    size = min(args.rows, args.cols)

    if args.test_pattern:
        try:
            offset = 0
            while True:
                display.show(render_test_pattern(size, offset))
                offset = (offset + 1) % size
                time.sleep(1.0 / args.fps)
        except KeyboardInterrupt:
            pass
        finally:
            display.clear()
        return

    idle = render_idle(size)
    playback_state = SharedPlaybackState()
    playback_lock = threading.Lock()
    stop_event = threading.Event()
    poll_thread = threading.Thread(
        target=poll_spotify,
        args=(spotify, playback_state, playback_lock, stop_event, args.poll_seconds),
        daemon=True,
    )
    poll_thread.start()

    glucose_state = SharedGlucoseState()
    glucose_lock = threading.Lock()
    glucose_thread: threading.Thread | None = None
    if args.nightscout_url:
        glucose_thread = threading.Thread(
            target=poll_nightscout,
            args=(
                args.nightscout_url,
                glucose_state,
                glucose_lock,
                stop_event,
                args.nightscout_poll_seconds,
            ),
            daemon=True,
        )
        glucose_thread.start()

    # The rotation cache holds dozens of long-lived PIL images. Moving them out of
    # GC's reach stops generational scans from stalling a frame mid-spin.
    gc.collect()
    gc.freeze()

    previous_scene: tuple | None = None
    previous_frame: Image.Image | None = None
    transition_from: Image.Image | None = None
    transition_started = 0.0
    transition_direction = 1
    last_pushed_key: tuple | None = None

    try:
        while True:
            frame_start = time.monotonic()
            with playback_lock:
                current_art_image = playback_state.image
                is_playing = playback_state.is_playing
                art_key = playback_state.art_key
                progress_ms = playback_state.current_progress_ms()
                duration_ms = playback_state.duration_ms

            # Paused counts as not playing: anything but active playback falls
            # through to the glucose screen.
            if current_art_image is not None and is_playing:
                frame, filled = render_now_playing(
                    current_art_image, size, progress_ms, duration_ms
                )
                # The scene identifies *what* is on screen; the frame key also
                # covers changes within a scene (bar position, glucose age) that
                # should redraw without animating.
                scene: tuple = ("art", art_key)
                frame_key: tuple = ("art", art_key, filled)
            else:
                frame = render_glucose_screen(args, glucose_state, glucose_lock, size)
                if frame is None:
                    frame = idle
                    scene = ("idle",)
                    frame_key = ("idle",)
                else:
                    scene = ("glucose",)
                    frame_key = ("glucose", id(frame))

            # Any scene change animates: track to track, art to glucose, and back.
            if (
                scene != previous_scene
                and previous_scene is not None
                and previous_frame is not None
                and args.transition_seconds > 0
            ):
                transition_from = previous_frame
                transition_started = frame_start
                # Music slides in from the right, glucose slides back in from the
                # left, so the pair reads as moving out and back rather than
                # always marching one way.
                transition_direction = 1 if scene[0] == "art" else -1
            previous_scene = scene

            # Hold the untransitioned frame: if a scene changes mid-animation the
            # next swipe should start from a settled picture, not a blended one.
            previous_frame = frame

            elapsed = frame_start - transition_started
            if transition_from is not None and elapsed < args.transition_seconds:
                progress = elapsed / args.transition_seconds
                frame = render_swipe(
                    transition_from, frame, progress, size, transition_direction
                )
                frame_key = ("swipe", round(progress, 3))
            elif transition_from is not None:
                transition_from = None

            # Pushing an identical frame costs ~25ms of SetImage for no visible
            # change, so only touch the panel when the picture actually differs.
            if frame_key != last_pushed_key:
                display.show(frame)
                last_pushed_key = frame_key

            if args.once:
                break

            # Only animations need the full frame rate. Idling at 25fps to
            # re-check an unchanged picture burns ~83% of the single core, and
            # every cycle taken here is taken from the matrix refresh thread -
            # which shows up as flicker, most visibly on bright full-panel art.
            active = transition_from is not None
            target_fps = args.fps if active else args.idle_fps
            sleep_for = max(0.0, (1.0 / target_fps) - (time.monotonic() - frame_start))
            time.sleep(sleep_for)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        poll_thread.join(timeout=1)
        display.clear()


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def render_preview_frames(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    art = demo_album_art(96)
    for index, angle in enumerate((0, 45, 90, 135)):
        render_record(art, angle, 64).save(directory / f"album-disk-{index:02d}.png")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Spin Spotify album art on a 64x64 RGB matrix.")
    parser.add_argument("--rows", type=int, default=64)
    parser.add_argument("--cols", type=int, default=64)
    parser.add_argument("--chain-length", type=int, default=1)
    parser.add_argument("--parallel", type=int, default=1)
    parser.add_argument("--brightness", type=int, default=100)
    # Defaults below were tuned by A/B testing flicker on the Pi Zero W + Adafruit
    # bonnet + 64x64 P4 panel. Shorter pwm_lsb_nanoseconds visibly reduces the
    # intermittent white flash; below 100 the frame rate collapses for no gain.
    parser.add_argument("--gpio-slowdown", type=int, default=4)
    parser.add_argument("--hardware-mapping", default="adafruit-hat")
    parser.add_argument("--pwm-bits", type=int, default=8)
    parser.add_argument("--pwm-lsb-nanoseconds", type=int, default=100)
    parser.add_argument("--limit-refresh-rate-hz", type=int, default=0)
    parser.add_argument(
        "--no-hardware-pulse",
        action="store_true",
        help="Avoid Pi onboard sound conflict at the cost of more possible flicker.",
    )
    parser.add_argument("--poll-seconds", type=positive_float, default=4.0)
    parser.add_argument("--fps", type=positive_float, default=25.0)
    parser.add_argument("--idle-fps", type=positive_float, default=4.0)
    parser.add_argument("--rpm", type=positive_float, default=20.0)
    parser.add_argument("--rotation-steps", type=int, default=72)
    parser.add_argument("--transition-seconds", type=float, default=0.8)
    parser.add_argument("--token-cache", type=Path, default=Path(".cache/spotify_token.json"))
    parser.add_argument(
        "--nightscout-url",
        default=os.environ.get("NIGHTSCOUT_URL", ""),
        help="Nightscout base URL. Shown when Spotify is not playing.",
    )
    parser.add_argument("--nightscout-poll-seconds", type=positive_float, default=60.0)
    parser.add_argument(
        "--font-dir", type=Path, default=Path(__file__).resolve().parent / "fonts"
    )
    parser.add_argument("--font-cache-dir", type=Path, default=Path(".cache/fonts"))
    parser.add_argument("--mock-output", type=Path, help="Write the current frame PNG instead of using RGB matrix hardware.")
    parser.add_argument("--preview-frames", type=Path, help="Render sample spinning-album-art disk frames and exit.")
    parser.add_argument("--auth-only", action="store_true", help="Authorize Spotify, cache the token, and exit without using the matrix.")
    parser.add_argument("--test-pattern", action="store_true", help="Show a bright moving color test pattern without using Spotify.")
    parser.add_argument("--once", action="store_true", help="Render one frame and exit.")
    parser.add_argument("--no-browser", action="store_true", help="Print the Spotify auth URL without trying to open a browser.")
    return parser


if __name__ == "__main__":
    run(build_parser().parse_args())
