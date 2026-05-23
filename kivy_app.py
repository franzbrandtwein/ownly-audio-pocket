#!/usr/bin/env python3
"""
Ownly Audio Pocket – Kivy Client
Verbindet sich per SOAP (Port 8767) mit dem Server und spielt Musik ab.

APK-Build: buildozer android debug  (siehe buildozer.spec)
"""

__version__ = '1.0.0'

import random
import threading
import tempfile
import os
import re
import time
import json
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

# Android ExoPlayer listener — defined once (jnius singleton pattern).
_ExoListenerClass = None

def _get_exo_listener_class():
    global _ExoListenerClass
    if _ExoListenerClass is None:
        from jnius import PythonJavaClass, java_method  # type: ignore

        class _ExoListener(PythonJavaClass):
            __javainterfaces__ = ['androidx/media3/common/Player$Listener']
            __javacontext__ = 'app'

            def __init__(self, on_ended, on_error):
                super().__init__()
                self._on_ended = on_ended
                self._on_error = on_error

            @java_method('(I)V')
            def onPlaybackStateChanged(self, state):
                if state == 4:   # Player.STATE_ENDED
                    self._on_ended()

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

    def start(self, music_dir, on_status=None):
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
                    if path.startswith('/audio/'):
                        try:
                            idx = int(path[7:])
                            fp = tracks_ref[idx]['abs']
                            size = os.path.getsize(fp)
                            self.send_response(200)
                            self.send_header('Content-Type', 'audio/mpeg')
                            self.send_header('Content-Length', str(size))
                            self.send_header('Accept-Ranges', 'bytes')
                            self.send_header('Access-Control-Allow-Origin', '*')
                            self.end_headers()
                            with open(fp, 'rb') as f:
                                self.wfile.write(f.read())
                        except Exception:
                            self.send_response(404)
                            self.end_headers()
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

            class _ReuseServer(http.server.HTTPServer):
                allow_reuse_address = True

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
    title: '☰ Menü'
    size_hint: .8, None
    height: dp(230)
    auto_dismiss: True
    BoxLayout:
        orientation: 'vertical'
        spacing: dp(8)
        padding: dp(12)
        Button:
            text: '🔗  Verbindungen'
            font_size: dp(14)
            size_hint_y: None
            height: dp(44)
            background_color: (.25, .25, .25, 1)
            on_release: root.dismiss(); app.open_connections()
        Button:
            text: '🖥  Server'
            font_size: dp(14)
            size_hint_y: None
            height: dp(44)
            background_color: (.18, .55, .18, 1)
            on_release: root.dismiss(); app.open_server_popup()
        Button:
            text: '⚙  Einstellungen'
            font_size: dp(14)
            size_hint_y: None
            height: dp(44)
            background_color: (.2, .3, .5, 1)
            on_release: root.dismiss(); app.open_settings()

<SettingsPopup>:
    title: 'Einstellungen'
    size_hint: .92, None
    height: dp(160)
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


class ConnectionsPopup(Popup):
    pass


class ServerPopup(Popup):
    pass


class MenuPopup(Popup):
    pass


class SettingsPopup(Popup):
    pass


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
            self._status.text = '❌ OpenCV nicht verfügbar'
            return

        if platform == 'android':
            self._open_android()
        else:
            self._open_desktop()

    def _open_desktop(self):
        import cv2
        self._cv_cap = cv2.VideoCapture(0)
        if not self._cv_cap.isOpened():
            self._status.text = '❌ Keine Kamera gefunden'
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
            self._status.text = '❌ Kamera-Berechtigung verweigert'

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
        self._status.text = '🔍 QR Code suchen …'

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
            self._status.text = '🔍 QR Code suchen …'

    # ── helpers ──────────────────────────────────────────────────────────────

    def _finish(self, data):
        self._running = False
        if self._tick:
            Clock.unschedule(self._tick)
        self._release_camera()
        self._status.text = f'✓ Erkannt: {data}'
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
        self._dl_wake_lock   = None   # PARTIAL_WAKE_LOCK held during background downloads
        self._cached_ids     = set()
        self._offline_only   = False
        self._expanded_bands  = set()
        self._expanded_albums = set()
        self._current_addr   = ''
        self._servers        = []   # list of {'addr': '...'} dicts
        self._conn_popup     = None
        self._embedded_server = EmbeddedServer()
        self._server_popup    = None
        self._settings_popup  = None

        Clock.schedule_once(self._load_servers, 0)
        Clock.schedule_once(self._load_server_music_dir, 0)
        Clock.schedule_once(self._load_cached_ids, 0)
        Clock.schedule_once(self._load_settings, 0)
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
            self.auto_cache_on_play = bool(s.get('auto_cache_on_play', False))
        except Exception:
            self.auto_cache_on_play = False

    def _save_settings(self):
        try:
            with open(self._settings_file(), 'w') as f:
                json.dump({'auto_cache_on_play': self.auto_cache_on_play}, f)
        except Exception:
            pass

    def toggle_auto_cache(self):
        self.auto_cache_on_play = not self.auto_cache_on_play
        self._save_settings()

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
        d = os.path.join(self.user_data_dir, 'tracks')
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
                f'📁 {n} Tracks offline verfügbar'))

    def _load_cached_ids(self, *_):
        d = os.path.join(self.user_data_dir, 'tracks')
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
                                  f'❌ Download: {e}'))

    def download_album(self, album):
        for t in self._all_tracks:
            if t['album'] == album and t['track_id'] not in self._cached_ids:
                self.download_track_by_id(t['idx'], t['track_id'])

    def _refresh_cache_markers(self):
        self._set_list_data(self._filtered)

    def toggle_offline_filter(self):
        self._offline_only = not self._offline_only
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
        self._root.ids.now_playing.text = '🔍 Suche Server …'
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
        self._root.ids.now_playing.text = '❌ Kein Server gefunden (5 s Timeout)'

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
            self._root.ids.now_playing.text = '⏳ Verbinde …'
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
        self._root.ids.now_playing.text = f'✓ {n} Tracks geladen'
        if self._current_addr:
            self._save_host(self._current_addr)
        self._save_track_meta(tracks)

    def _on_error(self, msg):
        try:
            self._root.ids.status_dot.dot_color = (.9, .2, .2, 1)
            self._root.ids.now_playing.text = f'❌ {msg[:70]}'
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
                self._root.ids.now_playing.text = f'❌ {server_addr} offline – {n} Tracks verbleibend'
            else:
                self._root.ids.now_playing.text = f'❌ {server_addr} offline – keine Tracks mehr'
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

    def play_idx(self, server_idx):
        """Start playing the track identified by server-side idx."""
        self._active_srv_idx = server_idx
        self._apply_active_marker()

        track = next((t for t in self._all_tracks if t['idx'] == server_idx), None)
        if not track:
            return

        label = f'{track["title"]}  —  {track["band"]}'
        self._root.ids.now_playing.text = f'⏳ {label}'
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
            # Auto-cache: trigger background download if setting is on
            if self.auto_cache_on_play and track.get('track_id'):
                self.download_track_by_id(server_idx, track['track_id'])
        if platform == 'android':
            # All MediaPlayer setup must run on main thread (needs Looper).
            # For HTTP URLs, download first via urllib (Python HTTP bypasses Android's
            # cleartext traffic policy that blocks native MediaPlayer HTTP streaming).
            if url.startswith('http'):
                self._root.ids.now_playing.text = f'⏳ Lade {label} …'
                threading.Thread(
                    target=self._download_then_play_android,
                    args=(url, label, server_addr),
                    daemon=True
                ).start()
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
                                    f'⏳ {l[:30]} {p}%'))
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
        """Main-thread: release old player, create ExoPlayer, prepare and play."""
        try:
            from jnius import autoclass  # type: ignore
            PythonActivity = autoclass('org.kivy.android.PythonActivity')
            ExoPlayerBuilder = autoclass('androidx.media3.exoplayer.ExoPlayer$Builder')
            MediaItem = autoclass('androidx.media3.common.MediaItem')
            Looper = autoclass('android.os.Looper')

            # Ensure this thread has a Looper (required by ExoPlayer).
            try:
                Looper.prepare()
            except Exception:
                pass  # already prepared

            # Release old player on the correct thread.
            for old in (self._old_sound, self._sound):
                if old is not None:
                    try:
                        old.release()
                    except Exception:
                        pass
            self._old_sound = None
            self._sound = None

            player = ExoPlayerBuilder(PythonActivity.mActivity).build()

            # Hold a CPU wake lock so audio keeps playing with screen off.
            try:
                PowerManager = autoclass('android.os.PowerManager')
                player.setWakeMode(PythonActivity.mActivity, PowerManager.PARTIAL_WAKE_LOCK)
            except Exception:
                pass

            ExoListenerClass = _get_exo_listener_class()
            self._mp_listener = ExoListenerClass(
                lambda: Clock.schedule_once(lambda _dt: self._auto_next()),
                lambda msg: Clock.schedule_once(lambda _dt: self._on_play_error(msg)),
            )
            player.addListener(self._mp_listener)

            media_url = f'file://{url}' if url.startswith('/') else url
            player.setMediaItem(MediaItem.fromUri(media_url))
            player.prepare()
            player.play()   # playWhenReady=true → plays as soon as buffered
            self._exo_playing = True

            self._sound = player
            self._root.ids.now_playing.text = f'> {label}'
            self._root.ids.play_btn.text = '||'
            self._start_progress_clock()
        except Exception as e:
            self._on_play_error(str(e))

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
            pos = player.getCurrentPosition()   # ms
            dur = player.getDuration()          # ms  (negative if unknown)
            if dur > 0:
                self._root.ids.progress_bar.value = int(pos * 1000 / dur)
                self._root.ids.time_label.text = (
                    f'{pos//60000}:{(pos//1000)%60:02d} / '
                    f'{dur//60000}:{(dur//1000)%60:02d}'
                )
            else:
                self._root.ids.progress_bar.value = 0
                self._root.ids.time_label.text = f'{pos//60000}:{(pos//1000)%60:02d}'
        except Exception:
            self._stop_progress_clock()

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
        else:
            self._root.ids.now_playing.text = f'❌ Kein Audio-Backend: {label}'
            self._root.ids.play_btn.text = '>'

    def _on_play_error(self, msg):
        self._exo_playing = False
        self._reset_progress()
        self._root.ids.now_playing.text = f'❌ {msg[:60]}'
        self._root.ids.play_btn.text = '>'

    def _on_track_ended(self, *_):
        self._exo_playing = False
        Clock.schedule_once(lambda _: self._auto_next())

    def _auto_next(self):
        self._reset_progress()
        if not self._filtered:
            return
        if self._shuffle:
            # pick random, avoid repeating same track
            candidates = [t for t in self._filtered if t['idx'] != self._active_srv_idx]
            nxt = random.choice(candidates) if candidates else self._filtered[0]
        else:
            cur_pos = next(
                (i for i, t in enumerate(self._filtered) if t['idx'] == self._active_srv_idx), -1
            )
            nxt = self._filtered[(cur_pos + 1) % len(self._filtered)]
        self.play_idx(nxt['idx'])

    def _exo_pause_resume(self):
        """Pause or resume ExoPlayer. Uses tracked boolean — avoids isPlaying() jnius issues."""
        try:
            if self._exo_playing:
                self._sound.pause()
                self._exo_playing = False
                self._stop_progress_clock()
                self._root.ids.play_btn.text = '>'
            else:
                self._sound.play()
                self._exo_playing = True
                self._start_progress_clock()
                self._root.ids.play_btn.text = '||'
        except Exception as e:
            self._root.ids.now_playing.text = f'❌ pause: {e}'

    def toggle_play(self):
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
        self._auto_next()

    def prev_track(self):
        if not self._filtered:
            return
        cur_pos = next(
            (i for i, t in enumerate(self._filtered) if t['idx'] == self._active_srv_idx), 0
        )
        nxt = self._filtered[(cur_pos - 1) % len(self._filtered)]
        self.play_idx(nxt['idx'])

    def toggle_shuffle(self):
        self._shuffle = not self._shuffle
        btn = self._root.ids.shuffle_btn
        btn.background_color = (.93, .4, .2, 1) if self._shuffle else (.18, .18, .18, 1)

    # ── QR scan ──────────────────────────────────────────────────────────────

    def open_menu(self):
        MenuPopup().open()

    def open_settings(self):
        if self._settings_popup is None:
            self._settings_popup = SettingsPopup()
        self._settings_popup.open()

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
        self._embedded_server.start(music_dir, on_status=_on_status)

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
            self._root.ids.now_playing.text = f'❌ Kein gültiger Server-Link: {data}'
            return
        host = m.group(1)
        addr = f'{host}:8767'
        self.connect(addr)

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _apply_active_marker(self):
        for t in self._filtered:
            t['is_active'] = (t['idx'] == self._active_srv_idx)
        # Rebuild grouped data so is_active is reflected in TrackRow entries
        self._set_list_data(self._filtered)

    def on_pause(self):
        """Android back/home button: keep running so audio continues in background."""
        return True

    def on_resume(self):
        pass

    def on_stop(self):
        if self._sound:
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
