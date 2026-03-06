"""Audio fingerprinting via AcoustID + ShazamIO fallback."""

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import acoustid
import yaml

from .config import FingerprintConfig, AcoustIDConfig

logger = logging.getLogger(__name__)


@dataclass
class TrackInfo:
    """Identified track metadata."""
    title: str
    artist: str
    album: Optional[str] = None
    duration: Optional[int] = None  # seconds
    mbid: Optional[str] = None  # MusicBrainz recording ID
    source: str = "acoustid"  # "acoustid" or "shazam"
    confidence: float = 0.0


# ── Corrections ──────────────────────────────────────────────────────────────
# Loaded once from corrections.yaml (next to config.yaml).
# Each entry: {match: {artist, title}, replace: {artist, title} | null}
# null → drop the match entirely.

_corrections: Optional[list[dict]] = None
_corrections_path: Path = Path.home() / "spindle" / "corrections.yaml"


def _load_corrections() -> list[dict]:
    """Load corrections file (cached after first call)."""
    global _corrections
    if _corrections is not None:
        return _corrections
    if _corrections_path.exists():
        try:
            with open(_corrections_path) as f:
                _corrections = yaml.safe_load(f) or []
            logger.info("Loaded %d fingerprint corrections", len(_corrections))
        except Exception as e:
            logger.error("Failed to load corrections.yaml: %s", e)
            _corrections = []
    else:
        _corrections = []
    return _corrections


def apply_corrections(track: TrackInfo) -> Optional[TrackInfo]:
    """Apply manual corrections to a fingerprint result.

    Returns:
      - Corrected TrackInfo if a replacement is defined
      - None if the match should be dropped (replace: null)
      - Original track if no correction matches
    """
    for entry in _load_corrections():
        match = entry.get("match", {})
        m_artist = match.get("artist", "").lower()
        m_title = match.get("title", "").lower()
        if (track.artist.lower() == m_artist and track.title.lower() == m_title):
            replace = entry.get("replace")
            if replace is None:
                logger.info(
                    "Correction: dropping '%s — %s' (blocked)",
                    track.artist, track.title,
                )
                return None
            logger.info(
                "Correction: '%s — %s' → '%s — %s'",
                track.artist, track.title,
                replace.get("artist", track.artist),
                replace.get("title", track.title),
            )
            return TrackInfo(
                title=replace.get("title", track.title),
                artist=replace.get("artist", track.artist),
                album=replace.get("album", track.album),
                duration=track.duration,
                mbid=None,
                source=track.source,
                confidence=track.confidence,
            )
    return track


def identify_acoustid(wav_path: Path, acoustid_cfg: AcoustIDConfig,
                       fp_cfg: FingerprintConfig) -> Optional[TrackInfo]:
    """Identify a track using AcoustID/Chromaprint.

    Returns TrackInfo or None if no match found.
    """
    try:
        results = acoustid.match(
            acoustid_cfg.api_key,
            str(wav_path),
            parse=False,
        )

        if results.get("status") != "ok":
            logger.warning("AcoustID API error: %s", results)
            return None

        for result in results.get("results", []):
            score = result.get("score", 0)
            if score < fp_cfg.min_confidence:
                continue

            recordings = result.get("recordings", [])
            if not recordings:
                continue

            rec = recordings[0]
            artists = rec.get("artists", [{}])
            artist_name = artists[0].get("name", "Unknown") if artists else "Unknown"

            # Try to get album from release groups
            album = None
            release_groups = rec.get("releasegroups", [])
            if release_groups:
                album = release_groups[0].get("title")

            return TrackInfo(
                title=rec.get("title", "Unknown"),
                artist=artist_name,
                album=album,
                duration=rec.get("duration"),
                mbid=rec.get("id"),
                source="acoustid",
                confidence=score,
            )

        logger.debug("AcoustID: no match above confidence threshold")
        return None

    except Exception as e:
        logger.error("AcoustID lookup failed: %s", e)
        return None


async def identify_shazam(wav_path: Path) -> Optional[TrackInfo]:
    """Identify a track using ShazamIO as fallback.

    Returns TrackInfo or None if no match found.
    """
    try:
        from shazamio import Shazam

        shazam = Shazam()
        result = await asyncio.wait_for(
            shazam.recognize(str(wav_path)),
            timeout=10.0,
        )

        track = result.get("track")
        if not track:
            logger.debug("ShazamIO: no match")
            return None

        return TrackInfo(
            title=track.get("title", "Unknown"),
            artist=track.get("subtitle", "Unknown"),
            album=track.get("sections", [{}])[0].get("metadata", [{}])[0].get("text")
            if track.get("sections") else None,
            source="shazam",
            confidence=1.0,  # Shazam doesn't give a score
        )

    except ImportError:
        logger.error("shazamio not installed — pip install shazamio")
        return None
    except asyncio.TimeoutError:
        logger.warning("ShazamIO lookup timed out (>10s) — skipping")
        return None
    except Exception as e:
        logger.error("ShazamIO lookup failed: %s", e)
        return None


def identify(wav_path: Path, acoustid_cfg: AcoustIDConfig,
             fp_cfg: FingerprintConfig) -> Optional[TrackInfo]:
    """Identify a track — tries AcoustID first, then ShazamIO fallback.

    Returns TrackInfo or None.
    """
    # Try AcoustID first
    track = identify_acoustid(wav_path, acoustid_cfg, fp_cfg)
    if track:
        logger.info("Identified via AcoustID: %s - %s (%.0f%%)",
                     track.artist, track.title, track.confidence * 100)
        track = apply_corrections(track)
        if track is None:
            return None
        return track

    # Fallback to ShazamIO
    if fp_cfg.shazam_fallback:
        logger.debug("AcoustID miss — trying ShazamIO fallback")
        track = asyncio.run(identify_shazam(wav_path))
        if track:
            logger.info("Identified via ShazamIO: %s - %s", track.artist, track.title)
            track = apply_corrections(track)
            if track is None:
                return None
            return track

    logger.info("Could not identify track")
    return None
