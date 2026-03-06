"""Vinyl scrobble session — unified album-lock + compilation mode manager.

Two modes:

  ALBUM       — locked to a specific album/side. Uses Spotify tracklist +
                timing to infer and scrobble unrecognized tracks. Handles
                backfill (tracks before first ID), forward-fill (gap between
                IDs), and timing-based advance.

  COMPILATION — active when consecutive identifications come from different
                albums/artists (bootleg, DJ mix, etc.). No tracklist.
                Scrobbles each recognized track once; unrecognized = skip.

Mode selection:

  Always starts in ALBUM mode on first identification.
  Switches to COMPILATION after MISMATCH_THRESHOLD consecutive IDs that
  don't belong to the locked album. A valid on-album ID resets the counter,
  so a single fingerprint false-positive won't trigger a mode switch.

Session dedup:

  A set of (artist_lower, normalized_title) is maintained across the entire
  silence-bounded session. Shared by both modes. Cleared only on silence.
  Prevents double-scrobbles even when the album-lock session resets
  internally (e.g. different Spotify editions of the same record).
"""

import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .fingerprint import TrackInfo
from .spotify import SpotifyClient, SpotifyTrack, AlbumTracklist

logger = logging.getLogger(__name__)

# ── Timing constants ─────────────────────────────────────────────────────────
VINYL_DRIFT          = 0.03  # ±3 % speed tolerance (wow/flutter/motor drift)
NEEDLE_TOLERANCE     = 15    # seconds of needle-drop imprecision
ADVANCE_BUFFER_MIN   = 3     # min seconds past duration before auto-advance
ADVANCE_BUFFER_MAX   = 15    # max seconds past duration before auto-advance
FALLBACK_TRACK_DURATION = 240  # assumed duration (s) when Spotify has none (4 min)

# ── Compilation detection ─────────────────────────────────────────────────────
MISMATCH_THRESHOLD = 2      # consecutive off-album IDs → switch to COMPILATION
CONFIRM_THRESHOLD  = 2      # times a track must be identified before acting on it


# ── Helpers ──────────────────────────────────────────────────────────────────

def normalize_title(title: str) -> str:
    """Normalize a track title for cross-edition dedup.

    Strips version/remix/remaster parentheticals, featured-artist tags,
    vinyl format markers, and trailing edition suffixes so that:
      "The Girl Is Mine (2008 with will.i.am)"  →  "the girl is mine"
      "Thriller 7\" – Special Edit"              →  "thriller"
      "Heroes (Remastered 2017)"                 →  "heroes"
    """
    s = title
    # Remove parenthetical / bracketed edition tags
    s = re.sub(
        r'\s*[\(\[]'
        r'(?:\d{4}\s+)?'
        r'(?:with|feat\.?|ft\.?|featuring|remix|remaster(?:ed)?|edit|'
        r'version|mix|single|radio|live|demo|acoustic|instrumental|bonus)'
        r'[^\)\]]*[\)\]]',
        '', s, flags=re.IGNORECASE,
    ).strip()
    # Remove trailing " - Special Edit", " - Remastered 2008", etc.
    s = re.sub(
        r'\s*[-–—]\s*'
        r'(?:special\s+edit|remaster(?:ed)?(?:\s+\d{4})?|single\s+version|'
        r'radio\s+edit|album\s+version|extended|remix)\s*$',
        '', s, flags=re.IGNORECASE,
    ).strip()
    # Remove vinyl format markers: 7", 12"
    s = re.sub(r'\s*\d+["\u201d]\s*', ' ', s).strip()
    return s.lower() if s else title.lower()


class SessionMode(Enum):
    ALBUM       = "album"
    COMPILATION = "compilation"


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class Anchor:
    """A confirmed fingerprint identification at a known wall-clock time."""
    track_index: int
    timestamp: float


@dataclass
class AlbumState:
    """All state for one album-mode session."""
    album_id:            str
    tracklist:           AlbumTracklist
    music_start:         float           # wall-clock time music first started
    anchors:             list = field(default_factory=list)
    current_index:       int   = 0       # best estimate of current track
    current_track_start: float = 0.0    # estimated wall-clock start of current track
    scrobbled:           set   = field(default_factory=set)  # track indices scrobbled
    exhausted:           bool  = False   # True once we've advanced past the last track


# ── Main class ────────────────────────────────────────────────────────────────

class ScrobbleSession:
    """Manages vinyl scrobbling state for one silence-bounded music session.

    Public API
    ----------
    on_identified(spotify_track, music_start) → [(TrackInfo, timestamp), ...]
    check_advance()                            → [(TrackInfo, timestamp), ...]
    on_silence()                               → [(TrackInfo, timestamp), ...]
    get_current_track()                        → Optional[TrackInfo]
    get_progress()                             → Optional[(elapsed, duration)]
    is_locked()                                → bool
    album_state                                → Optional[AlbumState]
    mode                                       → Optional[SessionMode]
    reset()
    """

    def __init__(
        self,
        spotify: SpotifyClient,
        min_play_seconds: int = 30,
        chunk_duration: int  = 10,
    ):
        self.spotify           = spotify
        self.min_play_seconds  = min_play_seconds
        self.chunk_duration    = chunk_duration

        self.mode: Optional[SessionMode] = None   # None = no session yet

        self._album: Optional[AlbumState] = None
        self._mismatch_count: int = 0
        self._comp_count: int = 0  # tracks scrobbled in compilation mode this session

        # Session-wide dedup: (artist_lower, normalized_title)
        # Shared across mode switches; cleared only on silence.
        self._session_scrobbled: set[tuple[str, str]] = set()

        # Confirmation counter: (artist_lower, title_lower) → hit count.
        # A track must be identified CONFIRM_THRESHOLD times before it
        # affects the session (starts album lock, becomes an anchor, or
        # gets scrobbled in compilation mode).  One-off false fingerprint
        # matches never reach 2 and are silently discarded.
        self._confirm_counts: dict[tuple[str, str], int] = {}

    # ── Properties ───────────────────────────────────────────────────────────

    @property
    def album_state(self) -> Optional[AlbumState]:
        return self._album

    @property
    def comp_scrobbled_count(self) -> int:
        """Number of tracks scrobbled in compilation mode this session."""
        return self._comp_count

    # ── Public API ───────────────────────────────────────────────────────────

    def on_identified(
        self,
        spotify_track: SpotifyTrack,
        music_start: float,
    ) -> list[tuple[TrackInfo, float]]:
        """Handle a confirmed Spotify-enriched fingerprint match.

        Returns a list of (TrackInfo, timestamp) ready to scrobble.
        The caller must also call check_advance() each chunk for
        timing-based track advances.
        """
        # ── Confirmation gate: require N identifications before acting ──────
        if not self._is_confirmed(spotify_track.track):
            return []

        # ── Compilation mode: no tracklist needed, just dedup + scrobble ────
        if self.mode == SessionMode.COMPILATION:
            return self._handle_compilation_id(spotify_track)

        album_id  = spotify_track.album_id
        if not album_id:
            return []

        tracklist = self.spotify.get_album_tracklist(album_id)
        if not tracklist or not tracklist.tracks:
            return []

        # ── First identification ever: start album session ────────────────
        if self.mode is None:
            return self._start_album_session(
                spotify_track, tracklist, album_id, music_start,
            )

        # ── Already in album mode ─────────────────────────────────────────
        if self.mode == SessionMode.ALBUM:
            return self._handle_album_id(
                spotify_track, tracklist, album_id, music_start,
            )

        # ── In compilation mode: simple per-track scrobble ────────────────
        return self._handle_compilation_id(spotify_track)

    def check_advance(self) -> list[tuple[TrackInfo, float]]:
        """Timing-based track advance — call every capture chunk.

        Only active in ALBUM mode. When the current track's duration plus
        a vinyl-drift buffer has elapsed, scrobbles the completed track and
        advances the index to the next one.

        Returns any newly completed tracks to scrobble.
        """
        if self.mode != SessionMode.ALBUM or not self._album or self._album.exhausted:
            return []

        current = self._album.tracklist.get_track_at(self._album.current_index)
        if not current:
            return []

        # Use fallback duration when Spotify has no data (e.g. older releases).
        # Without this, a None-duration track would block timing advance forever.
        duration = current.duration or FALLBACK_TRACK_DURATION

        elapsed = time.time() - self._album.current_track_start
        buffer  = min(
            max(duration * VINYL_DRIFT, ADVANCE_BUFFER_MIN),
            ADVANCE_BUFFER_MAX,
        )
        if elapsed < duration + buffer:
            return []

        result: list[tuple[TrackInfo, float]] = []

        # Scrobble the track that just finished
        result.extend(
            self._try_scrobble(self._album.current_index, self._album.current_track_start)
        )

        # Advance pointer
        next_idx   = self._album.current_index + 1
        next_track = self._album.tracklist.get_track_at(next_idx)

        if next_track:
            self._album.current_index       = next_idx
            self._album.current_track_start = time.time()
            logger.info(
                "Session: ⏭  track %d — %s — %s",
                next_idx + 1, next_track.artist, next_track.title,
            )
        else:
            # End of tracklist. Mark exhausted so check_advance short-circuits
            # and is_locked() returns False — but on_identified() can still
            # re-open the session if a side-B anchor arrives.
            self._album.exhausted = True
            logger.info("Session: end of tracklist — waiting for silence")

        return result

    def on_silence(self) -> list[tuple[TrackInfo, float]]:
        """End the session on silence.

        Scrobbles the current track if it has played long enough, then
        resets all state. Ready for the next session.
        """
        result: list[tuple[TrackInfo, float]] = []

        if self.mode == SessionMode.ALBUM and self._album:
            current = self._album.tracklist.get_track_at(self._album.current_index)
            elapsed = time.time() - self._album.current_track_start
            if current and elapsed >= self.min_play_seconds:
                result.extend(
                    self._try_scrobble(
                        self._album.current_index, self._album.current_track_start,
                    )
                )
            logger.info(
                "Session: 🔓 ALBUM ended — %s / %s (%d tracks scrobbled)",
                self._album.tracklist.artist,
                self._album.tracklist.album_name,
                len(self._album.scrobbled),
            )

        self.reset()
        return result

    def get_current_track(self) -> Optional[TrackInfo]:
        """Return the currently predicted track (for now-playing + display)."""
        if self.mode == SessionMode.ALBUM and self._album:
            return self._album.tracklist.get_track_at(self._album.current_index)
        return None

    def get_progress(self) -> Optional[tuple[float, float]]:
        """Return (elapsed, duration) for the current track, or None."""
        if self.mode != SessionMode.ALBUM or not self._album:
            return None
        current = self._album.tracklist.get_track_at(self._album.current_index)
        if not current or not current.duration:
            return None
        elapsed = min(time.time() - self._album.current_track_start, current.duration)
        return (elapsed, current.duration)

    def is_locked(self) -> bool:
        """True while in album mode with a live (non-exhausted) album session."""
        return (
            self.mode == SessionMode.ALBUM
            and self._album is not None
            and not self._album.exhausted
        )

    def reset(self) -> None:
        """Clear all session state (called automatically on silence)."""
        self.mode                = None
        self._album              = None
        self._mismatch_count     = 0
        self._comp_count         = 0
        self._session_scrobbled.clear()
        self._confirm_counts.clear()

    # ── Confirmation gate ────────────────────────────────────────────────────

    def _is_confirmed(self, track: TrackInfo) -> bool:
        """Return True once this track has been identified enough times.

        Increments the counter each call.  False fingerprint hits are
        typically one-offs; real tracks playing on vinyl are identified
        repeatedly (every ~2 s), so they pass quickly.
        """
        key = (track.artist.lower(), track.title.lower())
        self._confirm_counts[key] = self._confirm_counts.get(key, 0) + 1
        count = self._confirm_counts[key]
        if count < CONFIRM_THRESHOLD:
            logger.debug(
                "Session: confirm %d/%d — '%s — %s'",
                count, CONFIRM_THRESHOLD, track.artist, track.title,
            )
            return False
        return True

    # ── Session-level dedup ───────────────────────────────────────────────────

    def _dedup_key(self, track: TrackInfo) -> tuple[str, str]:
        return (track.artist.lower(), normalize_title(track.title))

    def _already_scrobbled(self, track: TrackInfo) -> bool:
        return self._dedup_key(track) in self._session_scrobbled

    def _mark_scrobbled(self, track: TrackInfo) -> None:
        self._session_scrobbled.add(self._dedup_key(track))

    # ── Album mode: session start ─────────────────────────────────────────────

    def _start_album_session(
        self,
        spotify_track: SpotifyTrack,
        tracklist: AlbumTracklist,
        album_id: str,
        music_start: float,
    ) -> list[tuple[TrackInfo, float]]:
        """Start a new album session on the very first identification."""
        now = time.time()

        track_index = self._resolve_track_index(tracklist, spotify_track.track.title)
        if track_index is None:
            logger.debug(
                "Session: '%s' not found in tracklist for %s — ignoring",
                spotify_track.track.title, tracklist.album_name,
            )
            return []

        self._album = AlbumState(
            album_id            = album_id,
            tracklist           = tracklist,
            music_start         = music_start,
            anchors             = [Anchor(track_index, now)],
            current_index       = track_index,
            current_track_start = music_start,
        )
        self.mode            = SessionMode.ALBUM
        self._mismatch_count = 0

        logger.info(
            "Session: 🔒 ALBUM — %s / %s  (track %d/%d, first ID: '%s')",
            tracklist.artist, tracklist.album_name,
            track_index + 1, len(tracklist.tracks),
            spotify_track.track.title,
        )

        # Retroactive backfill + refine current_track_start
        return self._retroactive_backfill(track_index, now)

    # ── Album mode: ongoing identifications ───────────────────────────────────

    def _handle_album_id(
        self,
        spotify_track: SpotifyTrack,
        tracklist: AlbumTracklist,
        album_id: str,
        music_start: float,
    ) -> list[tuple[TrackInfo, float]]:
        """Handle an identification while already in album mode."""
        assert self._album is not None
        now = time.time()

        track_index = self._find_in_current_album(spotify_track)

        if track_index is not None:
            # Valid anchor — reset mismatch counter
            self._mismatch_count = 0
            return self._apply_anchor(track_index, now)

        # Doesn't fit the locked album
        self._mismatch_count += 1
        logger.info(
            "Session: mismatch %d/%d — '%s — %s' while locked on '%s'",
            self._mismatch_count, MISMATCH_THRESHOLD,
            spotify_track.track.artist, spotify_track.track.title,
            self._album.tracklist.artist,
        )

        if self._mismatch_count >= MISMATCH_THRESHOLD:
            logger.info(
                "Session: 🎛  switching to COMPILATION mode"
                " (too many mismatches for album '%s')",
                self._album.tracklist.album_name,
            )
            # Finalize whatever is playing in album mode
            result = self._finalize_album_current()
            # Switch mode
            self.mode            = SessionMode.COMPILATION
            self._album          = None
            self._mismatch_count = 0
            # Also scrobble the triggering track now that we're in compilation
            result.extend(self._handle_compilation_id(spotify_track))
            return result

        # Single mismatch: tolerate (likely fingerprint noise), do nothing
        return []

    def _find_in_current_album(
        self, spotify_track: SpotifyTrack,
    ) -> Optional[int]:
        """Return this track's index in the locked album, or None.

        Accepts:
          1. Exact album_id match + exact title match
          2. Same artist + normalized title match (different Spotify edition)
        """
        if not self._album:
            return None

        # 1. Same Spotify album
        if spotify_track.album_id == self._album.album_id:
            idx = self._album.tracklist.find_track_index(spotify_track.track.title)
            if idx is not None:
                return idx
            # Try normalized match within same album
            norm = normalize_title(spotify_track.track.title)
            for i, t in enumerate(self._album.tracklist.tracks):
                if normalize_title(t.title) == norm:
                    return i
            return None  # same album ID but track not found — odd, treat as mismatch

        # 2. Different edition: same artist, normalized title exists in our tracklist
        locked_artist = self._album.tracklist.artist.lower()
        norm = normalize_title(spotify_track.track.title)

        # Accommodate "Various Artists" compilations — skip artist check
        if locked_artist != "various artists":
            if spotify_track.track.artist.lower() != locked_artist:
                # Different primary artist — but check if the title matches
                # a track in the locked album anyway. Feat. tracks (e.g.
                # "Aye (feat. Travis Scott)" by Lil Uzi Vert on a Travis
                # Scott album) have different primary artists on Spotify
                # but are still on the album.
                for i, t in enumerate(self._album.tracklist.tracks):
                    if normalize_title(t.title) == norm:
                        logger.debug(
                            "Session: feat. match — '%s — %s' ≈ tracklist '%s' (index %d)",
                            spotify_track.track.artist, spotify_track.track.title,
                            t.title, i,
                        )
                        return i
                return None  # genuinely different artist + title not in tracklist

        for i, t in enumerate(self._album.tracklist.tracks):
            if normalize_title(t.title) == norm:
                logger.debug(
                    "Session: edition match — '%s' ≈ '%s' (index %d)",
                    spotify_track.track.title, t.title, i,
                )
                return i

        return None

    def _apply_anchor(
        self, track_index: int, now: float,
    ) -> list[tuple[TrackInfo, float]]:
        """Record a valid anchor and update album session state."""
        assert self._album is not None
        self._album.anchors.append(Anchor(track_index, now))
        self._album.exhausted = False  # re-open if a side-B anchor arrives

        result: list[tuple[TrackInfo, float]] = []

        if track_index == self._album.current_index:
            logger.debug("Session: anchor confirms track %d ✓", track_index + 1)

        elif track_index > self._album.current_index:
            # Ahead of prediction — fill the gap
            gap = self._fill_forward(self._album.current_index, track_index)
            result.extend(gap)
            self._album.current_index       = track_index
            self._album.current_track_start = now
            logger.info(
                "Session: anchor → track %d (filled %d gap tracks)",
                track_index + 1, len(gap),
            )

        else:
            # Behind prediction — re-sync (drift or earlier false positive)
            logger.warning(
                "Session: anchor behind prediction (%d < %d), re-syncing",
                track_index + 1, self._album.current_index + 1,
            )
            self._album.current_index       = track_index
            self._album.current_track_start = now

        return result

    def _finalize_album_current(self) -> list[tuple[TrackInfo, float]]:
        """Scrobble the current album track if it has played long enough."""
        if not self._album:
            return []
        elapsed = time.time() - self._album.current_track_start
        if elapsed >= self.min_play_seconds:
            return self._try_scrobble(
                self._album.current_index, self._album.current_track_start,
            )
        return []

    # ── Album mode: scrobbling helpers ────────────────────────────────────────

    def _try_scrobble(
        self, index: int, timestamp: float, force: bool = False,
    ) -> list[tuple[TrackInfo, float]]:
        """Scrobble album track at `index` if eligible and not already done.

        Args:
            force: Skip the min-duration check. Used for gap-fill and
                   backfill where we *know* the track played (it sits
                   between two confirmed anchors or timing says so).
        """
        if not self._album:
            return []
        track = self._album.tracklist.get_track_at(index)
        if not track:
            return []
        if index in self._album.scrobbled:
            return []
        if self._already_scrobbled(track):
            logger.debug("Session dedup: '%s — %s'", track.artist, track.title)
            self._album.scrobbled.add(index)  # mark to avoid repeated log spam
            return []
        if not force and track.duration and track.duration < self.min_play_seconds:
            return []
        self._album.scrobbled.add(index)
        self._mark_scrobbled(track)
        return [(track, timestamp)]

    # ── Album mode: backfill + gap-fill ──────────────────────────────────────

    def _retroactive_backfill(
        self, anchor_index: int, anchor_time: float,
    ) -> list[tuple[TrackInfo, float]]:
        """Infer and scrobble tracks that played before the first identification.

        Logic:
          elapsed = anchor_time - music_start
          Walk backwards from anchor - 1. Sum durations. Find the earliest
          start track such that (elapsed - cumulative_prior_durations) falls
          within the anchor track's duration ± tolerance. That is the inferred
          needle-drop position on the album.
        """
        if not self._album or anchor_index == 0:
            return []

        elapsed = anchor_time - self._album.music_start
        if elapsed < self.chunk_duration:
            return []

        tracklist = self._album.tracklist
        tolerance = elapsed * VINYL_DRIFT + NEEDLE_TOLERANCE

        anchor_track = tracklist.get_track_at(anchor_index)
        anchor_dur   = (
            anchor_track.duration
            if anchor_track and anchor_track.duration
            else 600
        )

        best_start = anchor_index  # default: no backfill
        cumulative = 0.0

        for i in range(anchor_index - 1, -1, -1):
            track = tracklist.get_track_at(i)
            if not track or not track.duration:
                break
            cumulative += track.duration
            time_into_anchor = elapsed - cumulative
            if -tolerance <= time_into_anchor <= anchor_dur + tolerance:
                best_start = i
            if cumulative > elapsed + tolerance:
                break

        if best_start >= anchor_index:
            return []

        result: list[tuple[TrackInfo, float]] = []
        ts = self._album.music_start

        for i in range(best_start, anchor_index):
            track = tracklist.get_track_at(i)
            if not track:
                break
            result.extend(self._try_scrobble(i, ts, force=True))
            ts += track.duration if track.duration else FALLBACK_TRACK_DURATION

        # Refine estimated start time for the anchor track itself
        self._album.current_track_start = ts

        logger.info(
            "Session: retroactive backfill — %d tracks from track %d"
            " (%.0fs before first ID)",
            len(result), best_start + 1, elapsed,
        )
        for track, _ in result:
            logger.info("  ↳ %s — %s", track.artist, track.title)

        return result

    def _fill_forward(
        self, from_index: int, to_index: int,
    ) -> list[tuple[TrackInfo, float]]:
        """Scrobble all tracks from from_index up to (not including) to_index."""
        if not self._album:
            return []
        result: list[tuple[TrackInfo, float]] = []
        ts = self._album.current_track_start
        for i in range(from_index, to_index):
            track = self._album.tracklist.get_track_at(i)
            if not track:
                break
            result.extend(self._try_scrobble(i, ts, force=True))
            # Use fallback so the next track gets a distinct timestamp even
            # when Spotify has no duration data — duplicate timestamps are
            # silently rejected by Last.fm.
            ts += track.duration if track.duration else FALLBACK_TRACK_DURATION
        return result

    # ── Compilation mode ──────────────────────────────────────────────────────

    def _handle_compilation_id(
        self, spotify_track: SpotifyTrack,
    ) -> list[tuple[TrackInfo, float]]:
        """Compilation mode: scrobble this track if not already done."""
        track = spotify_track.track
        if self._already_scrobbled(track):
            logger.debug(
                "Session: compilation dedup — '%s — %s'", track.artist, track.title,
            )
            return []
        if track.duration and track.duration < self.min_play_seconds:
            return []
        self._mark_scrobbled(track)
        self._comp_count += 1
        logger.info(
            "Session: 🎛  COMPILATION scrobble #%d — %s — %s",
            self._comp_count, track.artist, track.title,
        )
        return [(track, time.time())]

    # ── Shared utility ────────────────────────────────────────────────────────

    @staticmethod
    def _resolve_track_index(
        tracklist: AlbumTracklist, title: str,
    ) -> Optional[int]:
        """Find track index by exact title, then by normalized title."""
        idx = tracklist.find_track_index(title)
        if idx is not None:
            return idx
        norm = normalize_title(title)
        for i, t in enumerate(tracklist.tracks):
            if normalize_title(t.title) == norm:
                return i
        return None
