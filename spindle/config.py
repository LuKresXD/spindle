"""Configuration loader for Spindle."""

from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

import yaml


DEFAULT_CONFIG_PATHS = [
    Path("config.yaml"),
    Path.home() / ".config" / "spindle" / "config.yaml",
    Path("/etc/spindle/config.yaml"),
]


@dataclass
class AcoustIDConfig:
    api_key: str = ""


@dataclass
class LastFMConfig:
    api_key: str = ""
    api_secret: str = ""
    username: str = ""
    password_hash: str = ""


@dataclass
class AudioConfig:
    device: str = "default"
    sample_rate: int = 44100
    channels: int = 1
    chunk_duration: int = 25


@dataclass
class FingerprintConfig:
    acoustid_timeout: int = 10
    shazam_fallback: bool = True
    min_confidence: float = 0.5


@dataclass
class ScrobbleConfig:
    min_play_seconds: int = 30
    min_play_fraction: float = 0.5
    now_playing: bool = True
    dedup_window: int = 300


@dataclass
class SilenceConfig:
    threshold_db: float = -40.0
    min_silence_seconds: float = 3.0


@dataclass
class DisplayConfig:
    enabled: bool = False
    width: int = 480
    height: int = 320
    spi_device: str = "/dev/spidev0.0"


@dataclass
class SpindleConfig:
    acoustid: AcoustIDConfig = field(default_factory=AcoustIDConfig)
    lastfm: LastFMConfig = field(default_factory=LastFMConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    fingerprint: FingerprintConfig = field(default_factory=FingerprintConfig)
    scrobble: ScrobbleConfig = field(default_factory=ScrobbleConfig)
    silence: SilenceConfig = field(default_factory=SilenceConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)


def load_config(path: Optional[Path] = None) -> SpindleConfig:
    """Load config from YAML file. Searches default paths if none given."""
    if path is not None:
        paths = [Path(path)]
    else:
        paths = DEFAULT_CONFIG_PATHS

    for p in paths:
        if p.exists():
            with open(p) as f:
                raw = yaml.safe_load(f) or {}
            return _parse_config(raw)

    # No config file found — return defaults
    return SpindleConfig()


def _parse_config(raw: dict) -> SpindleConfig:
    """Parse raw YAML dict into SpindleConfig."""
    cfg = SpindleConfig()

    if "acoustid" in raw:
        cfg.acoustid = AcoustIDConfig(**raw["acoustid"])
    if "lastfm" in raw:
        cfg.lastfm = LastFMConfig(**raw["lastfm"])
    if "audio" in raw:
        cfg.audio = AudioConfig(**raw["audio"])
    if "fingerprint" in raw:
        cfg.fingerprint = FingerprintConfig(**raw["fingerprint"])
    if "scrobble" in raw:
        cfg.scrobble = ScrobbleConfig(**raw["scrobble"])
    if "silence" in raw:
        cfg.silence = SilenceConfig(**raw["silence"])
    if "display" in raw:
        cfg.display = DisplayConfig(**raw["display"])

    return cfg
