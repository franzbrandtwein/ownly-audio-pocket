import http.server, os, json, urllib.parse, zipfile, io, ssl, base64
from pathlib import Path

MUSIC_DIR = Path("/home/herrvorragend/projekte/musik_downloader/music")
PORT = 8765
LOCAL_IP = "192.168.178.24"

def get_tracks():
    tracks = []
    for mp3 in sorted(MUSIC_DIR.rglob("*.mp3")):
        idx = len(tracks)
        tracks.append({
            "band": mp3.parent.parent.name,
            "album": mp3.parent.name,
            "title": mp3.stem,
            "idx": idx,
            "abs": str(mp3)
        })
    return tracks

TRACKS = get_tracks()

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
const CACHE = 'mh-shell-v9';
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
#player-now { font-size: 0.85em; color: #aaa; margin-bottom: 6px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
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
#install-btn { display: none; position: fixed; bottom: 20px; right: 20px; background: #e63; color: #fff; border: none; border-radius: 50px; padding: 12px 20px; font-size: 0.95em; cursor: pointer; box-shadow: 0 4px 15px rgba(0,0,0,0.5); z-index: 100; }
</style>
</head>
<body>
<div id="status-bar"></div>
<h1>🤘 Music</h1>
<div id="player">
  <div id="player-now">Nichts läuft...</div>
  <audio id="audio" controls preload="none"></audio>
</div>
<div id="list"></div>
<button id="install-btn">📲 App installieren</button>

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
let deferredInstall = null;
window.addEventListener('beforeinstallprompt', e => {
  e.preventDefault();
  deferredInstall = e;
  document.getElementById('install-btn').style.display = 'block';
});
document.getElementById('install-btn').onclick = () => {
  if (deferredInstall) { deferredInstall.prompt(); deferredInstall = null; }
  document.getElementById('install-btn').style.display = 'none';
};

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

audio.addEventListener('ended', () => {
  if (activeEl === null) return;
  const curIdx = parseInt(activeEl.dataset.idx);
  if (curIdx + 1 < tracks.length) {
    const next = tracks[curIdx + 1];
    const nextEl = document.querySelector('.track[data-idx="' + (curIdx + 1) + '"]');
    if (nextEl) {
      play(nextEl, next, cleanTitle(next.title));
      nextEl.scrollIntoView({block:'nearest'});
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
    const dot = document.getElementById('dot-' + idx);
    if (dot) { dot.textContent = '●'; dot.classList.add('cached'); dot.title = 'Offline verfügbar'; }
    btn.classList.remove('loading'); btn.classList.add('cached'); btn.textContent = '✓ Offline';
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
  const dot = document.getElementById('dot-' + idx);
  if (dot) { dot.textContent = '○'; dot.classList.remove('cached'); dot.title = 'Nicht offline'; }
  btn.classList.remove('cached'); btn.textContent = '⬇ Offline';
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
openDB().then(async () => {
  const cachedKeys = await idbKeys();

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
              detail.innerHTML = '<div class="track-detail-inner" style="color:#e63;font-size:0.85em">Fehler beim Laden</div>';
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
                [{"band": t["band"], "album": t["album"], "title": t["title"], "idx": t["idx"]} for t in TRACKS]
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

httpd = http.server.HTTPServer(('0.0.0.0', PORT), Handler)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain('/tmp/mh_cert.pem', '/tmp/mh_key.pem')
httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
print(f"https://0.0.0.0:{PORT}")
httpd.serve_forever()
