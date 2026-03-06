"""Spindle display driver — Waveshare 3.5" IPS LCD (480×320, /dev/fb0).

Split layout: album art left, text on solid dark panel right.
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
BG = (8, 8, 8)

# Layout
ART_SIZE = 250
ART_PAD = 10                             # left/top padding for art
ART_Y = (HEIGHT - ART_SIZE) // 2         # vertically centred → 35
TEXT_X = ART_PAD + ART_SIZE + 16         # 276
TEXT_W = WIDTH - TEXT_X - 14             # 190
TEXT_CENTER_X = TEXT_X + TEXT_W // 2      # centre of text column

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
    """Convert PIL RGB → RGB565 LE with ILI9486 INVON compensation.

    The framebuffer expects standard RGB565 (R in high bits).
    The INVON command inverts all pixels, so we XOR with 0xFFFF to pre-compensate.
    """
    arr = np.array(img.convert("RGB"), dtype=np.uint16)
    r, g, b = arr[:, :, 0] >> 3, arr[:, :, 1] >> 2, arr[:, :, 2] >> 3
    # RGB565: red in high bits, blue in low bits
    return ((r << 11 | g << 5 | b) ^ 0xFFFF).astype("<u2").tobytes()


def _enhance(img: Any) -> Any:
    """Boost saturation + contrast for washed-out SPI TFTs."""
    img = ImageEnhance.Color(img).enhance(1.3)
    img = ImageEnhance.Contrast(img).enhance(1.2)
    img = ImageEnhance.Sharpness(img).enhance(1.15)
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
                "title":      ImageFont.truetype(fp, 20),
                "artist":     ImageFont.truetype(fp, 15),
                "album":      ImageFont.truetype(fp, 12),
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
            self._write(self._render_track(track, cover_art, track_number))
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
        draw.text((cx, cy), "SPINDLE", fill=(220, 220, 220),
                  font=self._fonts["idle_big"], anchor="mm")
        draw.line([(cx - 45, cy + 24), (cx + 45, cy + 24)],
                  fill=(40, 40, 40), width=1)
        draw.text((cx, cy + 42), "Listening…", fill=(80, 80, 80),
                  font=self._fonts["idle_small"], anchor="mm")
        return img

    def _render_track(self, track: TrackInfo, cover_art: Optional[bytes],
                      track_number: int) -> Any:
        img = Image.new("RGB", (WIDTH, HEIGHT), BG)
        draw = ImageDraw.Draw(img)

        # ── Album art (left) ─────────────────────────────────────────
        if cover_art:
            try:
                art = Image.open(io.BytesIO(cover_art)).convert("RGB")
                art = art.resize((ART_SIZE, ART_SIZE), _LANCZOS)
                art = _enhance(art)
                img.paste(art, (ART_PAD, ART_Y))
            except Exception:
                self._draw_placeholder(draw)
        else:
            self._draw_placeholder(draw)

        # ── Text (right, vertically centred) ──────────────────────────
        f_title = self._fonts["title"]
        f_artist = self._fonts["artist"]
        f_album = self._fonts["album"]

        # Pre-compute text content + heights to centre vertically
        artist_text = _truncate(track.artist or "", draw, f_artist, TEXT_W)
        title_lines = _wrap(track.title or "", draw, f_title, TEXT_W, max_lines=3)
        album_text = _truncate(track.album or "", draw, f_album, TEXT_W) if track.album else ""

        # Measure total text block height
        line_h_artist = 20
        line_h_title = 26
        gap_after_artist = 6
        gap_after_title = 8
        line_h_album = 16

        block_h = line_h_artist + gap_after_artist
        block_h += len(title_lines) * line_h_title + gap_after_title
        if album_text:
            block_h += line_h_album

        # Centre the text block vertically on the screen
        y = (HEIGHT - block_h) // 2

        # Artist
        draw.text((TEXT_X, y), artist_text,
                  fill=(160, 160, 160), font=f_artist)
        y += line_h_artist + gap_after_artist

        # Title
        for line in title_lines:
            draw.text((TEXT_X, y), line,
                      fill=(255, 255, 255), font=f_title)
            y += line_h_title
        y += gap_after_title

        # Album
        if album_text:
            draw.text((TEXT_X, y), album_text,
                      fill=(90, 90, 90), font=f_album)

        return img

    def _draw_placeholder(self, draw: Any) -> None:
        """Draw placeholder when no album art is available."""
        draw.rectangle(
            [ART_PAD, ART_Y, ART_PAD + ART_SIZE, ART_Y + ART_SIZE],
            fill=(20, 20, 20),
        )
        cx = ART_PAD + ART_SIZE // 2
        cy = ART_Y + ART_SIZE // 2
        draw.text((cx, cy), "♫", fill=(45, 45, 45),
                  font=self._fonts["idle_big"], anchor="mm")

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
