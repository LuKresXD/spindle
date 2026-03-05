"""Spindle CLI — main entry point."""

import argparse
import logging
import logging.handlers
import signal
import sys
import time
from pathlib import Path
from typing import Optional

from . import __version__
from .config import load_config
from .capture import SlidingCapture, is_silence, DEFAULT_STEP
from .fingerprint import identify, TrackInfo
from .scrobbler import Scrobbler, canonicalize_track
from .display import Display
from .spotify import SpotifyClient
from .session import ScrobbleSession, SessionMode
from .notify import Notifier
from .history import ScrobbleHistory
from .bot import SpindleBot

logger = logging.getLogger("spindle")

_running = True

def _signal_handler(sig, frame):
    global _running
    logger.info("Shutting down...")
    _running = False


def main():
    parser = argparse.ArgumentParser(
        prog="spindle",
        description="🎵 Vinyl scrobbler — fingerprint what's playing and scrobble to Last.fm",
    )
    parser.add_argument("-c", "--config", type=Path, help="Path to config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    parser.add_argument("--dry-run", action="store_true", help="Identify only, don't scrobble")
    parser.add_argument(
        "--canonicalize-preview", action="store_true",
        help="In dry-run: also query Last.fm corrections",
    )
    parser.add_argument("--version", action="version", version=f"spindle {__version__}")
    args = parser.parse_args()

    # --- Config ---
    cfg = load_config(args.config)

    # --- Logging ---
    log_level  = logging.DEBUG if args.verbose else logging.INFO
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    log_datefmt = "%H:%M:%S"

    handlers: list[logging.Handler] = [logging.StreamHandler()]

    if cfg.logging.file:
        log_path = Path(cfg.logging.file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=cfg.logging.max_bytes,
            backupCount=cfg.logging.backup_count,
        )
        file_handler.setFormatter(logging.Formatter(log_format, datefmt=log_datefmt))
        handlers.append(file_handler)

    logging.basicConfig(level=log_level, format=log_format, datefmt=log_datefmt,
                        handlers=handlers)

    for noisy in ("httpcore", "httpx", "urllib3", "pylast", "asyncio",
                  "aiohttp_retry", "aiohttp", "shazamio", "shazamio_core",
                  "shazamio.request"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logger.info("Spindle v%s starting", __version__)

    if not cfg.acoustid.api_key:
        logger.error("AcoustID API key not set — check config.yaml")
        sys.exit(1)

    # --- Last.fm ---
    scrobbler = None
    lastfm_read_network = None

    if args.dry_run and args.canonicalize_preview:
        if not cfg.lastfm.api_key or not cfg.lastfm.api_secret:
            logger.error("Last.fm api_key/api_secret required for --canonicalize-preview")
            sys.exit(1)
        import pylast
        lastfm_read_network = pylast.LastFMNetwork(
            api_key=cfg.lastfm.api_key, api_secret=cfg.lastfm.api_secret,
        )

    if not args.dry_run:
        if not cfg.lastfm.api_key or not cfg.lastfm.username:
            logger.error("Last.fm credentials not set — check config.yaml")
            sys.exit(1)
        scrobbler = Scrobbler(cfg.lastfm, cfg.scrobble)
        scrobbler.connect()

    # --- Spotify + scrobble session ---
    spotify = None
    session = None
    if cfg.spotify.client_id and cfg.spotify.client_secret:
        spotify = SpotifyClient(cfg.spotify)
        session = ScrobbleSession(
            spotify,
            min_play_seconds=cfg.scrobble.min_play_seconds,
            chunk_duration=DEFAULT_STEP,
        )
        logger.info("Spotify lookup + smart session enabled")
    else:
        logger.info("Spotify lookup disabled (no credentials) — simple mode only")

    # --- Telegram ---
    notifier = Notifier(cfg.telegram)
    history  = ScrobbleHistory()

    # --- Display ---
    display = Display(enabled=cfg.display.enabled, fb_path=cfg.display.fb_path)
    display.init()
    display.show_idle()

    # --- Signals ---
    signal.signal(signal.SIGINT,  _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # --- Capture ---
    capture = SlidingCapture(cfg.audio)
    logger.info("Listening on device: %s", cfg.audio.device)
    logger.info("Window: %ds, step: %ds", cfg.audio.chunk_duration, capture.step)

    # --- Telegram bot ---
    bot = None
    if cfg.telegram.bot_token and cfg.telegram.chat_id and not args.dry_run:
        bot = SpindleBot(
            bot_token=cfg.telegram.bot_token,
            chat_id=cfg.telegram.chat_id,
            session=session,
            history=history,
        )
        bot.start()

    if args.dry_run:
        logger.info("DRY RUN — will identify but not scrobble")
    else:
        notifier.started()

    # ------------------------------------------------------------------ #
    #  Helpers                                                            #
    # ------------------------------------------------------------------ #

    def track_key(t: TrackInfo) -> str:
        return f"{t.artist.lower()} - {t.title.lower()}"

    def should_scrobble(t: TrackInfo, start: float) -> bool:
        """Eligibility check for simple-mode (no Spotify) scrobbling."""
        played = time.time() - start
        if played < cfg.scrobble.min_play_seconds:
            return False
        if t.duration and played < t.duration * cfg.scrobble.min_play_fraction:
            return False
        return True

    def do_scrobble(t: TrackInfo, timestamp: float, backfill: bool = False) -> None:
        if scrobbler and not args.dry_run:
            scrobbler.scrobble(t, timestamp=int(timestamp))
            history.log(t, timestamp, source="backfill" if backfill else "live")
            notifier.track_scrobbled(t, is_backfill=backfill)

    def do_now_playing(t: TrackInfo) -> None:
        if scrobbler and not args.dry_run:
            scrobbler.update_now_playing(t)

    def finalize_simple(t: TrackInfo, start: float) -> None:
        """Simple-mode: scrobble if eligible (used when Spotify is not configured)."""
        if not t or args.dry_run:
            return
        if should_scrobble(t, start):
            do_scrobble(t, start)

    # ------------------------------------------------------------------ #
    #  Simple-mode state (used only when Spotify is not configured)       #
    # ------------------------------------------------------------------ #
    current_track:    Optional[TrackInfo] = None
    current_art:      Optional[bytes]     = None
    track_start:      float               = 0.0
    track_scrobbled:  bool                = False
    music_start_time: Optional[float]     = None
    consecutive_silence: int              = 0

    # ------------------------------------------------------------------ #
    #  Main loop                                                          #
    # ------------------------------------------------------------------ #

    while _running:
        try:
            wav_path = capture.capture()

            try:
                # ============================================================
                #  SILENCE
                # ============================================================
                if is_silence(wav_path, cfg.silence):
                    consecutive_silence += 1
                    if consecutive_silence == 1:
                        logger.info("Silence detected")

                        # Smart session: finalize + reset
                        if session:
                            al          = session.album_state
                            al_artist   = al.tracklist.artist     if al else None
                            al_album    = al.tracklist.album_name if al else None
                            # Read comp count BEFORE on_silence() calls reset()
                            comp_count  = session.comp_scrobbled_count

                            for t, ts in session.on_silence():
                                do_scrobble(t, ts)

                            # al ref still valid after reset (Python GC)
                            if al_artist:
                                # Album session: count includes track added by on_silence
                                al_count = len(al.scrobbled) if al else 0
                                if al_count > 0:
                                    notifier.side_finished(
                                        al_artist, al_album,
                                        al_count, al.current_index + 1,
                                    )
                            elif comp_count > 0:
                                # Compilation session
                                notifier.compilation_finished(comp_count)

                        # Simple-mode fallback: finalize current track
                        if not session and current_track and not track_scrobbled:
                            finalize_simple(current_track, track_start)

                        # Reset loop state
                        current_track    = None
                        current_art      = None
                        track_scrobbled  = False
                        music_start_time = None
                        capture.reset()
                        display.show_idle()
                    continue

                # ============================================================
                #  MUSIC — mark start time
                # ============================================================
                if consecutive_silence > 0 or music_start_time is None:
                    music_start_time = time.time() - capture.step
                    logger.debug("Music started (est. %.0f)", music_start_time)
                consecutive_silence = 0

                # ============================================================
                #  TIMING ADVANCE (album mode only)
                # ============================================================
                if session and session.is_locked():
                    al_before = session.album_state
                    prev_idx  = al_before.current_index if al_before else -1

                    advance_scrobbles = session.check_advance()
                    for t, ts in advance_scrobbles:
                        do_scrobble(t, ts, backfill=False)

                    predicted = session.get_current_track()
                    if predicted:
                        current_track   = predicted
                        track_scrobbled = True
                        do_now_playing(predicted)
                        al = session.album_state
                        display.show_track(
                            predicted, cover_art=current_art,
                            track_number=al.current_index + 1 if al else 0,
                        )
                        if advance_scrobbles and al and al.current_index != prev_idx:
                            notifier.track_advanced(
                                al.tracklist.artist, al.tracklist.album_name,
                                predicted.title,
                                al.current_index + 1, len(al.tracklist.tracks),
                            )

                # ============================================================
                #  FINGERPRINT
                # ============================================================
                if not capture.is_full:
                    continue

                track = identify(wav_path, cfg.acoustid, cfg.fingerprint)

                if not track:
                    # No ID — keep now-playing updated from session prediction
                    if session and session.is_locked():
                        predicted = session.get_current_track()
                        if predicted:
                            do_now_playing(predicted)
                    continue

                # ============================================================
                #  SPOTIFY LOOKUP
                # ============================================================
                spotify_result = None
                if spotify:
                    spotify_result = spotify.lookup(track.artist, track.title)
                    if spotify_result:
                        track = spotify_result.track

                # Cover art (update whenever we get a fresh Spotify result)
                if spotify_result and spotify_result.album_art_url:
                    current_art = spotify.fetch_art(spotify_result.album_art_url)

                # ============================================================
                #  SMART SESSION (album-lock + compilation detection)
                # ============================================================
                if session and spotify_result and not args.dry_run:
                    was_locked  = session.is_locked()
                    was_mode    = session.mode

                    scrobbles = session.on_identified(spotify_result, music_start_time)
                    for t, ts in scrobbles:
                        do_scrobble(t, ts, backfill=True)

                    # Notify when album-lock first activates
                    if session.is_locked() and not was_locked:
                        al = session.album_state
                        if al:
                            notifier.album_locked(
                                al.tracklist.artist, al.tracklist.album_name,
                                al.current_index + 1, len(al.tracklist.tracks),
                                track.title,
                            )

                    # Log compilation mode switch
                    if (session.mode == SessionMode.COMPILATION
                            and was_mode != SessionMode.COMPILATION):
                        logger.info(
                            "Session: compilation mode active — per-track scrobble only",
                        )

                # ============================================================
                #  DISPLAY
                # ============================================================
                al_state = session.album_state if session else None
                display.show_track(
                    session.get_current_track() or track if session else track,
                    cover_art=current_art,
                    track_number=al_state.current_index + 1 if al_state else 0,
                )

                # ============================================================
                #  DRY RUN
                # ============================================================
                if args.dry_run:
                    line = (
                        f"🎵 {track.artist} — {track.title}"
                        f"{f' [{track.album}]' if track.album else ''}"
                        f" (via {track.source}, {track.confidence:.0%})"
                    )
                    if args.canonicalize_preview and lastfm_read_network is not None:
                        canon = canonicalize_track(track, lastfm_read_network)
                        if canon.artist != track.artist or canon.title != track.title:
                            line += f"\n   ↳ canonical: {canon.artist} — {canon.title}"
                        if canon.duration and not track.duration:
                            line += f"\n   ↳ duration: {canon.duration}s"
                    if session and session.is_locked():
                        progress = session.get_progress()
                        if progress:
                            line += f"\n   ↳ album-lock: {progress[0]:.0f}s / {progress[1]:.0f}s"
                    if session and session.mode == SessionMode.COMPILATION:
                        line += "\n   ↳ mode: COMPILATION"
                    print(line)
                    continue

                # ============================================================
                #  NOW PLAYING + SCROBBLE ELIGIBILITY
                # ============================================================
                if session and session.is_locked():
                    # Album mode: session drives everything
                    predicted = session.get_current_track() or track
                    do_now_playing(predicted)
                    current_track   = predicted
                    track_scrobbled = True

                elif session and session.mode == SessionMode.COMPILATION:
                    # Compilation mode: scrobble handled by session.on_identified above
                    do_now_playing(track)
                    current_track   = track
                    track_scrobbled = True

                else:
                    # Simple mode: no Spotify, or Spotify lookup returned nothing
                    if (current_track is None
                            or track_key(track) != track_key(current_track)):
                        if current_track and not track_scrobbled:
                            finalize_simple(current_track, track_start)
                        current_track   = track
                        track_start     = time.time()
                        track_scrobbled = False
                        logger.info("Now playing: %s — %s", track.artist, track.title)
                        do_now_playing(track)
                    else:
                        if not track_scrobbled and should_scrobble(current_track, track_start):
                            do_scrobble(current_track, track_start)
                            track_scrobbled = True
                        do_now_playing(track)

            finally:
                wav_path.unlink(missing_ok=True)

        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error("Error in main loop: %s", e, exc_info=True)
            notifier.error(str(e))
            time.sleep(5)

    # ------------------------------------------------------------------ #
    #  Shutdown                                                           #
    # ------------------------------------------------------------------ #
    if session:
        for t, ts in session.on_silence():
            do_scrobble(t, ts)
    elif current_track and not track_scrobbled:
        finalize_simple(current_track, track_start)
    if bot:
        bot.stop()
    display.clear()
    logger.info("Spindle stopped")


if __name__ == "__main__":
    main()
