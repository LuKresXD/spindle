"""Telegram notifications for Spindle events.

Sends rich notifications via the Telegram Bot API:
  - Album art photos when album-lock activates
  - Live-edited now-playing caption as tracks advance
  - Detailed session summaries on side finish
  - Mute/unmute support
"""

import logging
import threading
from typing import Optional

import requests

from .config import TelegramConfig
from .fingerprint import TrackInfo

logger = logging.getLogger(__name__)


class Notifier:
    """Sends Telegram notifications for scrobble events.

    Photo messages are sent when album art is available. The now-playing
    message is edited in-place as tracks advance (no spam).
    """

    def __init__(self, cfg: TelegramConfig):
        self.cfg = cfg
        self.enabled = bool(cfg.bot_token and cfg.chat_id)
        self.muted = False

        # Now-playing message tracking (for in-place edits)
        self._np_msg_id: Optional[int] = None
        self._np_is_photo: bool = False
        self.current_art_url: Optional[str] = None

        # Scrobble tracking for session summaries
        self._session_tracks: list[str] = []  # "artist — title" for this session

        if not self.enabled:
            logger.info("Telegram notifications disabled (no bot_token/chat_id)")

    # ── Low-level Telegram API ───────────────────────────────────────────

    def _api(self, method: str, payload: dict,
             timeout: int = 10) -> Optional[dict]:
        """Call a Telegram Bot API method. Returns the result dict or None."""
        if not self.enabled:
            return None
        try:
            url = f"https://api.telegram.org/bot{self.cfg.bot_token}/{method}"
            resp = requests.post(url, json=payload, timeout=timeout)
            if resp.ok:
                return resp.json().get("result")
            else:
                logger.debug("Telegram %s failed: %s", method, resp.text)
                return None
        except Exception as e:
            logger.debug("Telegram %s error: %s", method, e)
            return None

    def _send_text(self, text: str, silent: Optional[bool] = None) -> Optional[int]:
        """Send a text message synchronously. Returns message_id."""
        result = self._api("sendMessage", {
            "chat_id": self.cfg.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_notification": silent if silent is not None else self.cfg.silent,
        })
        return result.get("message_id") if result else None

    def _send_photo(self, photo_url: str, caption: str) -> Optional[int]:
        """Send a photo message synchronously. Returns message_id."""
        result = self._api("sendPhoto", {
            "chat_id": self.cfg.chat_id,
            "photo": photo_url,
            "caption": caption,
            "parse_mode": "HTML",
            "disable_notification": self.cfg.silent,
        }, timeout=15)
        if result:
            return result.get("message_id")
        # Fallback to text if photo fails
        logger.debug("Photo send failed, falling back to text")
        return self._send_text(caption)

    def _edit_caption(self, msg_id: int, caption: str) -> None:
        """Edit a photo message's caption."""
        self._api("editMessageCaption", {
            "chat_id": self.cfg.chat_id,
            "message_id": msg_id,
            "caption": caption,
            "parse_mode": "HTML",
        })

    def _edit_text(self, msg_id: int, text: str) -> None:
        """Edit a text message."""
        self._api("editMessageText", {
            "chat_id": self.cfg.chat_id,
            "message_id": msg_id,
            "text": text,
            "parse_mode": "HTML",
        })

    def _fire(self, fn, *args, **kwargs) -> None:
        """Run a function on a background thread (fire-and-forget)."""
        threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True).start()

    def _fire_send(self, text: str, silent: Optional[bool] = None) -> None:
        """Fire-and-forget text send (no message_id needed)."""
        if not self.enabled or self.muted:
            return
        self._fire(self._send_text, text, silent)

    # ── Caption builder ──────────────────────────────────────────────────

    def _build_np_caption(self, artist: str, album: str,
                          track_title: str, track_num: int,
                          total_tracks: int) -> str:
        """Build the now-playing caption."""
        filled = round(track_num / total_tracks * 10) if total_tracks else 0
        bar = "▓" * filled + "░" * (10 - filled)

        lines = [
            f"🎵 <b>{_esc(artist)}</b> — <i>{_esc(album)}</i>",
            "",
            f"▶️ {_esc(track_title)}",
            f"Track {track_num}/{total_tracks} {bar}",
        ]

        if self._session_tracks:
            lines.append(f"\n✅ Scrobbled: {len(self._session_tracks)}")

        return "\n".join(lines)

    # ── Public API ───────────────────────────────────────────────────────

    def album_locked(self, artist: str, album: str, track_num: int,
                     total_tracks: int, first_track: str,
                     art_url: Optional[str] = None) -> None:
        """Notify when album-lock activates — sends photo if art available."""
        if self.muted:
            self.current_art_url = art_url
            return

        self.current_art_url = art_url
        caption = self._build_np_caption(
            artist, album, first_track, track_num, total_tracks)

        def _do():
            if art_url:
                self._np_msg_id = self._send_photo(art_url, caption)
                self._np_is_photo = True
            else:
                self._np_msg_id = self._send_text(caption)
                self._np_is_photo = False

        # Run in thread to avoid blocking audio loop, but we need msg_id
        # before track_advanced is called (typically minutes later).
        t = threading.Thread(target=_do, daemon=True)
        t.start()

    def track_scrobbled(self, track: TrackInfo, is_backfill: bool = False) -> None:
        """Record a scrobble. Always tracks for session summary.
        Sends individual notification only if verbose mode is on."""
        self._session_tracks.append(f"{track.artist} — {track.title}")

        if self.cfg.verbose and not self.muted:
            prefix = "↩️" if is_backfill else "✅"
            self._fire_send(
                f"{prefix} {_esc(track.artist)} — {_esc(track.title)}", silent=True)

    def track_advanced(self, artist: str, album: str, track_title: str,
                       track_num: int, total_tracks: int) -> None:
        """Notify when album-lock advances — edits the now-playing message."""
        if self.muted:
            return

        caption = self._build_np_caption(
            artist, album, track_title, track_num, total_tracks)

        def _do():
            if self._np_msg_id:
                if self._np_is_photo:
                    self._edit_caption(self._np_msg_id, caption)
                else:
                    self._edit_text(self._np_msg_id, caption)
            else:
                # Photo hasn't been sent yet or failed — send new text
                self._np_msg_id = self._send_text(caption, silent=True)
                self._np_is_photo = False

        self._fire(_do)

    def side_finished(self, artist: str, album: str,
                      tracks_scrobbled: int, total_played: int) -> None:
        """Notify when an album side/session ends — includes track listing."""
        lines = [
            f"⏹ <b>Side finished</b>",
            f"{_esc(artist)} — <i>{_esc(album)}</i>",
            "",
        ]

        for i, track_name in enumerate(self._session_tracks, 1):
            # Show just the title (strip "Artist — " prefix)
            title = track_name.split(" — ", 1)[-1] if " — " in track_name else track_name
            lines.append(f"  {i}. {_esc(title)}")

        lines.append("")
        lines.append(f"✅ {tracks_scrobbled}/{total_played} tracks scrobbled")

        self._fire_send("\n".join(lines))
        self._np_msg_id = None
        self._np_is_photo = False
        self._session_tracks = []

    def compilation_finished(self, tracks_scrobbled: int) -> None:
        """Notify when a compilation session ends."""
        lines = [
            f"⏹ <b>Compilation finished</b>",
            "",
        ]

        for i, track_name in enumerate(self._session_tracks, 1):
            lines.append(f"  {i}. {_esc(track_name)}")

        lines.append("")
        lines.append(f"✅ {tracks_scrobbled} track{'s' if tracks_scrobbled != 1 else ''}")

        self._fire_send("\n".join(lines))
        self._np_msg_id = None
        self._np_is_photo = False
        self._session_tracks = []

    def error(self, message: str) -> None:
        """Notify on error (if error notifications enabled)."""
        if not self.cfg.errors:
            return
        self._fire_send(f"⚠️ Spindle: {_esc(message)}")

    def queue_flushed(self, count: int) -> None:
        """Notify when offline queue is flushed."""
        if count > 0:
            self._fire_send(
                f"📡 Flushed {count} queued scrobble{'s' if count != 1 else ''}")

    def started(self) -> None:
        """Notify that Spindle has started."""
        self._fire_send("🟢 Spindle started — listening for vinyl")
        self._np_msg_id = None
        self._np_is_photo = False
        self._session_tracks = []

    def track_advanced_simple(self, track: TrackInfo) -> None:
        """Simple notification for compilation/simple mode track changes."""
        if self.muted:
            return
        self._fire_send(
            f"🎵 {_esc(track.artist)} — <i>{_esc(track.title)}</i>",
            silent=True)


def _esc(text: str) -> str:
    """Escape HTML special characters."""
    return (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))
