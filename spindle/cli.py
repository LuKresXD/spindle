"""Spindle CLI — main entry point."""

import argparse
import logging
import signal
import sys
import time
from pathlib import Path

from . import __version__
from .config import load_config
from .capture import capture_chunk, is_silence
from .fingerprint import identify, TrackInfo
from .scrobbler import Scrobbler, canonicalize_track
from .display import Display
from .spotify import SpotifyClient
from .albumlock import AlbumLock

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

    # --- Logging ---
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("httpcore", "httpx", "urllib3", "pylast", "asyncio",
                  "aiohttp_retry", "aiohttp", "shazamio", "shazamio_core",
                  "shazamio.request"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # --- Config ---
    cfg = load_config(args.config)
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
            chunk_duration=cfg.audio.chunk_duration,
        )
        logger.info("Spotify lookup + album-lock enabled")
    else:
        logger.info("Spotify lookup disabled (no credentials)")

    # --- Display ---
    display = Display(enabled=cfg.display.enabled)
    display.init()
    display.show_idle()

    # --- Signals ---
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    logger.info("Listening on device: %s", cfg.audio.device)
    logger.info("Chunk duration: %ds", cfg.audio.chunk_duration)
    if args.dry_run:
        logger.info("DRY RUN — will identify but not scrobble")

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

    def do_scrobble(t: TrackInfo, timestamp: float) -> None:
        if scrobbler and not args.dry_run:
            scrobbler.scrobble(t, timestamp=int(timestamp))

    def do_now_playing(t: TrackInfo) -> None:
        if scrobbler and not args.dry_run:
            scrobbler.update_now_playing(t)

    def finalize_simple(t: TrackInfo, start: float) -> None:
        """Scrobble via simple mode (non-album-lock) if threshold met."""
        if not t or args.dry_run:
            return
        if should_scrobble(t, start):
            do_scrobble(t, start)

    # ------------------------------------------------------------------ #
    #  State                                                              #
    # ------------------------------------------------------------------ #

    # Simple-mode state (fallback when album-lock isn't active)
    current_track = None
    track_start = 0.0
    track_scrobbled = False

    # Shared
    music_start_time = None   # when music first started after silence
    consecutive_silence = 0

    # ------------------------------------------------------------------ #
    #  Main loop                                                          #
    # ------------------------------------------------------------------ #

    while _running:
        try:
            wav_path = capture_chunk(cfg.audio)

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
                            for t, ts in album_lock.on_silence():
                                do_scrobble(t, ts)

                        # Simple-mode: finalize
                        if current_track and not track_scrobbled:
                            finalize_simple(current_track, track_start)

                        # Reset all state
                        current_track = None
                        track_scrobbled = False
                        music_start_time = None
                        display.show_idle()
                    continue

                # ============================================================
                #  MUSIC DETECTED — mark start time
                # ============================================================
                if consecutive_silence > 0 or music_start_time is None:
                    # Music started DURING the chunk we just captured,
                    # which was chunk_duration seconds ago.
                    music_start_time = time.time() - cfg.audio.chunk_duration
                    logger.debug("Music started (est. %.0f)", music_start_time)

                consecutive_silence = 0

                # ============================================================
                #  ALBUM-LOCK: check timing advance
                # ============================================================
                if album_lock and album_lock.is_locked():
                    for t, ts in album_lock.check_advance():
                        do_scrobble(t, ts)

                    # Sync display / now-playing with predicted track
                    predicted = album_lock.get_current_track()
                    if predicted:
                        current_track = predicted
                        track_scrobbled = True  # album-lock manages scrobbling
                        do_now_playing(predicted)
                        display.show_track(predicted)

                # ============================================================
                #  FINGERPRINT
                # ============================================================
                track = identify(wav_path, cfg.acoustid, cfg.fingerprint)

                # No match — if album-locked, trust the prediction
                if not track:
                    if album_lock and album_lock.is_locked():
                        predicted = album_lock.get_current_track()
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

                # ============================================================
                #  ALBUM LOCK
                # ============================================================
                if album_lock and spotify_result and not args.dry_run:
                    for t, ts in album_lock.on_track_identified(
                        spotify_result, music_start_time,
                    ):
                        do_scrobble(t, ts)

                # ============================================================
                #  DISPLAY
                # ============================================================
                display.show_track(track)

                # ============================================================
                #  DRY RUN OUTPUT
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
                #  SCROBBLE LOGIC
                # ============================================================

                if album_lock and album_lock.is_locked():
                    # Album-lock active → it handles all scrobbling.
                    # Just update now-playing and sync state.
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
                        # Same track — scrobble once after threshold
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
            time.sleep(5)

    # ------------------------------------------------------------------ #
    #  Shutdown                                                           #
    # ------------------------------------------------------------------ #
    if album_lock:
        for t, ts in album_lock.on_silence():
            do_scrobble(t, ts)
    if current_track and not track_scrobbled:
        finalize_simple(current_track, track_start)
    display.clear()
    logger.info("Spindle stopped")


if __name__ == "__main__":
    main()
