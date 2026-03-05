"""Last.fm scrobbling via pylast with offline queue."""

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Optional

import pylast

from .config import LastFMConfig, ScrobbleConfig
from .fingerprint import TrackInfo

logger = logging.getLogger(__name__)

# Default queue file location
DEFAULT_QUEUE_PATH = Path.home() / ".local" / "share" / "spindle" / "scrobble_queue.json"

# How often to attempt flushing the queue (seconds)
QUEUE_FLUSH_INTERVAL = 60

# Minimum gap between now-playing updates for the same track (seconds).
# Without this, a 13-minute track triggers ~390 API calls (every 2s capture step).
NOW_PLAYING_COOLDOWN = 30


def canonicalize_track(track: TrackInfo, network: pylast.LastFMNetwork) -> TrackInfo:
    """Canonicalize artist/title using Last.fm corrections + proper caps + duration."""
    try:
        try:
            artist_obj = network.get_artist(track.artist)
            corrected_artist = artist_obj.get_correction() or track.artist
        except Exception:
            corrected_artist = track.artist

        title = track.title
        duration = track.duration

        t = pylast.Track(corrected_artist, title, network)

        try:
            title = t.get_correction() or title
        except Exception:
            pass

        try:
            title = t.get_title(properly_capitalized=True) or title
        except Exception:
            pass

        if not duration:
            try:
                dur_ms = t.get_duration()
                if isinstance(dur_ms, int) and dur_ms > 0:
                    duration = int(dur_ms / 1000)
            except Exception:
                pass

        if corrected_artist != track.artist or title != track.title or duration != track.duration:
            return TrackInfo(
                title=title,
                artist=corrected_artist,
                album=track.album,
                duration=duration,
                mbid=track.mbid,
                source=track.source,
                confidence=track.confidence,
            )

        return track

    except Exception:
        return track


class ScrobbleQueue:
    """Persistent JSON queue for offline scrobbles.

    Each entry: {"artist", "title", "album", "duration", "timestamp"}
    File is atomic-written (write to .tmp, rename) to avoid corruption.
    """

    def __init__(self, path: Optional[Path] = None):
        self.path = path or DEFAULT_QUEUE_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._queue: list[dict] = self._load()

    def _load(self) -> list[dict]:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                if isinstance(data, list):
                    logger.info("Loaded %d queued scrobbles from %s", len(data), self.path)
                    return data
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to load scrobble queue: %s", e)
        return []

    def _save(self) -> None:
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._queue, indent=2))
            tmp.rename(self.path)
        except OSError as e:
            logger.error("Failed to save scrobble queue: %s", e)

    def add(self, artist: str, title: str, album: str, duration: int,
            timestamp: int) -> None:
        self._queue.append({
            "artist": artist,
            "title": title,
            "album": album,
            "duration": duration,
            "timestamp": timestamp,
        })
        self._save()
        logger.info("Queued offline: %s — %s (queue size: %d)",
                     artist, title, len(self._queue))

    def pop_all(self) -> list[dict]:
        """Return all queued entries and clear the queue."""
        if not self._queue:
            return []
        entries = list(self._queue)
        self._queue.clear()
        self._save()
        return entries

    def put_back(self, entries: list[dict]) -> None:
        """Put failed entries back at the front of the queue."""
        self._queue = entries + self._queue
        self._save()

    def __len__(self) -> int:
        return len(self._queue)


class Scrobbler:
    """Handles Last.fm authentication, scrobbling, dedup, and offline queue.

    On network failure, scrobbles are saved to a persistent JSON queue.
    The queue is flushed periodically when the connection is back.
    """

    def __init__(self, lastfm_cfg: LastFMConfig, scrobble_cfg: ScrobbleConfig,
                 queue_path: Optional[Path] = None):
        self.cfg = lastfm_cfg
        self.scrobble_cfg = scrobble_cfg
        self.network: Optional[pylast.LastFMNetwork] = None
        self._last_scrobble: Optional[str] = None
        self._last_scrobble_time: float = 0
        self._queue = ScrobbleQueue(queue_path)
        self._last_flush_attempt: float = 0
        # Now-playing rate limiting
        self._last_np_key: Optional[str] = None
        self._last_np_time: float = 0

    def connect(self) -> None:
        if not self.cfg.api_key or not self.cfg.api_secret:
            raise ValueError("Last.fm API key and secret are required")

        self.network = pylast.LastFMNetwork(
            api_key=self.cfg.api_key,
            api_secret=self.cfg.api_secret,
            username=self.cfg.username,
            password_hash=self.cfg.password_hash,
        )
        logger.info("Connected to Last.fm as %s", self.cfg.username)

        # Flush any queued scrobbles from previous sessions
        self._flush_queue()

    def update_now_playing(self, track: TrackInfo) -> None:
        if not self.network or not self.scrobble_cfg.now_playing:
            return

        # Rate-limit: skip if the same track was updated within the cooldown window.
        # Prevents ~390 API calls per 13-minute track (one every 2s capture step).
        np_key = f"{track.artist.lower()}|||{track.title.lower()}"
        now    = time.time()
        if np_key == self._last_np_key and now - self._last_np_time < NOW_PLAYING_COOLDOWN:
            logger.debug("Now-playing cooldown: skipping %s — %s", track.artist, track.title)
            return

        track = self.canonicalize(track)

        try:
            self.network.update_now_playing(
                artist=track.artist,
                title=track.title,
                album=track.album or "",
                duration=track.duration or 0,
            )
            self._last_np_key  = np_key
            self._last_np_time = now
            logger.info("Now playing: %s - %s", track.artist, track.title)
        except Exception as e:
            logger.debug("Failed to update now playing: %s", e)

    def scrobble(self, track: TrackInfo, timestamp: Optional[int] = None) -> bool:
        """Scrobble a track. On failure, queues for later retry.

        Returns True if scrobbled immediately, False if queued or deduped.
        """
        if not self.network:
            logger.error("Not connected to Last.fm")
            return False

        track = self.canonicalize(track)

        # Dedup
        track_key = f"{track.artist} - {track.title}"
        now = time.time()
        if (self._last_scrobble == track_key
                and now - self._last_scrobble_time < self.scrobble_cfg.dedup_window):
            logger.debug("Skipping duplicate: %s", track_key)
            return False

        ts = timestamp or int(now)

        try:
            self.network.scrobble(
                artist=track.artist,
                title=track.title,
                timestamp=ts,
                album=track.album or "",
                duration=track.duration or 0,
            )
            self._last_scrobble = track_key
            self._last_scrobble_time = now
            logger.info("Scrobbled: %s", track_key)

            # Good connection — try flushing queue
            self._maybe_flush_queue()
            return True

        except Exception as e:
            logger.warning("Scrobble failed (queuing): %s", e)
            self._queue.add(
                artist=track.artist,
                title=track.title,
                album=track.album or "",
                duration=track.duration or 0,
                timestamp=ts,
            )
            return False

    def flush_queue(self) -> int:
        """Manually flush the offline queue. Returns number of scrobbles sent."""
        return self._flush_queue()

    def queue_size(self) -> int:
        return len(self._queue)

    def canonicalize(self, track: TrackInfo) -> TrackInfo:
        if not self.network:
            return track
        return canonicalize_track(track, self.network)

    @staticmethod
    def hash_password(password: str) -> str:
        return hashlib.md5(password.encode()).hexdigest()

    # --- Queue flushing ---

    def _maybe_flush_queue(self) -> None:
        """Flush queue if enough time has passed since last attempt."""
        if not self._queue:
            return
        now = time.time()
        if now - self._last_flush_attempt < QUEUE_FLUSH_INTERVAL:
            return
        self._flush_queue()

    def _flush_queue(self) -> int:
        """Attempt to send all queued scrobbles. Returns count sent."""
        self._last_flush_attempt = time.time()
        entries = self._queue.pop_all()
        if not entries:
            return 0

        logger.info("Flushing %d queued scrobbles...", len(entries))

        failed = []
        sent = 0

        for entry in entries:
            try:
                self.network.scrobble(
                    artist=entry["artist"],
                    title=entry["title"],
                    timestamp=entry["timestamp"],
                    album=entry.get("album", ""),
                    duration=entry.get("duration", 0),
                )
                sent += 1
                logger.info("  ✓ %s — %s", entry["artist"], entry["title"])
            except Exception as e:
                logger.warning("  ✗ %s — %s: %s", entry["artist"], entry["title"], e)
                failed.append(entry)

        if failed:
            self._queue.put_back(failed)
            logger.info("Flushed %d/%d (remaining: %d)", sent, len(entries), len(failed))
        else:
            logger.info("Flushed all %d queued scrobbles ✓", sent)

        return sent
