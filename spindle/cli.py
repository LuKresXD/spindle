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
from .fingerprint import identify
from .scrobbler import Scrobbler
from .display import Display

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
    parser.add_argument("--version", action="version", version=f"spindle {__version__}")
    args = parser.parse_args()

    # Logging
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Load config
    cfg = load_config(args.config)
    logger.info("Spindle v%s starting", __version__)

    # Validate required keys
    if not cfg.acoustid.api_key:
        logger.error("AcoustID API key not set — check config.yaml")
        sys.exit(1)

    # Connect to Last.fm (unless dry run)
    scrobbler = None
    if not args.dry_run:
        if not cfg.lastfm.api_key or not cfg.lastfm.username:
            logger.error("Last.fm credentials not set — check config.yaml")
            sys.exit(1)
        scrobbler = Scrobbler(cfg.lastfm, cfg.scrobble)
        scrobbler.connect()

    # Init display
    display = Display(enabled=cfg.display.enabled)
    display.init()
    display.show_idle()

    # Signal handling
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    logger.info("Listening on device: %s", cfg.audio.device)
    logger.info("Chunk duration: %ds", cfg.audio.chunk_duration)
    if args.dry_run:
        logger.info("DRY RUN — will identify but not scrobble")

    # Main loop
    consecutive_silence = 0
    while _running:
        try:
            # Capture audio
            wav_path = capture_chunk(cfg.audio)

            try:
                # Check for silence
                if is_silence(wav_path, cfg.silence):
                    consecutive_silence += 1
                    if consecutive_silence == 1:
                        logger.info("Silence detected — waiting for music")
                        display.show_idle()
                    continue

                consecutive_silence = 0

                # Identify track
                track = identify(wav_path, cfg.acoustid, cfg.fingerprint)
                if not track:
                    continue

                # Update display
                display.show_track(track)

                if args.dry_run:
                    print(f"🎵 {track.artist} — {track.title}"
                          f"{f' [{track.album}]' if track.album else ''}"
                          f" (via {track.source}, {track.confidence:.0%})")
                    continue

                # Scrobble
                scrobbler.update_now_playing(track)
                scrobbler.scrobble(track)

            finally:
                # Clean up temp file
                wav_path.unlink(missing_ok=True)

        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error("Error in main loop: %s", e, exc_info=True)
            time.sleep(5)  # back off on errors

    display.clear()
    logger.info("Spindle stopped")


if __name__ == "__main__":
    main()
