"""Audio capture from ALSA device."""

import subprocess
import tempfile
import logging
from pathlib import Path

import numpy as np

from .config import AudioConfig, SilenceConfig

logger = logging.getLogger(__name__)


def capture_chunk(audio_cfg: AudioConfig) -> Path:
    """Record a chunk of audio to a temporary WAV file.

    Returns path to the WAV file.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    output = Path(tmp.name)

    cmd = [
        "arecord",
        "-D", audio_cfg.device,
        "-f", "S16_LE",
        "-r", str(audio_cfg.sample_rate),
        "-c", str(audio_cfg.channels),
        "-d", str(audio_cfg.chunk_duration),
        "-q",
        str(output),
    ]

    logger.debug("Recording %ds chunk: %s", audio_cfg.chunk_duration, " ".join(cmd))

    try:
        subprocess.run(cmd, check=True, timeout=audio_cfg.chunk_duration + 5)
    except subprocess.TimeoutExpired:
        logger.error("arecord timed out after %ds", audio_cfg.chunk_duration + 5)
        output.unlink(missing_ok=True)
        raise
    except subprocess.CalledProcessError as e:
        logger.error("arecord failed: %s", e)
        output.unlink(missing_ok=True)
        raise

    logger.debug("Captured %s (%.1f KB)", output, output.stat().st_size / 1024)
    return output


def is_silence(wav_path: Path, silence_cfg: SilenceConfig) -> bool:
    """Check if a WAV file is effectively silence.

    Uses numpy to compute RMS and compare against threshold.
    """
    try:
        # Read raw PCM from WAV (skip 44-byte header)
        data = np.frombuffer(
            Path(wav_path).read_bytes()[44:],
            dtype=np.int16,
        )
        if len(data) == 0:
            return True

        # RMS in dB
        rms = np.sqrt(np.mean(data.astype(np.float64) ** 2))
        if rms == 0:
            return True

        db = 20 * np.log10(rms / 32768.0)
        logger.debug("Audio level: %.1f dB (threshold: %.1f dB)", db, silence_cfg.threshold_db)
        return db < silence_cfg.threshold_db

    except Exception as e:
        logger.warning("Silence detection failed: %s", e)
        return False
