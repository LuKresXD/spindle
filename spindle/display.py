"""Display module for Waveshare 3.5" IPS LCD (stub for v0.4)."""

import logging
from typing import Optional

from .fingerprint import TrackInfo

logger = logging.getLogger(__name__)


class Display:
    """Manages the Waveshare 3.5" IPS LCD display.

    Stub implementation — will be completed when hardware arrives.
    """

    def __init__(self, enabled: bool = False):
        self.enabled = enabled

    def init(self) -> None:
        """Initialize the display hardware."""
        if not self.enabled:
            logger.debug("Display disabled")
            return
        # TODO: Initialize SPI, GPIO, Waveshare driver
        logger.info("Display initialized (stub)")

    def show_track(self, track: TrackInfo, cover_art: Optional[bytes] = None) -> None:
        """Show current track info and album art on display."""
        if not self.enabled:
            return
        # TODO: Render track info + cover art
        logger.info("Display: %s - %s (stub)", track.artist, track.title)

    def show_idle(self) -> None:
        """Show idle/listening screen."""
        if not self.enabled:
            return
        # TODO: Show "Listening..." or spindle logo
        logger.debug("Display: idle (stub)")

    def clear(self) -> None:
        """Clear the display."""
        if not self.enabled:
            return
        # TODO: Clear display
        logger.debug("Display: cleared (stub)")
