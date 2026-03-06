"""Scrobble history — append-only JSONL log for stats and history queries."""

import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .fingerprint import TrackInfo

logger = logging.getLogger(__name__)

DEFAULT_HISTORY_PATH = Path.home() / ".local" / "share" / "spindle" / "history.jsonl"


@dataclass
class HistoryEntry:
    artist: str
    title: str
    album: str
    duration: Optional[int]
    timestamp: float  # unix epoch
    source: str  # "live", "backfill", "queue_flush"

    def to_dict(self) -> dict:
        return {
            "artist": self.artist,
            "title": self.title,
            "album": self.album,
            "duration": self.duration,
            "timestamp": self.timestamp,
            "source": self.source,
        }

    @staticmethod
    def from_dict(d: dict) -> "HistoryEntry":
        return HistoryEntry(
            artist=d["artist"],
            title=d["title"],
            album=d.get("album", ""),
            duration=d.get("duration"),
            timestamp=d["timestamp"],
            source=d.get("source", "live"),
        )


class ScrobbleHistory:
    """Append-only JSONL log of all scrobbles."""

    def __init__(self, path: Optional[Path] = None):
        self.path = path or DEFAULT_HISTORY_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, track: TrackInfo, timestamp: float,
            source: str = "live") -> None:
        entry = HistoryEntry(
            artist=track.artist,
            title=track.title,
            album=track.album or "",
            duration=track.duration,
            timestamp=timestamp,
            source=source,
        )
        try:
            with open(self.path, "a") as f:
                f.write(json.dumps(entry.to_dict()) + "\n")
        except OSError as e:
            logger.warning("Failed to write history: %s", e)

    # ── Loading helpers ──────────────────────────────────────────────────

    def _load_all(self) -> list[HistoryEntry]:
        """Load all history entries."""
        if not self.path.exists():
            return []
        entries = []
        try:
            with open(self.path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(HistoryEntry.from_dict(json.loads(line)))
                    except (json.JSONDecodeError, KeyError):
                        continue
        except OSError:
            pass
        return entries

    def _load_since(self, since: float) -> list[HistoryEntry]:
        """Load entries since a given timestamp."""
        return [e for e in self._load_all() if e.timestamp >= since]

    @staticmethod
    def _period_start(period: str) -> float:
        """Convert period name to epoch start timestamp."""
        now = time.time()
        if period == "today":
            return datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0,
            ).timestamp()
        elif period == "week":
            return now - 7 * 86400
        elif period == "month":
            return now - 30 * 86400
        return 0  # "all"

    # ── Queries ──────────────────────────────────────────────────────────

    def recent(self, count: int = 10, artist: Optional[str] = None) -> list[HistoryEntry]:
        """Get the most recent N entries, optionally filtered by artist."""
        if not self.path.exists():
            return []
        try:
            lines = self.path.read_text().strip().split("\n")
            entries = []
            for line in reversed(lines):
                if not line.strip():
                    continue
                try:
                    entry = HistoryEntry.from_dict(json.loads(line))
                    if artist and artist.lower() not in entry.artist.lower():
                        continue
                    entries.append(entry)
                except (json.JSONDecodeError, KeyError):
                    continue
                if len(entries) >= count:
                    break
            return entries
        except OSError:
            return []

    def stats(self) -> dict:
        """Get comprehensive scrobble statistics."""
        if not self.path.exists():
            return {
                "total": 0, "today": 0, "week": 0, "month": 0,
                "top_artists": [], "top_albums": [],
                "listening_time": 0, "listening_time_today": 0,
            }

        now = time.time()
        today_start = self._period_start("today")
        week_start = self._period_start("week")
        month_start = self._period_start("month")

        total = today = week = month = 0
        listening_time = listening_time_today = 0
        artist_counts: dict[str, int] = {}
        album_counts: dict[str, int] = {}

        try:
            with open(self.path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    total += 1
                    ts = d.get("timestamp", 0)
                    dur = d.get("duration") or 0
                    listening_time += dur
                    if ts >= today_start:
                        today += 1
                        listening_time_today += dur
                    if ts >= week_start:
                        week += 1
                    if ts >= month_start:
                        month += 1
                    artist = d.get("artist", "Unknown")
                    artist_counts[artist] = artist_counts.get(artist, 0) + 1
                    album = d.get("album", "")
                    if album:
                        key = f"{artist} — {album}"
                        album_counts[key] = album_counts.get(key, 0) + 1
        except OSError:
            pass

        top_artists = sorted(artist_counts.items(), key=lambda x: -x[1])[:5]
        top_albums = sorted(album_counts.items(), key=lambda x: -x[1])[:5]

        return {
            "total": total,
            "today": today,
            "week": week,
            "month": month,
            "top_artists": top_artists,
            "top_albums": top_albums,
            "listening_time": listening_time,
            "listening_time_today": listening_time_today,
        }

    def top(self, period: str = "all", n: int = 10) -> dict:
        """Time-scoped top artists and albums."""
        since = self._period_start(period)
        entries = self._load_since(since) if since > 0 else self._load_all()

        artist_counts: dict[str, int] = {}
        album_counts: dict[str, int] = {}
        total_time = 0

        for e in entries:
            artist_counts[e.artist] = artist_counts.get(e.artist, 0) + 1
            if e.album:
                key = f"{e.artist} — {e.album}"
                album_counts[key] = album_counts.get(key, 0) + 1
            total_time += e.duration or 0

        return {
            "period": period,
            "count": len(entries),
            "listening_time": total_time,
            "top_artists": sorted(artist_counts.items(), key=lambda x: -x[1])[:n],
            "top_albums": sorted(album_counts.items(), key=lambda x: -x[1])[:n],
        }

    def streak(self) -> dict:
        """Calculate listening streaks (consecutive days with scrobbles)."""
        entries = self._load_all()
        if not entries:
            return {"current": 0, "longest": 0, "last_date": None,
                    "total_days": 0, "first_date": None}

        dates = sorted(set(
            datetime.fromtimestamp(e.timestamp, tz=timezone.utc).date()
            for e in entries
        ))

        if not dates:
            return {"current": 0, "longest": 0, "last_date": None,
                    "total_days": 0, "first_date": None}

        longest = 1
        current_streak = 1

        for i in range(1, len(dates)):
            if (dates[i] - dates[i - 1]).days == 1:
                current_streak += 1
                longest = max(longest, current_streak)
            elif (dates[i] - dates[i - 1]).days > 1:
                current_streak = 1

        today = datetime.now(timezone.utc).date()
        if dates[-1] == today or dates[-1] == today - timedelta(days=1):
            active_streak = current_streak
        else:
            active_streak = 0

        return {
            "current": active_streak,
            "longest": max(longest, active_streak),
            "last_date": dates[-1].isoformat(),
            "total_days": len(dates),
            "first_date": dates[0].isoformat(),
        }

    def recognition_stats(self, since: Optional[float] = None) -> dict:
        """Fingerprint recognition rate: live (fingerprinted) vs backfill (inferred)."""
        entries = self._load_since(since) if since else self._load_all()
        live = sum(1 for e in entries if e.source == "live")
        backfill = sum(1 for e in entries if e.source == "backfill")
        total = live + backfill
        return {
            "live": live,
            "backfill": backfill,
            "total": total,
            "rate": live / total if total > 0 else 0,
        }

    def today_sessions(self) -> list[dict]:
        """Group today's scrobbles into album sessions."""
        today_start = self._period_start("today")
        entries = sorted(self._load_since(today_start), key=lambda x: x.timestamp)

        if not entries:
            return []

        sessions: list[dict] = []
        current_album = None
        current_tracks: list[HistoryEntry] = []
        current_start = 0.0

        for e in entries:
            album_key = f"{e.artist} — {e.album}" if e.album else e.artist
            if album_key != current_album:
                if current_tracks:
                    total_dur = sum(t.duration or 0 for t in current_tracks)
                    sessions.append({
                        "album": current_album,
                        "tracks": len(current_tracks),
                        "duration": total_dur,
                        "start": current_start,
                    })
                current_album = album_key
                current_tracks = [e]
                current_start = e.timestamp
            else:
                current_tracks.append(e)

        if current_tracks:
            total_dur = sum(t.duration or 0 for t in current_tracks)
            sessions.append({
                "album": current_album,
                "tracks": len(current_tracks),
                "duration": total_dur,
                "start": current_start,
            })

        return sessions
