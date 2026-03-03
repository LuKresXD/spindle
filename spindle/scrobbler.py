"""Last.fm scrobbling via pylast."""

import hashlib
import logging
import time
from typing import Optional

import pylast

from .config import LastFMConfig, ScrobbleConfig
from .fingerprint import TrackInfo

logger = logging.getLogger(__name__)


class Scrobbler:
    """Handles Last.fm authentication and scrobbling with dedup."""

    def __init__(self, lastfm_cfg: LastFMConfig, scrobble_cfg: ScrobbleConfig):
        self.cfg = lastfm_cfg
        self.scrobble_cfg = scrobble_cfg
        self.network: Optional[pylast.LastFMNetwork] = None
        self._last_scrobble: Optional[str] = None  # "artist - title"
        self._last_scrobble_time: float = 0

    def connect(self) -> None:
        """Authenticate with Last.fm."""
        if not self.cfg.api_key or not self.cfg.api_secret:
            raise ValueError("Last.fm API key and secret are required")

        self.network = pylast.LastFMNetwork(
            api_key=self.cfg.api_key,
            api_secret=self.cfg.api_secret,
            username=self.cfg.username,
            password_hash=self.cfg.password_hash,
        )
        logger.info("Connected to Last.fm as %s", self.cfg.username)

    def update_now_playing(self, track: TrackInfo) -> None:
        """Update 'Now Playing' on Last.fm."""
        if not self.network or not self.scrobble_cfg.now_playing:
            return

        try:
            self.network.update_now_playing(
                artist=track.artist,
                title=track.title,
                album=track.album or "",
                duration=track.duration or 0,
            )
            logger.info("Now playing: %s - %s", track.artist, track.title)
        except Exception as e:
            logger.error("Failed to update now playing: %s", e)

    def scrobble(self, track: TrackInfo, timestamp: Optional[int] = None) -> bool:
        """Scrobble a track to Last.fm.

        Returns True if scrobbled, False if skipped (dedup) or failed.
        """
        if not self.network:
            logger.error("Not connected to Last.fm")
            return False

        # Dedup check
        track_key = f"{track.artist} - {track.title}"
        now = time.time()
        if (self._last_scrobble == track_key and
                now - self._last_scrobble_time < self.scrobble_cfg.dedup_window):
            logger.debug("Skipping duplicate scrobble: %s (within %ds window)",
                         track_key, self.scrobble_cfg.dedup_window)
            return False

        try:
            self.network.scrobble(
                artist=track.artist,
                title=track.title,
                timestamp=timestamp or int(now),
                album=track.album or "",
                duration=track.duration or 0,
            )
            self._last_scrobble = track_key
            self._last_scrobble_time = now
            logger.info("Scrobbled: %s", track_key)
            return True

        except Exception as e:
            logger.error("Scrobble failed: %s", e)
            return False

    @staticmethod
    def hash_password(password: str) -> str:
        """Generate MD5 hash of password for pylast auth."""
        return hashlib.md5(password.encode()).hexdigest()
