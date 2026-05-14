# 🎧 Ownly Audio Pocket

A self-hosted PWA music player for your local network. Stream and cache your music collection on any device — no cloud, no subscriptions.

## Features

- 📱 **PWA** — installable on iOS & Android, works offline
- 🎵 **Offline caching** — download songs/albums to IndexedDB for offline playback
- 🗂️ **Band → Album → Track** hierarchy, all collapsible
- 🔍 **Song detail view** — ID3 metadata + cover art on tap
- 🔒 **HTTPS** — required for PWA + service worker on mobile
- ⚡ **Zero dependencies** — pure Python stdlib server

## Setup

### 1. Generate TLS certificate (required for PWA on mobile)

```bash
openssl req -x509 -newkey rsa:2048 \
  -keyout /tmp/mh_key.pem -out /tmp/mh_cert.pem \
  -days 730 -nodes \
  -subj "/CN=YOUR_LOCAL_IP" \
  -addext "subjectAltName=IP:YOUR_LOCAL_IP,IP:127.0.0.1"
```

### 2. Add your music

Put MP3s in the `music/` directory with this structure:

```
music/
└── Artist Name/
    └── Album Name/
        ├── 01 - Track Title.mp3
        └── ...
```

### 3. Configure and run

Edit `server.py` and set your local IP:

```python
LOCAL_IP = "192.168.178.X"
```

Then start the server:

```bash
python3 server.py
```

Open `https://YOUR_LOCAL_IP:8765` on your phone. Accept the self-signed certificate warning once.

### Install as PWA (optional)

- **iOS Safari**: Share → "Zum Home-Bildschirm"
- **Android Chrome**: Menu → "App installieren"

## Requirements

- Python 3.8+
- `mutagen` for ID3 tag reading: `pip install mutagen`
