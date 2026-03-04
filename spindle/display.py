"""Spindle display driver — Waveshare 3.5" IPS LCD (480×320, /dev/fb0).

Renders album art + track info using Pillow, writes RGB565 to the framebuffer.
The ILI9486 driver on this display has colour inversion active (INVON), so
every pixel value is XOR'd with 0xFFFF before writing.
"""

import io
import logging
import threading
from pathlib import Path
from typing import Optional

from .fingerprint import TrackInfo

try:
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    _HAS_DISPLAY_DEPS = True
except ImportError:
    _HAS_DISPLAY_DEPS = False

logger = logging.getLogger(__name__)

WIDTH = 480
HEIGHT = 320
BG_COLOR = (15, 15, 15)
ACCENT_COLOR = (180, 50, 50)

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
]


def _find_font() -> Optional[str]:
    for p in _FONT_CANDIDATES:
        if Path(p).exists():
            return p
    return None


def _to_fb(img: Image.Image) -> bytes:
    """Convert PIL RGB image → RGB565 little-endian bytes with ILI9486 inversion."""
    arr = np.array(img.convert("RGB"), dtype=np.uint16)
    r = arr[:, :, 0] >> 3
    g = arr[:, :, 1] >> 2
    b = arr[:, :, 2] >> 3
    rgb565 = (r << 11) | (g << 5) | b
    return (rgb565 ^ 0xFFFF).astype("<u2").tobytes()


def _truncate(text: str, draw: ImageDraw.Draw, font: ImageFont.ImageFont, max_w: int) -> str:
    """Truncate text with ellipsis to fit within max_w pixels."""
    if draw.textlength(text, font=font) <= max_w:
        return text
    while len(text) > 1:
        text = text[:-1]
        if draw.textlength(text + "…", font=font) <= max_w:
            return text + "…"
    return text


def _wrap(text: str, draw: ImageDraw.Draw, font: ImageFont.ImageFont, max_w: int, max_lines: int = 2) -> list[str]:
    """Word-wrap text into lines."""
    words = text.split()
    lines: list[str] = []
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
    return lines[:max_lines]


class Display:
    """Manages the Waveshare 3.5" IPS LCD display."""

    def __init__(self, enabled: bool = False, fb_path: str = "/dev/fb0"):
        self.enabled = enabled
        self._fb_path = fb_path
        self._lock = threading.Lock()
        self._fonts: dict[str, ImageFont.ImageFont] = {}
        self._last_track: Optional[TrackInfo] = None
        self._last_art: Optional[bytes] = None

    def init(self) -> None:
        """Load fonts and clear the display."""
        if not self.enabled:
            logger.debug("Display disabled — skipping init")
            return
        if not _HAS_DISPLAY_DEPS:
            logger.warning("Display enabled but Pillow/numpy not installed — disabling")
            self.enabled = False
            return

        font_path = _find_font()
        if font_path:
            try:
                self._fonts = {
                    "title":  ImageFont.truetype(font_path, 22),
                    "artist": ImageFont.truetype(font_path, 16),
                    "small":  ImageFont.truetype(font_path, 12),
                }
                logger.debug("Display fonts loaded from %s", font_path)
            except Exception as e:
                logger.warning("Font load failed (%s), using default", e)

        if not self._fonts:
            default = ImageFont.load_default()
            self._fonts = {"title": default, "artist": default, "small": default}

        self.show_idle()
        logger.info("Display initialised (%dx%d, %s)", WIDTH, HEIGHT, self._fb_path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def show_track(
        self,
        track: TrackInfo,
        cover_art: Optional[bytes] = None,
        position_sec: float = 0.0,
        track_number: int = 0,
        side: str = "",
    ) -> None:
        """Render track info + optional cover art."""
        if not self.enabled:
            return
        self._last_track = track
        self._last_art = cover_art
        try:
            img = self._render_track(track, cover_art, position_sec, track_number, side)
            self._write(img)
        except Exception:
            logger.exception("Display.show_track failed")

    def show_idle(self) -> None:
        """Show idle/listening screen."""
        if not self.enabled:
            return
        try:
            self._write(self._render_idle())
        except Exception:
            logger.exception("Display.show_idle failed")

    def clear(self) -> None:
        """Fill display with background colour."""
        if not self.enabled:
            return
        try:
            self._write(Image.new("RGB", (WIDTH, HEIGHT), BG_COLOR))
        except Exception:
            logger.exception("Display.clear failed")

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render_idle(self) -> Image.Image:
        img = Image.new("RGB", (WIDTH, HEIGHT), BG_COLOR)
        draw = ImageDraw.Draw(img)
        cx, cy = WIDTH // 2, HEIGHT // 2
        draw.text((cx, cy - 18), "Spindle",
                  fill=(210, 210, 210), font=self._fonts["title"], anchor="mm")
        draw.text((cx, cy + 14), "Listening…",
                  fill=(90, 90, 90), font=self._fonts["artist"], anchor="mm")
        # Subtle horizontal rule
        draw.line([(cx - 60, cy + 2), (cx + 60, cy + 2)], fill=(50, 50, 50), width=1)
        return img

    def _render_track(
        self,
        track: TrackInfo,
        cover_art: Optional[bytes],
        position_sec: float,
        track_number: int,
        side: str,
    ) -> Image.Image:
        img = Image.new("RGB", (WIDTH, HEIGHT), BG_COLOR)
        draw = ImageDraw.Draw(img)

        # ── Album art (left) ─────────────────────────────────────────
        ART = 280
        AX, AY = 10, (HEIGHT - ART) // 2   # centred vertically → y=20

        if cover_art:
            try:
                art = Image.open(io.BytesIO(cover_art)).convert("RGB")
                art = art.resize((ART, ART), Image.LANCZOS)
                img.paste(art, (AX, AY))
            except Exception:
                _draw_art_placeholder(draw, AX, AY, ART, self._fonts["title"])
        else:
            _draw_art_placeholder(draw, AX, AY, ART, self._fonts["title"])

        # ── Text panel (right) ───────────────────────────────────────
        TX = AX + ART + 12     # x start of text column
        TW = WIDTH - TX - 8    # available width ≈ 170 px
        f_title  = self._fonts["title"]
        f_artist = self._fonts["artist"]
        f_small  = self._fonts["small"]

        y = 18

        # Artist
        artist_str = _truncate(track.artist or "", draw, f_artist, TW)
        draw.text((TX, y), artist_str, fill=(170, 170, 170), font=f_artist)
        y += 24

        # Title (word-wrapped, up to 2 lines)
        title_lines = _wrap(track.title or "", draw, f_title, TW, max_lines=2)
        for line in title_lines:
            draw.text((TX, y), line, fill=(255, 255, 255), font=f_title)
            y += 28
        y += 4

        # Album
        if track.album:
            album_str = _truncate(track.album, draw, f_small, TW)
            draw.text((TX, y), album_str, fill=(120, 120, 120), font=f_small)
            y += 18

        # Side / track number
        if side or track_number:
            parts = []
            if side:
                parts.append(f"Side {side}")
            if track_number:
                parts.append(f"Track {track_number}")
            draw.text((TX, y), "  •  ".join(parts), fill=(80, 80, 80), font=f_small)
            y += 18

        # ── Progress bar ─────────────────────────────────────────────
        duration = track.duration or 0
        if duration > 0 and position_sec >= 0:
            BAR_Y  = HEIGHT - 26
            BAR_H  = 5
            TICK_H = 3

            # Track rail
            draw.rectangle([TX, BAR_Y, TX + TW, BAR_Y + BAR_H],
                           fill=(45, 45, 45))

            progress = min(1.0, position_sec / duration)
            fill_w = max(0, int(TW * progress))
            if fill_w:
                draw.rectangle([TX, BAR_Y, TX + fill_w, BAR_Y + BAR_H],
                               fill=ACCENT_COLOR)

            # Time labels
            def fmt(s: float) -> str:
                s = max(0, int(s))
                return f"{s // 60}:{s % 60:02d}"

            draw.text((TX, BAR_Y + BAR_H + 4),
                      fmt(position_sec), fill=(85, 85, 85), font=f_small)
            draw.text((TX + TW, BAR_Y + BAR_H + 4),
                      fmt(duration), fill=(85, 85, 85), font=f_small, anchor="ra")

        return img

    # ------------------------------------------------------------------
    # Low-level write
    # ------------------------------------------------------------------

    def _write(self, img: Image.Image) -> None:
        data = _to_fb(img)
        try:
            with self._lock:
                with open(self._fb_path, "wb") as fb:
                    fb.write(data)
        except OSError as e:
            logger.error("Framebuffer write error: %s", e)


def _draw_art_placeholder(
    draw: ImageDraw.Draw,
    x: int, y: int, size: int,
    font: ImageFont.ImageFont,
) -> None:
    draw.rectangle([x, y, x + size, y + size], fill=(35, 35, 35))
    draw.text((x + size // 2, y + size // 2), "♫",
              fill=(70, 70, 70), font=font, anchor="mm")
