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
from .albumlock import AlbumLock
from .notify import Notifier
from .history import ScrobbleHistory
from .bot import SpindleBot

logger = logging.getLogger("spindle")

_running = True

# When album-locked, only re-fingerprint every LOCKED_CONFIRM_INTERVAL
# chunks to verify position. Between checks, trust timing prediction.
# This saves CPU + API calls without affecting scrobble accuracy
# (album-lock timing handles track advances regardless).
LOCKED_CONFIRM_CHUNKS = 10  # 10 × 2s = confirm every ~20s


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
    log_level = logging.DEBUG if args.verbose else logging.INFO
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

    # --- Spotify + album lock ---
    spotify = None
    album_lock = None
    if cfg.spotify.client_id and cfg.spotify.client_secret:
        spotify = SpotifyClient(cfg.spotify)
        album_lock = AlbumLock(
            spotify,
            min_play_seconds=cfg.scrobble.min_play_seconds,
            chunk_duration=DEFAULT_STEP,
        )
        logger.info("Spotify lookup + album-lock enabled")
    else:
        logger.info("Spotify lookup disabled (no credentials)")

    # --- Telegram ---
    notifier = Notifier(cfg.telegram)
    history = ScrobbleHistory()

    # --- Display ---
    display = Display(enabled=cfg.display.enabled, fb_path=cfg.display.fb_path)
    display.init()
    display.show_idle()

    # --- Signals ---
    signal.signal(signal.SIGINT, _signal_handler)
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
            album_lock=album_lock,
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
        if not t or args.dry_run:
            return
        if should_scrobble(t, start):
            do_scrobble(t, start)

    # ------------------------------------------------------------------ #
    #  State                                                              #
    # ------------------------------------------------------------------ #

    current_track = None
    current_art: Optional[bytes] = None
    track_start = 0.0
    track_scrobbled = False
    music_start_time = None
    consecutive_silence = 0
    locked_chunk_counter = 0  # chunks since last fingerprint while locked

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

                        # Album-lock: end session
                        if album_lock:
                            al = album_lock.session if album_lock.is_locked() else None
                            al_artist = al.tracklist.artist if al else None
                            al_album = al.tracklist.album_name if al else None
                            al_scrobbled = len(al.scrobbled) if al else 0

                            for t, ts in album_lock.on_silence():
                                do_scrobble(t, ts)
                                if al:
                                    al_scrobbled = len(al.scrobbled)

                            if al_artist and al_scrobbled > 0:
                                notifier.side_finished(
                                    al_artist, al_album,
                                    al_scrobbled, al.current_index + 1,
                                )

                        # Simple-mode: finalize
                        if current_track and not track_scrobbled:
                            finalize_simple(current_track, track_start)

                        # Reset all state
                        current_track = None
                        current_art = None
                        track_scrobbled = False
                        music_start_time = None
                        locked_chunk_counter = 0
                        capture.reset()
                        display.show_idle()
                    continue

                # ============================================================
                #  MUSIC — mark start
                # ============================================================
                if consecutive_silence > 0 or music_start_time is None:
                    music_start_time = time.time() - capture.step
                    logger.debug("Music started (est. %.0f)", music_start_time)

                consecutive_silence = 0

                # ============================================================
                #  ALBUM-LOCK: timing advance
                # ============================================================
                if album_lock and album_lock.is_locked():
                    advance_scrobbles = album_lock.check_advance()
                    for t, ts in advance_scrobbles:
                        do_scrobble(t, ts, backfill=False)

                    predicted = album_lock.get_current_track()
                    if predicted:
                        current_track = predicted
                        track_scrobbled = True
                        do_now_playing(predicted)
                        pos, dur = album_lock.get_progress() or (0.0, 0.0)
                        al = album_lock.session
                        display.show_track(
                            predicted,
                            cover_art=current_art,
                            position_sec=pos,
                            track_number=al.current_index + 1 if al else 0,
                        )

                        if advance_scrobbles and album_lock.session:
                            al = album_lock.session
                            notifier.track_advanced(
                                al.tracklist.artist,
                                al.tracklist.album_name,
                                predicted.title,
                                al.current_index + 1,
                                len(al.tracklist.tracks),
                            )

                # ============================================================
                #  FINGERPRINT
                # ============================================================

                # Wait for buffer to fill (need full 10s window)
                if not capture.is_full:
                    continue

                # When album-locked, fingerprint is just a periodic confirmation.
                # Timing handles track advances — fingerprint only catches drift.
                # No urgency, so slow down to save CPU.
                if album_lock and album_lock.is_locked():
                    locked_chunk_counter += 1
                    if locked_chunk_counter < LOCKED_CONFIRM_CHUNKS:
                        continue
                    locked_chunk_counter = 0
                else:
                    locked_chunk_counter = 0

                # -- Fingerprint the audio --
                track = identify(wav_path, cfg.acoustid, cfg.fingerprint)

                if not track:
                    if album_lock and album_lock.is_locked():
                        predicted = album_lock.get_current_track()
                        if predicted:
                            do_now_playing(predicted)
                    continue

                # ============================================================
                #  SPOTIFY LOOKUP (cached — no API call for repeated tracks)
                # ============================================================
                spotify_result = None
                if spotify:
                    spotify_result = spotify.lookup(track.artist, track.title)
                    if spotify_result:
                        track = spotify_result.track

                # ============================================================
                #  ALBUM LOCK
                # ============================================================
                if album_lock and spotify_result and not args.dry_run:
                    was_locked = album_lock.is_locked()
                    scrobbles = album_lock.on_track_identified(
                        spotify_result, music_start_time,
                    )
                    for t, ts in scrobbles:
                        do_scrobble(t, ts, backfill=True)

                    if album_lock.is_locked() and not was_locked:
                        al = album_lock.session
                        if al:
                            notifier.album_locked(
                                al.tracklist.artist,
                                al.tracklist.album_name,
                                al.current_index + 1,
                                len(al.tracklist.tracks),
                                track.title,
                            )

                # ============================================================
                #  DISPLAY
                # ============================================================
                if spotify_result and spotify_result.album_art_url:
                    current_art = spotify.fetch_art(spotify_result.album_art_url)
                al_session = album_lock.session if album_lock else None
                display.show_track(
                    track,
                    cover_art=current_art,
                    track_number=al_session.current_index + 1 if al_session else 0,
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
                    if album_lock and album_lock.is_locked():
                        progress = album_lock.get_progress()
                        if progress:
                            line += f"\n   ↳ album-lock: {progress[0]:.0f}s / {progress[1]:.0f}s"
                    print(line)
                    continue

                # ============================================================
                #  SCROBBLE
                # ============================================================
                if album_lock and album_lock.is_locked():
                    predicted = album_lock.get_current_track() or track
                    do_now_playing(predicted)
                    current_track = predicted
                    track_scrobbled = True
                else:
                    # Simple mode — track change detection
                    if current_track is None or track_key(track) != track_key(current_track):
                        if current_track and not track_scrobbled:
                            finalize_simple(current_track, track_start)
                        current_track = track
                        track_start = time.time()
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
    if album_lock:
        for t, ts in album_lock.on_silence():
            do_scrobble(t, ts)
    if current_track and not track_scrobbled:
        finalize_simple(current_track, track_start)
    if bot:
        bot.stop()
    display.clear()
    logger.info("Spindle stopped")


if __name__ == "__main__":
    main()
