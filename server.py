import http.server, os, json, urllib.parse, zipfile, io, ssl, base64, threading, socket, datetime
from pathlib import Path

PORT       = 8765
ADMIN_PORT = 8766
BASE_DIR   = Path(__file__).resolve().parent
CONF_FILE  = BASE_DIR / ".ownly-config.json"
CERT_DIR   = BASE_DIR / ".certs"

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

LOCAL_IP = get_local_ip()

def _load_conf():
    default = str(BASE_DIR / "music")
    if CONF_FILE.exists():
        try:
            return json.loads(CONF_FILE.read_text())
        except Exception:
            pass
    return {"music_dir": default}

def _save_conf():
    CONF_FILE.write_text(json.dumps({"music_dir": str(CONFIG["music_dir"])}))

_c = _load_conf()
CONFIG = {"music_dir": Path(_c.get("music_dir", BASE_DIR / "music"))}

def ensure_cert():
    """Generate a self-signed cert with SAN for LOCAL_IP if not present."""
    CERT_DIR.mkdir(exist_ok=True)
    cert_file = CERT_DIR / "cert.pem"
    key_file  = CERT_DIR / "key.pem"
    if cert_file.exists() and key_file.exists():
        return cert_file, key_file
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        import ipaddress
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, LOCAL_IP)])
        san  = x509.SubjectAlternativeName([
            x509.IPAddress(ipaddress.ip_address(LOCAL_IP)),
            x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        ])
        now = datetime.datetime.utcnow()
        cert = (
            x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(san, critical=False)
            .sign(key, hashes.SHA256())
        )
        key_file.write_bytes(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ))
        cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        print(f"Cert generated: {cert_file}")
    except Exception as e:
        print(f"Cert error: {e}")
    return cert_file, key_file

def get_tracks():
    try:
        from mutagen.id3 import ID3
    except ImportError:
        ID3 = None
    tracks = []
    music_dir = CONFIG["music_dir"]
    if not music_dir.is_dir():
        return tracks
    for mp3 in sorted(music_dir.rglob("*.mp3")):
        idx = len(tracks)
        genre = ''
        if ID3:
            try:
                tags = ID3(str(mp3))
                genre = str(tags.get('TCON', '')).strip()
            except Exception:
                pass
        tracks.append({
            "band": mp3.parent.parent.name,
            "album": mp3.parent.name,
            "title": mp3.stem,
            "idx": idx,
            "abs": str(mp3),
            "genre": genre,
        })
    return tracks

TRACKS = get_tracks()

def reload_tracks():
    global TRACKS
    TRACKS = get_tracks()
    _save_conf()

def get_meta(track):
    try:
        from mutagen.mp3 import MP3
        from mutagen.id3 import ID3, APIC
        audio = MP3(track['abs'], ID3=ID3)
        tags = audio.tags or {}
        dur = int(audio.info.length)
        cover = None
        for tag in tags.values():
            if isinstance(tag, APIC):
                cover = f"data:{tag.mime};base64,{base64.b64encode(tag.data).decode()}"
                break
        return {
            'title':    str(tags.get('TIT2', track['title'])),
            'artist':   str(tags.get('TPE1', track['band'])),
            'album':    str(tags.get('TALB', track['album'])),
            'year':     str(tags.get('TDRC', '')),
            'genre':    str(tags.get('TCON', '')),
            'track':    str(tags.get('TRCK', '')),
            'duration': f"{dur//60}:{dur%60:02d}",
            'cover':    cover,
        }
    except Exception:
        return {'title': track['title'], 'artist': track['band'], 'album': track['album'],
                'year': '', 'genre': '', 'track': '', 'duration': '', 'cover': None}

MANIFEST = json.dumps({
    "name": "Machine Head",
    "short_name": "Machine Head",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#111111",
    "theme_color": "#ee6633",
    "icons": [
        {"src": "/icon-192.svg", "sizes": "192x192", "type": "image/svg+xml", "purpose": "any maskable"},
        {"src": "/icon-512.svg", "sizes": "512x512", "type": "image/svg+xml", "purpose": "any maskable"}
    ]
})

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <rect width="100" height="100" fill="#111"/>
  <rect x="5" y="5" width="90" height="90" rx="12" fill="#ee6633"/>
  <text x="50" y="68" font-size="55" text-anchor="middle" fill="#111" font-family="serif" font-weight="bold">MH</text>
</svg>"""

SERVICE_WORKER = r"""
const CACHE = 'mh-shell-v13';
const SHELL = ['/', '/manifest.json', '/icon-192.svg', '/icon-512.svg'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys =>
    Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
  ));
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  // Audio: network-first, SW doesn't interfere (IndexedDB handles caching in main JS)
  if (url.pathname.startsWith('/track/')) return;
  // App shell: cache-first
  e.respondWith(
    caches.match(e.request).then(cached => {
      const net = fetch(e.request).then(res => {
        if (res.ok) caches.open(CACHE).then(c => c.put(e.request, res.clone()));
        return res;
      }).catch(() => cached);
      return cached || net;
    })
  );
});
"""

HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<meta name="theme-color" content="#ee6633">
<link rel="manifest" href="/manifest.json">
<title>🤘 Music</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #111; color: #eee; font-family: sans-serif; }
h1 { text-align: center; padding: 16px; font-size: 1.4em; color: #e63; }
#player { position: sticky; top: 0; background: #1a1a1a; z-index: 10; border-bottom: 2px solid #e63; padding: 10px 14px; }
#player-top { display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px; }
#player-now { font-size: 0.85em; color: #aaa; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: 1; }
#shuffle-btn { background: none; border: 1px solid #444; border-radius: 20px; color: #666; font-size: 0.8em; padding: 3px 10px; cursor: pointer; flex-shrink: 0; margin-left: 10px; touch-action: manipulation; transition: all 0.2s; }
#shuffle-btn.on { color: #e63; border-color: #e63; }
audio { width: 100%; }
button.band-header { width: 100%; text-align: left; background: #1e1e1e; padding: 14px 14px; font-weight: bold; color: #e63; font-size: 1.05em; cursor: pointer; display: flex; align-items: center; justify-content: space-between; border: none; border-bottom: 2px solid #333; user-select: none; touch-action: manipulation; -webkit-tap-highlight-color: transparent; }
button.band-header:active { background: #252525; }
.band-body { overflow: hidden; max-height: 0; pointer-events: none; }
.band-body.open { max-height: 100000px; pointer-events: auto; }
button.album-header { width: 100%; text-align: left; background: #222; padding: 10px 14px 10px 20px; font-weight: bold; color: #aaa; font-size: 0.9em; border: none; border-left: 4px solid #555; cursor: pointer; display: flex; align-items: center; justify-content: space-between; gap: 8px; user-select: none; touch-action: manipulation; -webkit-tap-highlight-color: transparent; }
.album-row { display: flex; align-items: stretch; background: #222; border-left: 4px solid #555; }
button.album-toggle { flex: 1; text-align: left; background: none; border: none; padding: 10px 8px 10px 16px; font-weight: bold; color: #aaa; font-size: 0.9em; cursor: pointer; display: flex; align-items: center; gap: 8px; user-select: none; touch-action: manipulation; -webkit-tap-highlight-color: transparent; }
button.album-toggle:active { background: rgba(255,255,255,0.05); }
.album-title-text { flex: 1; }
.album-controls { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
.album-arrow { color: #666; font-size: 0.85em; transition: transform 0.25s; }
.album-arrow.open { transform: rotate(180deg); }
.album-body { overflow: hidden; max-height: 0; pointer-events: none; }
.album-body.open { max-height: 5000px; pointer-events: auto; }
.track { padding: 13px 14px 13px 20px; border-bottom: 1px solid #2a2a2a; cursor: pointer; display: flex; align-items: center; gap: 10px; touch-action: manipulation; -webkit-tap-highlight-color: transparent; user-select: none; }
.track.active { background: #2a1a1a; }
.track.expanded { background: #1f1f1f; }
.track-detail { overflow: hidden; max-height: 0; pointer-events: none; background: #161616; transition: max-height 0.3s ease; }
.track-detail.open { max-height: 420px; pointer-events: auto; }
.track-detail-inner { padding: 12px 14px 16px 24px; display: flex; gap: 14px; align-items: flex-start; }
.track-cover { width: 80px; height: 80px; object-fit: cover; border-radius: 4px; flex-shrink: 0; }
.track-meta { flex: 1; font-size: 0.82em; color: #999; display: flex; flex-direction: column; gap: 5px; }
.track-meta strong { color: #ddd; }
.track-play-btn { display: block; width: 100%; margin-top: 10px; background: #e63; color: #fff; border: none; border-radius: 6px; padding: 10px; font-size: 1em; font-weight: bold; cursor: pointer; touch-action: manipulation; }
.track-num { color: #555; font-size: 0.85em; min-width: 26px; }
.track-title { font-size: 0.95em; line-height: 1.3; flex: 1; }
.track-cached-dot { font-size: 0.6em; color: #444; flex-shrink: 0; }
.track-cached-dot.cached { color: #4c4; }
.dl-btn { font-size: 0.85em; padding: 10px 12px; flex-shrink: 0; cursor: pointer; background: none; border: 1px solid #555; border-radius: 6px; color: #aaa; transition: color 0.2s, border-color 0.2s; white-space: nowrap; touch-action: manipulation; }
.dl-btn.cached { color: #4c4; border-color: #4c4; }
.dl-btn.loading { color: #fa0; border-color: #fa0; animation: spin 1s linear infinite; display: inline-block; }
@keyframes spin { from{transform:rotate(0deg)} to{transform:rotate(360deg)} }
.album-dl-btn { font-size: 0.8em; cursor: pointer; background: none; border: 1px solid #555; color: #aaa; border-radius: 4px; padding: 3px 8px; transition: all 0.2s; white-space: nowrap; }
.album-dl-btn.cached { color: #4c4; border-color: #4c4; }
.album-dl-btn.loading { color: #fa0; border-color: #fa0; }
#status-bar { display: none; text-align: center; padding: 6px; font-size: 0.85em; font-weight: bold; }
#status-bar.offline { display: block; background: #4c4; color: #111; }
#status-bar.nocache { display: block; background: #e63; color: #fff; }
#install-btn { display: none; position: fixed; bottom: 20px; right: 20px; background: #e63; color: #fff; border: none; border-radius: 50px; padding: 12px 20px; font-size: 0.95em; cursor: pointer; box-shadow: 0 4px 15px rgba(0,0,0,0.5); z-index: 100; touch-action: manipulation; }
#ios-hint { display: none; position: fixed; bottom: 0; left: 0; right: 0; background: #1a1a1a; border-top: 2px solid #e63; padding: 14px 16px 24px; z-index: 100; font-size: 0.9em; color: #eee; }
#ios-hint-close { float: right; background: none; border: none; color: #aaa; font-size: 1.3em; cursor: pointer; margin-left: 8px; }
#filter-bar { background: #161616; border-bottom: 1px solid #2a2a2a; }
#filter-toggle { width: 100%; background: none; border: none; color: #777; padding: 9px 14px; text-align: left; cursor: pointer; display: flex; align-items: center; gap: 8px; font-size: 0.88em; touch-action: manipulation; -webkit-tap-highlight-color: transparent; }
#filter-toggle.active { color: #e63; }
#filter-badge { background: #e63; color: #fff; border-radius: 10px; padding: 1px 7px; font-size: 0.8em; display: none; }
#filter-panel { overflow: hidden; max-height: 0; pointer-events: none; transition: max-height 0.35s ease; }
#filter-panel.open { max-height: 600px; pointer-events: auto; }
#filter-panel-inner { padding: 12px 14px 18px; }
.filter-section-label { font-size: 0.75em; color: #555; text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 8px; margin-top: 14px; }
.filter-section-label:first-child { margin-top: 0; }
.filter-row { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.filter-row span { font-size: 0.9em; color: #bbb; }
.f-switch { position: relative; display: inline-block; width: 40px; height: 22px; flex-shrink: 0; }
.f-switch input { opacity: 0; width: 0; height: 0; }
.f-slider { position: absolute; cursor: pointer; inset: 0; background: #333; border-radius: 22px; transition: 0.2s; }
.f-slider:before { position: absolute; content: ""; height: 16px; width: 16px; left: 3px; bottom: 3px; background: #777; border-radius: 50%; transition: 0.2s; }
.f-switch input:checked + .f-slider { background: #e63; }
.f-switch input:checked + .f-slider:before { transform: translateX(18px); background: #fff; }
.genre-chips { display: flex; flex-wrap: wrap; gap: 6px; }
.genre-chip { background: none; border: 1px solid #3a3a3a; color: #777; border-radius: 20px; padding: 4px 11px; font-size: 0.8em; cursor: pointer; touch-action: manipulation; -webkit-tap-highlight-color: transparent; transition: all 0.15s; }
.genre-chip.on { background: #e63; border-color: #e63; color: #fff; }
#filter-reset { margin-top: 14px; background: none; border: 1px solid #333; color: #555; border-radius: 4px; padding: 6px 14px; font-size: 0.82em; cursor: pointer; touch-action: manipulation; display: none; }
#filter-reset.visible { display: inline-block; }
.track-wrapper.filtered { display: none; }
.album-section.filtered { display: none; }
.band-section.filtered { display: none; }
</style>
</head>
<body>
<div id="status-bar"></div>
<h1>🤘 Music</h1>
<div id="filter-bar">
  <button id="filter-toggle" onclick="toggleFilterPanel()">⚙ Filter <span id="filter-badge"></span></button>
  <div id="filter-panel">
    <div id="filter-panel-inner">
      <div class="filter-section-label">Verfügbarkeit</div>
      <div class="filter-row">
        <span>Nur offline verfügbar</span>
        <label class="f-switch"><input type="checkbox" id="offline-filter" onchange="applyFilters()"><span class="f-slider"></span></label>
      </div>
      <div class="filter-section-label">Genre</div>
      <div id="genre-chips" class="genre-chips"></div>
      <button id="filter-reset" onclick="resetFilters()">✕ Filter zurücksetzen</button>
    </div>
  </div>
</div>
<div id="player">
  <div id="player-top">
    <div id="player-now">Nichts läuft...</div>
    <button id="shuffle-btn" onclick="toggleShuffle()">⇌ Shuffle</button>
  </div>
  <audio id="audio" controls preload="none"></audio>
</div>
<div id="list"></div>
<button id="install-btn">📲 App installieren</button>
<div id="ios-hint">
  <button id="ios-hint-close">✕</button>
  <strong>📲 Als App installieren</strong><br>
  Tippe auf <strong>Teilen</strong> <span style="font-size:1.1em">⎋</span> und dann auf <strong>„Zum Home-Bildschirm"</strong>
</div>

<script>
const tracks = TRACKLIST_JSON;
const audio = document.getElementById('audio');
const nowPlaying = document.getElementById('player-now');
const statusBar = document.getElementById('status-bar');
const list = document.getElementById('list');
let activeEl = null;

// --- Service Worker ---
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(console.warn);
}

// --- Install prompt ---
const isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent) && !window.navigator.standalone;
const isAndroid = /android/i.test(navigator.userAgent);
let deferredInstall = null;

if (isIOS) {
  // iOS: show persistent install button that reveals instructions
  const btn = document.getElementById('install-btn');
  const hint = document.getElementById('ios-hint');
  btn.style.display = 'block';
  btn.onclick = () => { hint.style.display = 'block'; btn.style.display = 'none'; };
  document.getElementById('ios-hint-close').onclick = () => { hint.style.display = 'none'; };
} else {
  window.addEventListener('beforeinstallprompt', e => {
    e.preventDefault();
    deferredInstall = e;
    document.getElementById('install-btn').style.display = 'block';
  });
  document.getElementById('install-btn').onclick = () => {
    if (deferredInstall) { deferredInstall.prompt(); deferredInstall = null; }
    document.getElementById('install-btn').style.display = 'none';
  };
}

// --- Tap helper: touchend fires immediately, flag prevents double-fire from synthetic click ---
function onTap(el, handler) {
  let startY = 0;
  let didTouch = false;
  el.addEventListener('touchstart', e => { startY = e.touches[0].clientY; }, {passive: true});
  el.addEventListener('touchend', e => {
    if (Math.abs(e.changedTouches[0].clientY - startY) < 10) {
      e.preventDefault();
      didTouch = true;
      handler();
      setTimeout(() => { didTouch = false; }, 600);
    }
  }, {passive: false});
  el.addEventListener('click', () => { if (!didTouch) handler(); });
}

// --- Offline detection ---
function updateStatus() {
  if (!navigator.onLine) {
    statusBar.textContent = '📵 Offline — nur gecachte Songs spielbar';
    statusBar.className = 'offline';
  } else {
    statusBar.className = '';
    statusBar.style.display = 'none';
  }
}
window.addEventListener('online', updateStatus);
window.addEventListener('offline', updateStatus);
updateStatus();

// --- Player toggle ---
function togglePlayer() {}

// --- IndexedDB ---
let db;
const DB_NAME = 'MachineHead', STORE = 'audio';
function openDB() {
  return new Promise((res, rej) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = e => e.target.result.createObjectStore(STORE);
    req.onsuccess = e => { db = e.target.result; res(db); };
    req.onerror = rej;
  });
}
function idbGet(idx) {
  return new Promise((res, rej) => {
    const r = db.transaction(STORE,'readonly').objectStore(STORE).get(idx);
    r.onsuccess = () => res(r.result); r.onerror = rej;
  });
}
function idbPut(idx, blob) {
  return new Promise((res, rej) => {
    const r = db.transaction(STORE,'readwrite').objectStore(STORE).put(blob, idx);
    r.onsuccess = res; r.onerror = rej;
  });
}
function idbDelete(idx) {
  return new Promise((res, rej) => {
    const r = db.transaction(STORE,'readwrite').objectStore(STORE).delete(idx);
    r.onsuccess = res; r.onerror = rej;
  });
}
function idbKeys() {
  return new Promise((res, rej) => {
    const r = db.transaction(STORE,'readonly').objectStore(STORE).getAllKeys();
    r.onsuccess = () => res(new Set(r.result)); r.onerror = rej;
  });
}

function cleanTitle(raw) {
  return raw.replace(/^\d+ - \d+ - /, '').replace(/^\d+ - /, '');
}

// --- Blob URL cache: preloaded at startup so play() stays synchronous ---
const blobURLs = new Map();

// --- Play (synchronous — iOS requires audio.play() within user gesture) ---
function play(d, t, titleClean) {
  if (activeEl) activeEl.classList.remove('active');
  d.classList.add('active'); activeEl = d;
  nowPlaying.textContent = '▶ ' + titleClean + ' — ' + t.album;

  const blobURL = blobURLs.get(t.idx);
  if (blobURL) {
    audio.src = blobURL;
  } else if (!navigator.onLine) {
    nowPlaying.textContent = '❌ Nicht offline verfügbar: ' + titleClean;
    statusBar.textContent = '⚠️ Dieser Song wurde nicht offline gespeichert';
    statusBar.className = 'nocache'; statusBar.style.display = 'block';
    setTimeout(() => updateStatus(), 3000);
    return;
  } else {
    audio.src = '/track/' + t.idx;
  }
  audio.play().catch(() => {});
}

// --- Shuffle ---
let shuffle = false;
const shuffleHistory = [];
function toggleShuffle() {
  shuffle = !shuffle;
  document.getElementById('shuffle-btn').classList.toggle('on', shuffle);
}

audio.addEventListener('ended', () => {
  if (activeEl === null) return;
  if (shuffle) {
    const curIdx = parseInt(activeEl.dataset.idx);
    let nextIdx;
    do { nextIdx = Math.floor(Math.random() * tracks.length); } while (nextIdx === curIdx && tracks.length > 1);
    const next = tracks[nextIdx];
    const nextEl = document.querySelector('.track[data-idx="' + nextIdx + '"]');
    if (nextEl) { play(nextEl, next, cleanTitle(next.title)); nextEl.scrollIntoView({block:'nearest'}); }
  } else {
    const curIdx = parseInt(activeEl.dataset.idx);
    if (curIdx + 1 < tracks.length) {
      const next = tracks[curIdx + 1];
      const nextEl = document.querySelector('.track[data-idx="' + (curIdx + 1) + '"]');
      if (nextEl) { play(nextEl, next, cleanTitle(next.title)); nextEl.scrollIntoView({block:'nearest'}); }
    }
  }
});

// --- Cache track ---
async function cacheTrack(idx, btn) {
  if (btn.classList.contains('loading') || btn.classList.contains('cached')) return true;
  btn.classList.add('loading'); btn.textContent = '↻';
  try {
    const res = await fetch('/track/' + idx);
    const blob = await res.blob();
    await idbPut(idx, blob);
    blobURLs.set(idx, URL.createObjectURL(blob));
    cachedKeys.add(idx);
    const dot = document.getElementById('dot-' + idx);
    if (dot) { dot.textContent = '●'; dot.classList.add('cached'); dot.title = 'Offline verfügbar'; }
    btn.classList.remove('loading'); btn.classList.add('cached'); btn.textContent = '✓ Offline';
    applyFilters();
    return true;
  } catch {
    btn.classList.remove('loading'); btn.textContent = '⬇ Offline';
    return false;
  }
}

// --- Uncache track ---
async function uncacheTrack(idx, btn) {
  await idbDelete(idx);
  if (blobURLs.has(idx)) { URL.revokeObjectURL(blobURLs.get(idx)); blobURLs.delete(idx); }
  cachedKeys.delete(idx);
  const dot = document.getElementById('dot-' + idx);
  if (dot) { dot.textContent = '○'; dot.classList.remove('cached'); dot.title = 'Nicht offline'; }
  btn.classList.remove('cached'); btn.textContent = '⬇ Offline';
  applyFilters();
}

// --- Cache album ---
async function cacheAlbum(albumTracks, albumBtn) {
  if (albumBtn.classList.contains('loading')) return;
  // If all cached → delete all
  if (albumBtn.classList.contains('cached')) {
    for (const t of albumTracks) {
      const trackBtn = document.getElementById('dl-' + t.idx);
      if (trackBtn) await uncacheTrack(t.idx, trackBtn);
    }
    albumBtn.classList.remove('cached'); albumBtn.textContent = '⬇ Offline';
    return;
  }
  albumBtn.classList.add('loading');
  for (let i = 0; i < albumTracks.length; i++) {
    const t = albumTracks[i];
    albumBtn.textContent = '↻ ' + (i+1) + '/' + albumTracks.length;
    const trackBtn = document.getElementById('dl-' + t.idx);
    if (trackBtn && !trackBtn.classList.contains('cached')) await cacheTrack(t.idx, trackBtn);
  }
  albumBtn.classList.remove('loading'); albumBtn.classList.add('cached'); albumBtn.textContent = '✓ Offline';
}

// --- Build list ---
let cachedKeys = new Set();

function toggleFilterPanel() {
  document.getElementById('filter-panel').classList.toggle('open');
}

function applyFilters() {
  const offlineOnly = document.getElementById('offline-filter').checked;
  const activeGenres = new Set([...document.querySelectorAll('.genre-chip.on')].map(c => c.dataset.genre));
  const activeCount = (offlineOnly ? 1 : 0) + activeGenres.size;

  document.querySelectorAll('.track-wrapper').forEach(wrapper => {
    const idx = parseInt(wrapper.dataset.idx);
    const genre = wrapper.dataset.genre || '';
    const offlineOk = !offlineOnly || cachedKeys.has(idx);
    const genreOk = activeGenres.size === 0 || activeGenres.has(genre);
    wrapper.classList.toggle('filtered', !(offlineOk && genreOk));
  });
  document.querySelectorAll('.album-section').forEach(sec => {
    sec.classList.toggle('filtered', !sec.querySelector('.track-wrapper:not(.filtered)'));
  });
  document.querySelectorAll('.band-section').forEach(sec => {
    sec.classList.toggle('filtered', !sec.querySelector('.track-wrapper:not(.filtered)'));
  });

  const badge = document.getElementById('filter-badge');
  badge.style.display = activeCount > 0 ? 'inline' : 'none';
  badge.textContent = activeCount;
  document.getElementById('filter-toggle').classList.toggle('active', activeCount > 0);
  document.getElementById('filter-reset').classList.toggle('visible', activeCount > 0);
}

function resetFilters() {
  document.getElementById('offline-filter').checked = false;
  document.querySelectorAll('.genre-chip.on').forEach(c => c.classList.remove('on'));
  applyFilters();
}

openDB().then(async () => {
  cachedKeys = await idbKeys();

  // Preload blob URLs for cached tracks (makes play() synchronous = iOS-safe)
  cachedKeys.forEach(idx => {
    idbGet(idx).then(blob => { if (blob) blobURLs.set(idx, URL.createObjectURL(blob)); });
  });

  // Group: band → album → tracks
  const bandMap = {};
  tracks.forEach(t => {
    if (!bandMap[t.band]) bandMap[t.band] = {};
    if (!bandMap[t.band][t.album]) bandMap[t.band][t.album] = [];
    bandMap[t.band][t.album].push(t);
  });

  Object.entries(bandMap).forEach(([band, albums]) => {
    // Band section
    const bandSection = document.createElement('div');
    bandSection.className = 'band-section';

    const bandHeader = document.createElement('button');
    bandHeader.type = 'button';
    bandHeader.className = 'band-header';
    const bandArrow = document.createElement('span');
    bandArrow.className = 'band-arrow';
    bandArrow.textContent = '▶';
    bandHeader.appendChild(document.createTextNode(band));
    bandHeader.appendChild(bandArrow);

    const bandBody = document.createElement('div');
    bandBody.className = 'band-body';

    onTap(bandHeader, () => {
      const open = bandBody.classList.contains('open');
      bandBody.classList.toggle('open', !open);
      bandArrow.textContent = open ? '▶' : '▼';
    });

    Object.entries(albums).forEach(([album, albumTracks]) => {
      const allCached = albumTracks.every(t => cachedKeys.has(t.idx));

      const albumSection = document.createElement('div');
      albumSection.className = 'album-section';

      const albumRow = document.createElement('div');
      albumRow.className = 'album-row';

      const albumToggle = document.createElement('button');
      albumToggle.type = 'button';
      albumToggle.className = 'album-toggle';

      const albumArrow = document.createElement('span');
      albumArrow.className = 'album-arrow';
      albumArrow.textContent = '▶';
      albumToggle.appendChild(document.createTextNode(album));
      albumToggle.appendChild(albumArrow);

      const albumBtn = document.createElement('button');
      albumBtn.type = 'button';
      albumBtn.className = 'album-dl-btn' + (allCached ? ' cached' : '');
      albumBtn.textContent = allCached ? '✓ Offline' : '⬇ Offline';

      albumRow.appendChild(albumToggle);
      albumRow.appendChild(albumBtn);

      const albumBody = document.createElement('div');
      albumBody.className = 'album-body';

      onTap(albumToggle, () => {
        const open = albumBody.classList.contains('open');
        albumBody.classList.toggle('open', !open);
        albumArrow.textContent = open ? '▶' : '▼';
      });

      albumBtn.addEventListener('click', () => cacheAlbum(albumTracks, albumBtn));

      albumTracks.forEach((t, i) => {
        const wrapper = document.createElement('div');
        wrapper.className = 'track-wrapper';
        wrapper.dataset.idx = t.idx;
        wrapper.dataset.genre = t.genre || '';
        const d = document.createElement('div');
        d.className = 'track'; d.dataset.idx = t.idx;
        const num = t.title.match(/^(\d+)/);
        const titleClean = cleanTitle(t.title);
        const isCached = cachedKeys.has(t.idx);
        d.innerHTML = `<span class="track-num">${num ? num[1] : i+1}</span><span class="track-title">${titleClean}</span><span class="track-cached-dot${isCached ? ' cached' : ''}" id="dot-${t.idx}" title="${isCached ? 'Offline verfügbar' : 'Nicht offline'}">${isCached ? '●' : '○'}</span>`;

        // Detail panel (lazy-loaded metadata + offline button)
        const detail = document.createElement('div');
        detail.className = 'track-detail';
        let metaLoaded = false;

        // Create dlBtn early so cacheAlbum can find it by id
        const dlBtn = document.createElement('button');
        dlBtn.type = 'button';
        dlBtn.className = 'dl-btn' + (isCached ? ' cached' : '');
        dlBtn.id = 'dl-' + t.idx; dlBtn.textContent = isCached ? '✓ Offline' : '⬇ Offline';

        onTap(d, () => {
          const isOpen = detail.classList.contains('open');
          detail.classList.toggle('open', !isOpen);
          d.classList.toggle('expanded', !isOpen);
          if (!isOpen && !metaLoaded) {
            metaLoaded = true;
            detail.innerHTML = '<div class="track-detail-inner"><span style="color:#666;font-size:0.85em">⏳ Lade Infos…</span></div>';
            fetch('/meta/' + t.idx).then(r => r.json()).then(m => {
              const coverHtml = m.cover ? `<img class="track-cover" src="${m.cover}" alt="Cover">` : '';
              const rows = [
                ['Titel',    m.title],
                ['Künstler', m.artist],
                ['Album',    m.album],
                m.year     ? ['Jahr',   m.year]     : null,
                m.genre    ? ['Genre',  m.genre]    : null,
                m.track    ? ['Track',  m.track]    : null,
                m.duration ? ['Länge',  m.duration] : null,
              ].filter(Boolean).map(([k,v]) => `<div><strong>${k}:</strong> ${v}</div>`).join('');
              detail.innerHTML = `
                <div class="track-detail-inner">
                  ${coverHtml}
                  <div class="track-meta">
                    ${rows}
                    <div style="display:flex;gap:8px;margin-top:10px">
                      <button class="track-play-btn" type="button" style="flex:1">▶ Abspielen</button>
                    </div>
                  </div>
                </div>`;
              detail.querySelector('.track-play-btn').addEventListener('click', () => play(d, t, titleClean));
              // Move dlBtn from hidden slot into the buttons row
              dlBtn.style.display = '';
              detail.querySelector('div[style*="flex"]').appendChild(dlBtn);
            }).catch(() => {
              // Offline fallback: use data already available in JS
              const rows = [
                ['Titel',   t.title],
                ['Künstler', t.band],
                ['Album',   t.album],
                t.genre ? ['Genre', t.genre] : null,
              ].filter(Boolean).map(([k,v]) => `<div><strong>${k}:</strong> ${v}</div>`).join('');
              detail.innerHTML = `
                <div class="track-detail-inner">
                  <div class="track-meta">
                    ${rows}
                    <div style="display:flex;gap:8px;margin-top:10px">
                      <button class="track-play-btn" type="button" style="flex:1">▶ Abspielen</button>
                    </div>
                  </div>
                </div>`;
              detail.querySelector('.track-play-btn').addEventListener('click', () => play(d, t, titleClean));
              dlBtn.style.display = '';
              detail.querySelector('div[style*="flex"]').appendChild(dlBtn);
            });
          }
        });

        dlBtn.addEventListener('click', e => {
          e.stopPropagation();
          if (dlBtn.classList.contains('cached')) {
            uncacheTrack(t.idx, dlBtn).then(() => {
              albumBtn.classList.remove('cached');
              albumBtn.textContent = '⬇ Offline';
            });
          } else {
            cacheTrack(t.idx, dlBtn).then(() => {
              if (albumTracks.every(x => document.getElementById('dl-'+x.idx)?.classList.contains('cached'))) {
                albumBtn.classList.add('cached');
                albumBtn.textContent = '✓ Offline';
              }
            });
          }
        });

        wrapper.appendChild(d);
        wrapper.appendChild(detail);
        // dlBtn always in DOM so getElementById works for cacheAlbum; hidden until detail opens
        dlBtn.style.display = 'none';
        wrapper.appendChild(dlBtn);
        albumBody.appendChild(wrapper);
      });

      albumSection.appendChild(albumRow);
      albumSection.appendChild(albumBody);
      bandBody.appendChild(albumSection);
    });

    bandSection.appendChild(bandHeader);
    bandSection.appendChild(bandBody);
    list.appendChild(bandSection);
  });

  // Populate genre chips
  const allGenres = [...new Set(tracks.map(t => t.genre).filter(Boolean))].sort();
  const chipsEl = document.getElementById('genre-chips');
  allGenres.forEach(g => {
    const chip = document.createElement('button');
    chip.type = 'button'; chip.className = 'genre-chip';
    chip.dataset.genre = g; chip.textContent = g;
    chip.addEventListener('click', () => { chip.classList.toggle('on'); applyFilters(); });
    chipsEl.appendChild(chip);
  });
});
</script>
</body>
</html>"""

SW_JS = SERVICE_WORKER

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send_cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET')

    def do_GET(self):
        path = urllib.parse.unquote(self.path.split('?')[0])

        if path == '/':
            body = HTML.replace('TRACKLIST_JSON', json.dumps(
                [{"band": t["band"], "album": t["album"], "title": t["title"], "idx": t["idx"], "genre": t["genre"]} for t in TRACKS]
            )).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_cors()
            self.end_headers(); self.wfile.write(body)

        elif path == '/manifest.json':
            body = MANIFEST.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/manifest+json')
            self.send_cors()
            self.end_headers(); self.wfile.write(body)

        elif path == '/sw.js':
            body = SW_JS.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/javascript')
            self.send_header('Service-Worker-Allowed', '/')
            self.send_cors()
            self.end_headers(); self.wfile.write(body)

        elif path in ('/icon-192.svg', '/icon-512.svg'):
            body = ICON_SVG.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'image/svg+xml')
            self.end_headers(); self.wfile.write(body)

        elif path.startswith('/meta/'):
            try:
                idx = int(path[6:])
                meta = get_meta(TRACKS[idx])
                body = json.dumps(meta).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_cors()
                self.end_headers(); self.wfile.write(body)
            except: self.send_response(404); self.end_headers()

        elif path.startswith('/track/'):
            try:
                idx = int(path[7:])
                fp = TRACKS[idx]["abs"]
                size = os.path.getsize(fp)
                self.send_response(200)
                self.send_header('Content-Type', 'audio/mpeg')
                self.send_header('Content-Length', str(size))
                self.send_header('Accept-Ranges', 'bytes')
                self.send_cors()
                self.end_headers()
                with open(fp, 'rb') as f: self.wfile.write(f.read())
            except: self.send_response(404); self.end_headers()

        elif path.startswith('/album-zip/'):
            try:
                album_name = urllib.parse.unquote(path[11:])
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, 'w', zipfile.ZIP_STORED) as zf:
                    for t in [t for t in TRACKS if t["album"] == album_name]:
                        zf.write(t["abs"], Path(t["abs"]).name)
                data = buf.getvalue()
                safe = album_name.replace('/', '-').replace(' ', '_') + '.zip'
                self.send_response(200)
                self.send_header('Content-Type', 'application/zip')
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Content-Disposition', f'attachment; filename="{safe}"')
                self.end_headers(); self.wfile.write(data)
            except: self.send_response(500); self.end_headers()

        else:
            self.send_response(404); self.end_headers()

ADMIN_HTML = """<!DOCTYPE html>
<html lang="de">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ownly Audio Pocket – Admin</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:system-ui,sans-serif;background:#111;color:#ddd;padding:32px 24px;max-width:700px}
  h1{font-size:1.3em;color:#ee6633;margin-bottom:24px}
  label{display:block;font-size:.85em;color:#aaa;margin-bottom:6px}
  input[type=text]{width:100%;padding:10px 12px;background:#1e1e1e;border:1px solid #333;color:#eee;border-radius:6px;font-size:1em;margin-bottom:12px}
  input[type=text]:focus{outline:none;border-color:#ee6633}
  button{padding:10px 22px;background:#ee6633;color:#fff;border:none;border-radius:6px;font-size:.95em;cursor:pointer}
  button:hover{background:#ff7744}
  .msg{margin-top:18px;padding:12px 16px;border-radius:6px;font-size:.9em}
  .ok{background:#1a3a1a;color:#4c4}
  .err{background:#3a1a1a;color:#e64}
  .stat{margin-top:22px;background:#1a1a1a;border-radius:8px;padding:16px}
  .stat h2{font-size:.95em;color:#aaa;margin-bottom:10px}
  .stat-row{display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #222;font-size:.88em}
  .stat-row:last-child{border:none}
  .browse{margin-bottom:4px}
  .browse a{color:#ee6633;text-decoration:none;font-size:.85em;margin-right:8px}
  .browse a:hover{text-decoration:underline}
  .dir-list{background:#1a1a1a;border-radius:6px;padding:10px 14px;margin-bottom:12px;max-height:220px;overflow-y:auto}
  .dir-list a{display:block;padding:5px 4px;color:#cc9;text-decoration:none;font-size:.88em;border-bottom:1px solid #222}
  .dir-list a:last-child{border:none}
  .dir-list a:hover{color:#ee6633}
</style></head>
<body>
<h1>⚙ Ownly Audio Pocket – Admin</h1>

<form method="POST" action="/set-dir">
  <label for="d">Musikverzeichnis</label>
  <div class="browse">
    <a href="/browse?p=__PARENT__">↑ Übergeordnetes Verzeichnis</a>
    <span style="color:#555;font-size:.8em">|</span>
    <a href="/browse?p=__CWD__">Verzeichnis verwenden</a>
  </div>
  <div class="dir-list">__SUBDIRS__</div>
  <input type="text" id="d" name="dir" value="__DIR__">
  <button type="submit">Übernehmen &amp; neu laden</button>
</form>

__MSG__

<div class="stat">
  <h2>Aktuelle Bibliothek</h2>
  __STATS__
</div>

</body></html>"""

class AdminHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _render(self, msg=''):
        cur = str(CONFIG["music_dir"])
        parent = str(Path(cur).parent)
        # subdirectories of current dir
        try:
            subdirs = sorted([str(p) for p in Path(cur).iterdir() if p.is_dir()])
        except Exception:
            subdirs = []
        subdir_links = ''.join(
            f'<a href="/browse?p={urllib.parse.quote(d)}">{Path(d).name}/</a>'
            for d in subdirs
        ) or '<span style="color:#555;font-size:.85em">Keine Unterverzeichnisse</span>'

        # stats
        bands = {}
        for t in TRACKS:
            bands.setdefault(t['band'], set()).add(t['album'])
        stat_rows = ''.join(
            f'<div class="stat-row"><span>{b}</span><span>{len(albums)} Alben, {sum(1 for t in TRACKS if t["band"]==b)} Tracks</span></div>'
            for b, albums in sorted(bands.items())
        )
        if not stat_rows:
            stat_rows = '<div style="color:#666;font-size:.85em">Keine Tracks gefunden</div>'

        html = ADMIN_HTML \
            .replace('__DIR__', cur) \
            .replace('__PARENT__', parent) \
            .replace('__CWD__', cur) \
            .replace('__SUBDIRS__', subdir_links) \
            .replace('__MSG__', msg) \
            .replace('__STATS__', stat_rows)
        body = html.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == '/':
            self._render()
        elif path == '/browse':
            qs = urllib.parse.parse_qs(parsed.query)
            p = qs.get('p', [str(CONFIG["music_dir"])])[0]
            # clicking a dir link fills the input — just redirect with it pre-filled
            CONFIG["music_dir"] = Path(p)
            self._render('<div class="msg ok">Verzeichnis gewechselt zu: <strong>' + p + '</strong> – noch nicht gespeichert. Klicke „Übernehmen" um Tracks zu laden.</div>')
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        if self.path == '/set-dir':
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length).decode()
            params = urllib.parse.parse_qs(body)
            new_dir = params.get('dir', [''])[0].strip()
            p = Path(new_dir)
            if not p.is_dir():
                self._render(f'<div class="msg err">Verzeichnis nicht gefunden: <strong>{new_dir}</strong></div>')
                return
            CONFIG["music_dir"] = p
            reload_tracks()
            self._render(f'<div class="msg ok">✓ Verzeichnis gesetzt: <strong>{new_dir}</strong> – {len(TRACKS)} Tracks geladen.</div>')
        else:
            self.send_response(404); self.end_headers()

admin_httpd = http.server.HTTPServer(('0.0.0.0', ADMIN_PORT), AdminHandler)
t = threading.Thread(target=admin_httpd.serve_forever, daemon=True)
t.start()
print(f"Admin: http://0.0.0.0:{ADMIN_PORT}")

cert_file, key_file = ensure_cert()
httpd = http.server.HTTPServer(('0.0.0.0', PORT), Handler)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain(str(cert_file), str(key_file))
httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
print(f"https://0.0.0.0:{PORT}")
httpd.serve_forever()
