"""Telegram bot — command handler for Spindle.

Runs a long-polling loop in a background thread. Handles:
  /status  — current playback state
  /history — recent scrobbles
  /stats   — scrobble statistics
  /help    — command list
"""

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

import requests

if TYPE_CHECKING:
    from .albumlock import AlbumLock
    from .history import ScrobbleHistory

logger = logging.getLogger(__name__)


class SpindleBot:
    """Telegram bot with command handling."""

    def __init__(self, bot_token: str, chat_id: str,
                 album_lock: "Optional[AlbumLock]" = None,
                 history: "Optional[ScrobbleHistory]" = None,
                 start_time: Optional[float] = None):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.album_lock = album_lock
        self.history = history
        self.start_time = start_time or time.time()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._offset = 0

    def start(self) -> None:
        """Start the bot polling loop in a background thread."""
        if not self.bot_token or not self.chat_id:
            return
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        logger.info("Telegram bot started (polling)")

    def stop(self) -> None:
        self._running = False

    def _poll_loop(self) -> None:
        """Long-polling loop for Telegram updates."""
        while self._running:
            try:
                url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"
                resp = requests.get(url, params={
                    "offset": str(self._offset),
                    "timeout": "30",
                    "allowed_updates": '["message"]',
                }, timeout=35)

                if not resp.ok:
                    logger.debug("Bot poll failed: %s", resp.status_code)
                    time.sleep(5)
                    continue

                data = resp.json()
                for update in data.get("result", []):
                    self._offset = update["update_id"] + 1
                    self._handle_update(update)

            except requests.exceptions.Timeout:
                continue
            except Exception as e:
                logger.debug("Bot poll error: %s", e)
                time.sleep(5)

    def _handle_update(self, update: dict) -> None:
        """Handle a single Telegram update."""
        msg = update.get("message", {})
        text = msg.get("text", "").strip()
        chat = msg.get("chat", {})
        chat_id = str(chat.get("id", ""))

        # Only respond to the configured chat
        if chat_id != self.chat_id:
            return

        if not text.startswith("/"):
            return

        cmd = text.split()[0].split("@")[0].lower()

        handlers = {
            "/status": self._cmd_status,
            "/history": self._cmd_history,
            "/stats": self._cmd_stats,
            "/help": self._cmd_help,
            "/start": self._cmd_help,
        }

        handler = handlers.get(cmd)
        if handler:
            reply = handler()
            self._send(reply)

    def _send(self, text: str) -> None:
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            requests.post(url, json={
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
            }, timeout=10)
        except Exception as e:
            logger.debug("Bot send error: %s", e)

    # --- Commands ---

    def _cmd_status(self) -> str:
        uptime = _format_duration(time.time() - self.start_time)

        if self.album_lock and self.album_lock.is_locked():
            al = self.album_lock.session
            track = self.album_lock.get_current_track()
            progress = self.album_lock.get_progress()

            if al and track:
                idx = al.current_index + 1
                total = len(al.tracklist.tracks)
                bar = _progress_bar(idx, total)

                lines = [
                    "🎵 <b>Now playing</b>",
                    f"{_esc(track.artist)} — <i>{_esc(track.title)}</i>",
                    f"Album: {_esc(al.tracklist.album_name)}",
                    f"Track {idx}/{total} {bar}",
                ]

                if progress:
                    elapsed, duration = progress
                    lines.append(f"Time: {_format_mmss(elapsed)} / {_format_mmss(duration)}")

                lines.append(f"Scrobbled: {len(al.scrobbled)} tracks this session")
                lines.append(f"\n⏱ Uptime: {uptime}")
                return "\n".join(lines)

        return f"⏸ <b>Idle</b> — no music detected\n\n⏱ Uptime: {uptime}"

    def _cmd_history(self) -> str:
        if not self.history:
            return "📜 No history available"

        entries = self.history.recent(10)
        if not entries:
            return "📜 No scrobbles yet"

        lines = ["📜 <b>Recent scrobbles</b>\n"]
        for e in entries:
            dt = datetime.fromtimestamp(e.timestamp, tz=timezone.utc)
            time_str = dt.strftime("%H:%M")
            src = " ↩️" if e.source == "backfill" else ""
            lines.append(f"<code>{time_str}</code> {_esc(e.artist)} — {_esc(e.title)}{src}")

        return "\n".join(lines)

    def _cmd_stats(self) -> str:
        if not self.history:
            return "📊 No stats available"

        s = self.history.stats()
        if s["total"] == 0:
            return "📊 No scrobbles yet"

        lines = [
            "📊 <b>Scrobble stats</b>\n",
            f"Today: <b>{s['today']}</b>",
            f"This week: <b>{s['week']}</b>",
            f"All time: <b>{s['total']}</b>",
        ]

        if s["top_artists"]:
            lines.append("\n🏆 <b>Top artists</b>")
            for i, (artist, count) in enumerate(s["top_artists"], 1):
                lines.append(f"{i}. {_esc(artist)} ({count})")

        return "\n".join(lines)

    def _cmd_help(self) -> str:
        return (
            "🎵 <b>Spindle</b> — vinyl scrobbler\n\n"
            "/status — what's playing now\n"
            "/history — recent scrobbles\n"
            "/stats — scrobble statistics\n"
            "/help — this message"
        )


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _progress_bar(current: int, total: int, width: int = 8) -> str:
    filled = round(current / total * width)
    return "▓" * filled + "░" * (width - filled)


def _format_duration(seconds: float) -> str:
    s = int(seconds)
    if s < 3600:
        return f"{s // 60}m"
    h = s // 3600
    m = (s % 3600) // 60
    return f"{h}h {m}m"


def _format_mmss(seconds: float) -> str:
    m = int(seconds) // 60
    s = int(seconds) % 60
    return f"{m}:{s:02d}"
