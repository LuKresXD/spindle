"""Spindle display driver — Waveshare 3.5" IPS LCD (480×320, /dev/fb0).

Full-bleed album art with gradient text overlay. Writes RGB565 to the
framebuffer with ILI9486 colour-inversion compensation (XOR 0xFFFF).
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
BG = (12, 12, 12)

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
    """Boost saturation, contrast and sharpness for washed-out SPI TFTs."""
    img = ImageEnhance.Color(img).enhance(1.25)       # +25% saturation
    img = ImageEnhance.Contrast(img).enhance(1.15)     # +15% contrast
    img = ImageEnhance.Sharpness(img).enhance(1.1)     # +10% sharpness
    return img


def _cover_crop(img: Any, tw: int, th: int) -> Any:
    """Scale + centre-crop to exactly tw×th."""
    src_r = img.width / img.height
    tgt_r = tw / th
    if src_r < tgt_r:
        nw, nh = tw, int(tw / src_r)
    else:
        nw, nh = int(th * src_r), th
    img = img.resize((nw, nh), _LANCZOS)
    x, y = (nw - tw) // 2, (nh - th) // 2
    return img.crop((x, y, x + tw, y + th))


def _apply_gradient(img: Any,
                    start_frac: float = 0.30,
                    end_opacity: float = 0.06) -> Any:
    """Darken bottom portion of the image with a smooth gradient."""
    arr = np.array(img, dtype=np.float32)
    start = int(HEIGHT * start_frac)
    h = HEIGHT - start
    factors = np.linspace(1.0, end_opacity, h)[:, None, None]
    arr[start:] *= factors
    return Image.fromarray(arr.clip(0, 255).astype(np.uint8))


def _truncate(text: str, draw: Any, font: Any, max_w: int) -> str:
    if draw.textlength(text, font=font) <= max_w:
        return text
    while len(text) > 1:
        text = text[:-1]
        if draw.textlength(text + "…", font=font) <= max_w:
            return text + "…"
    return text


def _plain_text(draw: Any, x: int, y: int,
                text: str, font: Any, fill: Any, **kw: Any) -> None:
    """Draw plain text, no effects."""
    draw.text((x, y), text, fill=fill, font=font, **kw)


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
                "title":      ImageFont.truetype(fp, 28),
                "artist":     ImageFont.truetype(fp, 20),
                "album":      ImageFont.truetype(fp, 15),
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
        """Render track info. Skips redraw if same track is already shown."""
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

        # ── Full-bleed album art ─────────────────────────────────────
        if cover_art:
            try:
                art = Image.open(io.BytesIO(cover_art)).convert("RGB")
                art = _cover_crop(art, WIDTH, HEIGHT)
                art = _enhance(art)
                img.paste(art, (0, 0))
            except Exception:
                pass

        # ── Gradient overlay (darken bottom for text) ────────────────
        img = _apply_gradient(img, start_frac=0.25, end_opacity=0.03)
        draw = ImageDraw.Draw(img)

        pad = 18
        max_w = WIDTH - 2 * pad

        # Build text bottom-up
        y = HEIGHT - pad

        # Album
        if track.album:
            y -= 20
            txt = _truncate(track.album, draw, self._fonts["album"], max_w)
            _plain_text(draw, pad, y, txt, self._fonts["album"], (170, 170, 170))

        # Title
        y -= 34
        txt = _truncate(track.title or "", draw, self._fonts["title"], max_w)
        _plain_text(draw, pad, y, txt, self._fonts["title"], (255, 255, 255))

        # Artist
        y -= 26
        txt = _truncate(track.artist or "", draw, self._fonts["artist"], max_w)
        _plain_text(draw, pad, y, txt, self._fonts["artist"], (210, 210, 210))

        return img

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
