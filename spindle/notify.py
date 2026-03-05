"""Telegram notifications for Spindle events.

Sends messages via the Telegram Bot API when interesting things happen:
  - Album locked (identified album + track)
  - Side finished (summary of what was scrobbled)
  - Errors (optional)

Uses raw HTTP — no extra dependencies needed.
"""

import logging
import threading

import requests

from .config import TelegramConfig
from .fingerprint import TrackInfo

logger = logging.getLogger(__name__)


class Notifier:
    """Sends Telegram notifications for scrobble events.

    All sends are fire-and-forget on a background thread to avoid
    blocking the main audio capture loop.
    """

    def __init__(self, cfg: TelegramConfig):
        self.cfg = cfg
        self.enabled = bool(cfg.bot_token and cfg.chat_id)
        if not self.enabled:
            logger.info("Telegram notifications disabled (no bot_token/chat_id)")

    def _send(self, text: str, parse_mode: str = "HTML") -> None:
        """Send a message in the background."""
        if not self.enabled:
            return

        def _do_send():
            try:
                url = f"https://api.telegram.org/bot{self.cfg.bot_token}/sendMessage"
                resp = requests.post(url, json={
                    "chat_id": self.cfg.chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                    "disable_notification": self.cfg.silent,
                }, timeout=10)
                if not resp.ok:
                    logger.debug("Telegram send failed: %s", resp.text)
            except Exception as e:
                logger.debug("Telegram send error: %s", e)

        threading.Thread(target=_do_send, daemon=True).start()

    def album_locked(self, artist: str, album: str, track_num: int,
                     total_tracks: int, first_track: str) -> None:
        """Notify when album-lock activates."""
        self._send(
            f"🎵 <b>Now playing</b>\n"
            f"{_esc(artist)} — <i>{_esc(album)}</i>\n"
            f"Track {track_num}/{total_tracks}: {_esc(first_track)}"
        )

    def track_scrobbled(self, track: TrackInfo, is_backfill: bool = False) -> None:
        """Notify on individual scrobble (only if verbose notifications enabled)."""
        if not self.cfg.verbose:
            return
        prefix = "↩️" if is_backfill else "✅"
        self._send(f"{prefix} {_esc(track.artist)} — {_esc(track.title)}")

    def side_finished(self, artist: str, album: str,
                      tracks_scrobbled: int, total_played: int) -> None:
        """Notify when an album side/session ends."""
        self._send(
            f"⏹ <b>Side finished</b>\n"
            f"{_esc(artist)} — <i>{_esc(album)}</i>\n"
            f"Scrobbled {tracks_scrobbled}/{total_played} tracks"
        )

    def compilation_finished(self, tracks_scrobbled: int) -> None:
        """Notify when a compilation session ends."""
        self._send(
            f"⏹ <b>Compilation finished</b>\n"
            f"Scrobbled {tracks_scrobbled} track{'s' if tracks_scrobbled != 1 else ''}"
        )

    def error(self, message: str) -> None:
        """Notify on error (if error notifications enabled)."""
        if not self.cfg.errors:
            return
        self._send(f"⚠️ Spindle: {_esc(message)}")

    def track_advanced(self, artist: str, album: str, track_title: str,
                       track_num: int, total_tracks: int) -> None:
        """Notify when album-lock advances to next track."""
        filled = round(track_num / total_tracks * 8)
        bar = "▓" * filled + "░" * (8 - filled)
        self._send(
            f"⏭ {_esc(track_title)}\n"
            f"{track_num}/{total_tracks} {bar}"
        )

    def queue_flushed(self, count: int) -> None:
        """Notify when offline queue is flushed."""
        if count > 0:
            self._send(f"📡 Flushed {count} queued scrobble{'s' if count != 1 else ''}")

    def started(self) -> None:
        """Notify that Spindle has started."""
        self._send("🟢 Spindle started — listening for vinyl")


def _esc(text: str) -> str:
    """Escape HTML special characters."""
    return (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))
