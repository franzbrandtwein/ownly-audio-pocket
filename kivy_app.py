#!/usr/bin/env python3
"""
Ownly Audio Pocket – Kivy Client
Verbindet sich per SOAP (Port 8767) mit dem Server und spielt Musik ab.

APK-Build: buildozer android debug  (siehe buildozer.spec)
"""

__version__ = '1.0.0'

import random
import queue
import threading
import tempfile
import os
import re
import time
import json
import socket
import socketserver
import http.server
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET

from kivy.app import App
from kivy.lang import Builder
from kivy.uix.recycleview import RecycleView
from kivy.uix.recycleview.views import RecycleDataViewBehavior
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.widget import Widget
from kivy.uix.popup import Popup
from kivy.uix.image import Image as KivyImage
from kivy.uix.label import Label
from kivy.uix.button import Button
from kivy.uix.progressbar import ProgressBar  # noqa: F401 — needed for KV
from kivy.properties import (
    BooleanProperty, StringProperty, NumericProperty, ListProperty
)
from kivy.core.audio import SoundLoader
from kivy.clock import Clock
from kivy.graphics.texture import Texture
from kivy.metrics import dp
from kivy.utils import platform

# ---------------------------------------------------------------------------
# Minimal SOAP client  (no zeep / lxml required)
# ---------------------------------------------------------------------------
_SOAP_NS   = 'http://ownly.audio/soap'
_SOAP_ENV  = 'http://schemas.xmlsoap.org/soap/envelope/'

# ---------------------------------------------------------------------------
# Local streaming proxy — lets ExoPlayer stream HTTP without cleartext policy
# ---------------------------------------------------------------------------
class _LocalProxy(threading.Thread):
    """Prefetching HTTP proxy on 127.0.0.1 (cleartext always allowed).

    ExoPlayer → http://127.0.0.1:<port>/
    Proxy      → downloads full file to temp buffer → serves ExoPlayer

    The file is downloaded to a temp file immediately when the proxy starts,
    independently of ExoPlayer's read rate.  ExoPlayer connections (including
    Range requests after initial buffering) are served straight from the temp
    file — no second server connection needed.  This avoids WiFi-sleep
    reconnection failures mid-track.

    Optional cache_dest: temp file is renamed to cache_dest when the download
    completes.  on_cached(track_id) is called on the Kivy thread on success.
    """

    _CHUNK = 65536
    _CONNECT_TIMEOUT = 8      # urlopen timeout per attempt (seconds)
    _RETRY_DELAY = 2.0        # seconds between retries
    _TOTAL_TIMEOUT = 120      # total budget for all download attempts (seconds)

    def __init__(self, remote_url, cache_dest=None, track_id=None, on_cached=None, on_debug=None, on_done=None):
        super().__init__(daemon=True)
        self.remote_url  = remote_url
        self._cache_dest = cache_dest
        self._track_id   = track_id
        self._on_cached  = on_cached
        self._on_debug   = on_debug
        self._on_done    = on_done
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(('127.0.0.1', 0))
        self._srv.listen(8)
        self.port = self._srv.getsockname()[1]
        self._stopped = False

        # Prefetch state — written by download thread, read by handle threads
        import tempfile as _tf
        self._tmp_path   = (cache_dest + '.tmp') if cache_dest else \
                           _tf.mktemp(suffix='.audio.tmp')
        self._dl_total   = 0     # Content-Length from server (0 = unknown)
        self._dl_written = 0     # bytes written to _tmp_path so far
        self._dl_done    = False # True when download finished successfully
        self._dl_error   = None  # Exception if download failed
        self._dl_cond    = threading.Condition()  # notified on each chunk + done/error

    def stop(self):
        self._stopped = True
        try:
            self._srv.close()
        except Exception:
            pass
        # Wake any waiting _handle threads so they can exit
        with self._dl_cond:
            self._dl_cond.notify_all()

    def run(self):
        # Start background download immediately — before ExoPlayer even connects
        threading.Thread(target=self._download_worker, daemon=True).start()
        # Accept ExoPlayer connections
        while not self._stopped:
            try:
                self._srv.settimeout(2.0)
                conn, _ = self._srv.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    # ── Download worker ──────────────────────────────────────────────────────

    def _dbg(self, msg):
        if self._on_debug:
            try:
                self._on_debug(msg)
            except Exception:
                pass

    def _download_worker(self):
        """Download full file to temp with time-bounded retries.  Notifies _dl_cond on progress."""
        import time as _time
        deadline = _time.monotonic() + self._TOTAL_TIMEOUT
        attempt = 0
        last_err = None
        while not self._stopped:
            if attempt:
                _time.sleep(self._RETRY_DELAY)
                if self._stopped or _time.monotonic() >= deadline:
                    break
                self._dbg(f'proxy: retry {attempt}')
            attempt += 1
            try:
                resp = urllib.request.urlopen(self.remote_url, timeout=self._CONNECT_TIMEOUT)
                total = int(resp.headers.get('Content-Length', 0))
                self._dbg(f'proxy: server OK {total}B')
                if self._cache_dest:
                    self._dbg(f'proxy: cache → {self._tmp_path[-40:]}')
                written = 0
                # Open file BEFORE notifying _handle threads — prevents FileNotFoundError
                # race where _handle tries to open the file before it is created.
                with open(self._tmp_path, 'wb') as f:
                    with self._dl_cond:
                        self._dl_total = total
                        self._dl_written = 0   # reset so _handle re-syncs on retry
                        self._dl_cond.notify_all()
                    while not self._stopped:
                        chunk = resp.read(self._CHUNK)
                        if not chunk:
                            break
                        f.write(chunk)
                        written += len(chunk)
                        with self._dl_cond:
                            self._dl_written = written
                            self._dl_cond.notify_all()
                with self._dl_cond:
                    self._dl_done = True
                    self._dl_cond.notify_all()
                self._dbg(f'proxy: download fertig {written}B')
                if self._on_done:
                    try:
                        self._on_done()
                    except Exception:
                        pass
                last_err = None
                break
            except Exception as e:
                last_err = e
                self._dbg(f'proxy ERR urlopen: {e}')

        if last_err:
            with self._dl_cond:
                self._dl_error = last_err
                self._dl_cond.notify_all()
            return

        # Rename to final cache path if requested
        if self._cache_dest and not self._stopped:
            try:
                os.rename(self._tmp_path, self._cache_dest)
                if self._on_cached and self._track_id:
                    tid = self._track_id
                    from kivy.clock import Clock as _Clock
                    _Clock.schedule_once(lambda _: self._on_cached(tid))
            except Exception:
                try:
                    os.remove(self._tmp_path)
                except Exception:
                    pass
        elif not self._cache_dest:
            # No caching requested — clean up temp file when proxy stops
            pass  # cleaned in stop() is not needed; OS cleans on exit

    # ── ExoPlayer connection handler ─────────────────────────────────────────

    def _handle(self, conn):
        try:
            self._dbg('proxy: verbunden')
            # Read HTTP request headers
            raw = b''
            while b'\r\n\r\n' not in raw:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                raw += chunk
            req_text = raw.decode('utf-8', errors='replace')

            # Parse optional Range header
            range_start = 0
            for line in req_text.splitlines():
                if line.lower().startswith('range:'):
                    try:
                        range_start = int(line.split('=')[1].split('-')[0])
                    except Exception:
                        pass

            # Wait until we know the total size (download worker sets _dl_total)
            with self._dl_cond:
                while self._dl_total == 0 and not self._dl_done and \
                      self._dl_error is None and not self._stopped:
                    self._dl_cond.wait(timeout=10.0)
                if self._dl_error or self._stopped:
                    return
                total = self._dl_total

            # Build response headers
            status = 206 if range_start > 0 else 200
            status_text = 'Partial Content' if status == 206 else 'OK'
            content_len = max(0, total - range_start) if total else 0
            hdr = f'HTTP/1.1 {status} {status_text}\r\nContent-Type: audio/mpeg\r\n'
            if content_len:
                hdr += f'Content-Length: {content_len}\r\n'
            if range_start > 0 and total:
                hdr += f'Content-Range: bytes {range_start}-{total - 1}/{total}\r\n'
            hdr += 'Accept-Ranges: bytes\r\nConnection: close\r\n\r\n'
            conn.sendall(hdr.encode())

            # Stream from temp file, waiting for download worker to write more
            bytes_sent = 0
            pos = range_start
            with open(self._tmp_path, 'rb') as f:
                f.seek(pos)
                while not self._stopped:
                    with self._dl_cond:
                        # Wait until more data is available or download is done/errored
                        while self._dl_written <= pos and not self._dl_done \
                              and self._dl_error is None and not self._stopped:
                            self._dl_cond.wait(timeout=2.0)
                        available = self._dl_written
                        done = self._dl_done

                    if available <= pos and done:
                        break  # No more data coming
                    if self._stopped or self._dl_error:
                        break

                    chunk = f.read(self._CHUNK)
                    if not chunk:
                        if done:
                            break
                        continue
                    conn.sendall(chunk)
                    bytes_sent += len(chunk)
                    pos += len(chunk)

            self._dbg(f'proxy: fertig {bytes_sent}B')
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass


# Android ExoPlayer listener — defined once (jnius singleton pattern).
# Reset to None here so a fresh build always redefines the class.
_ExoListenerClass = None

# Java Runnable wrapper — for posting Python callables to a HandlerThread.
_ExoRunnableClass = None

def _get_exo_runnable_class():
    global _ExoRunnableClass
    if _ExoRunnableClass is None:
        from jnius import PythonJavaClass, java_method  # type: ignore

        class _ExoRunnable(PythonJavaClass):
            __javainterfaces__ = ['java/lang/Runnable']
            __javacontext__ = 'app'

            def __init__(self, fn):
                super().__init__()
                self._fn = fn

            @java_method('()V')
            def run(self):
                try:
                    self._fn()
                except Exception:
                    pass

        _ExoRunnableClass = _ExoRunnable
    return _ExoRunnableClass

def _get_exo_listener_class():
    global _ExoListenerClass
    if _ExoListenerClass is None:
        from jnius import PythonJavaClass, java_method  # type: ignore

        class _ExoListener(PythonJavaClass):
            __javainterfaces__ = ['androidx/media3/common/Player$Listener']
            __javacontext__ = 'app'

            def __init__(self, on_ended, on_error, on_state=None):
                super().__init__()
                self._on_ended = on_ended
                self._on_error = on_error
                self._on_state = on_state

            _STATE_NAMES = {1: 'IDLE', 2: 'BUFFERING', 3: 'READY', 4: 'ENDED'}

            @java_method('(I)V')
            def onPlaybackStateChanged(self, state):
                if self._on_state:
                    name = self._STATE_NAMES.get(state, str(state))
                    self._on_state(f'exo state: {name}')
                if state == 4:   # Player.STATE_ENDED
                    self._on_ended()

            @java_method('(Z)V')
            def onIsPlayingChanged(self, is_playing):
                if self._on_state:
                    self._on_state(f'exo: isPlaying={is_playing}')

            @java_method('(ZI)V')
            def onPlayWhenReadyChanged(self, play_when_ready, reason):
                # reason: 1=USER, 2=AUDIO_FOCUS_LOSS, 3=BECOMING_NOISY, 4=REMOTE, 5=END_OF_MEDIA
                _reasons = {1: 'USER', 2: 'AUDIO_FOCUS_LOSS', 3: 'BECOMING_NOISY',
                            4: 'REMOTE', 5: 'END_OF_MEDIA'}
                r = _reasons.get(reason, str(reason))
                if self._on_state:
                    self._on_state(f'exo: playWhenReady={play_when_ready} reason={r}')

            @java_method('(Landroidx/media3/common/PlaybackException;)V')
            def onPlayerError(self, error):
                try:
                    msg = f'ExoPlayer Fehler {error.errorCode}'
                except Exception:
                    msg = 'ExoPlayer Fehler'
                self._on_error(msg)

        _ExoListenerClass = _ExoListener
    return _ExoListenerClass


class EmbeddedServer:
    """
    Minimal SOAP + audio HTTP server + UDP broadcast.
    No spyne/extra dependencies — pure stdlib.
    Compatible with the existing _soap_request client.
    """
    SOAP_NS = 'http://ownly.audio/soap'
    SOAP_ENV = 'http://schemas.xmlsoap.org/soap/envelope/'
    SOAP_PORT = 8767

    def __init__(self):
        self._httpd = None
        self._udp_running = False
        self._tracks = []

    @property
    def is_running(self):
        return self._httpd is not None

    def local_ip(self):
        import socket as _s
        try:
            s = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return '127.0.0.1'

    def local_addr(self):
        return f'{self.local_ip()}:{self.SOAP_PORT}'

    def scan_tracks(self, music_dir):
        """Scan music_dir recursively for MP3 files. Returns track list."""
        import hashlib
        from pathlib import Path
        tracks = []
        p = Path(music_dir)
        if not p.is_dir():
            return tracks
        for mp3 in sorted(p.rglob('*.mp3')):
            try:
                sha1 = hashlib.sha1()
                with open(str(mp3), 'rb') as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk:
                            break
                        sha1.update(chunk)
                tracks.append({
                    'band': mp3.parent.parent.name,
                    'album': mp3.parent.name,
                    'title': mp3.stem,
                    'idx': len(tracks),
                    'id': sha1.hexdigest(),
                    'abs': str(mp3),
                    'genre': '',
                })
            except Exception:
                pass
        return tracks

    def start(self, music_dir, on_status=None, log_file=None):
        """Start server in a daemon thread. on_status(msg) will be called on the Kivy main thread."""
        if self.is_running:
            return

        def _status(msg):
            if on_status:
                Clock.schedule_once(lambda _, m=msg: on_status(m))

        def _run():
            _status('Scanne Musikverzeichnis …')
            self._tracks = self.scan_tracks(music_dir)
            _status(f'{len(self._tracks)} Tracks gefunden, starte Server …')

            tracks_ref = self._tracks
            soap_port = self.SOAP_PORT
            local_ip = self.local_ip()
            ns = self.SOAP_NS
            env_ns = self.SOAP_ENV

            class _Handler(http.server.BaseHTTPRequestHandler):
                def log_message(self, *a):
                    pass

                def do_GET(self):
                    path = self.path.split('?')[0]
                    if path == '/log':
                        try:
                            with open(log_file, 'r', errors='replace') as _lf:
                                body = _lf.read()
                        except Exception as e:
                            body = f'Log nicht verfügbar: {e}'
                        # HTML wrapper for easy browser viewing with auto-refresh
                        data = (
                            '<html><head><meta charset="utf-8">'
                            '<meta http-equiv="refresh" content="3">'
                            '<style>body{font-family:monospace;font-size:12px;'
                            'background:#111;color:#cfc;white-space:pre-wrap;padding:8px}'
                            '</style></head><body>' +
                            body.replace('&', '&amp;').replace('<', '&lt;') +
                            '</body></html>'
                        ).encode('utf-8')
                        self.send_response(200)
                        self.send_header('Content-Type', 'text/html; charset=utf-8')
                        self.send_header('Content-Length', str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                    elif path.startswith('/audio/'):
                        try:
                            idx = int(path[7:])
                            fp = tracks_ref[idx]['abs']
                            size = os.path.getsize(fp)
                            # Parse Range header
                            range_hdr = self.headers.get('Range', '')
                            start = 0
                            end = size - 1
                            if range_hdr.startswith('bytes='):
                                parts = range_hdr[6:].split('-')
                                try:
                                    start = int(parts[0]) if parts[0] else 0
                                    end = int(parts[1]) if len(parts) > 1 and parts[1] else size - 1
                                except ValueError:
                                    pass
                            length = end - start + 1
                            if start > 0 or end < size - 1:
                                self.send_response(206)
                                self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
                            else:
                                self.send_response(200)
                            self.send_header('Content-Type', 'audio/mpeg')
                            self.send_header('Content-Length', str(length))
                            self.send_header('Accept-Ranges', 'bytes')
                            self.send_header('Access-Control-Allow-Origin', '*')
                            self.end_headers()
                            with open(fp, 'rb') as f:
                                f.seek(start)
                                remaining = length
                                while remaining > 0:
                                    chunk = f.read(min(65536, remaining))
                                    if not chunk:
                                        break
                                    self.wfile.write(chunk)
                                    remaining -= len(chunk)
                        except Exception:
                            try:
                                self.send_response(404)
                                self.end_headers()
                            except Exception:
                                pass
                    else:
                        self.send_response(404)
                        self.end_headers()

                def do_POST(self):
                    length = int(self.headers.get('Content-Length', 0))
                    body = self.rfile.read(length).decode('utf-8', errors='ignore')
                    if 'GetTracks' in body:
                        resp = self._tracks_xml()
                    else:
                        self.send_response(500)
                        self.end_headers()
                        return
                    data = resp.encode('utf-8')
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/xml; charset=utf-8')
                    self.send_header('Content-Length', str(len(data)))
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(data)

                def _tracks_xml(self):
                    def esc(s):
                        return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                    items = ''.join(
                        f'<tns:TrackInfo>'
                        f'<tns:idx>{t["idx"]}</tns:idx>'
                        f'<tns:id>{esc(t["id"])}</tns:id>'
                        f'<tns:band>{esc(t["band"])}</tns:band>'
                        f'<tns:album>{esc(t["album"])}</tns:album>'
                        f'<tns:title>{esc(t["title"])}</tns:title>'
                        f'<tns:genre>{esc(t.get("genre", ""))}</tns:genre>'
                        f'</tns:TrackInfo>'
                        for t in tracks_ref
                    )
                    return (
                        f'<?xml version="1.0" encoding="utf-8"?>'
                        f'<soap:Envelope xmlns:soap="{env_ns}" xmlns:tns="{ns}">'
                        f'<soap:Body>'
                        f'<tns:GetTracksResponse>'
                        f'<tns:GetTracksResult>{items}</tns:GetTracksResult>'
                        f'</tns:GetTracksResponse>'
                        f'</soap:Body>'
                        f'</soap:Envelope>'
                    )

            class _ReuseServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
                allow_reuse_address = True
                daemon_threads = True

                def handle_error(self, request, client_address):
                    # Suppress expected client-disconnect errors (BrokenPipe, ConnectionReset)
                    import sys
                    exc = sys.exc_info()[1]
                    if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
                        return
                    super().handle_error(request, client_address)

            try:
                self._httpd = _ReuseServer(('0.0.0.0', soap_port), _Handler)
                self._udp_running = True
                threading.Thread(target=self._udp_loop, daemon=True).start()
                _status(f'Laeuft: {local_ip}:{soap_port} ({len(tracks_ref)} Tracks)')
                self._httpd.serve_forever()
            except Exception as e:
                self._httpd = None
                import traceback; traceback.print_exc()
                _status(f'Fehler: {e}')

        threading.Thread(target=_run, daemon=True).start()

    def stop(self):
        self._udp_running = False
        if self._httpd:
            self._httpd.shutdown()
            self._httpd = None

    def _udp_loop(self):
        import socket as _s
        sock = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
        sock.setsockopt(_s.SOL_SOCKET, _s.SO_BROADCAST, 1)
        msg = f'OWNLY:{self.SOAP_PORT}'.encode()
        while self._udp_running:
            try:
                sock.sendto(msg, ('<broadcast>', 8768))
            except Exception:
                pass
            time.sleep(3)
        sock.close()


def _soap_request(host, port, method, **params):
    """
    Execute a SOAP 1.1 call and return the parsed XML root element.
    Uses only stdlib – works on Android without zeep/lxml.
    """
    body_inner = ''.join(
        f'<tns:{k}>{v}</tns:{k}>' for k, v in params.items()
    )
    envelope = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope'
        '  xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"'
        f'  xmlns:tns="{_SOAP_NS}">'
        '<soap:Body>'
        f'<tns:{method}>{body_inner}</tns:{method}>'
        '</soap:Body>'
        '</soap:Envelope>'
    )
    url = f'http://{host}:{port}/'
    req = urllib.request.Request(
        url,
        data=envelope.encode('utf-8'),
        headers={
            'Content-Type': 'text/xml; charset=utf-8',
            'SOAPAction':   f'"{_SOAP_NS}#{method}"',
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return ET.fromstring(resp.read())


def _soap_text(element, tag):
    """Find first child with local name 'tag', regardless of namespace."""
    for child in element:
        if child.tag.split('}')[-1] == tag:
            return child.text or ''
    return ''

# ---------------------------------------------------------------------------
# KV layout
# ---------------------------------------------------------------------------
KV = """
#:import dp kivy.metrics.dp

<TrackRow>:
    size_hint_y: None
    height: dp(54)
    padding: dp(20), dp(6), dp(4), dp(6)
    spacing: dp(4)
    orientation: 'horizontal'
    canvas.before:
        Color:
            rgba: (.22, .14, .05, 1) if self.is_active else (.12, .12, .12, 1)
        Rectangle:
            size: self.size
            pos: self.pos
        Color:
            rgba: (.2, .2, .2, 1)
        Line:
            points: self.x, self.y, self.x + self.width, self.y
            width: 1

    Label:
        text: root.title
        font_size: dp(13)
        halign: 'left'
        valign: 'middle'
        text_size: self.size
        color: (1, .7, .25, 1) if root.is_active else (.95, .95, .95, 1)
        bold: root.is_active

    Button:
        size_hint_x: None
        width: dp(34)
        text: 'ok' if root.is_cached else 'dl'
        font_size: dp(11)
        background_color: 0, 0, 0, 0
        color: (.3, .9, .3, 1) if root.is_cached else (.45, .45, .45, 1)
        on_release: app.download_track_by_id(root.idx, root.track_id)

<BandHeader>:
    size_hint_y: None
    height: dp(38)
    padding: dp(8), dp(4)
    spacing: dp(6)
    orientation: 'horizontal'
    canvas.before:
        Color:
            rgba: (.1, .1, .1, 1)
        Rectangle:
            size: self.size
            pos: self.pos
        Color:
            rgba: (.93, .4, .2, .6)
        Line:
            points: self.x, self.y, self.x + self.width, self.y
            width: 1.5

    Label:
        text: 'v' if root.is_expanded else '>'
        size_hint_x: None
        width: dp(14)
        font_size: dp(13)
        bold: True
        color: (.93, .4, .2, 1)

    Label:
        text: root.band
        font_size: dp(13)
        bold: True
        halign: 'left'
        valign: 'middle'
        text_size: self.size
        color: (.93, .4, .2, 1)

<AlbumHeader>:
    size_hint_y: None
    height: dp(30)
    padding: dp(28), dp(2), dp(6), dp(2)
    spacing: dp(4)
    orientation: 'horizontal'
    canvas.before:
        Color:
            rgba: (.13, .13, .13, 1)
        Rectangle:
            size: self.size
            pos: self.pos

    Label:
        text: 'v' if root.is_expanded else '>'
        size_hint_x: None
        width: dp(14)
        font_size: dp(11)
        color: (.55, .55, .55, 1)

    Label:
        text: root.album
        font_size: dp(11)
        halign: 'left'
        valign: 'middle'
        text_size: self.size
        color: (.55, .55, .55, 1)
        italic: True

    Button:
        size_hint_x: None
        width: dp(34)
        text: 'ok' if root.album_cached else 'dl'
        font_size: dp(11)
        background_color: 0, 0, 0, 0
        color: (.3, .9, .3, 1) if root.album_cached else (.45, .45, .45, 1)
        on_release: app.download_album(root.album)

<TrackList>:
    bar_width: dp(4)
    bar_color: (.93, .4, .2, .8)
    key_viewclass: 'viewclass'
    key_size: 'height'
    RecycleBoxLayout:
        default_size: None, dp(54)
        default_size_hint: 1, None
        size_hint_y: None
        height: self.minimum_height
        orientation: 'vertical'
        spacing: 0

<StatusDot>:
    canvas:
        Color:
            rgba: self.dot_color
        Ellipse:
            pos: self.x + dp(1), self.center_y - dp(6)
            size: dp(12), dp(12)

<ServerPopup>:
    title: 'Mein Server'
    size_hint: .92, None
    height: dp(280)
    auto_dismiss: True
    BoxLayout:
        orientation: 'vertical'
        spacing: dp(10)
        padding: dp(12)
        TextInput:
            id: music_dir_input
            hint_text: '/sdcard/Music'
            font_size: dp(13)
            multiline: False
            size_hint_y: None
            height: dp(44)
            background_color: (.18, .18, .18, 1)
            foreground_color: (.9, .9, .9, 1)
            cursor_color: (1, .5, .2, 1)
        Button:
            id: toggle_btn
            text: 'Server starten'
            font_size: dp(14)
            size_hint_y: None
            height: dp(44)
            background_color: (.18, .55, .18, 1)
            on_release: app.toggle_embedded_server(music_dir_input.text)
        TextInput:
            id: srv_status
            text: 'Server gestoppt'
            font_size: dp(12)
            foreground_color: (.7, .7, .7, 1)
            background_color: (0, 0, 0, 0)
            size_hint_y: None
            height: dp(28)
            readonly: True
            multiline: False
            halign: 'center'
        Button:
            text: 'Selbst verbinden'
            font_size: dp(13)
            size_hint_y: None
            height: dp(40)
            background_color: (.93, .4, .2, 1)
            on_release: app.connect_to_self(); root.dismiss()

<ConnectionsPopup>:
    title: 'Verbindungen'
    size_hint: .92, .72
    auto_dismiss: True
    BoxLayout:
        orientation: 'vertical'
        spacing: dp(8)
        padding: dp(10)
        ScrollView:
            size_hint_y: 1
            BoxLayout:
                id: servers_list
                orientation: 'vertical'
                size_hint_y: None
                height: self.minimum_height
                spacing: dp(4)
        BoxLayout:
            size_hint_y: None
            height: dp(40)
            spacing: dp(6)
            TextInput:
                id: host_input
                hint_text: 'IP:Port manuell eingeben'
                font_size: dp(13)
                multiline: False
                background_color: (.18, .18, .18, 1)
                foreground_color: (.9, .9, .9, 1)
                cursor_color: (1, .5, .2, 1)
                on_text_validate: app.connect(self.text); root.dismiss()
            Button:
                text: 'Verbinden'
                size_hint_x: None
                width: dp(90)
                font_size: dp(12)
                background_color: (.93, .4, .2, 1)
                on_release: app.connect(host_input.text); root.dismiss()
        BoxLayout:
            size_hint_y: None
            height: dp(40)
            spacing: dp(6)
            Button:
                text: 'Suchen'
                font_size: dp(13)
                background_color: (.18, .18, .18, 1)
                on_release: app.autodiscover()
            Button:
                text: 'QR'
                font_size: dp(13)
                size_hint_x: None
                width: dp(52)
                background_color: (.18, .18, .18, 1)
                on_release: app.open_qr_scan(); root.dismiss()

<MenuPopup>:
    title: 'Menü'
    size_hint: .8, None
    height: dp(278)
    auto_dismiss: True
    BoxLayout:
        orientation: 'vertical'
        spacing: dp(8)
        padding: dp(12)
        Button:
            text: 'Verbindungen'
            font_size: dp(14)
            size_hint_y: None
            height: dp(44)
            background_color: (.25, .25, .25, 1)
            on_release: root.dismiss(); app.open_connections()
        Button:
            text: 'Server'
            font_size: dp(14)
            size_hint_y: None
            height: dp(44)
            background_color: (.18, .55, .18, 1)
            on_release: root.dismiss(); app.open_server_popup()
        Button:
            text: 'Einstellungen'
            font_size: dp(14)
            size_hint_y: None
            height: dp(44)
            background_color: (.2, .3, .5, 1)
            on_release: root.dismiss(); app.open_settings()
        Button:
            text: 'Logs'
            font_size: dp(14)
            size_hint_y: None
            height: dp(44)
            background_color: (.4, .25, .1, 1)
            on_release: root.dismiss(); app.open_log_popup()

<LogPopup>:
    title: 'Debug-Log'
    size_hint: .97, .85
    auto_dismiss: True
    BoxLayout:
        orientation: 'vertical'
        spacing: dp(6)
        padding: dp(8)
        ScrollView:
            id: log_scroll
            TextInput:
                id: log_label
                text: ''
                font_size: dp(11)
                foreground_color: (.85, .95, .75, 1)
                background_color: (.08, .08, .08, 1)
                readonly: True
                halign: 'left'
                size_hint_y: None
                height: max(self.minimum_height, log_scroll.height)
        BoxLayout:
            size_hint_y: None
            height: dp(44)
            spacing: dp(8)
            Button:
                text: 'Löschen'
                font_size: dp(13)
                background_color: (.5, .15, .15, 1)
                on_release: app.clear_log()
            Button:
                text: 'Schließen'
                font_size: dp(13)
                background_color: (.25, .25, .25, 1)
                on_release: root.dismiss()


<SettingsPopup>:
    title: 'Einstellungen'
    size_hint: .92, None
    height: dp(260)
    auto_dismiss: True
    BoxLayout:
        orientation: 'vertical'
        spacing: dp(10)
        padding: dp(14)
        BoxLayout:
            size_hint_y: None
            height: dp(44)
            spacing: dp(10)
            Label:
                text: 'Beim Abspielen offline sichern'
                font_size: dp(13)
                color: (.9, .9, .9, 1)
                halign: 'left'
                valign: 'middle'
                text_size: self.size
            Button:
                size_hint_x: None
                width: dp(64)
                font_size: dp(13)
                text: 'EIN' if app.auto_cache_on_play else 'AUS'
                background_color: (.18, .55, .18, 1) if app.auto_cache_on_play else (.45, .45, .45, 1)
                on_release: app.toggle_auto_cache()
        Label:
            size_hint_y: None
            height: dp(18)
            text: 'Offline-Ordner:'
            font_size: dp(12)
            color: (.7, .7, .7, 1)
            halign: 'left'
            text_size: self.size
        BoxLayout:
            size_hint_y: None
            height: dp(44)
            spacing: dp(8)
            TextInput:
                id: offline_dir_input
                font_size: dp(12)
                multiline: False
                background_color: (.15, .15, .15, 1)
                foreground_color: (.95, .95, .95, 1)
            Button:
                size_hint_x: None
                width: dp(44)
                font_size: dp(18)
                text: '..'
                background_color: (.3, .3, .3, 1)
                on_release: app.open_dir_chooser(offline_dir_input)
            Button:
                size_hint_x: None
                width: dp(90)
                font_size: dp(12)
                text: 'Speichern'
                background_color: (.2, .4, .7, 1)
                on_release: app.set_offline_tracks_dir(offline_dir_input.text)
        Label:
            id: offline_dir_status
            size_hint_y: None
            height: dp(20)
            font_size: dp(11)
            color: (.6, .9, .6, 1)
            halign: 'left'
            text_size: self.size
            text: ''

<DirChooserPopup>:
    title: 'Ordner wählen'
    size_hint: .95, .9
    auto_dismiss: True
    BoxLayout:
        orientation: 'vertical'
        spacing: dp(6)
        padding: dp(8)
        FileChooserListView:
            id: chooser
            dirselect: True
            filters: ['*/']
            path: root.start_path
        BoxLayout:
            size_hint_y: None
            height: dp(48)
            spacing: dp(8)
            Label:
                id: sel_label
                text: chooser.path
                font_size: dp(11)
                color: (.8, .8, .8, 1)
                shorten: True
                shorten_from: 'left'
            Button:
                size_hint_x: None
                width: dp(110)
                text: 'Auswählen'
                font_size: dp(13)
                background_color: (.2, .5, .2, 1)
                on_release: root.confirm(chooser.path)

<OwnlyRoot>:
    orientation: 'vertical'
    canvas.before:
        Color:
            rgba: (.08, .08, .08, 1)
        Rectangle:
            size: self.size
            pos: self.pos

    # ── Header bar ──────────────────────────────────────────────────────────
    BoxLayout:
        size_hint_y: None
        height: dp(50)
        padding: dp(4), dp(6)
        spacing: dp(4)
        canvas.before:
            Color:
                rgba: (.11, .11, .11, 1)
            Rectangle:
                size: self.size
                pos: self.pos

        Button:
            text: '|||'
            font_size: dp(15)
            size_hint_x: None
            width: dp(48)
            background_color: (.18, .18, .18, 1)
            on_release: app.open_menu()

        Button:
            text: 'Srv'
            font_size: dp(12)
            size_hint_x: None
            width: dp(44)
            background_color: (.18, .55, .18, 1)
            on_release: app.open_server_popup()

        Label:
            text: 'Ownly Audio'
            font_size: dp(14)
            bold: True
            color: (.93, .4, .2, 1)
            halign: 'left'
            valign: 'middle'
            text_size: self.size

        StatusDot:
            id: status_dot
            size_hint_x: None
            width: dp(20)

    # ── Search + offline filter ─────────────────────────────────────────────
    BoxLayout:
        size_hint_y: None
        height: dp(36)
        spacing: dp(4)
        padding: 0
        TextInput:
            id: search_input
            hint_text: 'Suchen …'
            font_size: dp(13)
            multiline: False
            background_color: (.13, .13, .13, 1)
            foreground_color: (.9, .9, .9, 1)
            on_text: app.filter_tracks(self.text)
        Button:
            id: offline_btn
            text: 'Off'
            size_hint_x: None
            width: dp(40)
            font_size: dp(11)
            background_color: (.13, .13, .13, 1)
            color: (.45, .45, .45, 1)
            on_release: app.toggle_offline_filter()

    # ── Track list ───────────────────────────────────────────────────────────
    TrackList:
        id: track_list

    # ── Player bar ───────────────────────────────────────────────────────────
    BoxLayout:
        orientation: 'vertical'
        size_hint_y: None
        height: dp(106)
        padding: dp(8), dp(4)
        spacing: dp(4)
        canvas.before:
            Color:
                rgba: (.1, .1, .1, 1)
            Rectangle:
                size: self.size
                pos: self.pos
            Color:
                rgba: (.2, .2, .2, 1)
            Line:
                points: self.x, self.top, self.x + self.width, self.top
                width: 1

        TextInput:
            id: now_playing
            text: '— nichts ausgewählt —'
            font_size: dp(12)
            foreground_color: (.75, .75, .75, 1)
            background_color: (0, 0, 0, 0)
            size_hint_y: None
            height: dp(24)
            readonly: True
            multiline: False
            halign: 'center'
            padding: dp(4), dp(2)

        # ── Progress ───────────────────────────────────────────────────────
        BoxLayout:
            size_hint_y: None
            height: dp(16)
            spacing: dp(8)
            ProgressBar:
                id: progress_bar
                max: 1000
                value: 0
            Label:
                id: time_label
                text: ''
                size_hint_x: None
                width: dp(90)
                font_size: dp(11)
                halign: 'right'
                valign: 'middle'
                text_size: self.size
                color: (.45, .45, .45, 1)

        BoxLayout:
            spacing: dp(6)
            size_hint_y: None
            height: dp(44)

            Button:
                text: '|<'
                font_size: dp(16)
                background_color: (.18, .18, .18, 1)
                on_release: app.prev_track()

            Button:
                id: play_btn
                text: '>'
                font_size: dp(20)
                background_color: (.93, .4, .2, 1)
                on_release: app.toggle_play()

            Button:
                text: '>|'
                font_size: dp(16)
                background_color: (.18, .18, .18, 1)
                on_release: app.next_track()

            Button:
                id: shuffle_btn
                text: 'Mix'
                font_size: dp(13)
                background_color: (.18, .18, .18, 1)
                on_release: app.toggle_shuffle()
"""


# ---------------------------------------------------------------------------
# Widget classes
# ---------------------------------------------------------------------------

class TrackRow(RecycleDataViewBehavior, BoxLayout):
    idx      = NumericProperty(0)
    track_id = StringProperty('')
    title    = StringProperty('')
    band     = StringProperty('')
    album    = StringProperty('')
    genre    = StringProperty('')
    is_active = BooleanProperty(False)
    is_cached = BooleanProperty(False)

    def refresh_view_attrs(self, rv, index, data):
        self.idx      = data.get('idx', 0)
        self.track_id = data.get('track_id', '')
        self.title    = data.get('title', '')
        self.band     = data.get('band', '')
        self.album    = data.get('album', '')
        self.genre    = data.get('genre', '')
        self.is_active = data.get('is_active', False)
        self.is_cached = data.get('is_cached', False)
        return super().refresh_view_attrs(rv, index, data)

    def on_touch_down(self, touch):
        if self.collide_point(*touch.pos):
            if super().on_touch_down(touch):
                return True
            App.get_running_app().play_idx(self.idx)
            return True
        return False


class BandHeader(RecycleDataViewBehavior, BoxLayout):
    band        = StringProperty('')
    is_expanded = BooleanProperty(False)

    def refresh_view_attrs(self, rv, index, data):
        self.band        = data.get('band', '')
        self.is_expanded = data.get('is_expanded', False)
        return super().refresh_view_attrs(rv, index, data)

    def on_touch_down(self, touch):
        if self.collide_point(*touch.pos):
            App.get_running_app().toggle_band(self.band)
            return True
        return False


class AlbumHeader(RecycleDataViewBehavior, BoxLayout):
    album        = StringProperty('')
    album_key    = StringProperty('')
    album_cached = BooleanProperty(False)
    is_expanded  = BooleanProperty(False)

    def refresh_view_attrs(self, rv, index, data):
        self.album        = data.get('album', '')
        self.album_key    = data.get('album_key', '')
        self.album_cached = data.get('album_cached', False)
        self.is_expanded  = data.get('is_expanded', False)
        return super().refresh_view_attrs(rv, index, data)

    def on_touch_down(self, touch):
        if self.collide_point(*touch.pos):
            if super().on_touch_down(touch):   # download button gets priority
                return True
            App.get_running_app().toggle_album(self.album_key)
            return True
        return False


class TrackList(RecycleView):
    pass


class StatusDot(Widget):
    dot_color = ListProperty([.35, .35, .35, 1])


class OwnlyRoot(BoxLayout):
    pass


class LogPopup(Popup):
    pass

class ConnectionsPopup(Popup):
    pass


class ServerPopup(Popup):
    pass


class MenuPopup(Popup):
    pass


class SettingsPopup(Popup):
    pass


class DirChooserPopup(Popup):
    start_path = StringProperty('/')

    def __init__(self, on_confirm, **kwargs):
        super().__init__(**kwargs)
        self._on_confirm = on_confirm

    def confirm(self, path):
        self._on_confirm(path)
        self.dismiss()


# ---------------------------------------------------------------------------
# QR Scanner  (cv2.VideoCapture on desktop · Kivy Camera on Android)
# ---------------------------------------------------------------------------

class QRScanPopup(Popup):
    """
    Camera overlay that decodes QR codes using OpenCV.

    Desktop  → cv2.VideoCapture(0) grabs frames directly.
    Android  → Kivy Camera widget captures via Android Camera API;
               cv2.QRCodeDetector reads the texture pixels.
    """

    def __init__(self, on_result, **kwargs):
        super().__init__(
            title='QR Code scannen',
            size_hint=(.95, .85),
            auto_dismiss=False,
            **kwargs
        )
        self._on_result = on_result
        self._running   = False
        self._detector  = None
        self._cv_cap    = None   # desktop only
        self._kivy_cam  = None   # android only
        self._tick      = None

        root = BoxLayout(orientation='vertical', spacing=dp(8), padding=dp(8))

        # Camera feed placeholder – replaced by Kivy Camera on Android
        self._cam_img = KivyImage(allow_stretch=True)
        self._cam_container = BoxLayout()
        self._cam_container.add_widget(self._cam_img)

        self._status = Label(
            text='Kamera wird geöffnet …',
            size_hint_y=None, height=dp(28),
            color=(.7, .7, .7, 1), font_size=dp(12)
        )
        close_btn = Button(
            text='Abbrechen',
            size_hint_y=None, height=dp(40),
            background_color=(.3, .3, .3, 1)
        )
        close_btn.bind(on_release=lambda *_: self._close())

        root.add_widget(self._cam_container)
        root.add_widget(self._status)
        root.add_widget(close_btn)
        self.content = root

    # ── open ────────────────────────────────────────────────────────────────

    def on_open(self):
        try:
            import cv2
            self._detector = cv2.QRCodeDetector()
        except ImportError:
            self._status.text = '[X] OpenCV nicht verfügbar'
            return

        if platform == 'android':
            self._open_android()
        else:
            self._open_desktop()

    def _open_desktop(self):
        import cv2
        self._cv_cap = cv2.VideoCapture(0)
        if not self._cv_cap.isOpened():
            self._status.text = '[X] Keine Kamera gefunden'
            return
        self._running = True
        self._tick = Clock.schedule_interval(self._update_desktop, 1 / 20)

    def _open_android(self):
        try:
            from android.permissions import (  # type: ignore
                request_permissions, check_permission, Permission
            )
            if check_permission(Permission.CAMERA):
                self._start_kivy_camera()
            else:
                request_permissions(
                    [Permission.CAMERA],
                    self._on_permission_result
                )
        except ImportError:
            # android.permissions not available – try camera anyway
            self._start_kivy_camera()

    def _on_permission_result(self, permissions, results):
        if results and all(results):
            self._start_kivy_camera()
        else:
            self._status.text = '[X] Kamera-Berechtigung verweigert'

    def _start_kivy_camera(self):
        from kivy.uix.camera import Camera  # type: ignore
        self._cam_container.clear_widgets()
        self._kivy_cam = Camera(
            resolution=(640, 480),
            play=True,
            allow_stretch=True,
        )
        self._cam_container.add_widget(self._kivy_cam)
        self._running = True
        self._tick = Clock.schedule_interval(self._update_android, 1 / 10)

    # ── per-frame update ─────────────────────────────────────────────────────

    def _update_desktop(self, dt):
        if not self._running or self._cv_cap is None:
            return
        import cv2
        import numpy as np
        ret, frame = self._cv_cap.read()
        if not ret:
            return

        data, _, _ = self._detector.detectAndDecode(frame)
        if data:
            self._finish(data)
            return

        # Animated scan line
        h, w = frame.shape[:2]
        t = int(Clock.get_boottime() * 80) % h
        cv2.line(frame, (0, t), (w, t), (238, 102, 51), 2)

        # BGR → RGB → Kivy texture (flip vertically)
        rgb  = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        flip = np.flipud(rgb)
        tex  = Texture.create(size=(w, h), colorfmt='rgb')
        tex.blit_buffer(flip.tobytes(), colorfmt='rgb', bufferfmt='ubyte')
        self._cam_img.texture = tex
        self._status.text = 'QR Code suchen …'

    def _update_android(self, dt):
        if not self._running or self._kivy_cam is None:
            return
        import cv2
        import numpy as np
        tex = self._kivy_cam.texture
        if tex is None:
            return

        # Kivy texture: RGBA, bottom-left origin → convert for cv2
        pixels = np.frombuffer(tex.pixels, dtype=np.uint8)
        frame  = pixels.reshape(tex.height, tex.width, 4)[:, :, :3]
        frame  = np.flipud(frame)                           # correct orientation
        frame_bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)

        data, _, _ = self._detector.detectAndDecode(frame_bgr)
        if data:
            self._finish(data)
        else:
            self._status.text = 'QR Code suchen …'

    # ── helpers ──────────────────────────────────────────────────────────────

    def _finish(self, data):
        self._running = False
        if self._tick:
            Clock.unschedule(self._tick)
        self._release_camera()
        self._status.text = f'Erkannt: {data}'
        Clock.schedule_once(lambda _: self._deliver(data))

    def _deliver(self, data):
        self.dismiss()
        self._on_result(data)

    def _close(self):
        self._running = False
        if self._tick:
            Clock.unschedule(self._tick)
        self._release_camera()
        self.dismiss()

    def _release_camera(self):
        if self._cv_cap:
            self._cv_cap.release()
            self._cv_cap = None
        if self._kivy_cam:
            self._kivy_cam.play = False
            self._kivy_cam = None

    def on_dismiss(self):
        self._close()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class OwnlyApp(App):
    title = 'Ownly Audio Pocket'

    auto_cache_on_play = BooleanProperty(False)

    def build(self):
        try:
            return self._build_inner()
        except Exception:
            import traceback
            err = traceback.format_exc()
            from kivy.uix.scrollview import ScrollView
            from kivy.uix.textinput import TextInput as _TI
            sv = ScrollView()
            ti = _TI(
                text=err,
                font_size='11sp',
                readonly=True,
                multiline=True,
                background_color=(0.08, 0.08, 0.08, 1),
                foreground_color=(1, 0.4, 0.4, 1),
                size_hint_y=None,
            )
            ti.bind(minimum_height=ti.setter('height'))
            sv.add_widget(ti)
            return sv

    def _build_inner(self):
        Builder.load_string(KV)
        self._root = OwnlyRoot()
        from kivy.core.window import Window
        if platform == 'android':
            Window.softinput_mode = 'below_target'
            from kivy.clock import Clock as _Clock
            _Clock.schedule_once(self._apply_android_insets, 0.5)
        else:
            Window.size = (420, 750)
            Window.minimum_width = 320
            Window.minimum_height = 500

        self._all_tracks    = []
        self._filtered      = []
        self._active_srv_idx = -1
        self._sound          = None
        self._old_sound      = None
        self._shuffle        = False
        self._exo_playing    = False   # track ExoPlayer play/pause state
        self._mp_listener    = None
        self._progress_clock = None
        self._server_host    = ''
        self._soap_port      = 8767
        self._tmp_file       = None
        self._dl_wake_lock       = None   # PARTIAL_WAKE_LOCK held during background downloads
        self._playback_wake_lock = None   # PARTIAL_WAKE_LOCK held while a track is playing
        self._exo_handler        = None   # Handler on ExoPlayerThread — ExoPlayer runs here
        self._proxy          = None   # _LocalProxy instance for current stream
        self._wifi_lock      = None   # WiFi lock — keeps radio on during streaming
        self._cached_ids          = set()
        self._offline_only        = False
        self._offline_tracks_dir  = ''   # empty = use default (user_data_dir/tracks)
        self._expanded_bands  = set()
        self._expanded_albums = set()
        self._current_addr   = ''
        self._servers        = []   # list of {'addr': '...'} dicts
        self._conn_popup     = None
        self._embedded_server = EmbeddedServer()
        self._server_popup    = None
        self._settings_popup  = None
        self._log_popup       = None
        self._log_lines       = []   # list of str, max 200
        self._log_queue       = queue.Queue()  # thread-safe log buffer
        self._pending_auto_next = False         # set from Java thread, checked by daemon thread

        # Persistent debug log file — written directly (no Clock dependency, works when screen off)
        self._log_file      = os.path.join(self.user_data_dir, 'debug.log')
        self._log_file_lock = threading.Lock()
        try:
            with open(self._log_file, 'w') as _lf:
                _lf.write(f'=== Ownly debug log {time.strftime("%Y-%m-%d %H:%M:%S")} ===\n')
        except Exception:
            self._log_file = None

        # UDP log sender — fires every log line to log_server.py on the PC.
        # UDP is stateless: no connection to lose when app goes to background.
        self._log_send_queue = queue.Queue()
        threading.Thread(target=self._run_udp_log_sender, daemon=True).start()

        Clock.schedule_interval(self._drain_log_queue, 0.25)
        # Python daemon thread polls _pending_auto_next — keeps running when SDL is paused
        threading.Thread(target=self._auto_next_watcher, daemon=True).start()

        if platform == 'android':
            self._register_screen_receiver()

        Clock.schedule_once(self._load_servers, 0)
        Clock.schedule_once(self._load_server_music_dir, 0)
        self._load_settings()          # must run before _load_cached_ids
        Clock.schedule_once(self._load_cached_ids, 0)
        Clock.schedule_once(self._load_settings, 0)   # re-apply on main thread for UI bindings
        # Delay startup connect so Android finishes rendering the widget tree
        Clock.schedule_once(self._load_saved_host, 1.0)
        return self._root

    def _host_file(self):
        import os
        return os.path.join(self.user_data_dir, 'last_host.txt')

    def _servers_file(self):
        return os.path.join(self.user_data_dir, 'servers.json')

    def _settings_file(self):
        return os.path.join(self.user_data_dir, 'settings.json')

    def _load_settings(self, *_):
        try:
            with open(self._settings_file()) as f:
                s = json.load(f)
            self.auto_cache_on_play    = bool(s.get('auto_cache_on_play', False))
            self._offline_tracks_dir   = s.get('offline_tracks_dir', '')
        except Exception:
            self.auto_cache_on_play    = False
            self._offline_tracks_dir   = ''

    def _save_settings(self):
        try:
            with open(self._settings_file(), 'w') as f:
                json.dump({
                    'auto_cache_on_play':  self.auto_cache_on_play,
                    'offline_tracks_dir':  self._offline_tracks_dir,
                }, f)
        except Exception:
            pass

    def toggle_auto_cache(self):
        self.auto_cache_on_play = not self.auto_cache_on_play
        self._save_settings()

    def set_offline_tracks_dir(self, new_path):
        """Change the offline-tracks directory, moving all existing files there."""
        new_path = new_path.strip()
        old_dir  = self._cache_dir()          # resolves current (possibly default) path
        new_dir  = new_path or os.path.join(self.user_data_dir, 'tracks')

        if os.path.realpath(old_dir) == os.path.realpath(new_dir):
            return  # nothing to do

        try:
            os.makedirs(new_dir, exist_ok=True)
        except Exception as e:
            if self._settings_popup:
                self._settings_popup.ids.offline_dir_status.text = f'[X] {e}'
            return

        moved = 0
        errors = []
        if os.path.isdir(old_dir):
            for fname in os.listdir(old_dir):
                src = os.path.join(old_dir, fname)
                dst = os.path.join(new_dir, fname)
                try:
                    import shutil
                    shutil.move(src, dst)
                    moved += 1
                except Exception as e:
                    errors.append(str(e))

        self._offline_tracks_dir = new_path
        self._save_settings()
        self._load_cached_ids()   # refresh index from new location

        msg = f'Verschoben: {moved} Dateien'
        if errors:
            msg += f'  ({len(errors)} Fehler)'
        if self._settings_popup:
            self._settings_popup.ids.offline_dir_status.text = msg

    def _load_servers(self, *_):
        try:
            with open(self._servers_file()) as f:
                self._servers = json.load(f)
        except Exception:
            self._servers = []

    def _save_servers(self):
        try:
            with open(self._servers_file(), 'w') as f:
                json.dump(self._servers, f)
        except Exception:
            pass

    def _add_server(self, addr):
        addr = addr.strip()
        if addr and not any(s['addr'] == addr for s in self._servers):
            self._servers.append({'addr': addr})
            self._save_servers()

    def _remove_server(self, addr):
        self._servers = [s for s in self._servers if s['addr'] != addr]
        self._save_servers()
        if self._conn_popup:
            self._populate_servers_list()

    def _populate_servers_list(self):
        from kivy.uix.boxlayout import BoxLayout as _BL
        from kivy.uix.button import Button as _Btn
        from kivy.uix.label import Label as _Lbl
        from kivy.metrics import dp as _dp
        sl = self._conn_popup.ids.servers_list
        sl.clear_widgets()
        if not self._servers:
            sl.add_widget(_Lbl(
                text='Keine Server bekannt. Suchen oder manuell eingeben.',
                font_size=_dp(12), color=(.5, .5, .5, 1),
                size_hint_y=None, height=_dp(40),
                halign='center', valign='middle',
            ))
            return
        for srv in self._servers:
            addr = srv['addr']
            row = _BL(size_hint_y=None, height=_dp(44), spacing=_dp(6))
            is_active = (addr == self._current_addr)
            conn_btn = _Btn(
                text=addr,
                font_size=_dp(13),
                background_color=(.93, .4, .2, 1) if is_active else (.18, .18, .18, 1),
            )
            conn_btn.bind(on_release=lambda b, a=addr: (
                self._conn_popup.dismiss(),
                self.connect(a),
            ))
            rem_btn = _Btn(
                text='x', size_hint_x=None, width=_dp(38),
                font_size=_dp(13), background_color=(.35, .1, .1, 1),
            )
            rem_btn.bind(on_release=lambda b, a=addr: self._remove_server(a))
            row.add_widget(conn_btn)
            row.add_widget(rem_btn)
            sl.add_widget(row)

    def _server_music_dir_file(self):
        return os.path.join(self.user_data_dir, 'server_music_dir.txt')

    def _load_server_music_dir(self, *_):
        try:
            with open(self._server_music_dir_file()) as f:
                return f.read().strip()
        except Exception:
            return '/sdcard/Music'

    def _save_server_music_dir(self, path):
        try:
            with open(self._server_music_dir_file(), 'w') as f:
                f.write(path)
        except Exception:
            pass

    def _load_saved_host(self, *_):
        try:
            with open(self._host_file(), 'r') as f:
                saved = f.read().strip()
            if saved:
                self._current_addr = saved
                self.connect(saved)
        except Exception:
            pass

    def _save_host(self, addr):
        try:
            with open(self._host_file(), 'w') as f:
                f.write(addr)
        except Exception:
            pass

    # ── Offline cache ────────────────────────────────────────────────────────

    def _cache_dir(self):
        d = self._offline_tracks_dir.strip() or os.path.join(self.user_data_dir, 'tracks')
        os.makedirs(d, exist_ok=True)
        return d

    def _cache_path(self, track_id):
        return os.path.join(self._cache_dir(), str(track_id))

    def _cache_meta_file(self):
        return os.path.join(self.user_data_dir, 'cached_tracks_meta.json')

    def _save_track_meta(self, tracks):
        """Persist track metadata so cached tracks survive server disconnects/restarts."""
        try:
            existing = {}
            try:
                with open(self._cache_meta_file()) as f:
                    existing = {t['track_id']: t for t in json.load(f) if t.get('track_id')}
            except Exception:
                pass
            for t in tracks:
                tid = t.get('track_id')
                if tid:
                    existing[tid] = {k: t[k] for k in
                                     ('idx', 'title', 'band', 'album', 'track_id', 'server_addr')
                                     if k in t}
            with open(self._cache_meta_file(), 'w') as f:
                json.dump(list(existing.values()), f)
        except Exception:
            pass

    def _load_offline_tracks(self):
        """Populate _all_tracks with locally cached tracks (runs at startup)."""
        if not self._cached_ids:
            return
        try:
            with open(self._cache_meta_file()) as f:
                all_meta = json.load(f)
        except Exception:
            return
        cached_tracks = [
            dict(t, is_active=False)
            for t in all_meta
            if t.get('track_id') in self._cached_ids
        ]
        if cached_tracks:
            self._all_tracks = cached_tracks
            self._filtered   = list(cached_tracks)
            n = len(cached_tracks)
            Clock.schedule_once(lambda _: self._set_list_data(self._filtered))
            Clock.schedule_once(lambda _: setattr(
                self._root.ids.now_playing, 'text',
                f'{n} Tracks offline verfügbar'))

    def _load_cached_ids(self, *_):
        d = self._cache_dir()
        if os.path.isdir(d):
            self._cached_ids = {
                f for f in os.listdir(d)
                if os.path.getsize(os.path.join(d, f)) > 0
            }
        else:
            self._cached_ids = set()
        self._load_offline_tracks()

    def download_track_by_id(self, idx, track_id):
        if not track_id or track_id in self._cached_ids:
            return
        threading.Thread(
            target=self._do_download, args=(idx, track_id), daemon=True
        ).start()

    def _do_download(self, idx, track_id):
        url  = f'http://{self._server_host}:{self._soap_port}/audio/{idx}'
        dest = self._cache_path(track_id)
        tmp  = dest + '.tmp'
        # look up title for status display
        title = next((t['title'] for t in self._all_tracks if t['track_id'] == track_id), str(idx))
        try:
            resp = urllib.request.urlopen(url, timeout=60)
            total = int(resp.headers.get('Content-Length', 0))
            downloaded = 0
            with open(tmp, 'wb') as f:
                while chunk := resp.read(65536):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total > 0:
                        pct = int(downloaded * 100 / total)
                        Clock.schedule_once(lambda _, p=pct, t=title:
                            setattr(self._root.ids.now_playing, 'text',
                                    f'dl {t[:30]} {p}%'))
            resp.close()
            os.rename(tmp, dest)
            self._cached_ids.add(track_id)
            Clock.schedule_once(lambda _: self._refresh_cache_markers())
        except Exception as e:
            try:
                os.remove(tmp)
            except Exception:
                pass
            Clock.schedule_once(
                lambda _: setattr(self._root.ids.now_playing, 'text',
                                  f'[X] Download: {e}'))

    def download_album(self, album):
        for t in self._all_tracks:
            if t['album'] == album and t['track_id'] not in self._cached_ids:
                self.download_track_by_id(t['idx'], t['track_id'])

    def _on_track_cached_by_proxy(self, track_id):
        """Called on main thread when the proxy has finished saving a track to cache."""
        self._cached_ids.add(track_id)
        self._refresh_cache_markers()

    def _refresh_cache_markers(self):
        self._set_list_data(self._filtered)

    def toggle_offline_filter(self):
        self._offline_only = not self._offline_only
        self.log(f'user: offline-filter {"AN" if self._offline_only else "AUS"}')
        self._root.ids.offline_btn.color = (
            (.3, .9, .3, 1) if self._offline_only else (.45, .45, .45, 1)
        )
        self._apply_filters()

    def toggle_band(self, band):
        if band in self._expanded_bands:
            self._expanded_bands.discard(band)
            # collapse all albums of this band too
            prefix = band + '::'
            for key in list(self._expanded_albums):
                if key.startswith(prefix):
                    self._expanded_albums.discard(key)
        else:
            self._expanded_bands.add(band)
        self._apply_filters()

    def toggle_album(self, album_key):
        if album_key in self._expanded_albums:
            self._expanded_albums.discard(album_key)
        else:
            self._expanded_albums.add(album_key)
        self._apply_filters()

    def _apply_filters(self):
        q = self._root.ids.search_input.text.lower().strip()
        result = self._all_tracks
        if q:
            result = [
                t for t in result
                if q in t['title'].lower()
                or q in t['band'].lower()
                or q in t['album'].lower()
            ]
        if self._offline_only:
            result = [t for t in result if t['track_id'] in self._cached_ids]
        self._filtered = result
        self._apply_active_marker()
        self._set_list_data(self._filtered)

    def autodiscover(self):
        """Listen for UDP broadcast from server, then connect automatically."""
        self._root.ids.now_playing.text = 'Suche Server …'
        threading.Thread(target=self._do_autodiscover, daemon=True).start()

    def _do_autodiscover(self):
        """Listen for UDP broadcasts for up to 4 seconds, collect all unique servers."""
        import socket as _sock
        found = {}
        try:
            s = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
            s.setsockopt(_sock.SOL_SOCKET, _sock.SO_REUSEADDR, 1)
            s.bind(('', 8768))
            s.settimeout(1.0)
            deadline = time.time() + 4.0
            while time.time() < deadline:
                try:
                    data, addr = s.recvfrom(64)
                    msg = data.decode().strip()
                    if msg.startswith('OWNLY:'):
                        port = msg.split(':')[1]
                        found[f'{addr[0]}:{port}'] = True
                except _sock.timeout:
                    pass
            s.close()
        except Exception:
            pass
        if found:
            hosts = list(found.keys())
            Clock.schedule_once(lambda _: self._on_discovered_multi(hosts))
        else:
            Clock.schedule_once(lambda _: self._on_discover_fail())

    def _on_discovered_multi(self, hosts):
        for h in hosts:
            self._add_server(h)
        if self._conn_popup:
            self._populate_servers_list()
        if not self._current_addr:
            # Auto-connect to first found server if not yet connected
            self.connect(hosts[0])
        else:
            n = len(hosts)
            label = 'Server' if n == 1 else 'Server'
            self._root.ids.now_playing.text = (
                f'{n} {label} gefunden — im Menu wechseln.'
            )

    def _on_discover_fail(self):
        self._root.ids.now_playing.text = '[X] Kein Server gefunden (5s)'

    def _apply_android_insets(self, *_):
        from kivy.core.window import Window
        from kivy.metrics import dp
        sb = getattr(Window, 'statusbar_height', dp(28))
        self._root.padding = [0, sb, 0, dp(52)]

    # ── Connection ──────────────────────────────────────────────────────────

    def connect(self, addr):
        addr = addr.strip()
        if not addr:
            return
        self._current_addr = addr
        self._add_server(addr)
        if ':' in addr:
            host, port = addr.rsplit(':', 1)
            self._server_host = host
            self._soap_port   = int(port)
        else:
            self._server_host = addr
        try:
            self._root.ids.status_dot.dot_color = (.9, .7, .1, 1)
            self._root.ids.now_playing.text = 'Verbinde …'
        except Exception:
            pass
        threading.Thread(
            target=self._do_connect,
            args=(self._server_host, self._soap_port, addr),
            daemon=True,
        ).start()

    def _do_connect(self, host, port, addr):
        try:
            root = _soap_request(host, port, 'GetTracks')
            tracks = []
            for item in root.iter():
                if item.tag.split('}')[-1] != 'TrackInfo':
                    continue
                tracks.append({
                    'idx':        int(_soap_text(item, 'idx') or 0),
                    'track_id':   _soap_text(item, 'id'),
                    'title':      _soap_text(item, 'title'),
                    'band':       _soap_text(item, 'band'),
                    'album':      _soap_text(item, 'album'),
                    'genre':      _soap_text(item, 'genre'),
                    'is_active':  False,
                    'server_addr': addr,   # track which server this came from
                })
            Clock.schedule_once(lambda _: self._on_connected(tracks))
        except BaseException as e:
            msg = f'{host}:{port} → {str(e) or type(e).__name__}'
            Clock.schedule_once(lambda _: self._on_error(msg))

    def _build_grouped_data(self, tracks):
        """Build flat list with BandHeader / AlbumHeader / TrackRow entries."""
        from collections import OrderedDict
        from kivy.metrics import dp
        grouped = OrderedDict()
        for t in tracks:
            band  = t.get('band', '?')
            album = t.get('album', '?')
            grouped.setdefault(band, OrderedDict()).setdefault(album, []).append(t)

        result = []
        for band, albums in grouped.items():
            band_expanded = band in self._expanded_bands
            result.append({
                'viewclass': 'BandHeader', 'band': band,
                'height': dp(38), 'is_expanded': band_expanded,
            })
            if not band_expanded:
                continue
            for album, album_tracks in albums.items():
                album_key    = f'{band}::{album}'
                album_expanded = album_key in self._expanded_albums
                album_cached = bool(album_tracks) and all(
                    t.get('track_id') in self._cached_ids for t in album_tracks
                )
                result.append({
                    'viewclass': 'AlbumHeader', 'album': album,
                    'album_key': album_key,
                    'height': dp(30), 'album_cached': album_cached,
                    'is_expanded': album_expanded,
                })
                if not album_expanded:
                    continue
                for t in album_tracks:
                    result.append(dict(
                        t,
                        viewclass='TrackRow',
                        height=dp(54),
                        is_cached=(t.get('track_id') in self._cached_ids),
                    ))
        return result

    def _set_list_data(self, tracks):
        self._root.ids.track_list.data = self._build_grouped_data(tracks)

    def _on_connected(self, tracks):
        addr = tracks[0]['server_addr'] if tracks else self._current_addr
        # Keep tracks from other sources (different server or offline-only cached)
        other_tracks = [t for t in self._all_tracks if t.get('server_addr') != addr]
        self._all_tracks = other_tracks + tracks
        self._filtered   = list(self._all_tracks)
        self._set_list_data(self._filtered)
        self._root.ids.status_dot.dot_color = (.2, .9, .3, 1)
        n = len(tracks)
        self._root.ids.now_playing.text = f'{n} Tracks geladen'
        if self._current_addr:
            self._save_host(self._current_addr)
        self._save_track_meta(tracks)

    def _on_error(self, msg):
        try:
            self._root.ids.status_dot.dot_color = (.9, .2, .2, 1)
            self._root.ids.now_playing.text = f'[X] {msg[:70]}'
        except Exception:
            pass

    def _on_server_gone(self, server_addr):
        """Remove non-cached tracks from unreachable server; keep locally cached ones."""
        self._all_tracks = [
            t for t in self._all_tracks
            if t.get('server_addr') != server_addr
            or t.get('track_id') in self._cached_ids
        ]
        self._apply_filters()   # rebuilds _filtered from _all_tracks
        try:
            self._root.ids.status_dot.dot_color = (.9, .2, .2, 1)
            n = len(self._all_tracks)
            if n:
                self._root.ids.now_playing.text = f'[X] {server_addr} offline – {n} Tracks'
            else:
                self._root.ids.now_playing.text = f'[X] {server_addr} offline'
        except Exception:
            pass
        if self._filtered:
            self._auto_next()
        else:
            self._reset_progress()

    # ── Search / filter ─────────────────────────────────────────────────────

    def filter_tracks(self, query):
        self._apply_filters()

    # ── Playback ─────────────────────────────────────────────────────────────

    def _acquire_wifi_lock(self):
        """Keep WiFi radio awake so streaming proxy doesn't lose connection.
        Uses WIFI_MODE_FULL (= 1) which works with display off.
        WIFI_MODE_FULL_HIGH_PERF requires screen-on since API 29."""
        try:
            from jnius import autoclass as _ac  # type: ignore
            PythonActivity = _ac('org.kivy.android.PythonActivity')
            wm = PythonActivity.mActivity.getSystemService(
                PythonActivity.mActivity.WIFI_SERVICE)
            if self._wifi_lock is None:
                self._wifi_lock = wm.createWifiLock(1, 'OwnlyAudioPocket::Stream')  # 1 = WIFI_MODE_FULL
            if not self._wifi_lock.isHeld():
                self._wifi_lock.acquire()
        except Exception:
            pass

    def _release_wifi_lock(self):
        try:
            if self._wifi_lock and self._wifi_lock.isHeld():
                self._wifi_lock.release()
        except Exception:
            pass

    def _start_audio_service(self, track_label=''):
        """Start the Java foreground service (main process) to keep app alive in background."""
        if platform != 'android':
            return
        try:
            from jnius import autoclass
            from android.runnable import run_on_ui_thread  # type: ignore
            PythonActivity       = autoclass('org.kivy.android.PythonActivity')
            Intent               = autoclass('android.content.Intent')
            Build                = autoclass('android.os.Build$VERSION')
            ForegroundAudioSvc   = autoclass('de.ownly.ownlyaudiopocket.ForegroundAudioService')
            intent = Intent(PythonActivity.mActivity, ForegroundAudioSvc)
            intent.putExtra('track', track_label)

            @run_on_ui_thread
            def _do_start():
                try:
                    if Build.SDK_INT >= 26:
                        PythonActivity.mActivity.startForegroundService(intent)
                    else:
                        PythonActivity.mActivity.startService(intent)
                    self.log('audio_service: gestartet')
                except Exception as e:
                    self.log(f'audio_service: startForegroundService FEHLER: {e}')

            _do_start()
        except Exception as e:
            self.log(f'audio_service: setup FEHLER: {e}')
        # Hold CPU awake for playback — ExoPlayer's setWakeMode may not work via pyjnius
        self._acquire_playback_wake_lock()

    def _stop_audio_service(self):
        """Stop the Java foreground service."""
        self._release_playback_wake_lock()
        if platform != 'android':
            return
        try:
            from jnius import autoclass
            PythonActivity       = autoclass('org.kivy.android.PythonActivity')
            Intent               = autoclass('android.content.Intent')
            ForegroundAudioSvc   = autoclass('de.ownly.ownlyaudiopocket.ForegroundAudioService')
            PythonActivity.mActivity.stopService(
                Intent(PythonActivity.mActivity, ForegroundAudioSvc))
        except Exception:
            pass

    def play_idx(self, server_idx):
        """Start playing the track identified by server-side idx."""
        track = next((t for t in self._all_tracks if t['idx'] == server_idx), None)
        self.log(f'user: play_idx {server_idx} ({track["title"][:25] if track else "?"})')
        self._active_srv_idx = server_idx
        self._apply_active_marker()

        track = next((t for t in self._all_tracks if t['idx'] == server_idx), None)
        if not track:
            return

        label = f'{track["title"]}  —  {track["band"]}'
        self._root.ids.now_playing.text = f'{label}'
        self._root.ids.play_btn.text = '||'

        if self._sound:
            if platform == 'android':
                # ExoPlayer must only be released on its own Looper thread.
                # Stash for release inside _setup_exoplayer (via Clock.schedule_once).
                self._old_sound = self._sound
                self._sound = None
                self._stop_progress_clock()
            else:
                try:
                    self._sound.unbind(on_stop=self._on_track_ended)
                    self._sound.stop()
                except Exception:
                    pass
                self._sound = None

        # Stop any previous local proxy
        if self._proxy:
            self._proxy.stop()
            self._proxy = None

        # Clean up previous temp file
        if self._tmp_file and os.path.exists(self._tmp_file):
            try:
                os.unlink(self._tmp_file)
            except Exception:
                pass
            self._tmp_file = None

        # Use local cached file if available, otherwise stream from server
        server_addr = track.get('server_addr', self._current_addr)
        local_path = self._cache_path(track.get('track_id', ''))
        if track.get('track_id') and os.path.isfile(local_path):
            url = local_path
        else:
            url = f'http://{self._server_host}:{self._soap_port}/audio/{server_idx}'
        if platform == 'android':
            if url.startswith('http'):
                # Stream via local proxy: ExoPlayer connects to 127.0.0.1 (cleartext
                # always allowed), proxy fetches remote URL via Python urllib.
                # If auto_cache_on_play is on, the proxy saves while streaming —
                # no separate download thread needed (avoids competing connections).
                cache_dest = None
                tid = track.get('track_id')
                if self.auto_cache_on_play and tid and tid not in self._cached_ids:
                    cache_dest = self._cache_path(tid)
                def _status(msg):
                    self.log(msg)

                proxy = _LocalProxy(url,
                                    cache_dest=cache_dest,
                                    track_id=tid,
                                    on_cached=self._on_track_cached_by_proxy,
                                    on_debug=_status)
                self._proxy = proxy
                proxy.start()
                self.log(f'proxy:{proxy.port} → {url[-35:]}')
                Clock.schedule_once(
                    lambda _: self._setup_exoplayer(f'http://127.0.0.1:{proxy.port}/', label))
            else:
                Clock.schedule_once(lambda _: self._setup_exoplayer(url, label))
        else:
            threading.Thread(
                target=self._fetch_and_play,
                args=(url, label, server_addr),
                daemon=True
            ).start()

    def _acquire_dl_wake_lock(self):
        """Acquire PARTIAL_WAKE_LOCK so download threads keep running with screen off."""
        try:
            from jnius import autoclass as _ac  # type: ignore
            PowerManager = _ac('android.os.PowerManager')
            PythonActivity = _ac('org.kivy.android.PythonActivity')
            pm = PythonActivity.mActivity.getSystemService(
                PythonActivity.mActivity.POWER_SERVICE)
            if self._dl_wake_lock is None:
                self._dl_wake_lock = pm.newWakeLock(
                    PowerManager.PARTIAL_WAKE_LOCK, 'OwnlyAudioPocket::Download')
            if not self._dl_wake_lock.isHeld():
                self._dl_wake_lock.acquire()
        except Exception:
            pass

    def _acquire_playback_wake_lock(self):
        """Acquire PARTIAL_WAKE_LOCK so the CPU stays awake during background playback."""
        if platform != 'android':
            return
        try:
            from jnius import autoclass as _ac  # type: ignore
            PowerManager  = _ac('android.os.PowerManager')
            PythonActivity = _ac('org.kivy.android.PythonActivity')
            pm = PythonActivity.mActivity.getSystemService(
                PythonActivity.mActivity.POWER_SERVICE)
            if self._playback_wake_lock is None:
                self._playback_wake_lock = pm.newWakeLock(
                    PowerManager.PARTIAL_WAKE_LOCK, 'OwnlyAudioPocket::Playback')
            if not self._playback_wake_lock.isHeld():
                self._playback_wake_lock.acquire()
            self.log('wakelock: playback gehalten')
        except Exception as e:
            self.log(f'wakelock: playback FEHLER: {e}')

    def _release_playback_wake_lock(self):
        try:
            if self._playback_wake_lock and self._playback_wake_lock.isHeld():
                self._playback_wake_lock.release()
                self.log('wakelock: playback freigegeben')
        except Exception:
            pass

    def _release_dl_wake_lock(self):
        try:
            if self._dl_wake_lock and self._dl_wake_lock.isHeld():
                self._dl_wake_lock.release()
        except Exception:
            pass

    def _download_then_play_android(self, url, label, server_addr):
        """Android: download via urllib to temp file, then play locally via ExoPlayer."""
        self._acquire_dl_wake_lock()
        try:
            tmp = tempfile.NamedTemporaryFile(suffix='.mp3', delete=False)
            tmp_path = tmp.name
            resp = urllib.request.urlopen(url, timeout=60)
            total = int(resp.headers.get('Content-Length', 0))
            downloaded = 0
            with resp:
                while chunk := resp.read(65536):
                    tmp.write(chunk)
                    downloaded += len(chunk)
                    if total > 0:
                        pct = int(downloaded * 100 / total)
                        Clock.schedule_once(lambda _, p=pct, l=label:
                            setattr(self._root.ids.now_playing, 'text',
                                    f'{l[:30]} {p}%'))
            tmp.close()
            self._tmp_file = tmp_path
            Clock.schedule_once(lambda _: self._setup_exoplayer(tmp_path, label))
        except urllib.error.HTTPError as e:
            # Server reachable but track missing/broken — skip to next
            Clock.schedule_once(lambda _: self._on_play_error(f'HTTP {e.code}: {label[:40]}'))
        except (urllib.error.URLError, OSError, ConnectionError, TimeoutError):
            # Server unreachable — remove all its tracks
            Clock.schedule_once(lambda _: self._on_server_gone(server_addr))
        except Exception as e:
            Clock.schedule_once(lambda _: self._on_play_error(str(e)))
        finally:
            self._release_dl_wake_lock()

    def _setup_exoplayer(self, url, label):
        """Builds ExoPlayer on a dedicated HandlerThread, independent of SDL2/Kivy main thread.

        SDL2 may block the Android main Looper when the activity is paused.
        Running ExoPlayer on its own HandlerThread ensures callbacks (onIsPlayingChanged etc.)
        always fire and ExoPlayer keeps playing regardless of Kivy/SDL2 state.
        """
        self.log(f'setup_exo: {url[-50:]}')
        self._acquire_wifi_lock()

        if platform != 'android':
            # Should not be reached on desktop (separate path in play_idx), but guard.
            return

        from android.runnable import run_on_ui_thread  # type: ignore

        @run_on_ui_thread
        def _on_ui_thread():
            try:
                from jnius import autoclass  # type: ignore

                # Create a dedicated HandlerThread for ExoPlayer once — survives between tracks.
                if self._exo_handler is None:
                    HandlerThread = autoclass('android.os.HandlerThread')
                    Handler = autoclass('android.os.Handler')
                    ht = HandlerThread('ExoPlayerThread')
                    ht.start()
                    self._exo_handler = Handler(ht.getLooper())
                    self.log('exo: HandlerThread gestartet')

                ht_looper = self._exo_handler.getLooper()
                PythonActivity = autoclass('org.kivy.android.PythonActivity')
                activity = PythonActivity.mActivity

                DefaultHttpDataSourceFactory = autoclass(
                    'androidx.media3.datasource.DefaultHttpDataSource$Factory')
                DefaultDataSourceFactory = autoclass(
                    'androidx.media3.datasource.DefaultDataSource$Factory')
                ProgressiveMediaSourceFactory = autoclass(
                    'androidx.media3.exoplayer.source.ProgressiveMediaSource$Factory')

                # 30s HTTP timeout; wrap in DefaultDataSource so file:// also works.
                http_dsf = DefaultHttpDataSourceFactory()
                http_dsf.setConnectTimeoutMs(30000)
                http_dsf.setReadTimeoutMs(30000)
                dsf = DefaultDataSourceFactory(activity, http_dsf)
                msf = ProgressiveMediaSourceFactory(dsf)

                media_url = f'file://{url}' if url.startswith('/') else url
                ExoRunnableClass = _get_exo_runnable_class()

                def _build_and_play():
                    """Runs on ExoPlayerThread — ALL ExoPlayer calls must happen here."""
                    try:
                        self.log('exo: ui_thread start')
                        ExoPlayerBuilder = autoclass('androidx.media3.exoplayer.ExoPlayer$Builder')
                        MediaItem = autoclass('androidx.media3.common.MediaItem')
                        C = autoclass('androidx.media3.common.C')

                        # Release old players on the ExoPlayer thread (correct looper).
                        for old in (self._old_sound, self._sound):
                            if old is not None:
                                try:
                                    old.release()
                                except Exception:
                                    pass
                        self._old_sound = None
                        self._sound = None

                        player = ExoPlayerBuilder(activity) \
                            .setLooper(ht_looper) \
                            .setMediaSourceFactory(msf) \
                            .build()

                        # PARTIAL_WAKE_LOCK + WifiLock: keep CPU/WiFi alive with screen off.
                        player.setWakeMode(C.WAKE_MODE_NETWORK)
                        # Disable becoming-noisy pause: don't stop when audio routing changes.
                        try:
                            player.setHandleAudioBecomingNoisy(False)
                            self.log('exo: BecomingNoisy deaktiviert')
                        except Exception as _e:
                            self.log(f'exo: BecomingNoisy warn: {_e}')
                        self.log('exo: player gebaut')

                        # handleAudioFocus=False: keep playing when other apps take audio focus.
                        try:
                            AudioAttributes = autoclass('androidx.media3.common.AudioAttributes')
                            try:
                                AudioAttributesBuilder = autoclass(
                                    'androidx.media3.common.AudioAttributes$Builder')
                                audio_attrs = AudioAttributesBuilder() \
                                    .setUsage(C.USAGE_MEDIA) \
                                    .setContentType(C.AUDIO_CONTENT_TYPE_MUSIC) \
                                    .build()
                            except Exception:
                                # Builder inner class unavailable — fall back to DEFAULT.
                                audio_attrs = AudioAttributes.DEFAULT
                            player.setAudioAttributes(audio_attrs, False)
                            self.log('exo: AudioAttributes gesetzt (handleAudioFocus=False)')
                        except Exception as _e:
                            self.log(f'exo: AudioAttributes fehlgeschlagen: {_e}')

                        ExoListenerClass = _get_exo_listener_class()
                        self._mp_listener = ExoListenerClass(
                            lambda: setattr(self, '_pending_auto_next', True),
                            lambda msg: Clock.schedule_once(lambda _dt: self._on_play_error(msg)),
                            lambda s: self.log(s),
                        )
                        player.addListener(self._mp_listener)

                        player.setMediaItem(MediaItem.fromUri(media_url))
                        player.prepare()
                        player.play()
                        self.log('exo: prepare+play OK')

                        self._sound = player
                        self._exo_playing = True

                        # Back to Kivy thread for UI updates.
                        Clock.schedule_once(lambda _: self._on_exo_started(label))
                    except Exception as e:
                        self.log(f'exo: FEHLER in HandlerThread: {e}')
                        Clock.schedule_once(lambda _dt: self._on_play_error(str(e)))

                self._exo_handler.post(ExoRunnableClass(_build_and_play))
            except Exception as e:
                self.log(f'exo: FEHLER in ui_thread: {e}')
                Clock.schedule_once(lambda _dt: self._on_play_error(str(e)))

        _on_ui_thread()

    def _on_exo_started(self, label):
        """Kivy-thread callback after ExoPlayer is set up on the UI thread."""
        self._root.ids.now_playing.text = f'> {label}'
        self._root.ids.play_btn.text = '||'
        self._start_progress_clock()
        self._start_audio_service(label)

    def _start_progress_clock(self):
        self._stop_progress_clock()
        self._progress_clock = Clock.schedule_interval(self._update_progress, 1.0)

    def _stop_progress_clock(self):
        if self._progress_clock:
            self._progress_clock.cancel()
            self._progress_clock = None

    def _update_progress(self, dt):
        player = self._sound
        if player is None:
            self._stop_progress_clock()
            return
        try:
            if platform == 'android':
                pos_ms = player.getCurrentPosition()   # ms
                dur_ms = player.getDuration()          # ms (negative if unknown)
            else:
                # Kivy SoundLoader: get_pos() → seconds, length → seconds
                pos_ms = int(player.get_pos() * 1000)
                dur_ms = int((player.length or 0) * 1000)

            if dur_ms > 0:
                self._root.ids.progress_bar.value = int(pos_ms * 1000 / dur_ms)
                self._root.ids.time_label.text = (
                    f'{pos_ms//60000}:{(pos_ms//1000)%60:02d} / '
                    f'{dur_ms//60000}:{(dur_ms//1000)%60:02d}'
                )
            else:
                self._root.ids.progress_bar.value = 0
                self._root.ids.time_label.text = f'{pos_ms//60000}:{(pos_ms//1000)%60:02d}'
        except Exception as e:
            # Show error instead of silently killing the clock — helps debugging
            self._root.ids.time_label.text = f'dbg:{e}'

    def _reset_progress(self):
        self._stop_progress_clock()
        self._root.ids.progress_bar.value = 0
        self._root.ids.time_label.text = ''

    def _fetch_and_play(self, url, label, server_addr):
        """Desktop: download to temp file, then load and play."""
        try:
            tmp = tempfile.NamedTemporaryFile(suffix='.mp3', delete=False)
            tmp_path = tmp.name
            with urllib.request.urlopen(url, timeout=60) as resp:
                while chunk := resp.read(65536):
                    tmp.write(chunk)
            tmp.close()
            self._tmp_file = tmp_path
            # Load in background thread to keep main thread free
            sound = SoundLoader.load(tmp_path)
            Clock.schedule_once(lambda _: self._play_file(sound, label))
        except urllib.error.HTTPError as e:
            Clock.schedule_once(lambda _: self._on_play_error(f'HTTP {e.code}: {label[:40]}'))
        except (urllib.error.URLError, OSError, ConnectionError, TimeoutError):
            Clock.schedule_once(lambda _: self._on_server_gone(server_addr))
        except Exception as e:
            Clock.schedule_once(lambda _: self._on_play_error(str(e)))

    def _play_file(self, sound, label):
        self._sound = sound
        if self._sound:
            self._sound.bind(on_stop=self._on_track_ended)
            self._sound.play()
            self._root.ids.now_playing.text = f'> {label}'
            self._start_progress_clock()
        else:
            self._root.ids.now_playing.text = f'[X] Kein Audio-Backend: {label}'
            self._root.ids.play_btn.text = '>'

    def _on_play_error(self, msg):
        self.log(f'[X] {msg}')
        self._exo_playing = False
        self._reset_progress()
        self._root.ids.now_playing.text = f'[X] {msg[:60]}'
        self._root.ids.play_btn.text = '>'

    def _on_track_ended(self, *_):
        self._exo_playing = False
        Clock.schedule_once(lambda _: self._auto_next())

    def _auto_next_watcher(self):
        """Daemon thread: polls _pending_auto_next every 200ms.

        Python daemon threads are NOT paused when Android turns the display off —
        only the SDL/Kivy main thread pauses. This ensures the next track starts
        even with the screen off.
        """
        import time as _time
        _heartbeat = 0
        while True:
            _time.sleep(0.2)
            _heartbeat += 1
            if _heartbeat >= 150:   # every 30s
                _heartbeat = 0
                self.log('watcher: alive')
                # Poll ExoPlayer state via its HandlerThread (thread-safe).
                try:
                    _handler = self._exo_handler
                    if _handler is not None and self._exo_playing:
                        ExoRunnableClass = _get_exo_runnable_class()
                        def _poll():
                            try:
                                _p = self._sound
                                _ep = self._exo_playing
                                if _p is not None:
                                    _ip = bool(_p.isPlaying())
                                    _pwr = bool(_p.getPlayWhenReady())
                                    self.log(f'watcher: poll exo_playing={_ep} isPlaying={_ip} pwr={_pwr}')
                                    # Auto-resume if paused unexpectedly
                                    if _ep and not _ip and _pwr:
                                        _p.play()
                                        self.log('watcher: auto-resume play()')
                                else:
                                    self.log(f'watcher: poll _sound=None exo_playing={_ep}')
                            except Exception as _e:
                                self.log(f'watcher: state poll err: {_e}')
                        _handler.post(ExoRunnableClass(_poll))
                except Exception:
                    pass
            if not self._pending_auto_next:
                continue
            self._pending_auto_next = False
            self.log('watcher: auto_next ausgelöst')
            if platform == 'android':
                self._trigger_next_android_bg()
            else:
                # Desktop: SDL is never paused, safe to use Clock
                Clock.schedule_once(lambda _: self._auto_next())

    def _start_server_keepalive(self, server_url):
        """Ping server every 20s so WiFi stays awake between track downloads.

        Called when the current track's proxy download finishes. Keeps running
        until _trigger_next_android_bg() stops it at the start of the next track.
        The result: when auto-next fires, WiFi is already up and the proxy
        connects to the server in milliseconds instead of ~60 seconds.
        """
        import threading as _th
        from urllib.parse import urlparse as _up
        # Stop any previous keepalive
        self._stop_server_keepalive()
        try:
            p = _up(server_url)
            ping_url = f'{p.scheme}://{p.netloc}/audio/0'
        except Exception:
            return
        stop = _th.Event()
        self._keepalive_stop = stop
        def _run():
            import urllib.request as _ur
            self.log('keepalive: start')
            while not stop.wait(20):
                try:
                    req = _ur.Request(ping_url)
                    req.get_method = lambda: 'HEAD'
                    _ur.urlopen(req, timeout=4)
                except Exception:
                    pass
            self.log('keepalive: stop')
        _th.Thread(target=_run, daemon=True).start()

    def _stop_server_keepalive(self):
        ev = getattr(self, '_keepalive_stop', None)
        if ev:
            ev.set()
        self._keepalive_stop = None

    def _trigger_next_android_bg(self):
        """Start the next track from a background thread (safe when SDL is paused).

        Does not touch Kivy UI directly — all UI updates are queued via
        Clock.schedule_once and fire when the user brings the app back to
        foreground. Audio playback starts immediately via @run_on_ui_thread.
        """
        # Stop WiFi keepalive from previous track — new proxy will take over
        self._stop_server_keepalive()
        # Acquire WiFi lock early so the radio is awake before the proxy needs it
        self._acquire_wifi_lock()

        if not self._filtered:
            self.log('auto_next: keine Tracks')
            return

        if self._offline_only and self._servers:
            threading.Thread(target=self._probe_servers_for_reconnect, daemon=True).start()

        if self._shuffle:
            candidates = [t for t in self._filtered if t['idx'] != self._active_srv_idx]
            nxt = random.choice(candidates) if candidates else self._filtered[0]
        else:
            cur_pos = next(
                (i for i, t in enumerate(self._filtered) if t['idx'] == self._active_srv_idx), -1
            )
            nxt = self._filtered[(cur_pos + 1) % len(self._filtered)]

        idx = nxt['idx']
        track = next((t for t in self._all_tracks if t['idx'] == idx), None)
        if not track:
            return

        label = f'{track["title"]}  —  {track["band"]}'
        self.log(f'auto_next → {track["title"][:30]}')
        self._active_srv_idx = idx

        # Stash old player for cleanup inside _setup_exoplayer (on UI thread)
        self._old_sound = self._sound
        self._sound = None

        # Stop old proxy
        if self._proxy:
            self._proxy.stop()
            self._proxy = None

        # Determine URL (local cache or remote stream)
        local_path = self._cache_path(track.get('track_id', ''))
        if track.get('track_id') and os.path.isfile(local_path):
            exo_url = local_path
        else:
            exo_url = f'http://{self._server_host}:{self._soap_port}/audio/{idx}'

        if exo_url.startswith('http'):
            tid = track.get('track_id')
            cache_dest = None
            if self.auto_cache_on_play and tid and tid not in self._cached_ids:
                cache_dest = self._cache_path(tid)
            proxy = _LocalProxy(exo_url,
                                cache_dest=cache_dest,
                                track_id=tid,
                                on_cached=self._on_track_cached_by_proxy,
                                on_debug=self.log,
                                on_done=lambda u=exo_url: self._start_server_keepalive(u))
            self._proxy = proxy
            proxy.start()
            self.log(f'proxy:{proxy.port} → {exo_url[-35:]}')
            exo_url = f'http://127.0.0.1:{proxy.port}/'

        # _setup_exoplayer is safe from any thread: no Kivy UI access,
        # inner @run_on_ui_thread posts to Android Looper (always running).
        self._setup_exoplayer(exo_url, label)
        self._start_audio_service(label)

        # Queue UI updates — fired when SDL resumes (user unlocks screen)
        def _ui_sync(_dt, _label=label, _idx=idx):
            self._stop_progress_clock()
            self._apply_active_marker()
            try:
                self._root.ids.now_playing.text = f'> {_label}'
                self._root.ids.play_btn.text = '||'
            except Exception:
                pass
        Clock.schedule_once(_ui_sync)

    def _auto_next(self):
        self._reset_progress()
        if not self._filtered:
            self.log('auto_next: keine Tracks in Liste')
            return
        # If offline, silently probe known servers — reconnect if one responds
        if self._offline_only and self._servers:
            threading.Thread(target=self._probe_servers_for_reconnect, daemon=True).start()
        if self._shuffle:
            # pick random, avoid repeating same track
            candidates = [t for t in self._filtered if t['idx'] != self._active_srv_idx]
            nxt = random.choice(candidates) if candidates else self._filtered[0]
        else:
            cur_pos = next(
                (i for i, t in enumerate(self._filtered) if t['idx'] == self._active_srv_idx), -1
            )
            nxt = self._filtered[(cur_pos + 1) % len(self._filtered)]
        self.log(f'auto_next → {nxt["title"][:30]}')
        self.play_idx(nxt['idx'])

    def _probe_servers_for_reconnect(self):
        """Background: try each known server; if one responds, reconnect silently."""
        for srv in list(self._servers):
            addr = srv['addr']
            try:
                host, port = (addr.rsplit(':', 1) + ['8767'])[:2]
                url = f'http://{host}:{port}/audio/0'
                req = urllib.request.Request(url)
                req.get_method = lambda: 'HEAD'
                urllib.request.urlopen(req, timeout=2)
                # Server is up — reconnect on main thread
                Clock.schedule_once(lambda _, a=addr: self.connect(a))
                return
            except Exception:
                continue

    def _exo_pause_resume(self):
        """Pause or resume ExoPlayer on the Android UI thread."""
        from android.runnable import run_on_ui_thread  # type: ignore

        @run_on_ui_thread
        def _on_ui():
            try:
                if self._exo_playing:
                    self._sound.pause()
                    self._exo_playing = False
                    Clock.schedule_once(lambda _: (
                        self._stop_progress_clock() or
                        setattr(self._root.ids.play_btn, 'text', '>')
                    ))
                else:
                    self._sound.play()
                    self._exo_playing = True
                    Clock.schedule_once(lambda _: (
                        self._start_progress_clock() or
                        setattr(self._root.ids.play_btn, 'text', '||')
                    ))
            except Exception as e:
                Clock.schedule_once(lambda _dt: setattr(
                    self._root.ids.now_playing, 'text', f'[X] pause: {e}'))

        _on_ui()

    def toggle_play(self):
        self.log('user: play/pause')
        if not self._sound:
            return
        if platform == 'android':
            self._exo_pause_resume()
            return
        # Kivy SoundLoader path (desktop)
        if self._sound.state == 'play':
            self._sound.unbind(on_stop=self._on_track_ended)
            self._sound.stop()
            self._root.ids.play_btn.text = '>'
        else:
            self._sound.bind(on_stop=self._on_track_ended)
            self._sound.play()
            self._root.ids.play_btn.text = '||'

    def next_track(self):
        self.log('user: weiter')
        self._auto_next()

    def prev_track(self):
        self.log('user: zurück')
        if not self._filtered:
            return
        cur_pos = next(
            (i for i, t in enumerate(self._filtered) if t['idx'] == self._active_srv_idx), 0
        )
        nxt = self._filtered[(cur_pos - 1) % len(self._filtered)]
        self.play_idx(nxt['idx'])

    def toggle_shuffle(self):
        self._shuffle = not self._shuffle
        self.log(f'user: shuffle {"AN" if self._shuffle else "AUS"}')
        btn = self._root.ids.shuffle_btn
        btn.background_color = (.93, .4, .2, 1) if self._shuffle else (.18, .18, .18, 1)

    # ── QR scan ──────────────────────────────────────────────────────────────

    # ── Debug log ─────────────────────────────────────────────────────────────

    def _run_udp_log_sender(self):
        """Send every log line to log_server.py on the PC via UDP.

        UDP is stateless — no connection to maintain, no TCP handshake to lose
        when the app goes to background. Each log line is an independent datagram.
        """
        import socket as _s
        import time as _t
        _PORT = 9999
        sock = None

        while True:
            try:
                line = self._log_send_queue.get(timeout=30)
            except Exception:
                continue

            host = getattr(self, '_server_host', '')
            if not host:
                continue

            if sock is None:
                try:
                    sock = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
                except Exception:
                    _t.sleep(2)
                    continue

            try:
                sock.sendto((line + '\n').encode('utf-8', errors='replace'), (host, _PORT))
            except Exception:
                try:
                    sock.close()
                except Exception:
                    pass
                sock = None

    def log(self, msg):
        """Append a line to the debug log. Thread-safe via queue + direct file write + TCP."""
        ts = time.strftime('%H:%M:%S')
        line = f'[{ts}] {msg}'
        self._log_queue.put(line)
        # Write directly to file — bypasses Clock so background events are captured
        # even when the SDL thread is paused (screen off).
        if getattr(self, '_log_file', None):
            try:
                with self._log_file_lock:
                    with open(self._log_file, 'a') as _lf:
                        _lf.write(line + '\n')
            except Exception:
                pass
        # Send to PC log server (non-blocking)
        try:
            self._log_send_queue.put_nowait(line)
        except Exception:
            pass

    def _drain_log_queue(self, dt):
        """Called every 250ms on main thread — drains thread-safe log queue."""
        added = False
        while True:
            try:
                line = self._log_queue.get_nowait()
            except queue.Empty:
                break
            self._log_lines.append(line)
            added = True
        if len(self._log_lines) > 200:
            self._log_lines = self._log_lines[-200:]
        if added and self._log_popup:
            self._refresh_log_view()

    def _refresh_log_view(self):
        if not self._log_popup:
            return
        self._log_popup.ids.log_label.text = '\n'.join(self._log_lines)
        # scroll to bottom
        def _scroll(_dt=None):
            if self._log_popup:
                self._log_popup.ids.log_scroll.scroll_y = 0
        Clock.schedule_once(_scroll)

    def clear_log(self):
        self._log_lines = []
        if self._log_popup:
            self._log_popup.ids.log_label.text = ''

    def open_log_popup(self):
        if self._log_popup is None:
            self._log_popup = LogPopup()
            self._log_popup.bind(on_dismiss=lambda _: setattr(self, '_log_popup', None))
        self._refresh_log_view()
        self._log_popup.open()
        Clock.schedule_once(lambda _: self._refresh_log_view())

    def open_menu(self):
        MenuPopup().open()

    def open_settings(self):
        if self._settings_popup is None:
            self._settings_popup = SettingsPopup()
        self._settings_popup.ids.offline_dir_input.text = self._offline_tracks_dir or self._cache_dir()
        self._settings_popup.ids.offline_dir_status.text = ''
        self._settings_popup.open()

    def open_dir_chooser(self, target_input):
        """Open a directory browser; on confirm, fill target_input with the chosen path."""
        start = target_input.text.strip() or '/'
        if not os.path.isdir(start):
            start = os.path.expanduser('~')

        def _on_confirm(path):
            target_input.text = path

        popup = DirChooserPopup(on_confirm=_on_confirm, start_path=start)
        popup.open()

    def open_connections(self):
        if self._conn_popup is None:
            self._conn_popup = ConnectionsPopup()
        self._conn_popup.ids.host_input.text = ''
        self._populate_servers_list()
        self._conn_popup.open()

    def open_server_popup(self):
        if self._server_popup is None:
            self._server_popup = ServerPopup()
        saved_dir = self._load_server_music_dir()
        self._server_popup.ids.music_dir_input.text = saved_dir
        btn = self._server_popup.ids.toggle_btn
        if self._embedded_server.is_running:
            btn.text = 'Server stoppen'
            btn.background_color = (.55, .18, .18, 1)
        else:
            btn.text = 'Server starten'
            btn.background_color = (.18, .55, .18, 1)
        self._server_popup.open()

    def toggle_embedded_server(self, music_dir):
        music_dir = music_dir.strip() or '/sdcard/Music'
        self._save_server_music_dir(music_dir)

        if self._embedded_server.is_running:
            self._embedded_server.stop()
            if self._server_popup:
                self._server_popup.ids.toggle_btn.text = 'Server starten'
                self._server_popup.ids.toggle_btn.background_color = (.18, .55, .18, 1)
                self._server_popup.ids.srv_status.text = 'Server gestoppt'
        else:
            if platform == 'android':
                self._request_storage_and_start(music_dir)
            else:
                self._start_embedded_server(music_dir)

    def _request_storage_and_start(self, music_dir):
        try:
            from android.permissions import request_permissions, check_permission, Permission  # type: ignore
            perm = getattr(Permission, 'READ_MEDIA_AUDIO', None) or Permission.READ_EXTERNAL_STORAGE
            if check_permission(str(perm)):
                self._start_embedded_server(music_dir)
            else:
                def _cb(permissions, grants):
                    if grants and grants[0]:
                        Clock.schedule_once(lambda _: self._start_embedded_server(music_dir))
                    else:
                        if self._server_popup:
                            self._server_popup.ids.srv_status.text = 'Berechtigung verweigert'
                request_permissions([str(perm)], _cb)
        except ImportError:
            self._start_embedded_server(music_dir)

    def _start_embedded_server(self, music_dir):
        def _on_status(msg):
            if self._server_popup:
                self._server_popup.ids.srv_status.text = msg
            is_running = self._embedded_server.is_running
            if self._server_popup:
                btn = self._server_popup.ids.toggle_btn
                if is_running:
                    btn.text = 'Server stoppen'
                    btn.background_color = (.55, .18, .18, 1)
                else:
                    btn.text = 'Server starten'
                    btn.background_color = (.18, .55, .18, 1)
        self._embedded_server.start(music_dir, on_status=_on_status,
                                    log_file=getattr(self, '_log_file', None))

    def connect_to_self(self):
        """Connect this client to the embedded server running on this device."""
        addr = self._embedded_server.local_addr()
        self.connect(addr)

    def open_qr_scan(self):
        """Open QR scanner. On Android uses native ZXing intent, on desktop cv2 popup."""
        if platform == 'android':
            self._android_zxing_scan()
        else:
            popup = QRScanPopup(on_result=self._on_qr_scanned)
            popup.open()

    _QR_REQUEST_CODE = 0xC0DE

    def _android_zxing_scan(self):
        try:
            from android.permissions import check_permission, request_permissions  # type: ignore
        except Exception as e:
            self._root.ids.now_playing.text = f'QR perm: {e}'
            return

        if check_permission('android.permission.CAMERA'):
            self._launch_zxing()
        else:
            def _on_perm(permissions, grants):
                if grants and grants[0]:
                    Clock.schedule_once(lambda _dt: self._launch_zxing(), 0)
                else:
                    self._root.ids.now_playing.text = 'QR: Kamera-Berechtigung verweigert'
            request_permissions(['android.permission.CAMERA'], _on_perm)

    def _launch_zxing(self):
        try:
            from jnius import autoclass          # type: ignore
            from android import activity as _act # type: ignore

            PythonActivity = autoclass('org.kivy.android.PythonActivity')
            Intent         = autoclass('android.content.Intent')
            ComponentName  = autoclass('android.content.ComponentName')

            def on_result(req, result_code, intent_data):
                _act.unbind(on_activity_result=on_result)
                if result_code == -1 and intent_data:
                    data = intent_data.getStringExtra('SCAN_RESULT')
                    if data:
                        Clock.schedule_once(lambda _dt: self._on_qr_scanned(data), 0)

            _act.bind(on_activity_result=on_result)
            pkg    = PythonActivity.mActivity.getPackageName()
            intent = Intent('com.google.zxing.client.android.SCAN')
            intent.setComponent(ComponentName(
                pkg, 'com.journeyapps.barcodescanner.CaptureActivity'
            ))
            intent.putExtra('SCAN_FORMATS', 'QR_CODE')
            PythonActivity.mActivity.startActivityForResult(intent, 49374)
        except Exception as e:
            # Show full error so it's readable
            self._root.ids.now_playing.text = str(e)[:120]

    def _on_qr_scanned(self, data):
        """
        Extract IP from scanned URL (e.g. https://192.168.1.5:8765)
        and connect to SOAP port 8767 on that host.
        """
        m = re.search(r'https?://([0-9a-zA-Z._-]+)(?::(\d+))?', data)
        if not m:
            self._root.ids.now_playing.text = f'[X] Kein Server-Link: {data}'
            return
        host = m.group(1)
        addr = f'{host}:8767'
        self.connect(addr)

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _register_screen_receiver(self):
        """Register a BroadcastReceiver for screen on/off events (Android only)."""
        try:
            from android.broadcast import BroadcastReceiver  # type: ignore
            self._screen_receiver = BroadcastReceiver(
                self._on_screen_event,
                actions=[
                    'android.intent.action.SCREEN_OFF',
                    'android.intent.action.SCREEN_ON',
                ]
            )
            self._screen_receiver.start()
            self.log('system: screen-receiver registriert')
        except Exception as e:
            self.log(f'system: screen-receiver FEHLER: {e}')

    def _on_screen_event(self, context, intent):
        action = intent.getAction()
        if action == 'android.intent.action.SCREEN_OFF':
            self.log('system: display AUS')
        elif action == 'android.intent.action.SCREEN_ON':
            self.log('system: display AN')

    def _apply_active_marker(self):
        for t in self._filtered:
            t['is_active'] = (t['idx'] == self._active_srv_idx)
        # Rebuild grouped data so is_active is reflected in TrackRow entries
        self._set_list_data(self._filtered)

    def on_pause(self):
        """Android back/home button: keep running so audio continues in background."""
        self.log('lifecycle: app → hintergrund')
        return True

    def on_resume(self):
        self.log('lifecycle: app → vordergrund')

    def on_stop(self):
        self.log('lifecycle: on_stop (app wird beendet)')
        self._release_wifi_lock()
        self._stop_audio_service()
        if self._proxy:
            self._proxy.stop()
            self._proxy = None
        if self._sound and platform == 'android':
            _player = self._sound
            _handler = self._exo_handler
            try:
                if _handler is not None:
                    ExoRunnableClass = _get_exo_runnable_class()
                    def _release():
                        try:
                            _player.stop()
                            _player.release()
                        except Exception:
                            pass
                    _handler.post(ExoRunnableClass(_release))
                else:
                    from android.runnable import run_on_ui_thread  # type: ignore
                    @run_on_ui_thread
                    def _release_ui():
                        try:
                            _player.stop()
                            _player.release()
                        except Exception:
                            pass
                    _release_ui()
            except Exception:
                pass
        elif self._sound:
            try:
                self._sound.stop()
                if hasattr(self._sound, 'release'):
                    self._sound.release()
            except Exception:
                pass
        if self._tmp_file and os.path.exists(self._tmp_file):
            try:
                os.unlink(self._tmp_file)
            except Exception:
                pass


if __name__ == '__main__':
    OwnlyApp().run()
