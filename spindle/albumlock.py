"""Album-lock scrobbling — predict and scrobble tracks based on album tracklist.

When a track is identified, we "lock" onto the album and use the tracklist
+ durations to predict upcoming tracks. If fingerprinting misses on subsequent
chunks, we scrobble based on timing alone (because vinyl plays in order).

Silence breaks the lock (record flip or end of side).
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional

from .fingerprint import TrackInfo
from .spotify import SpotifyClient, SpotifyTrack, AlbumTracklist

logger = logging.getLogger(__name__)


@dataclass
class AlbumLockState:
    """Current album-lock state."""
    album_id: str
    tracklist: AlbumTracklist
    current_index: int  # 0-based index in tracklist
    track_start_time: float  # when the current track started playing
    scrobbled_indices: set  # tracks already scrobbled in this session
    locked: bool = True


class AlbumLock:
    """Manages album-lock scrobbling state."""

    def __init__(self, spotify: SpotifyClient, min_play_seconds: int = 30):
        self.spotify = spotify
        self.min_play_seconds = min_play_seconds
        self.state: Optional[AlbumLockState] = None

    def on_track_identified(self, spotify_track: SpotifyTrack) -> Optional[list[TrackInfo]]:
        """Called when a track is identified via fingerprint + Spotify lookup.

        Returns a list of TrackInfo to scrobble (may include backfilled tracks),
        or None if nothing to scrobble yet.
        """
        album_id = spotify_track.album_id
        if not album_id:
            return None

        # Get album tracklist
        tracklist = self.spotify.get_album_tracklist(album_id)
        if not tracklist or not tracklist.tracks:
            return None

        # Find the identified track's position
        track_index = tracklist.find_track_index(spotify_track.track.title)
        if track_index is None:
            logger.debug("Track '%s' not found in album tracklist", spotify_track.track.title)
            return None

        to_scrobble = []
        now = time.time()

        if self.state and self.state.album_id == album_id and self.state.locked:
            # Same album — check if this confirms our prediction or we need to catch up
            expected_index = self._predicted_index(now)

            if track_index == expected_index or track_index == self.state.current_index:
                # On track (pun intended) — prediction matches identification
                logger.debug("Album-lock confirmed: track %d", track_index + 1)
            elif track_index > self.state.current_index:
                # We're ahead — backfill any skipped tracks
                to_scrobble = self._backfill(self.state.current_index, track_index, now)
                logger.info(
                    "Album-lock: jumped from track %d to %d, backfilling %d tracks",
                    self.state.current_index + 1, track_index + 1, len(to_scrobble),
                )
            else:
                # Track went backwards — break lock and re-lock
                logger.info("Album-lock: track went backwards (%d → %d), re-locking",
                            self.state.current_index + 1, track_index + 1)
                self.state = None

        if not self.state or self.state.album_id != album_id:
            # New album lock
            logger.info(
                "Album-lock: locked onto %s — %s (track %d/%d)",
                tracklist.artist, tracklist.album_name,
                track_index + 1, len(tracklist.tracks),
            )
            self.state = AlbumLockState(
                album_id=album_id,
                tracklist=tracklist,
                current_index=track_index,
                track_start_time=now,
                scrobbled_indices=set(),
            )
        else:
            # Update position
            self.state.current_index = track_index
            self.state.track_start_time = now

        return to_scrobble if to_scrobble else None

    def check_advance(self) -> Optional[TrackInfo]:
        """Called on each chunk to check if the current track has ended by timing.

        If the track duration has elapsed, advance to the next track and return
        the completed track for scrobbling. Returns None if no advance needed.
        """
        if not self.state or not self.state.locked:
            return None

        current = self.state.tracklist.get_track_at(self.state.current_index)
        if not current or not current.duration:
            return None

        elapsed = time.time() - self.state.track_start_time

        # Has the current track finished? (with a small buffer for timing drift)
        if elapsed >= current.duration + 2:
            # Current track is done — scrobble it if not already done
            to_scrobble = None
            if self.state.current_index not in self.state.scrobbled_indices:
                if elapsed >= self.min_play_seconds:
                    to_scrobble = current
                    self.state.scrobbled_indices.add(self.state.current_index)

            # Advance to next track
            next_index = self.state.current_index + 1
            next_track = self.state.tracklist.get_track_at(next_index)

            if next_track:
                self.state.current_index = next_index
                self.state.track_start_time = time.time()
                logger.info(
                    "Album-lock: auto-advanced to track %d — %s — %s",
                    next_index + 1, next_track.artist, next_track.title,
                )
                return to_scrobble
            else:
                # End of album
                logger.info("Album-lock: end of album reached")
                self.state.locked = False
                return to_scrobble

        return None

    def get_predicted_track(self) -> Optional[TrackInfo]:
        """Get the currently predicted track (for display/now-playing updates)."""
        if not self.state or not self.state.locked:
            return None
        return self.state.tracklist.get_track_at(self.state.current_index)

    def on_silence(self) -> Optional[TrackInfo]:
        """Called when silence is detected. Breaks the lock.

        Returns the current track for scrobbling if it played long enough.
        """
        if not self.state or not self.state.locked:
            return None

        to_scrobble = None
        current = self.state.tracklist.get_track_at(self.state.current_index)
        elapsed = time.time() - self.state.track_start_time

        if (current and self.state.current_index not in self.state.scrobbled_indices
                and elapsed >= self.min_play_seconds):
            to_scrobble = current
            self.state.scrobbled_indices.add(self.state.current_index)

        logger.info("Album-lock: broken by silence (record flip or end of side)")
        self.state.locked = False
        return to_scrobble

    def is_locked(self) -> bool:
        return bool(self.state and self.state.locked)

    def reset(self):
        """Fully reset the album lock."""
        self.state = None

    def _predicted_index(self, now: float) -> int:
        """Predict which track should be playing based on elapsed time."""
        if not self.state:
            return 0

        elapsed = now - self.state.track_start_time
        index = self.state.current_index
        cumulative = 0.0

        while index < len(self.state.tracklist.tracks):
            track = self.state.tracklist.tracks[index]
            if track.duration:
                cumulative += track.duration
                if elapsed < cumulative:
                    return index
            index += 1

        return min(index, len(self.state.tracklist.tracks) - 1)

    def _backfill(self, from_index: int, to_index: int, now: float) -> list[TrackInfo]:
        """Generate scrobbles for tracks between from_index and to_index."""
        if not self.state:
            return []

        to_scrobble = []
        for i in range(from_index, to_index):
            if i in self.state.scrobbled_indices:
                continue
            track = self.state.tracklist.get_track_at(i)
            if track and track.duration and track.duration >= self.min_play_seconds:
                to_scrobble.append(track)
                self.state.scrobbled_indices.add(i)

        return to_scrobble
