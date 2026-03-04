"""Spindle display driver — Waveshare 3.5" IPS LCD (480×320, /dev/fb0).

Split layout: album art left, text panel right on solid dark background.
Writes RGB565 to the framebuffer with ILI9486 colour-inversion (XOR 0xFFFF).
"""

import io
import logging
import threading
from pathlib import Path
from typing import Any, Optional

from .fingerprint import TrackInfo

try:
    import numpy as np
    from PIL import Image, ImageDraw, ImageEnhance, ImageFont
    _HAS_DEPS = True
    _LANCZOS = getattr(Image, "Resampling", Image).LANCZOS
except ImportError:
    _HAS_DEPS = False
    _LANCZOS = None

logger = logging.getLogger(__name__)

WIDTH = 480
HEIGHT = 320
BG = (10, 10, 10)

# Layout constants
ART_SIZE = 260
ART_X = 20
ART_Y = (HEIGHT - ART_SIZE) // 2  # vertically centred = 30

TEXT_X = ART_X + ART_SIZE + 16     # 296
TEXT_W = WIDTH - TEXT_X - 12       # 172

_FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
]


def _find_font() -> Optional[str]:
    for p in _FONT_PATHS:
        if Path(p).exists():
            return p
    return None


def _to_fb(img: Any) -> bytes:
    """Convert PIL RGB → RGB565 LE with ILI9486 inversion."""
    arr = np.array(img.convert("RGB"), dtype=np.uint16)
    r, g, b = arr[:, :, 0] >> 3, arr[:, :, 1] >> 2, arr[:, :, 2] >> 3
    return ((r << 11 | g << 5 | b) ^ 0xFFFF).astype("<u2").tobytes()


def _enhance(img: Any) -> Any:
    """Boost saturation + contrast for washed-out SPI TFTs."""
    img = ImageEnhance.Color(img).enhance(1.25)
    img = ImageEnhance.Contrast(img).enhance(1.15)
    return img


def _truncate(text: str, draw: Any, font: Any, max_w: int) -> str:
    if draw.textlength(text, font=font) <= max_w:
        return text
    while len(text) > 1:
        text = text[:-1]
        if draw.textlength(text + "…", font=font) <= max_w:
            return text + "…"
    return text


def _wrap(text: str, draw: Any, font: Any, max_w: int,
          max_lines: int = 2) -> list:
    """Word-wrap text into lines."""
    words = text.split()
    lines: list = []
    current = ""
    for word in words:
        test = (current + " " + word).strip()
        if draw.textlength(test, font=font) <= max_w:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
        if len(lines) >= max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)
    # Truncate last line if it overflows
    if lines:
        lines[-1] = _truncate(lines[-1], draw, font, max_w)
    return lines[:max_lines]


class Display:
    """Manages the Waveshare 3.5" IPS LCD display."""

    def __init__(self, enabled: bool = False, fb_path: str = "/dev/fb0"):
        self.enabled = enabled
        self._fb_path = fb_path
        self._lock = threading.Lock()
        self._fonts: dict = {}
        self._last_key: Optional[tuple] = None

    def init(self) -> None:
        if not self.enabled:
            logger.debug("Display disabled")
            return
        if not _HAS_DEPS:
            logger.warning("Display enabled but Pillow/numpy missing — disabling")
            self.enabled = False
            return

        fp = _find_font()
        if fp:
            self._fonts = {
                "title":      ImageFont.truetype(fp, 21),
                "artist":     ImageFont.truetype(fp, 16),
                "album":      ImageFont.truetype(fp, 13),
                "idle_big":   ImageFont.truetype(fp, 32),
                "idle_small": ImageFont.truetype(fp, 16),
            }
        else:
            d = ImageFont.load_default()
            self._fonts = {k: d for k in ("title", "artist", "album",
                                           "idle_big", "idle_small")}

        self._last_key = None
        self.show_idle()
        logger.info("Display initialised (%dx%d, %s)", WIDTH, HEIGHT, self._fb_path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def show_track(self, track: TrackInfo, cover_art: Optional[bytes] = None,
                   track_number: int = 0, side: str = "") -> None:
        if not self.enabled:
            return
        key = (track.artist, track.title)
        if key == self._last_key:
            return
        self._last_key = key
        try:
            self._write(self._render_track(track, cover_art, track_number, side))
        except Exception:
            logger.exception("show_track failed")

    def show_idle(self) -> None:
        if not self.enabled:
            return
        self._last_key = None
        try:
            self._write(self._render_idle())
        except Exception:
            logger.exception("show_idle failed")

    def clear(self) -> None:
        if not self.enabled:
            return
        self._last_key = None
        try:
            self._write(Image.new("RGB", (WIDTH, HEIGHT), BG))
        except Exception:
            logger.exception("clear failed")

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render_idle(self) -> Any:
        img = Image.new("RGB", (WIDTH, HEIGHT), BG)
        draw = ImageDraw.Draw(img)
        cx = WIDTH // 2
        cy = HEIGHT // 2 - 10
        draw.text((cx, cy), "SPINDLE", fill=(240, 240, 240),
                  font=self._fonts["idle_big"], anchor="mm")
        draw.line([(cx - 50, cy + 24), (cx + 50, cy + 24)],
                  fill=(50, 50, 50), width=1)
        draw.text((cx, cy + 42), "Listening…", fill=(100, 100, 100),
                  font=self._fonts["idle_small"], anchor="mm")
        return img

    def _render_track(self, track: TrackInfo, cover_art: Optional[bytes],
                      track_number: int, side: str) -> Any:
        img = Image.new("RGB", (WIDTH, HEIGHT), BG)
        draw = ImageDraw.Draw(img)

        # ── Album art (left) ─────────────────────────────────────────
        if cover_art:
            try:
                art = Image.open(io.BytesIO(cover_art)).convert("RGB")
                art = art.resize((ART_SIZE, ART_SIZE), _LANCZOS)
                art = _enhance(art)
                img.paste(art, (ART_X, ART_Y))
            except Exception:
                self._draw_art_placeholder(draw)
        else:
            self._draw_art_placeholder(draw)

        # ── Text panel (right, solid dark background) ────────────────
        y = ART_Y + 10

        # Artist
        artist_text = _truncate(track.artist or "", draw,
                                self._fonts["artist"], TEXT_W)
        draw.text((TEXT_X, y), artist_text,
                  fill=(160, 160, 160), font=self._fonts["artist"])
        y += 24

        # Title (word-wrapped, up to 3 lines)
        title_lines = _wrap(track.title or "", draw,
                            self._fonts["title"], TEXT_W, max_lines=3)
        for line in title_lines:
            draw.text((TEXT_X, y), line,
                      fill=(255, 255, 255), font=self._fonts["title"])
            y += 26
        y += 8

        # Album
        if track.album:
            album_text = _truncate(track.album, draw,
                                   self._fonts["album"], TEXT_W)
            draw.text((TEXT_X, y), album_text,
                      fill=(100, 100, 100), font=self._fonts["album"])
            y += 20

        # Track number
        if track_number:
            draw.text((TEXT_X, y), f"Track {track_number}",
                      fill=(60, 60, 60), font=self._fonts["album"])

        return img

    def _draw_art_placeholder(self, draw: Any) -> None:
        draw.rectangle([ART_X, ART_Y, ART_X + ART_SIZE, ART_Y + ART_SIZE],
                       fill=(25, 25, 25))
        draw.text((ART_X + ART_SIZE // 2, ART_Y + ART_SIZE // 2), "♫",
                  fill=(50, 50, 50), font=self._fonts["title"], anchor="mm")

    # ------------------------------------------------------------------
    # Framebuffer
    # ------------------------------------------------------------------

    def _write(self, img: Any) -> None:
        data = _to_fb(img)
        try:
            with self._lock:
                with open(self._fb_path, "wb") as fb:
                    fb.write(data)
        except OSError as e:
            logger.error("Framebuffer write: %s", e)
