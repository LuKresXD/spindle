"""Telegram bot — rich command handler for Spindle.

Commands:
  /now          — current playback with album art
  /album        — full tracklist with progress
  /session      — session details (mode, duration, recognition)
  /history [n]  — recent scrobbles (default 10)
  /recent <q>   — search history by artist name
  /stats        — comprehensive statistics
  /top [period] — top artists/albums (today|week|month|all)
  /streak       — listening streak info
  /corrections  — view fingerprint corrections
  /mute         — mute notifications
  /unmute       — unmute notifications
  /help         — command list
"""

import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, TYPE_CHECKING

import requests
import yaml

if TYPE_CHECKING:
    from .session import ScrobbleSession, SessionMode
    from .history import ScrobbleHistory
    from .notify import Notifier

logger = logging.getLogger(__name__)

CORRECTIONS_PATH = Path.home() / "spindle" / "corrections.yaml"


class SpindleBot:
    """Telegram bot with rich command handling."""

    def __init__(self, bot_token: str, chat_id: str,
                 session: "Optional[ScrobbleSession]" = None,
                 history: "Optional[ScrobbleHistory]" = None,
                 notifier: "Optional[Notifier]" = None,
                 start_time: Optional[float] = None):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.session = session
        self.history = history
        self.notifier = notifier
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

    # ── Telegram API helpers ─────────────────────────────────────────────

    def _poll_loop(self) -> None:
        while self._running:
            try:
                url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"
                resp = requests.get(url, params={
                    "offset": str(self._offset),
                    "timeout": "30",
                    "allowed_updates": '["message","callback_query"]',
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
        msg = update.get("message", {})
        text = msg.get("text", "").strip()
        chat = msg.get("chat", {})
        chat_id = str(chat.get("id", ""))

        if chat_id != self.chat_id:
            return

        if not text.startswith("/"):
            return

        parts = text.split(maxsplit=1)
        cmd = parts[0].split("@")[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        # Commands that send photos go through special handling
        if cmd == "/now":
            self._cmd_now()
            return

        handlers = {
            "/album": self._cmd_album,
            "/session": self._cmd_session,
            "/stats": self._cmd_stats,
            "/streak": self._cmd_streak,
            "/corrections": self._cmd_corrections,
            "/mute": self._cmd_mute,
            "/unmute": self._cmd_unmute,
            "/help": self._cmd_help,
            "/start": self._cmd_help,
        }

        # Commands with arguments
        if cmd == "/history":
            n = int(args) if args.isdigit() else 10
            self._send(self._cmd_history(n))
            return
        if cmd == "/recent":
            self._send(self._cmd_recent(args.strip()))
            return
        if cmd == "/top":
            period = args.strip().lower() if args.strip() else "week"
            self._send(self._cmd_top(period))
            return

        handler = handlers.get(cmd)
        if handler:
            self._send(handler())

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

    def _send_photo(self, photo_url: str, caption: str) -> None:
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendPhoto"
            resp = requests.post(url, json={
                "chat_id": self.chat_id,
                "photo": photo_url,
                "caption": caption,
                "parse_mode": "HTML",
            }, timeout=15)
            if not resp.ok:
                # Fallback to text
                self._send(caption)
        except Exception as e:
            logger.debug("Bot sendPhoto error: %s", e)
            self._send(caption)

    # ── Commands ─────────────────────────────────────────────────────────

    def _cmd_now(self) -> None:
        """Current playback with album art photo."""
        uptime = _format_duration(time.time() - self.start_time)

        if self.session and self.session.is_locked():
            al = self.session.album_state
            track = self.session.get_current_track()
            progress = self.session.get_progress()

            if al and track:
                idx = al.current_index + 1
                total = len(al.tracklist.tracks)
                bar = _progress_bar(idx, total, width=10)

                lines = [
                    f"🎵 <b>{_esc(al.tracklist.artist)}</b> — <i>{_esc(al.tracklist.album_name)}</i>",
                    "",
                    f"▶️ <b>{_esc(track.title)}</b>",
                    f"Track {idx}/{total} {bar}",
                ]

                if progress:
                    elapsed, duration = progress
                    lines.append(f"⏱ {_format_mmss(elapsed)} / {_format_mmss(duration)}")

                lines.append(f"✅ Scrobbled: {len(al.scrobbled)}")
                lines.append(f"\n🕐 Uptime: {uptime}")

                caption = "\n".join(lines)
                art_url = self.notifier.current_art_url if self.notifier else None

                if art_url:
                    self._send_photo(art_url, caption)
                else:
                    self._send(caption)
                return

        elif self.session and self.session.mode:
            from .session import SessionMode
            if self.session.mode == SessionMode.COMPILATION:
                track = self.session.get_current_track()
                if track:
                    lines = [
                        "🎛 <b>Compilation mode</b>",
                        f"▶️ {_esc(track.artist)} — <i>{_esc(track.title)}</i>",
                        f"✅ Scrobbled: {self.session.comp_scrobbled_count}",
                        f"\n🕐 Uptime: {uptime}",
                    ]
                    self._send("\n".join(lines))
                    return

        self._send(f"⏸ <b>Idle</b> — no music detected\n\n🕐 Uptime: {uptime}")

    def _cmd_album(self) -> str:
        """Full tracklist with scrobble checkmarks."""
        if not self.session or not self.session.is_locked():
            return "💿 No album locked — drop the needle first"

        al = self.session.album_state
        if not al:
            return "💿 No album state"

        tracklist = al.tracklist
        lines = [
            f"💿 <b>{_esc(tracklist.artist)}</b> — <i>{_esc(tracklist.album_name)}</i>",
            "",
        ]

        for i, track in enumerate(tracklist.tracks):
            if i == al.current_index:
                prefix = "▶️"
            elif i in al.scrobbled:
                prefix = "✅"
            else:
                prefix = "  "

            dur = _format_mmss(track.duration) if track.duration else "?:??"
            lines.append(f"{prefix} {i + 1}. {_esc(track.title)} <code>{dur}</code>")

        lines.append("")
        lines.append(f"✅ {len(al.scrobbled)}/{len(tracklist.tracks)} scrobbled")

        return "\n".join(lines)

    def _cmd_session(self) -> str:
        """Current session details."""
        if not self.session:
            return "📡 No session active"

        from .session import SessionMode

        mode = self.session.mode
        if mode is None:
            return "📡 <b>Session</b>: idle — waiting for music"

        lines = ["📡 <b>Session info</b>\n"]

        if mode == SessionMode.ALBUM:
            al = self.session.album_state
            if al:
                lines.append(f"Mode: <b>ALBUM</b>")
                lines.append(f"Album: {_esc(al.tracklist.artist)} — <i>{_esc(al.tracklist.album_name)}</i>")
                lines.append(f"Track: {al.current_index + 1}/{len(al.tracklist.tracks)}")
                lines.append(f"Scrobbled: {len(al.scrobbled)}")
                lines.append(f"Anchors: {len(al.anchors)} fingerprint confirmations")

                progress = self.session.get_progress()
                if progress:
                    lines.append(f"Position: {_format_mmss(progress[0])} / {_format_mmss(progress[1])}")

        elif mode == SessionMode.COMPILATION:
            lines.append(f"Mode: <b>COMPILATION</b>")
            lines.append(f"Scrobbled: {self.session.comp_scrobbled_count} tracks")

        # Recognition stats for today
        if self.history:
            rec = self.history.recognition_stats(self.history._period_start("today"))
            if rec["total"] > 0:
                lines.append(f"\n🎯 Recognition today:")
                lines.append(f"  Fingerprinted: {rec['live']}")
                lines.append(f"  Timing-inferred: {rec['backfill']}")
                rate_pct = rec["rate"] * 100
                lines.append(f"  Rate: {rate_pct:.0f}%")

        return "\n".join(lines)

    def _cmd_history(self, n: int = 10) -> str:
        """Recent scrobbles."""
        if not self.history:
            return "📜 No history available"

        entries = self.history.recent(n)
        if not entries:
            return "📜 No scrobbles yet"

        lines = [f"📜 <b>Recent scrobbles</b> ({len(entries)})\n"]
        for e in entries:
            dt = datetime.fromtimestamp(e.timestamp, tz=timezone.utc)
            time_str = dt.strftime("%H:%M")
            src = " ↩️" if e.source == "backfill" else ""
            lines.append(
                f"<code>{time_str}</code> {_esc(e.artist)} — {_esc(e.title)}{src}")

        return "\n".join(lines)

    def _cmd_recent(self, query: str) -> str:
        """Search history by artist name."""
        if not query:
            return "🔍 Usage: /recent &lt;artist name&gt;"

        if not self.history:
            return "📜 No history available"

        entries = self.history.recent(15, artist=query)
        if not entries:
            return f"🔍 No scrobbles found for '{_esc(query)}'"

        lines = [f"🔍 <b>Recent:</b> {_esc(query)}\n"]
        for e in entries:
            dt = datetime.fromtimestamp(e.timestamp, tz=timezone.utc)
            time_str = dt.strftime("%m/%d %H:%M")
            lines.append(f"<code>{time_str}</code> {_esc(e.title)}")

        return "\n".join(lines)

    def _cmd_stats(self) -> str:
        """Comprehensive statistics."""
        if not self.history:
            return "📊 No stats available"

        s = self.history.stats()
        if s["total"] == 0:
            return "📊 No scrobbles yet"

        lines = [
            "📊 <b>Scrobble stats</b>\n",
            f"Today: <b>{s['today']}</b> ({_format_duration(s['listening_time_today'])})",
            f"This week: <b>{s['week']}</b>",
            f"This month: <b>{s['month']}</b>",
            f"All time: <b>{s['total']}</b> ({_format_listening(s['listening_time'])})",
        ]

        # Recognition rate
        rec = self.history.recognition_stats()
        if rec["total"] > 0:
            lines.append(f"\n🎯 Recognition rate: {rec['rate']:.0%}")
            lines.append(f"  Fingerprinted: {rec['live']} · Inferred: {rec['backfill']}")

        # Top artists
        if s["top_artists"]:
            lines.append("\n🏆 <b>Top artists</b>")
            for i, (artist, count) in enumerate(s["top_artists"], 1):
                lines.append(f"  {i}. {_esc(artist)} ({count})")

        # Top albums
        if s["top_albums"]:
            lines.append("\n💿 <b>Top albums</b>")
            for i, (album, count) in enumerate(s["top_albums"], 1):
                lines.append(f"  {i}. {_esc(album)} ({count})")

        # Streak
        st = self.history.streak()
        if st["current"] > 0:
            lines.append(f"\n🔥 Streak: {st['current']} day{'s' if st['current'] != 1 else ''}")

        # Today's sessions
        sessions = self.history.today_sessions()
        if sessions:
            lines.append("\n📀 <b>Today's sessions</b>")
            for sess in sessions:
                dur = _format_duration(sess["duration"]) if sess["duration"] else "?"
                lines.append(f"  • {_esc(sess['album'])} — {sess['tracks']} tracks ({dur})")

        return "\n".join(lines)

    def _cmd_top(self, period: str) -> str:
        """Time-scoped top artists and albums."""
        if not self.history:
            return "🏆 No stats available"

        valid = ("today", "week", "month", "all")
        if period not in valid:
            period = "week"

        data = self.history.top(period)
        if data["count"] == 0:
            return f"🏆 No scrobbles for period: {period}"

        period_labels = {
            "today": "Today",
            "week": "This week",
            "month": "This month",
            "all": "All time",
        }

        lines = [
            f"🏆 <b>{period_labels[period]}</b>"
            f" — {data['count']} tracks ({_format_listening(data['listening_time'])})\n",
        ]

        if data["top_artists"]:
            lines.append("🎤 <b>Top artists</b>")
            for i, (artist, count) in enumerate(data["top_artists"], 1):
                lines.append(f"  {i}. {_esc(artist)} — {count}")

        if data["top_albums"]:
            lines.append("\n💿 <b>Top albums</b>")
            for i, (album, count) in enumerate(data["top_albums"], 1):
                lines.append(f"  {i}. {_esc(album)} — {count}")

        return "\n".join(lines)

    def _cmd_streak(self) -> str:
        """Listening streak info."""
        if not self.history:
            return "🔥 No stats available"

        st = self.history.streak()

        if st["current"] == 0 and st["longest"] == 0:
            return "🔥 No listening days recorded yet — drop the needle!"

        lines = ["🔥 <b>Listening streak</b>\n"]

        if st["current"] > 0:
            lines.append(f"Current: <b>{st['current']} day{'s' if st['current'] != 1 else ''}</b> 🔥")
        else:
            lines.append("Current: <b>0 days</b> — listen to vinyl today!")

        lines.append(f"Longest: <b>{st['longest']} day{'s' if st['longest'] != 1 else ''}</b>")
        lines.append(f"Total listening days: {st['total_days']}")

        if st["first_date"]:
            lines.append(f"First scrobble: {st['first_date']}")
        if st["last_date"]:
            lines.append(f"Last scrobble: {st['last_date']}")

        return "\n".join(lines)

    def _cmd_corrections(self) -> str:
        """Show fingerprint corrections."""
        if not CORRECTIONS_PATH.exists():
            return "🔧 No corrections file found"

        try:
            with open(CORRECTIONS_PATH) as f:
                corrections = yaml.safe_load(f) or []
        except Exception as e:
            return f"🔧 Error loading corrections: {_esc(str(e))}"

        if not corrections:
            return "🔧 No corrections configured"

        lines = [f"🔧 <b>Fingerprint corrections</b> ({len(corrections)})\n"]

        for c in corrections:
            match = c.get("match", {})
            replace = c.get("replace")

            m_str = f"{_esc(match.get('artist', '?'))} — {_esc(match.get('title', '?'))}"

            if replace is None:
                lines.append(f"🚫 {m_str}\n    → <i>blocked</i>")
            else:
                r_str = f"{_esc(replace.get('artist', '?'))} — {_esc(replace.get('title', '?'))}"
                lines.append(f"🔄 {m_str}\n    → {r_str}")

        return "\n".join(lines)

    def _cmd_mute(self) -> str:
        """Mute notifications."""
        if self.notifier:
            self.notifier.muted = True
        return "🔇 Notifications muted. Use /unmute to re-enable."

    def _cmd_unmute(self) -> str:
        """Unmute notifications."""
        if self.notifier:
            self.notifier.muted = False
        return "🔔 Notifications unmuted."

    def _cmd_help(self) -> str:
        mute_status = ""
        if self.notifier:
            mute_status = " 🔇" if self.notifier.muted else " 🔔"

        return (
            f"🎵 <b>Spindle</b> — vinyl scrobbler{mute_status}\n\n"
            "<b>Playback</b>\n"
            "/now — what's playing (with album art)\n"
            "/album — full tracklist with progress\n"
            "/session — session details\n\n"
            "<b>History</b>\n"
            "/history [n] — recent scrobbles\n"
            "/recent &lt;artist&gt; — search by artist\n\n"
            "<b>Stats</b>\n"
            "/stats — comprehensive statistics\n"
            "/top [today|week|month|all] — top charts\n"
            "/streak — listening streak\n\n"
            "<b>Settings</b>\n"
            "/corrections — view fingerprint corrections\n"
            "/mute · /unmute — toggle notifications\n"
            "/help — this message"
        )


# ── Formatting helpers ───────────────────────────────────────────────────────

def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _progress_bar(current: int, total: int, width: int = 10) -> str:
    filled = round(current / total * width) if total else 0
    return "▓" * filled + "░" * (width - filled)


def _format_duration(seconds: float) -> str:
    """Short human duration: 42m, 1h 23m."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    h = s // 3600
    m = (s % 3600) // 60
    return f"{h}h {m}m"


def _format_listening(seconds: float) -> str:
    """Longer format for listening time: 2h 15m, 45m, 3d 2h."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    if s < 86400:
        h = s // 3600
        m = (s % 3600) // 60
        return f"{h}h {m}m"
    d = s // 86400
    h = (s % 86400) // 3600
    return f"{d}d {h}h"


def _format_mmss(seconds: float) -> str:
    m = int(seconds) // 60
    s = int(seconds) % 60
    return f"{m}:{s:02d}"
