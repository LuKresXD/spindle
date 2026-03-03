"""Spindle CLI — main entry point."""

import argparse
import logging
import os
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

# Graceful shutdown
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
        "--canonicalize-preview",
        action="store_true",
        help="In dry-run: also query Last.fm corrections and show the canonical artist/title",
    )
    parser.add_argument("--version", action="version", version=f"spindle {__version__}")
    args = parser.parse_args()

    # Logging
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("httpcore", "httpx", "urllib3", "pylast", "asyncio",
                  "aiohttp_retry", "aiohttp", "shazamio", "shazamio_core",
                  "shazamio.request"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # Load config
    cfg = load_config(args.config)
    logger.info("Spindle v%s starting", __version__)

    if not cfg.acoustid.api_key:
        logger.error("AcoustID API key not set — check config.yaml")
        sys.exit(1)

    # Last.fm
    scrobbler = None
    lastfm_read_network = None

    if args.dry_run and args.canonicalize_preview:
        if not cfg.lastfm.api_key or not cfg.lastfm.api_secret:
            logger.error("Last.fm api_key/api_secret required for --canonicalize-preview")
            sys.exit(1)
        import pylast
        lastfm_read_network = pylast.LastFMNetwork(
            api_key=cfg.lastfm.api_key,
            api_secret=cfg.lastfm.api_secret,
        )

    if not args.dry_run:
        if not cfg.lastfm.api_key or not cfg.lastfm.username:
            logger.error("Last.fm credentials not set — check config.yaml")
            sys.exit(1)
        scrobbler = Scrobbler(cfg.lastfm, cfg.scrobble)
        scrobbler.connect()

    # Spotify + album lock
    spotify = None
    album_lock = None
    if cfg.spotify.client_id and cfg.spotify.client_secret:
        spotify = SpotifyClient(cfg.spotify)
        album_lock = AlbumLock(spotify, min_play_seconds=cfg.scrobble.min_play_seconds)
        logger.info("Spotify lookup + album-lock enabled")
    else:
        logger.info("Spotify lookup disabled (no credentials)")

    # Display
    display = Display(enabled=cfg.display.enabled)
    display.init()
    display.show_idle()

    # Signals
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    logger.info("Listening on device: %s", cfg.audio.device)
    logger.info("Chunk duration: %ds", cfg.audio.chunk_duration)
    if args.dry_run:
        logger.info("DRY RUN — will identify but not scrobble")

    # --- Helpers ---

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
        """Scrobble a single track."""
        if not scrobbler or args.dry_run:
            return
        scrobbler.scrobble(t, timestamp=int(timestamp))

    def finalize(t: TrackInfo, start: float) -> None:
        if not t or args.dry_run:
            return
        if should_scrobble(t, start):
            do_scrobble(t, start)
        else:
            logger.debug("Track too short to scrobble: %.0fs played", time.time() - start)

    # --- Track state ---
    current_track = None
    track_start = 0.0
    track_scrobbled = False

    # --- Main loop ---
    consecutive_silence = 0
    while _running:
        try:
            wav_path = capture_chunk(cfg.audio)

            try:
                # --- Silence ---
                if is_silence(wav_path, cfg.silence):
                    consecutive_silence += 1
                    if consecutive_silence == 1:
                        logger.info("Silence detected — waiting for music")
                        if current_track and not track_scrobbled:
                            finalize(current_track, track_start)
                        # Album lock: scrobble current track on silence
                        if album_lock:
                            silence_track = album_lock.on_silence()
                            if silence_track and not track_scrobbled:
                                do_scrobble(silence_track, track_start)
                        current_track = None
                        track_scrobbled = False
                        display.show_idle()
                    continue

                consecutive_silence = 0

                # --- Album-lock: check if current track ended by timing ---
                if album_lock and album_lock.is_locked() and not args.dry_run:
                    advanced_track = album_lock.check_advance()
                    if advanced_track:
                        # Previous track finished by timing — scrobble it
                        do_scrobble(advanced_track, track_start)
                        # Update state to the new predicted track
                        predicted = album_lock.get_predicted_track()
                        if predicted:
                            current_track = predicted
                            track_start = time.time()
                            track_scrobbled = False
                            logger.info("Now playing (album-lock): %s — %s",
                                        predicted.artist, predicted.title)
                            scrobbler.update_now_playing(predicted)
                            display.show_track(predicted)

                # --- Fingerprint ---
                track = identify(wav_path, cfg.acoustid, cfg.fingerprint)

                # If no fingerprint but album-locked, trust the prediction
                if not track and album_lock and album_lock.is_locked():
                    predicted = album_lock.get_predicted_track()
                    if predicted:
                        logger.debug("No fingerprint — trusting album-lock: %s — %s",
                                     predicted.artist, predicted.title)
                        # Update now playing with predicted track
                        if not args.dry_run:
                            scrobbler.update_now_playing(predicted)
                    continue

                if not track:
                    continue

                # --- Spotify lookup ---
                spotify_result = None
                if spotify:
                    spotify_result = spotify.lookup(track.artist, track.title)
                    if spotify_result:
                        track = spotify_result.track
                        if not track.album and spotify_result.album_name:
                            track = TrackInfo(
                                title=track.title,
                                artist=track.artist,
                                album=spotify_result.album_name,
                                duration=track.duration,
                                mbid=track.mbid,
                                source=track.source,
                                confidence=track.confidence,
                            )

                # --- Album lock ---
                if album_lock and spotify_result and not args.dry_run:
                    backfill = album_lock.on_track_identified(spotify_result)
                    if backfill:
                        # Scrobble backfilled tracks
                        for bf_track in backfill:
                            logger.info("Album-lock backfill: %s — %s",
                                        bf_track.artist, bf_track.title)
                            do_scrobble(bf_track, time.time() - (bf_track.duration or 180))

                # --- Display ---
                display.show_track(track)

                # --- Dry run ---
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
                        line += f"\n   ↳ album-lock: active"
                    print(line)
                    continue

                # --- Track change detection ---
                if current_track is None or track_key(track) != track_key(current_track):
                    if current_track and not track_scrobbled:
                        finalize(current_track, track_start)
                    current_track = track
                    track_start = time.time()
                    track_scrobbled = False
                    logger.info("Now playing: %s — %s", track.artist, track.title)
                    scrobbler.update_now_playing(track)
                else:
                    # Same track — scrobble once after threshold
                    if not track_scrobbled and should_scrobble(current_track, track_start):
                        do_scrobble(current_track, track_start)
                        track_scrobbled = True
                    scrobbler.update_now_playing(track)

            finally:
                wav_path.unlink(missing_ok=True)

        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error("Error in main loop: %s", e, exc_info=True)
            time.sleep(5)

    # Finalize on exit
    if current_track and not track_scrobbled:
        finalize(current_track, track_start)
    if album_lock:
        silence_track = album_lock.on_silence()
        if silence_track:
            do_scrobble(silence_track, track_start)
    display.clear()
    logger.info("Spindle stopped")


if __name__ == "__main__":
    main()
