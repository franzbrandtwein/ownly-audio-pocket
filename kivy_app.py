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

# Android MediaPlayer completion listener — defined once to avoid jnius
# re-registering a Java proxy class on every play() call (→ crash).
_AndroidListenerClass = None

def _get_android_listener_class():
    global _AndroidListenerClass
    if _AndroidListenerClass is None:
        from jnius import PythonJavaClass, java_method  # type: ignore
        class _Listener(PythonJavaClass):
            __javainterfaces__ = ['android/media/MediaPlayer$OnCompletionListener']
            __javacontext__ = 'app'
            def __init__(self, cb):
                super().__init__()
                self._cb = cb
            @java_method('(Landroid/media/MediaPlayer;)V')
            def onCompletion(self, _mp):
                self._cb()
        _AndroidListenerClass = _Listener
    return _AndroidListenerClass


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

        StatusDot:
            id: status_dot
            size_hint_x: None
            width: dp(14)

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
        try:
            return self._build_inner()
        except Exception:
            import traceback
            err = traceback.format_exc()
            # Show error on screen so we can read it
            from kivy.uix.scrollview import ScrollView
            from kivy.uix.label import Label as _L
            sv = ScrollView()
            lbl = _L(text=err, font_size='11sp', size_hint_y=None, markup=False)
            lbl.bind(texture_size=lbl.setter('size'))
            sv.add_widget(lbl)
            return sv

    def _build_inner(self):
        Builder.load_string(KV)
        self._root = OwnlyRoot()
        if platform == 'android':
            from kivy.core.window import Window
            Window.softinput_mode = 'below_target'
            from kivy.clock import Clock as _Clock
            _Clock.schedule_once(self._apply_android_insets, 0.5)

        self._all_tracks    = []
        self._filtered      = []
        self._active_srv_idx = -1
        self._sound          = None
        self._shuffle        = False
        self._mp_listener    = None
        self._progress_clock = None
        self._server_host    = ''
        self._soap_port      = 8767
        self._tmp_file       = None
        self._cached_ids     = set()
        self._offline_only   = False
        self._expanded_bands  = set()
        self._expanded_albums = set()

        Clock.schedule_once(self._load_saved_host, 0)
        Clock.schedule_once(self._load_cached_ids, 0)
        return self._root

    def _host_file(self):
        import os
        return os.path.join(self.user_data_dir, 'last_host.txt')

    def _load_saved_host(self, *_):
        try:
            with open(self._host_file(), 'r') as f:
                saved = f.read().strip()
            if saved:
                self._root.ids.server_input.text = saved
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

    def _load_cached_ids(self, *_):
        d = os.path.join(self.user_data_dir, 'tracks')
        if os.path.isdir(d):
            self._cached_ids = {
                f for f in os.listdir(d)
                if os.path.getsize(os.path.join(d, f)) > 0
            }
        else:
            self._cached_ids = set()

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
        try:
            urllib.request.urlretrieve(url, tmp)
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

    def _apply_android_insets(self, *_):
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
        self._root.ids.status_dot.dot_color = (.9, .7, .1, 1)  # yellow = connecting
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
        self._all_tracks = tracks
        self._filtered   = list(tracks)
        self._set_list_data(self._filtered)
        self._root.ids.status_dot.dot_color = (.2, .9, .3, 1)
        n = len(tracks)
        self._root.ids.now_playing.text = f'✓ {n} Tracks geladen'
        addr = self._root.ids.server_input.text.strip()
        if addr:
            self._save_host(addr)

    def _on_error(self, msg):
        self._root.ids.status_dot.dot_color = (.9, .2, .2, 1)
        self._root.ids.now_playing.text = f'❌ {msg[:70]}'

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

        # Use local cached file if available, otherwise stream from server
        local_path = self._cache_path(track.get('track_id', ''))
        if track.get('track_id') and os.path.isfile(local_path):
            url = local_path
        else:
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
        """Android: play HTTP stream or local file via MediaPlayer."""
        try:
            from jnius import autoclass  # type: ignore
            MediaPlayer = autoclass('android.media.MediaPlayer')
            mp = MediaPlayer()

            ListenerClass = _get_android_listener_class()
            self._mp_listener = ListenerClass(
                lambda: Clock.schedule_once(lambda _dt: self._auto_next())
            )
            mp.setOnCompletionListener(self._mp_listener)

            mp.setDataSource(url)
            mp.prepare()   # blocking, in background thread → no ANR
            Clock.schedule_once(lambda _: self._play_mediaplayer(mp, label))
        except Exception as e:
            Clock.schedule_once(lambda _: self._on_play_error(str(e)))

    def _play_mediaplayer(self, mp, label):
        """Main-thread: start prepared MediaPlayer."""
        try:
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
        mp = self._sound
        if mp is None:
            self._stop_progress_clock()
            return
        try:
            pos = mp.getCurrentPosition()   # ms
            dur = mp.getDuration()          # ms  (-1 if unknown)
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
        self._reset_progress()
        self._root.ids.now_playing.text = f'❌ {msg[:60]}'
        self._root.ids.play_btn.text = '>'

    def _on_track_ended(self, *_):
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
                self._stop_progress_clock()
                self._root.ids.play_btn.text = '>'
            else:
                self._sound.start()
                self._start_progress_clock()
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
        # Rebuild grouped data so is_active is reflected in TrackRow entries
        self._set_list_data(self._filtered)

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
