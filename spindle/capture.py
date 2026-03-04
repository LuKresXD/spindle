"""Audio capture from ALSA device with sliding window.

Records in short segments (default 2s) and concatenates the last N into a
full fingerprint window (default 10s). This gives a new 10s window every 2s,
dramatically reducing identification latency without sacrificing quality.

Example with step=2s, window=10s:
  t=0s   → record 2s → buffer=[A]           → fingerprint A (2s, short)
  t=2s   → record 2s → buffer=[A,B]         → fingerprint A+B (4s)
  ...
  t=8s   → record 2s → buffer=[A,B,C,D,E]  → fingerprint A+B+C+D+E (10s)
  t=10s  → record 2s → buffer=[B,C,D,E,F]  → fingerprint B+C+D+E+F (10s)
"""

import collections
import struct
import subprocess
import tempfile
import logging
from pathlib import Path

import numpy as np

from .config import AudioConfig, SilenceConfig

logger = logging.getLogger(__name__)

# Default step size (seconds) — how often we get a new window
DEFAULT_STEP = 2


class SlidingCapture:
    """Sliding window audio capture.

    Records `step` seconds at a time, keeps a ring buffer of segments,
    and concatenates them into `chunk_duration` second windows.
    """

    def __init__(self, audio_cfg: AudioConfig, step: int = DEFAULT_STEP):
        self.cfg = audio_cfg
        self.step = step
        # How many segments to keep for a full window
        self.num_segments = max(audio_cfg.chunk_duration // step, 1)
        self._segments: collections.deque[bytes] = collections.deque(
            maxlen=self.num_segments,
        )
        logger.info(
            "Sliding capture: %ds window, %ds step (%d segments)",
            audio_cfg.chunk_duration, step, self.num_segments,
        )

    def capture(self) -> Path:
        """Record one step and return a WAV of the full sliding window."""
        segment = self._record_segment()
        self._segments.append(segment)

        # Concatenate all segments in buffer
        full_pcm = b"".join(self._segments)

        # Write as WAV
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.close()
        output = Path(tmp.name)
        self._write_wav(output, full_pcm)

        duration = len(full_pcm) / (self.cfg.sample_rate * self.cfg.channels * 2)
        logger.debug("Window: %.1fs (%d segments, %.1f KB)",
                     duration, len(self._segments), output.stat().st_size / 1024)
        return output

    def reset(self) -> None:
        """Clear the buffer (call after silence to start fresh)."""
        self._segments.clear()

    def _record_segment(self) -> bytes:
        """Record `step` seconds and return raw PCM bytes."""
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.close()
        path = Path(tmp.name)

        cmd = [
            "arecord",
            "-D", self.cfg.device,
            "-f", "S16_LE",
            "-r", str(self.cfg.sample_rate),
            "-c", str(self.cfg.channels),
            "-d", str(self.step),
            "-q",
            str(path),
        ]

        try:
            subprocess.run(cmd, check=True, timeout=self.step + 5)
            raw = path.read_bytes()
            return raw[44:] if len(raw) > 44 else b""
        finally:
            path.unlink(missing_ok=True)

    def _write_wav(self, path: Path, pcm_data: bytes) -> None:
        """Write raw PCM data as a WAV file."""
        ch = self.cfg.channels
        sr = self.cfg.sample_rate
        bps = 16
        with open(path, "wb") as f:
            f.write(b"RIFF")
            f.write(struct.pack("<I", 36 + len(pcm_data)))
            f.write(b"WAVE")
            f.write(b"fmt ")
            f.write(struct.pack("<IHHIIHH", 16, 1, ch, sr,
                                sr * ch * bps // 8, ch * bps // 8, bps))
            f.write(b"data")
            f.write(struct.pack("<I", len(pcm_data)))
            f.write(pcm_data)


def is_silence(wav_path: Path, silence_cfg: SilenceConfig) -> bool:
    """Check if a WAV file is effectively silence."""
    try:
        data = np.frombuffer(
            Path(wav_path).read_bytes()[44:],
            dtype=np.int16,
        )
        if len(data) == 0:
            return True

        rms = np.sqrt(np.mean(data.astype(np.float64) ** 2))
        if rms == 0:
            return True

        db = 20 * np.log10(rms / 32768.0)
        logger.debug("Audio level: %.1f dB (threshold: %.1f dB)", db, silence_cfg.threshold_db)
        return db < silence_cfg.threshold_db

    except Exception as e:
        logger.warning("Silence detection failed: %s", e)
        return False
