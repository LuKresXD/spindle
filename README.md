# 🎵 Spindle

Vinyl scrobbler for Raspberry Pi. Listens to your turntable, identifies tracks via audio fingerprinting, and scrobbles to Last.fm.

## How it works

```
Turntable → RCA Y-splitter → USB audio adapter → Raspberry Pi
                ↓
            Speakers
```

1. **Capture** — Records audio from USB adapter in short chunks
2. **Fingerprint** — Identifies tracks via [AcoustID](https://acoustid.org) (Chromaprint), with [ShazamIO](https://github.com/dotX12/shazamio) fallback
3. **Scrobble** — Sends to [Last.fm](https://last.fm) with now-playing updates and dedup
4. **Display** *(coming soon)* — Album art on a 3.5" LCD

## Hardware

- Raspberry Pi 4
- USB audio adapter (e.g., Plugable)
- RCA Y-splitters (to feed both speakers and Pi)
- *(Optional)* Waveshare 3.5" IPS LCD

## Quick start

### 1. Install system dependencies

```bash
sudo apt install -y python3-pip python3-venv ffmpeg libchromaprint-tools portaudio19-dev alsa-utils
```

### 2. Clone and install

```bash
git clone https://github.com/LuKresXD/spindle.git
cd spindle
python3 -m venv venv
source venv/bin/activate
pip install -e .
```

### 3. Configure

```bash
cp config.example.yaml config.yaml
# Edit config.yaml with your API keys
```

You'll need:
- [AcoustID API key](https://acoustid.org/new-application)
- [Last.fm API account](https://www.last.fm/api/account/create)

### 4. Run

```bash
# Dry run (identify only, no scrobbling)
spindle --dry-run -v

# Full mode
spindle -v
```

## Roadmap

- [x] Project scaffold
- [ ] v0.1 — Audio capture + AcoustID fingerprinting
- [ ] v0.2 — Last.fm scrobbling with dedup
- [ ] v0.3 — ShazamIO fallback
- [ ] v0.4 — Waveshare LCD display with album art
- [ ] v0.5 — systemd service, polish

## License

MIT
