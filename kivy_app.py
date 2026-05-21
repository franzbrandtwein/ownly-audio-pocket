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
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET

from kivy.app import App
from kivy.lang import Builder
from kivy.uix.recycleview import RecycleView
from kivy.uix.recycleview.views import RecycleDataViewBehavior
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.popup import Popup
from kivy.uix.image import Image as KivyImage
from kivy.uix.label import Label
from kivy.uix.button import Button
from kivy.properties import (
    BooleanProperty, StringProperty, NumericProperty
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
    """Find first child matching '{_SOAP_NS}tag' and return its text."""
    node = element.find(f'{{{_SOAP_NS}}}{tag}')
    return (node.text or '') if node is not None else ''

# ---------------------------------------------------------------------------
# KV layout
# ---------------------------------------------------------------------------
KV = """
#:import dp kivy.metrics.dp

<TrackRow>:
    size_hint_y: None
    height: dp(62)
    padding: dp(12), dp(6)
    spacing: dp(0)
    orientation: 'horizontal'
    canvas.before:
        Color:
            rgba: (.22, .14, .05, 1) if self.is_active else (.14, .14, .14, 1)
        Rectangle:
            size: self.size
            pos: self.pos
        Color:
            rgba: (.2, .2, .2, 1)
        Line:
            points: self.x, self.y, self.x + self.width, self.y
            width: 1

    BoxLayout:
        orientation: 'vertical'
        spacing: dp(2)
        Label:
            text: root.title
            font_size: dp(13)
            halign: 'left'
            valign: 'middle'
            text_size: self.size
            color: (1, .7, .25, 1) if root.is_active else (.95, .95, .95, 1)
            bold: root.is_active
        Label:
            text: root.band + '  ·  ' + root.album
            font_size: dp(10)
            halign: 'left'
            valign: 'middle'
            text_size: self.size
            color: (.55, .55, .55, 1)

<TrackList>:
    viewclass: 'TrackRow'
    bar_width: dp(4)
    bar_color: (.93, .4, .2, .8)
    RecycleBoxLayout:
        default_size: None, dp(62)
        default_size_hint: 1, None
        size_hint_y: None
        height: self.minimum_height
        orientation: 'vertical'
        spacing: 0

<OwnlyRoot>:
    orientation: 'vertical'
    canvas.before:
        Color:
            rgba: (.08, .08, .08, 1)
        Rectangle:
            size: self.size
            pos: self.pos

    # ── Server bar ──────────────────────────────────────────────────────────
    BoxLayout:
        size_hint_y: None
        height: dp(50)
        padding: dp(8), dp(6)
        spacing: dp(6)
        canvas.before:
            Color:
                rgba: (.11, .11, .11, 1)
            Rectangle:
                size: self.size
                pos: self.pos

        TextInput:
            id: server_input
            text: '192.168.x.x:8767'
            hint_text: 'Server IP:Port'
            font_size: dp(13)
            multiline: False
            background_color: (.18, .18, .18, 1)
            foreground_color: (.9, .9, .9, 1)
            cursor_color: (1, .5, .2, 1)
            size_hint_x: 0.55
            on_text_validate: app.connect(self.text)

        Button:
            text: 'QR'
            font_size: dp(13)
            size_hint_x: None
            width: dp(42)
            background_color: (.18, .18, .18, 1)
            on_release: app.open_qr_scan()

        Button:
            text: 'Verbinden'
            font_size: dp(12)
            size_hint_x: 0.28
            background_color: (.93, .4, .2, 1)
            on_release: app.connect(server_input.text)

        Label:
            id: status_dot
            text: '●'
            color: (.35, .35, .35, 1)
            size_hint_x: None
            width: dp(20)
            font_size: dp(16)

    # ── Search ──────────────────────────────────────────────────────────────
    TextInput:
        id: search_input
        hint_text: 'Suchen …'
        font_size: dp(13)
        multiline: False
        background_color: (.13, .13, .13, 1)
        foreground_color: (.9, .9, .9, 1)
        size_hint_y: None
        height: dp(36)
        on_text: app.filter_tracks(self.text)

    # ── Track list ───────────────────────────────────────────────────────────
    TrackList:
        id: track_list

    # ── Player bar ───────────────────────────────────────────────────────────
    BoxLayout:
        orientation: 'vertical'
        size_hint_y: None
        height: dp(84)
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

        Label:
            id: now_playing
            text: '— nichts ausgewählt —'
            font_size: dp(12)
            color: (.75, .75, .75, 1)
            size_hint_y: None
            height: dp(20)
            halign: 'center'
            valign: 'middle'
            text_size: self.size

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

    def refresh_view_attrs(self, rv, index, data):
        self.idx      = data.get('idx', 0)
        self.track_id = data.get('track_id', '')
        self.title    = data.get('title', '')
        self.band     = data.get('band', '')
        self.album    = data.get('album', '')
        self.genre    = data.get('genre', '')
        self.is_active = data.get('is_active', False)
        return super().refresh_view_attrs(rv, index, data)

    def on_touch_down(self, touch):
        if self.collide_point(*touch.pos):
            App.get_running_app().play_idx(self.idx)
            return True
        return super().on_touch_down(touch)


class TrackList(RecycleView):
    pass


class OwnlyRoot(BoxLayout):
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

    def build(self):
        Builder.load_string(KV)
        self._root = OwnlyRoot()
        if platform == 'android':
            from kivy.core.window import Window
            Window.softinput_mode = 'below_target'
            from kivy.clock import Clock as _Clock
            _Clock.schedule_once(self._apply_android_insets, 0.5)

        self._all_tracks    = []   # raw list from SOAP
        self._filtered      = []   # currently visible (matches search)
        self._active_srv_idx = -1  # server-side idx of currently playing track
        self._sound          = None
        self._shuffle        = False
        self._server_host    = ''
        self._soap_port      = 8767
        self._tmp_file       = None

        return self._root

    def _apply_android_insets(self, dt):
        from kivy.core.window import Window
        from kivy.metrics import dp
        sb = getattr(Window, 'statusbar_height', dp(28))
        self._root.padding = [0, sb, 0, dp(52)]

    # ── Connection ──────────────────────────────────────────────────────────

    def connect(self, addr):
        addr = addr.strip()
        if ':' in addr:
            host, port = addr.rsplit(':', 1)
            self._server_host = host
            self._soap_port   = int(port)
        else:
            self._server_host = addr
        self._root.ids.status_dot.color = (.9, .7, .1, 1)  # yellow = connecting
        self._root.ids.now_playing.text = '⏳ Verbinde …'
        threading.Thread(target=self._do_connect, daemon=True).start()

    def _do_connect(self):
        try:
            root = _soap_request(self._server_host, self._soap_port, 'GetTracks')
            tracks = []
            for item in root.iter(f'{{{_SOAP_NS}}}TrackInfo'):
                tracks.append({
                    'idx':       int(_soap_text(item, 'idx') or 0),
                    'track_id':  _soap_text(item, 'id'),
                    'title':     _soap_text(item, 'title'),
                    'band':      _soap_text(item, 'band'),
                    'album':     _soap_text(item, 'album'),
                    'genre':     _soap_text(item, 'genre'),
                    'is_active': False,
                })
            Clock.schedule_once(lambda _: self._on_connected(tracks))
        except Exception as e:
            Clock.schedule_once(lambda _: self._on_error(str(e)))

    def _on_connected(self, tracks):
        self._all_tracks = tracks
        self._filtered   = list(tracks)
        self._root.ids.track_list.data = self._filtered
        self._root.ids.status_dot.color = (.2, .9, .3, 1)
        n = len(tracks)
        self._root.ids.now_playing.text = f'✓ {n} Tracks geladen'

    def _on_error(self, msg):
        self._root.ids.status_dot.color = (.9, .2, .2, 1)
        self._root.ids.now_playing.text = f'❌ {msg[:70]}'

    # ── Search / filter ─────────────────────────────────────────────────────

    def filter_tracks(self, query):
        q = query.lower().strip()
        if q:
            self._filtered = [
                t for t in self._all_tracks
                if q in t['title'].lower()
                or q in t['band'].lower()
                or q in t['album'].lower()
            ]
        else:
            self._filtered = list(self._all_tracks)
        self._apply_active_marker()
        self._root.ids.track_list.data = self._filtered

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
            try:
                self._sound.stop()
                # Release if Android MediaPlayer
                if hasattr(self._sound, 'release'):
                    self._sound.release()
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

        url = f'http://{self._server_host}:{self._soap_port}/audio/{server_idx}'
        if platform == 'android':
            # Stream URL directly via Android MediaPlayer — no temp file, no blocking load()
            threading.Thread(
                target=self._stream_android,
                args=(url, label),
                daemon=True
            ).start()
        else:
            threading.Thread(
                target=self._fetch_and_play,
                args=(url, label),
                daemon=True
            ).start()

    def _stream_android(self, url, label):
        """Android: play HTTP stream via MediaPlayer directly (no full download needed)."""
        try:
            from jnius import autoclass  # type: ignore
            MediaPlayer = autoclass('android.media.MediaPlayer')
            mp = MediaPlayer()
            mp.setDataSource(url)
            mp.prepare()           # blocking but in background thread → no ANR
            Clock.schedule_once(lambda _: self._play_mediaplayer(mp, label))
        except Exception as e:
            Clock.schedule_once(lambda _: self._on_play_error(str(e)))

    def _play_mediaplayer(self, mp, label):
        """Main-thread: start prepared MediaPlayer."""
        if self._sound:
            try:
                self._sound.stop()
                self._sound.release()
            except Exception:
                pass
        self._sound = mp
        mp.start()
        self._root.ids.now_playing.text = f'> {label}'
        self._root.ids.play_btn.text = '||'
        # Poll for completion (MediaPlayer has no easy Python callback)
        Clock.schedule_interval(self._check_mp_done, 1.0)

    def _check_mp_done(self, dt):
        mp = self._sound
        if mp is None:
            return False  # unschedule
        try:
            from jnius import autoclass  # type: ignore
            MediaPlayer = autoclass('android.media.MediaPlayer')
            if not isinstance(mp, MediaPlayer):
                return False
            if not mp.isPlaying():
                Clock.schedule_once(lambda _: self._auto_next())
                return False  # unschedule
        except Exception:
            return False

    def _fetch_and_play(self, url, label):
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
        self._root.ids.now_playing.text = f'❌ {msg[:60]}'
        self._root.ids.play_btn.text = '>'

    def _on_track_ended(self, *_):
        Clock.schedule_once(lambda _: self._auto_next())

    def _auto_next(self):
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

    def _is_mp_playing(self):
        """True if current sound is an Android MediaPlayer and currently playing."""
        try:
            from jnius import autoclass  # type: ignore
            MediaPlayer = autoclass('android.media.MediaPlayer')
            return isinstance(self._sound, MediaPlayer) and self._sound.isPlaying()
        except Exception:
            return False

    def _mp_pause_resume(self):
        """Pause or resume Android MediaPlayer."""
        try:
            if self._sound.isPlaying():
                self._sound.pause()
                self._root.ids.play_btn.text = '>'
            else:
                self._sound.start()
                self._root.ids.play_btn.text = '||'
        except Exception as e:
            self._root.ids.now_playing.text = f'❌ {e}'

    def toggle_play(self):
        if not self._sound:
            return
        if platform == 'android':
            try:
                from jnius import autoclass  # type: ignore
                MediaPlayer = autoclass('android.media.MediaPlayer')
                if isinstance(self._sound, MediaPlayer):
                    self._mp_pause_resume()
                    return
            except Exception:
                pass
        # Kivy SoundLoader path
        if self._sound.state == 'play':
            self._sound.stop()
            self._root.ids.play_btn.text = '>'
        else:
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
        self._root.ids.server_input.text = addr
        self.connect(addr)

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _apply_active_marker(self):
        for t in self._filtered:
            t['is_active'] = (t['idx'] == self._active_srv_idx)
        self._root.ids.track_list.refresh_from_data()

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
