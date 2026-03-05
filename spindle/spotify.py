"""Spotify API lookup for canonical track/artist names + album tracklists.

Uses client credentials flow (no user auth needed) to search for tracks
and return Spotify's canonical artist + title format, e.g.:
  - Artist: "Playboi Carti" (primary only, not "Playboi Carti & Nicki Minaj")
  - Title:  "Poke It Out (with Nicki Minaj)"
"""

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

from .config import SpotifyConfig
from .fingerprint import TrackInfo

logger = logging.getLogger(__name__)

TOKEN_URL = "https://accounts.spotify.com/api/token"
SEARCH_URL = "https://api.spotify.com/v1/search"
ALBUM_URL = "https://api.spotify.com/v1/albums"


@dataclass
class SpotifyTrack:
    """Extended track info with album position data."""
    track: TrackInfo
    album_id: str = ""
    album_name: str = ""
    track_number: int = 0
    disc_number: int = 1
    total_tracks: int = 0
    album_art_url: str = ""  # URL of the best-fit album art image


@dataclass
class AlbumTracklist:
    """Full album tracklist from Spotify."""
    album_id: str
    album_name: str
    artist: str
    tracks: list = field(default_factory=list)  # list of TrackInfo, ordered by disc+track number

    def get_track_at(self, index: int) -> Optional[TrackInfo]:
        """Get track at 0-based index."""
        if 0 <= index < len(self.tracks):
            return self.tracks[index]
        return None

    def find_track_index(self, title: str) -> Optional[int]:
        """Find a track's 0-based index by title (case-insensitive)."""
        title_lower = title.lower()
        for i, t in enumerate(self.tracks):
            if t.title.lower() == title_lower:
                return i
        return None


class SpotifyClient:
    def __init__(self, cfg: SpotifyConfig):
        self.cfg = cfg
        self._token: Optional[str] = None
        self._token_expiry: float = 0
        self._album_cache: dict[str, AlbumTracklist] = {}
        self._lookup_cache: dict[str, Optional["SpotifyTrack"]] = {}
        self._art_cache: dict[str, bytes] = {}  # url → image bytes
        self._backoff_until: float = 0  # rate limit backoff timestamp

    def _get_token(self) -> str:
        """Get (or refresh) a client credentials token."""
        if self._token and time.time() < self._token_expiry - 30:
            return self._token

        resp = requests.post(
            TOKEN_URL,
            data={"grant_type": "client_credentials"},
            auth=(self.cfg.client_id, self.cfg.client_secret),
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._token_expiry = time.time() + data["expires_in"]
        logger.debug("Spotify token refreshed (expires in %ds)", data["expires_in"])
        return self._token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._get_token()}"}

    def lookup(self, artist: str, title: str) -> Optional[SpotifyTrack]:
        """Search Spotify for a track and return canonical info + album position.

        Returns SpotifyTrack with canonical names and album metadata, or None.
        Results are cached to avoid redundant API calls (important with 2s sliding window).
        """
        cache_key = f"{artist.lower()}|||{title.lower()}"
        if cache_key in self._lookup_cache:
            return self._lookup_cache[cache_key]

        # Rate limit backoff
        if time.time() < self._backoff_until:
            logger.debug("Spotify: backing off (rate limited)")
            return None

        try:
            # Strip "(feat. ...)" / "(ft. ...)" etc. — Spotify search chokes on them
            clean_title = re.sub(
                r'\s*[\(\[](feat\.?|ft\.?|featuring)\b[^\)\]]*[\)\]]',
                '', title, flags=re.IGNORECASE,
            ).strip() or title
            query = f"track:{clean_title} artist:{artist}"
            resp = requests.get(
                SEARCH_URL,
                params={"q": query, "type": "track", "limit": "10"},
                headers=self._headers(),
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

            tracks = data.get("tracks", {}).get("items", [])
            if not tracks:
                logger.debug("Spotify: no results for '%s - %s'", artist, title)
                self._lookup_cache[cache_key] = None
                return None

            # --- Pick best match: artist match + prefer standard album edition ---
            _EDITION_RE = re.compile(
                r'\b(?:deluxe|super\s+deluxe|anniversary|remaster(?:ed)?|'
                r'expanded|bonus|special\s+edition|'
                r'\d{4}\s+(?:remaster|edition|mix))\b',
                re.IGNORECASE,
            )

            def _score(t: dict) -> tuple:
                """Higher score = better match. Returns (artist_match, standard_album)."""
                t_artist = t["artists"][0]["name"].lower()
                a_lower = artist.lower()
                artist_match = (
                    a_lower.split("&")[0].strip() in t_artist
                    or t_artist in a_lower
                )
                album_name = t.get("album", {}).get("name", "")
                standard_album = not bool(_EDITION_RE.search(album_name))
                return (int(artist_match), int(standard_album))

            best = max(tracks, key=_score)

            # Log if we overrode a deluxe edition
            album_name_chosen = best.get("album", {}).get("name", "")
            if _EDITION_RE.search(album_name_chosen):
                logger.debug(
                    "Spotify: all results are non-standard editions, using: %s",
                    album_name_chosen,
                )

            spotify_artist = best["artists"][0]["name"]
            spotify_title = best["name"]
            album_data = best.get("album", {})
            spotify_album = album_data.get("name", "")
            album_id = album_data.get("id", "")
            spotify_duration = int(best.get("duration_ms", 0) / 1000) or None

            logger.info(
                "Spotify canonical: %s — %s (was: %s — %s)",
                spotify_artist, spotify_title, artist, title,
            )

            track_info = TrackInfo(
                title=spotify_title,
                artist=spotify_artist,
                album=spotify_album,
                duration=spotify_duration,
                source="spotify_lookup",
                confidence=1.0,
            )

            # Pick album art URL closest to 300px (good fit for 280px display)
            images = album_data.get("images", [])
            art_url = ""
            if images:
                best_img = min(images, key=lambda i: abs(i.get("width", 0) - 300))
                art_url = best_img.get("url", "")

            result = SpotifyTrack(
                track=track_info,
                album_id=album_id,
                album_name=spotify_album,
                track_number=best.get("track_number", 0),
                disc_number=best.get("disc_number", 1),
                total_tracks=album_data.get("total_tracks", 0),
                album_art_url=art_url,
            )
            self._lookup_cache[cache_key] = result
            return result

        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                retry_after = int(e.response.headers.get("Retry-After", 30))
                self._backoff_until = time.time() + retry_after
                logger.warning("Spotify rate limited — backing off %ds", retry_after)
            else:
                logger.warning("Spotify lookup failed: %s", e)
            return None
        except Exception as e:
            logger.warning("Spotify lookup failed: %s", e)
            return None

    def get_album_tracklist(self, album_id: str) -> Optional[AlbumTracklist]:
        """Fetch full album tracklist from Spotify. Cached per album_id."""
        if album_id in self._album_cache:
            return self._album_cache[album_id]

        try:
            resp = requests.get(
                f"{ALBUM_URL}/{album_id}",
                headers=self._headers(),
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

            album_name = data.get("name", "")
            album_artist = data["artists"][0]["name"] if data.get("artists") else ""

            tracks = []
            for item in data.get("tracks", {}).get("items", []):
                artist_name = item["artists"][0]["name"] if item.get("artists") else album_artist
                duration_s = int(item.get("duration_ms", 0) / 1000) or None
                tracks.append(TrackInfo(
                    title=item["name"],
                    artist=artist_name,
                    album=album_name,
                    duration=duration_s,
                    source="spotify_album",
                    confidence=1.0,
                ))

            tracklist = AlbumTracklist(
                album_id=album_id,
                album_name=album_name,
                artist=album_artist,
                tracks=tracks,
            )

            logger.info(
                "Fetched album tracklist: %s — %s (%d tracks)",
                album_artist, album_name, len(tracks),
            )

            self._album_cache[album_id] = tracklist
            return tracklist

        except Exception as e:
            logger.warning("Failed to fetch album tracklist: %s", e)
            return None

    def fetch_art(self, url: str) -> Optional[bytes]:
        """Download album art image bytes. Cached by URL."""
        if not url:
            return None
        if url in self._art_cache:
            return self._art_cache[url]
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            data = resp.content
            self._art_cache[url] = data
            logger.debug("Fetched album art (%d bytes) from %s", len(data), url)
            return data
        except Exception as e:
            logger.warning("Failed to fetch album art: %s", e)
            return None
