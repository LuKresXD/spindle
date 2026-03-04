"""Album-lock v2: anchor-based track prediction for vinyl scrobbling.

Core concept:
  Vinyl plays tracks in order. When we identify ANY track on an album, we can
  infer what played before (retroactive backfill) and predict what comes next
  (forward tracking). Multiple identifications strengthen confidence.

Algorithm:
  1. First identification → lock onto album, backfill previous tracks using
     elapsed time since music started, then track forward by duration timing.
  2. Subsequent IDs (same album) → fill gaps between prediction and reality.
  3. No identification (while locked) → trust timing predictions.
  4. Silence → end session, scrobble current track if eligible.
  5. Different album ID → end current session, start fresh.

Vinyl-aware timing:
  - Speed varies ±3% (wow/flutter, off-center pressings, motor drift)
  - Inter-track gaps ~1-2s (lead-in grooves)
  - Needle drop precision ~±15s
  - Track durations range from ~20s to 13+ min — no assumptions
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from .fingerprint import TrackInfo
from .spotify import SpotifyClient, SpotifyTrack, AlbumTracklist

logger = logging.getLogger(__name__)

# --- Timing constants ---
VINYL_DRIFT = 0.03        # ±3% speed tolerance
NEEDLE_TOLERANCE = 15     # seconds of needle-drop imprecision
ADVANCE_BUFFER_MIN = 3    # min seconds past duration before auto-advance
ADVANCE_BUFFER_MAX = 15   # max seconds past duration before auto-advance


@dataclass
class Anchor:
    """A confirmed fingerprint identification at a known time."""
    track_index: int
    timestamp: float


@dataclass
class Session:
    """State for one continuous music session (silence → silence)."""
    album_id: str
    tracklist: AlbumTracklist
    music_start: float                        # first non-silence timestamp
    anchors: list = field(default_factory=list)
    current_index: int = 0                    # best estimate of current track
    current_track_start: float = 0.0          # estimated start of current track
    scrobbled: set = field(default_factory=set)
    locked: bool = True


class AlbumLock:
    """Manages album-lock scrobbling across vinyl sessions."""

    def __init__(self, spotify: SpotifyClient, min_play_seconds: int = 30,
                 chunk_duration: int = 10):
        self.spotify = spotify
        self.min_play_seconds = min_play_seconds
        self.chunk_duration = chunk_duration
        self.session: Optional[Session] = None

    # ------------------------------------------------------------------ #
    #  Public API — all return list[(TrackInfo, scrobble_timestamp)]       #
    # ------------------------------------------------------------------ #

    def on_track_identified(
        self,
        spotify_track: SpotifyTrack,
        music_start_time: float,
    ) -> list[tuple[TrackInfo, float]]:
        """Handle a fingerprint match. Returns tracks to scrobble."""

        album_id = spotify_track.album_id
        if not album_id:
            return []

        tracklist = self.spotify.get_album_tracklist(album_id)
        if not tracklist or not tracklist.tracks:
            return []

        track_index = tracklist.find_track_index(spotify_track.track.title)
        if track_index is None:
            logger.debug("Track '%s' not in tracklist for %s",
                         spotify_track.track.title, tracklist.album_name)
            return []

        now = time.time()
        to_scrobble: list[tuple[TrackInfo, float]] = []

        # --- New session (first ID, different album, or session ended) ---
        if not self.session or self.session.album_id != album_id or not self.session.locked:
            # Finalize previous session
            if self.session and self.session.locked:
                to_scrobble.extend(self._end_session())

            self.session = Session(
                album_id=album_id,
                tracklist=tracklist,
                music_start=music_start_time,
                anchors=[Anchor(track_index, now)],
                current_index=track_index,
                # Best estimate: music started → this track started.
                # Retroactive backfill refines this further if prior tracks exist.
                current_track_start=music_start_time,
            )

            logger.info(
                "Album-lock: 🔒 %s — %s (track %d/%d)",
                tracklist.artist, tracklist.album_name,
                track_index + 1, len(tracklist.tracks),
            )

            # Retroactive backfill: what played before this anchor?
            backfill = self._retroactive_backfill(track_index, now)
            to_scrobble.extend(backfill)
            return to_scrobble

        # --- Same album, still locked ---
        self.session.anchors.append(Anchor(track_index, now))

        if track_index == self.session.current_index:
            logger.debug("Album-lock: anchor confirms track %d ✓", track_index + 1)
            return []

        if track_index > self.session.current_index:
            # Ahead of prediction — fill the gap
            gap = self._fill_forward(self.session.current_index, track_index)
            to_scrobble.extend(gap)
            self.session.current_index = track_index
            self.session.current_track_start = now
            logger.info(
                "Album-lock: anchor → track %d (filled %d gap tracks)",
                track_index + 1, len(gap),
            )
            return to_scrobble

        # Behind prediction — re-sync (drift or false positive earlier)
        logger.warning(
            "Album-lock: anchor behind prediction (%d < %d), re-syncing",
            track_index + 1, self.session.current_index + 1,
        )
        self.session.current_index = track_index
        self.session.current_track_start = now
        return []

    def check_advance(self) -> list[tuple[TrackInfo, float]]:
        """Check if current track ended by timing. Call every chunk.

        Auto-advances to the next track and returns the just-finished track
        for scrobbling. Buffer scales with track duration (vinyl speed drift).
        """
        if not self.session or not self.session.locked:
            return []

        current = self.session.tracklist.get_track_at(self.session.current_index)
        if not current or not current.duration:
            return []

        elapsed = time.time() - self.session.current_track_start

        # Buffer: 3% of duration, clamped to [3s, 15s]
        buffer = min(max(current.duration * VINYL_DRIFT, ADVANCE_BUFFER_MIN),
                     ADVANCE_BUFFER_MAX)

        if elapsed < current.duration + buffer:
            return []

        to_scrobble: list[tuple[TrackInfo, float]] = []

        # Scrobble current (fully played) track
        if (self.session.current_index not in self.session.scrobbled
                and current.duration >= self.min_play_seconds):
            to_scrobble.append((current, self.session.current_track_start))
            self.session.scrobbled.add(self.session.current_index)

        # Advance to next track
        next_idx = self.session.current_index + 1
        next_track = self.session.tracklist.get_track_at(next_idx)

        if next_track:
            self.session.current_index = next_idx
            self.session.current_track_start = time.time()
            logger.info(
                "Album-lock: → track %d — %s — %s",
                next_idx + 1, next_track.artist, next_track.title,
            )
        else:
            logger.info("Album-lock: end of tracklist reached")
            self.session.locked = False

        return to_scrobble

    def on_silence(self) -> list[tuple[TrackInfo, float]]:
        """Handle silence detection. Ends the session."""
        return self._end_session()

    def get_current_track(self) -> Optional[TrackInfo]:
        """Currently predicted track (for now-playing / display)."""
        if not self.session or not self.session.locked:
            return None
        return self.session.tracklist.get_track_at(self.session.current_index)

    def get_progress(self) -> Optional[tuple[float, float]]:
        """(elapsed, total_duration) for current track, or None."""
        if not self.session or not self.session.locked:
            return None
        current = self.session.tracklist.get_track_at(self.session.current_index)
        if not current or not current.duration:
            return None
        elapsed = time.time() - self.session.current_track_start
        return (min(elapsed, current.duration), current.duration)

    def is_locked(self) -> bool:
        return bool(self.session and self.session.locked)

    def reset(self):
        self.session = None

    # ------------------------------------------------------------------ #
    #  Internals                                                          #
    # ------------------------------------------------------------------ #

    def _end_session(self) -> list[tuple[TrackInfo, float]]:
        """End current session. Returns final track if it played long enough."""
        if not self.session or not self.session.locked:
            return []

        result: list[tuple[TrackInfo, float]] = []
        current = self.session.tracklist.get_track_at(self.session.current_index)
        elapsed = time.time() - self.session.current_track_start

        if (current and self.session.current_index not in self.session.scrobbled
                and elapsed >= self.min_play_seconds):
            result.append((current, self.session.current_track_start))
            self.session.scrobbled.add(self.session.current_index)

        logger.info("Album-lock: 🔓 session ended")
        self.session.locked = False
        return result

    def _retroactive_backfill(
        self, anchor_index: int, anchor_time: float,
    ) -> list[tuple[TrackInfo, float]]:
        """Backfill tracks before the first anchor using elapsed time.

        Logic:
          elapsed = anchor_time − music_start
          Walk backwards from anchor−1. For each candidate start track,
          sum durations[start..anchor−1]. The remainder (elapsed − sum)
          is the estimated time-into-anchor. Accept if remainder is
          between 0 and anchor.duration (with tolerance).

        This correctly handles:
          - Needle dropped at track 1 → all prior tracks backfilled
          - Needle dropped mid-album → only tracks from drop point
          - Tracks with wildly different durations (20s to 13min)
        """
        if not self.session or anchor_index == 0:
            return []

        elapsed = anchor_time - self.session.music_start
        if elapsed < self.chunk_duration:
            # Less than one chunk — anchor is essentially the first track
            return []

        tracklist = self.session.tracklist
        tolerance = elapsed * VINYL_DRIFT + NEEDLE_TOLERANCE

        anchor_track = tracklist.get_track_at(anchor_index)
        anchor_dur = (anchor_track.duration if anchor_track and anchor_track.duration
                      else 600)  # generous fallback

        # Walk backwards — find the earliest track that fits
        cumulative = 0.0
        best_start = anchor_index  # default: nothing to backfill

        for i in range(anchor_index - 1, -1, -1):
            track = tracklist.get_track_at(i)
            if not track or not track.duration:
                break  # can't reason past unknown durations

            cumulative += track.duration

            # Time we've been in the anchor track = elapsed − cumulative
            time_into_anchor = elapsed - cumulative

            # Valid if time_into_anchor ∈ [−tolerance, anchor_dur + tolerance]
            if time_into_anchor >= -tolerance and time_into_anchor <= anchor_dur + tolerance:
                best_start = i

            if cumulative > elapsed + tolerance:
                break  # no point going further

        if best_start >= anchor_index:
            return []

        # Build scrobble list with reconstructed timestamps
        result: list[tuple[TrackInfo, float]] = []
        ts = self.session.music_start

        for i in range(best_start, anchor_index):
            track = tracklist.get_track_at(i)
            if not track:
                break
            if track.duration and track.duration >= self.min_play_seconds:
                result.append((track, ts))
                self.session.scrobbled.add(i)
            ts += track.duration if track.duration else 0

        # Refine current_track_start: anchor track started at music_start + Σ(prior durations)
        self.session.current_track_start = ts

        logger.info(
            "Album-lock: retroactive backfill — %d tracks starting from track %d "
            "(%.0fs music before identification)",
            len(result), best_start + 1, elapsed,
        )
        for track, t in result:
            logger.info("  ↳ %s — %s", track.artist, track.title)

        return result

    def _fill_forward(
        self, from_index: int, to_index: int,
    ) -> list[tuple[TrackInfo, float]]:
        """Fill tracks from from_index up to (not including) to_index.

        Used when a new anchor is ahead of our prediction. All intermediate
        tracks must have played fully (vinyl plays in order), so scrobble them.
        """
        if not self.session:
            return []

        result: list[tuple[TrackInfo, float]] = []
        ts = self.session.current_track_start

        for i in range(from_index, to_index):
            track = self.session.tracklist.get_track_at(i)
            if not track:
                break

            if i not in self.session.scrobbled:
                if track.duration and track.duration >= self.min_play_seconds:
                    result.append((track, ts))
                    self.session.scrobbled.add(i)

            ts += track.duration if track.duration else 0

        return result
