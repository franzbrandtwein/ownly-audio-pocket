#!/usr/bin/env python3
"""
Ownly Audio Pocket – Kivy Client
Verbindet sich per SOAP (Port 8767) mit dem Server und spielt Musik ab.
"""

import random
import threading
import tempfile
import os
import urllib.request
import re

from kivy.app import App
from kivy.lang import Builder
from kivy.uix.recycleview import RecycleView
from kivy.uix.recycleview.views import RecycleDataViewBehavior
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.popup import Popup
from kivy.uix.image import Image as KivyImage
from kivy.properties import (
    BooleanProperty, StringProperty, NumericProperty
)
from kivy.core.audio import SoundLoader
from kivy.clock import Clock
from kivy.graphics.texture import Texture
from kivy.metrics import dp

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
            text: '📷'
            font_size: dp(16)
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
                text: '⏮'
                font_size: dp(16)
                background_color: (.18, .18, .18, 1)
                on_release: app.prev_track()

            Button:
                id: play_btn
                text: '▶'
                font_size: dp(18)
                background_color: (.93, .4, .2, 1)
                on_release: app.toggle_play()

            Button:
                text: '⏭'
                font_size: dp(16)
                background_color: (.18, .18, .18, 1)
                on_release: app.next_track()

            Button:
                id: shuffle_btn
                text: '🔀'
                font_size: dp(14)
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
# QR Scanner
# ---------------------------------------------------------------------------

class QRScanPopup(Popup):
    """Camera overlay that decodes QR codes using OpenCV."""

    def __init__(self, on_result, **kwargs):
        super().__init__(
            title='QR Code scannen',
            size_hint=(.95, .85),
            auto_dismiss=False,
            **kwargs
        )
        self._on_result = on_result
        self._cap       = None
        self._running   = False
        self._detector  = None

        # Layout: camera feed + status + close button
        root = BoxLayout(orientation='vertical', spacing=dp(8),
                         padding=dp(8))

        self._cam_img = KivyImage(allow_stretch=True)
        self._status  = __import__('kivy.uix.label', fromlist=['Label']).Label(
            text='Kamera wird geöffnet …',
            size_hint_y=None, height=dp(28),
            color=(.7, .7, .7, 1), font_size=dp(12)
        )

        from kivy.uix.button import Button
        close_btn = Button(
            text='Abbrechen',
            size_hint_y=None, height=dp(40),
            background_color=(.3, .3, .3, 1)
        )
        close_btn.bind(on_release=lambda *_: self._close())

        root.add_widget(self._cam_img)
        root.add_widget(self._status)
        root.add_widget(close_btn)
        self.content = root

    def on_open(self):
        try:
            import cv2
            self._detector = cv2.QRCodeDetector()
            self._cap = cv2.VideoCapture(0)
            if not self._cap.isOpened():
                self._status.text = '❌ Keine Kamera gefunden'
                return
            self._running = True
            self._tick = Clock.schedule_interval(self._update_frame, 1 / 20)
        except ImportError:
            self._status.text = '❌ opencv-python nicht installiert'

    def _update_frame(self, dt):
        if not self._running or self._cap is None:
            return
        import cv2, numpy as np
        ret, frame = self._cap.read()
        if not ret:
            return

        # Try to decode QR code
        data, _, _ = self._detector.detectAndDecode(frame)
        if data:
            self._running = False
            Clock.unschedule(self._tick)
            self._cap.release()
            self._cap = None
            Clock.schedule_once(lambda _: self._on_decoded(data))
            return

        # Draw scanning overlay (horizontal line animation)
        h, w = frame.shape[:2]
        t = int(Clock.get_boottime() * 80) % h
        cv2.line(frame, (0, t), (w, t), (238, 102, 51), 2)

        # Convert BGR → RGB → Kivy texture (flipped vertically)
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        frame_flip = np.flipud(frame_rgb)
        tex = Texture.create(size=(w, h), colorfmt='rgb')
        tex.blit_buffer(frame_flip.tobytes(), colorfmt='rgb', bufferfmt='ubyte')
        self._cam_img.texture = tex
        self._status.text = '🔍 QR Code suchen …'

    def _on_decoded(self, data):
        self._status.text = f'✓ Erkannt: {data}'
        self.dismiss()
        self._on_result(data)

    def _close(self):
        self._running = False
        if hasattr(self, '_tick'):
            Clock.unschedule(self._tick)
        if self._cap:
            self._cap.release()
            self._cap = None
        self.dismiss()

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

        self._all_tracks    = []   # raw list from SOAP
        self._filtered      = []   # currently visible (matches search)
        self._active_srv_idx = -1  # server-side idx of currently playing track
        self._sound          = None
        self._shuffle        = False
        self._soap_client    = None
        self._server_host    = ''
        self._soap_port      = 8767
        self._tmp_file       = None

        return self._root

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
            from zeep import Client
            url = f'http://{self._server_host}:{self._soap_port}/?wsdl'
            self._soap_client = Client(url)
            raw = self._soap_client.service.GetTracks()
            tracks = []
            for t in (raw or []):
                tracks.append({
                    'idx':      int(t.idx),
                    'track_id': t.id    or '',
                    'title':    t.title or '',
                    'band':     t.band  or '',
                    'album':    t.album or '',
                    'genre':    t.genre or '',
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
        self._root.ids.play_btn.text = '⏸'

        if self._sound:
            self._sound.stop()
            self._sound = None

        # Clean up previous temp file
        if self._tmp_file and os.path.exists(self._tmp_file):
            try:
                os.unlink(self._tmp_file)
            except Exception:
                pass
            self._tmp_file = None

        url = f'http://{self._server_host}:{self._soap_port}/audio/{server_idx}'
        threading.Thread(
            target=self._fetch_and_play,
            args=(url, label),
            daemon=True
        ).start()

    def _fetch_and_play(self, url, label):
        try:
            tmp = tempfile.NamedTemporaryFile(suffix='.mp3', delete=False)
            with urllib.request.urlopen(url, timeout=60) as resp:
                while chunk := resp.read(65536):
                    tmp.write(chunk)
            tmp.close()
            self._tmp_file = tmp.name
            Clock.schedule_once(lambda _: self._play_file(tmp.name, label))
        except Exception as e:
            Clock.schedule_once(lambda _: self._on_play_error(str(e)))

    def _play_file(self, path, label):
        self._sound = SoundLoader.load(path)
        if self._sound:
            self._sound.bind(on_stop=self._on_track_ended)
            self._sound.play()
            self._root.ids.now_playing.text = f'▶ {label}'
        else:
            self._root.ids.now_playing.text = f'❌ Kein Audio-Backend: {label}'
            self._root.ids.play_btn.text = '▶'

    def _on_play_error(self, msg):
        self._root.ids.now_playing.text = f'❌ {msg[:60]}'
        self._root.ids.play_btn.text = '▶'

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

    def toggle_play(self):
        if self._sound:
            if self._sound.state == 'play':
                self._sound.stop()
                self._root.ids.play_btn.text = '▶'
            else:
                self._sound.play()
                self._root.ids.play_btn.text = '⏸'

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
        """Open camera popup to scan the server panel QR code."""
        popup = QRScanPopup(on_result=self._on_qr_scanned)
        popup.open()

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
            self._sound.stop()
        if self._tmp_file and os.path.exists(self._tmp_file):
            try:
                os.unlink(self._tmp_file)
            except Exception:
                pass


if __name__ == '__main__':
    OwnlyApp().run()
