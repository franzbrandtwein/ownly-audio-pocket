#!/usr/bin/env python3
"""Ownly Audio Pocket – GUI Launcher"""

import sys, webbrowser, threading, time, socket, io
from pathlib import Path
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QFrame, QLineEdit, QFileDialog
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt5.QtGui import QFont, QPixmap, QImage

import server as _server

PORT = _server.PORT

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def wait_for_port(port, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=0.5)
            s.close()
            return True
        except Exception:
            time.sleep(0.4)
    return False


class Signals(QObject):
    ready         = pyqtSignal()
    failed        = pyqtSignal(str)
    stopped       = pyqtSignal()
    tracks_loaded = pyqtSignal(str)  # summary text after reload


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self._server_thread = None
        self.signals = Signals()
        self.ip      = get_local_ip()
        self.url     = f"https://{self.ip}:{PORT}"

        self._build_ui()
        self.setStyleSheet("QWidget{background:#111;color:#ddd;}")
        self.signals.ready.connect(self._on_ready)
        self.signals.failed.connect(self._on_failed)
        self.signals.stopped.connect(self._on_stopped)
        self.signals.tracks_loaded.connect(self._on_tracks_loaded)
        self._start_server()

    # ── UI ──────────────────────────────────────────────────────────────────

    def _build_ui(self):
        self.setWindowTitle("Ownly Audio Pocket")
        self.setFixedSize(820, 460)

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Left panel ───────────────────────────────────────────────────────
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(32, 28, 32, 24)
        ll.setSpacing(0)

        title = QLabel("🎵 Ownly Audio Pocket")
        title.setFont(QFont("System", 17, QFont.Bold))
        title.setStyleSheet("color:#ee6633;")
        ll.addWidget(title)
        ll.addSpacing(8)

        self.status_dot = QLabel("●")
        self.status_dot.setStyleSheet("color:#888;font-size:14px;")
        self.status_lbl = QLabel("Server wird gestartet …")
        self.status_lbl.setStyleSheet("color:#999;font-size:13px;")
        sr = QHBoxLayout(); sr.setSpacing(6)
        sr.addWidget(self.status_dot); sr.addWidget(self.status_lbl); sr.addStretch()
        ll.addLayout(sr)
        ll.addSpacing(16)

        # ── Player URL card ───────────────────────────────────────────────────
        card = QFrame()
        card.setStyleSheet("background:#1e1e1e;border-radius:10px;")
        cl = QVBoxLayout(card); cl.setContentsMargins(18, 14, 18, 14); cl.setSpacing(8)

        lp = QLabel("Player"); lp.setStyleSheet("color:#888;font-size:11px;")
        self.url_lbl = QLabel(self.url)
        self.url_lbl.setStyleSheet("color:#eee;font-size:13px;font-weight:bold;")
        self.url_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        br = QHBoxLayout(); br.setSpacing(8)
        self.btn_copy    = self._btn("Kopieren",          self._copy_url,    "#333")
        self.btn_browser = self._btn("Im Browser öffnen", self._open_browser, "#ee6633")
        br.addWidget(self.btn_copy, 1); br.addWidget(self.btn_browser, 2)
        cl.addWidget(lp); cl.addWidget(self.url_lbl); cl.addLayout(br)
        ll.addWidget(card)
        ll.addSpacing(16)

        # ── Settings card ─────────────────────────────────────────────────────
        scard = QFrame()
        scard.setStyleSheet("background:#1e1e1e;border-radius:10px;")
        sl = QVBoxLayout(scard); sl.setContentsMargins(18, 14, 18, 14); sl.setSpacing(10)

        ldir = QLabel("Musikverzeichnis"); ldir.setStyleSheet("color:#888;font-size:11px;")
        sl.addWidget(ldir)

        dir_row = QHBoxLayout(); dir_row.setSpacing(8)
        self.dir_input = QLineEdit(str(_server.CONFIG["music_dir"]))
        self.dir_input.setStyleSheet(
            "QLineEdit{background:#2a2a2a;color:#eee;border:1px solid #333;"
            "border-radius:6px;padding:5px 10px;font-size:12px;}"
            "QLineEdit:focus{border-color:#ee6633;}"
        )
        self.btn_browse = self._btn("…", self._browse_dir, "#333")
        self.btn_browse.setFixedWidth(36)
        self.btn_browse.setEnabled(False)
        dir_row.addWidget(self.dir_input); dir_row.addWidget(self.btn_browse)
        sl.addLayout(dir_row)

        self.btn_apply = self._btn("Übernehmen & neu laden", self._apply_dir, "#ee6633")
        self.btn_apply.setEnabled(False)
        sl.addWidget(self.btn_apply)

        self.stats_lbl = QLabel(self._stats_text())
        self.stats_lbl.setStyleSheet("color:#666;font-size:11px;")
        sl.addWidget(self.stats_lbl)

        ll.addWidget(scard)
        ll.addStretch()

        bot = QHBoxLayout(); bot.addStretch()
        self.btn_stop = self._btn("Server beenden", self._stop_server, "#550000")
        self.btn_stop.setEnabled(False)
        bot.addWidget(self.btn_stop)
        ll.addLayout(bot)

        root.addWidget(left, 1)

        # ── Right panel: QR code ─────────────────────────────────────────────
        right = QWidget()
        right.setFixedWidth(290)
        right.setStyleSheet("background:#1a1a1a;")
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 12)
        rl.setAlignment(Qt.AlignCenter)

        self.qr_label = QLabel("⏳")
        self.qr_label.setAlignment(Qt.AlignCenter)
        self.qr_label.setStyleSheet("color:#555;font-size:32px;")

        self.qr_hint = QLabel("Mit Handy scannen")
        self.qr_hint.setAlignment(Qt.AlignCenter)
        self.qr_hint.setStyleSheet("color:#666;font-size:11px;margin-top:6px;")

        rl.addWidget(self.qr_label)
        rl.addWidget(self.qr_hint)
        root.addWidget(right)

        for b in (self.btn_copy, self.btn_browser):
            b.setEnabled(False)

    def _btn(self, text, slot, color):
        b = QPushButton(text)
        b.setFixedHeight(32)
        b.setCursor(Qt.PointingHandCursor)
        b.setStyleSheet(
            f"QPushButton{{background:{color};color:#fff;border:none;"
            f"border-radius:6px;padding:0 14px;font-size:12px;}}"
            f"QPushButton:hover{{opacity:0.85;}}"
            f"QPushButton:disabled{{background:#222;color:#555;}}"
        )
        b.clicked.connect(slot)
        return b

    def _stats_text(self):
        tracks = _server.TRACKS
        if not tracks:
            return "Keine Tracks geladen"
        bands  = len({t["band"]  for t in tracks})
        albums = len({t["album"] for t in tracks})
        return f"{len(tracks)} Tracks  ·  {bands} Interpreten  ·  {albums} Alben"

    # ── State ────────────────────────────────────────────────────────────────

    def _on_ready(self):
        self.status_dot.setStyleSheet("color:#44cc44;font-size:14px;")
        self.status_lbl.setText("Server läuft")
        self.status_lbl.setStyleSheet("color:#44cc44;font-size:13px;")
        for b in (self.btn_copy, self.btn_browser,
                  self.btn_browse, self.btn_apply, self.btn_stop):
            b.setEnabled(True)
        self.stats_lbl.setText(self._stats_text())
        self.stats_lbl.setStyleSheet("color:#888;font-size:11px;")
        self._show_qr()

    def _on_failed(self, msg):
        self.status_dot.setStyleSheet("color:#e63;font-size:14px;")
        self.status_lbl.setText(f"Fehler: {msg}")
        self.status_lbl.setStyleSheet("color:#e63;font-size:13px;")

    def _on_stopped(self):
        self.status_dot.setStyleSheet("color:#888;font-size:14px;")
        self.status_lbl.setText("Server gestoppt")
        self.status_lbl.setStyleSheet("color:#888;font-size:13px;")
        for b in (self.btn_copy, self.btn_browser,
                  self.btn_browse, self.btn_apply, self.btn_stop):
            b.setEnabled(False)

    def _on_tracks_loaded(self, summary):
        self.stats_lbl.setText(summary)
        self.stats_lbl.setStyleSheet("color:#44cc44;font-size:11px;")
        QTimer.singleShot(3000, lambda: self.stats_lbl.setStyleSheet("color:#888;font-size:11px;"))
        self.btn_apply.setText("Übernehmen & neu laden")
        self.btn_apply.setEnabled(True)

    # ── Actions ──────────────────────────────────────────────────────────────

    def _show_qr(self):
        try:
            import qrcode
            qr = qrcode.QRCode(box_size=4, border=2,
                               error_correction=qrcode.constants.ERROR_CORRECT_M)
            qr.add_data(self.url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = io.BytesIO(); img.save(buf, format="PNG"); buf.seek(0)
            qimg = QImage.fromData(buf.read())
            px = QPixmap.fromImage(qimg).scaled(
                230, 230, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.qr_label.setPixmap(px)
            self.qr_label.setStyleSheet("")
            self.qr_hint.setStyleSheet("color:#888;font-size:11px;margin-top:6px;")
        except ImportError:
            self.qr_label.setText("pip install qrcode")
            self.qr_label.setStyleSheet("color:#e63;font-size:11px;")

    def _copy_url(self):
        QApplication.clipboard().setText(self.url)
        self.btn_copy.setText("✓ Kopiert")
        QTimer.singleShot(1500, lambda: self.btn_copy.setText("Kopieren"))

    def _open_browser(self): webbrowser.open(self.url)

    def _browse_dir(self):
        cur = self.dir_input.text() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Musikverzeichnis wählen", cur)
        if chosen:
            self.dir_input.setText(chosen)

    def _apply_dir(self):
        new_dir = Path(self.dir_input.text().strip())
        if not new_dir.is_dir():
            self.stats_lbl.setText(f"⚠ Verzeichnis nicht gefunden: {new_dir}")
            self.stats_lbl.setStyleSheet("color:#e63;font-size:11px;")
            return
        self.btn_apply.setText("⏳ Lade …")
        self.btn_apply.setEnabled(False)

        def _reload():
            _server.CONFIG["music_dir"] = new_dir
            _server.reload_tracks()
            summary = self._stats_text()
            self.signals.tracks_loaded.emit(summary)

        threading.Thread(target=_reload, daemon=True).start()

    # ── Server ───────────────────────────────────────────────────────────────

    def _start_server(self):
        def run():
            try:
                self._server_thread = threading.Thread(
                    target=_server.start_server, daemon=True)
                self._server_thread.start()
            except Exception as e:
                self.signals.failed.emit(str(e))
                return
            if wait_for_port(PORT):
                self.signals.ready.emit()
            else:
                self.signals.failed.emit("Timeout beim Starten")
        threading.Thread(target=run, daemon=True).start()

    def _stop_server(self):
        self.signals.stopped.emit()

    def closeEvent(self, event):
        self._stop_server()
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setApplicationName("Ownly Audio Pocket")
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())

