#!/usr/bin/env python3
"""Ownly Audio Pocket – GUI Launcher"""

import sys, subprocess, webbrowser, threading, time, socket, platform, io
from pathlib import Path
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QFrame
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt5.QtGui import QFont, QPixmap, QImage

SERVER_SCRIPT = Path(__file__).resolve().parent / "server.py"
PORT       = 8765
ADMIN_PORT = 8766
IS_WINDOWS = platform.system() == "Windows"

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
    ready   = pyqtSignal()
    failed  = pyqtSignal(str)
    stopped = pyqtSignal()


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.proc    = None
        self.signals = Signals()
        self.ip      = get_local_ip()
        self.url     = f"https://{self.ip}:{PORT}"
        self.admin   = f"http://{self.ip}:{ADMIN_PORT}"

        self._build_ui()
        self.setStyleSheet("QWidget{background:#111;color:#ddd;}")
        self.signals.ready.connect(self._on_ready)
        self.signals.failed.connect(self._on_failed)
        self.signals.stopped.connect(self._on_stopped)
        self._start_server()

    # ── UI ──────────────────────────────────────────────────────────────────

    def _build_ui(self):
        self.setWindowTitle("Ownly Audio Pocket")
        self.setFixedSize(780, 360)

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
        ll.addSpacing(18)

        card = QFrame()
        card.setStyleSheet("background:#1e1e1e;border-radius:10px;")
        cl = QVBoxLayout(card); cl.setContentsMargins(18, 14, 18, 14); cl.setSpacing(8)

        # Player
        lp = QLabel("Player"); lp.setStyleSheet("color:#888;font-size:11px;")
        self.url_lbl = QLabel(self.url)
        self.url_lbl.setStyleSheet("color:#eee;font-size:13px;font-weight:bold;")
        self.url_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        br = QHBoxLayout(); br.setSpacing(8)
        self.btn_copy    = self._btn("Kopieren",          self._copy_url,    "#333")
        self.btn_browser = self._btn("Im Browser öffnen", self._open_browser, "#ee6633")
        br.addWidget(self.btn_copy, 1); br.addWidget(self.btn_browser, 2)
        cl.addWidget(lp); cl.addWidget(self.url_lbl); cl.addLayout(br)

        div = QFrame(); div.setFrameShape(QFrame.HLine)
        div.setStyleSheet("color:#333;"); cl.addWidget(div)

        # Admin
        la = QLabel("Admin"); la.setStyleSheet("color:#888;font-size:11px;")
        self.admin_lbl = QLabel(self.admin)
        self.admin_lbl.setStyleSheet("color:#aaa;font-size:13px;")
        self.admin_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        ar = QHBoxLayout(); ar.setSpacing(8)
        self.btn_copy_admin    = self._btn("Kopieren",          self._copy_admin,        "#333")
        self.btn_browser_admin = self._btn("Im Browser öffnen", self._open_browser_admin, "#333")
        ar.addWidget(self.btn_copy_admin, 1); ar.addWidget(self.btn_browser_admin, 2)
        cl.addWidget(la); cl.addWidget(self.admin_lbl); cl.addLayout(ar)

        ll.addWidget(card)
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

        self.qr_label = QLabel()
        self.qr_label.setAlignment(Qt.AlignCenter)
        self.qr_label.setText("⏳")
        self.qr_label.setStyleSheet("color:#555;font-size:32px;")

        self.qr_hint = QLabel("Mit Handy scannen")
        self.qr_hint.setAlignment(Qt.AlignCenter)
        self.qr_hint.setStyleSheet("color:#666;font-size:11px;margin-top:6px;")

        rl.addWidget(self.qr_label)
        rl.addWidget(self.qr_hint)
        root.addWidget(right)

        for b in (self.btn_copy, self.btn_browser,
                  self.btn_copy_admin, self.btn_browser_admin):
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

    # ── State ────────────────────────────────────────────────────────────────

    def _on_ready(self):
        self.status_dot.setStyleSheet("color:#44cc44;font-size:14px;")
        self.status_lbl.setText("Server läuft")
        self.status_lbl.setStyleSheet("color:#44cc44;font-size:13px;")
        for b in (self.btn_copy, self.btn_browser,
                  self.btn_copy_admin, self.btn_browser_admin, self.btn_stop):
            b.setEnabled(True)
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
                  self.btn_copy_admin, self.btn_browser_admin, self.btn_stop):
            b.setEnabled(False)

    # ── Actions ──────────────────────────────────────────────────────────────

    def _show_qr(self):
        try:
            import qrcode
            qr = qrcode.QRCode(box_size=4, border=2,
                               error_correction=qrcode.constants.ERROR_CORRECT_M)
            qr.add_data(self.url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
            qimg = QImage.fromData(buf.read())
            px = QPixmap.fromImage(qimg).scaled(
                230, 230, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.qr_label.setPixmap(px)
            self.qr_label.setStyleSheet("")
            self.qr_hint.setText("Mit Handy scannen")
            self.qr_hint.setStyleSheet("color:#888;font-size:11px;margin-top:6px;")
        except ImportError:
            self.qr_label.setText("pip install qrcode")
            self.qr_label.setStyleSheet("color:#e63;font-size:11px;")

    def _copy_url(self):
        QApplication.clipboard().setText(self.url)
        self.btn_copy.setText("✓ Kopiert")
        QTimer.singleShot(1500, lambda: self.btn_copy.setText("Kopieren"))

    def _copy_admin(self):
        QApplication.clipboard().setText(self.admin)
        self.btn_copy_admin.setText("✓ Kopiert")
        QTimer.singleShot(1500, lambda: self.btn_copy_admin.setText("Kopieren"))

    def _open_browser(self):    webbrowser.open(self.url)
    def _open_browser_admin(self): webbrowser.open(self.admin)

    # ── Server ───────────────────────────────────────────────────────────────

    def _start_server(self):
        def run():
            try:
                kwargs = {}
                if IS_WINDOWS:
                    # Hide console window on Windows
                    si = subprocess.STARTUPINFO()
                    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    kwargs["startupinfo"] = si
                    kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                self.proc = subprocess.Popen(
                    [sys.executable, str(SERVER_SCRIPT)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    **kwargs,
                )
            except Exception as e:
                self.signals.failed.emit(str(e))
                return
            if wait_for_port(PORT):
                self.signals.ready.emit()
            else:
                self.signals.failed.emit("Timeout beim Starten")

        threading.Thread(target=run, daemon=True).start()

    def _stop_server(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
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
    def __init__(self):
        super().__init__()
        self.proc    = None
        self.signals = Signals()
        self.ip      = get_local_ip()
        self.url     = f"https://{self.ip}:{PORT}"
        self.admin   = f"http://{self.ip}:{ADMIN_PORT}"

        self._build_ui()
        self._apply_dark_theme()
        self.signals.ready.connect(self._on_ready)
        self.signals.failed.connect(self._on_failed)
        self.signals.stopped.connect(self._on_stopped)
        self._start_server()

    # ── UI ──────────────────────────────────────────────────────────────────

    def _build_ui(self):
        self.setWindowTitle("Ownly Audio Pocket")
        self.setFixedSize(480, 310)

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 28, 28, 28)
        root.setSpacing(0)

        # Title
        title = QLabel("🎵 Ownly Audio Pocket")
        title.setFont(QFont("System", 16, QFont.Bold))
        title.setStyleSheet("color: #ee6633;")
        root.addWidget(title)

        root.addSpacing(6)

        # Status row
        self.status_dot = QLabel("●")
        self.status_dot.setStyleSheet("color: #888; font-size: 14px;")
        self.status_lbl = QLabel("Server wird gestartet …")
        self.status_lbl.setStyleSheet("color: #999; font-size: 13px;")
        status_row = QHBoxLayout()
        status_row.setSpacing(6)
        status_row.addWidget(self.status_dot)
        status_row.addWidget(self.status_lbl)
        status_row.addStretch()
        root.addLayout(status_row)

        root.addSpacing(20)

        # URL card
        card = QFrame()
        card.setStyleSheet("background:#1e1e1e; border-radius:10px;")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(18, 14, 18, 14)
        card_layout.setSpacing(10)

        # Player URL
        lbl_player = QLabel("Player")
        lbl_player.setStyleSheet("color:#888; font-size:11px;")
        self.url_lbl = QLabel(self.url)
        self.url_lbl.setStyleSheet("color:#eee; font-size:14px; font-weight:bold;")
        self.url_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self.btn_copy    = self._btn("Kopieren",          self._copy_url,    "#333")
        self.btn_browser = self._btn("Im Browser öffnen", self._open_browser, "#ee6633")
        btn_row.addWidget(self.btn_copy)
        btn_row.addWidget(self.btn_browser)
        btn_row.addStretch()

        card_layout.addWidget(lbl_player)
        card_layout.addWidget(self.url_lbl)
        card_layout.addLayout(btn_row)

        # Divider
        div = QFrame()
        div.setFrameShape(QFrame.HLine)
        div.setStyleSheet("color:#333;")
        card_layout.addWidget(div)

        # Admin URL
        lbl_admin = QLabel("Admin")
        lbl_admin.setStyleSheet("color:#888; font-size:11px;")
        self.admin_lbl = QLabel(self.admin)
        self.admin_lbl.setStyleSheet("color:#aaa; font-size:13px;")
        self.admin_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)

        admin_row = QHBoxLayout()
        admin_row.setSpacing(8)
        self.btn_copy_admin    = self._btn("Kopieren",          self._copy_admin,        "#333")
        self.btn_browser_admin = self._btn("Im Browser öffnen", self._open_browser_admin, "#333")
        admin_row.addWidget(self.btn_copy_admin)
        admin_row.addWidget(self.btn_browser_admin)
        admin_row.addStretch()

        card_layout.addWidget(lbl_admin)
        card_layout.addWidget(self.admin_lbl)
        card_layout.addLayout(admin_row)

        root.addWidget(card)
        root.addStretch()

        # Stop button
        bot_row = QHBoxLayout()
        bot_row.addStretch()
        self.btn_stop = self._btn("Server beenden", self._stop_server, "#550000")
        self.btn_stop.setEnabled(False)
        bot_row.addWidget(self.btn_stop)
        root.addLayout(bot_row)

        # Start disabled
        for b in (self.btn_copy, self.btn_browser,
                  self.btn_copy_admin, self.btn_browser_admin):
            b.setEnabled(False)

    def _btn(self, text, slot, color):
        b = QPushButton(text)
        b.setFixedHeight(32)
        b.setCursor(Qt.PointingHandCursor)
        b.setStyleSheet(
            f"QPushButton{{background:{color};color:#fff;border:none;"
            f"border-radius:6px;padding:0 14px;font-size:12px;}}"
            f"QPushButton:hover{{background:{color}cc;}}"
            f"QPushButton:disabled{{background:#222;color:#555;}}"
        )
        b.clicked.connect(slot)
        return b

    def _apply_dark_theme(self):
        self.setStyleSheet("QWidget{background:#111;color:#ddd;}")

    # ── State ────────────────────────────────────────────────────────────────

    def _on_ready(self):
        self.status_dot.setStyleSheet("color: #44cc44; font-size: 14px;")
        self.status_lbl.setText("Server läuft")
        self.status_lbl.setStyleSheet("color: #44cc44; font-size: 13px;")
        for b in (self.btn_copy, self.btn_browser,
                  self.btn_copy_admin, self.btn_browser_admin,
                  self.btn_stop):
            b.setEnabled(True)

    def _on_failed(self, msg):
        self.status_dot.setStyleSheet("color: #e63; font-size: 14px;")
        self.status_lbl.setText(f"Fehler: {msg}")
        self.status_lbl.setStyleSheet("color: #e63; font-size: 13px;")

    def _on_stopped(self):
        self.status_dot.setStyleSheet("color: #888; font-size: 14px;")
        self.status_lbl.setText("Server gestoppt")
        self.status_lbl.setStyleSheet("color: #888; font-size: 13px;")
        for b in (self.btn_copy, self.btn_browser,
                  self.btn_copy_admin, self.btn_browser_admin,
                  self.btn_stop):
            b.setEnabled(False)

    # ── Actions ──────────────────────────────────────────────────────────────

    def _copy_url(self):
        QApplication.clipboard().setText(self.url)
        self.btn_copy.setText("✓ Kopiert")
        QTimer.singleShot(1500, lambda: self.btn_copy.setText("Kopieren"))

    def _copy_admin(self):
        QApplication.clipboard().setText(self.admin)
        self.btn_copy_admin.setText("✓ Kopiert")
        QTimer.singleShot(1500, lambda: self.btn_copy_admin.setText("Kopieren"))

    def _open_browser(self):
        webbrowser.open(self.url)

    def _open_browser_admin(self):
        webbrowser.open(self.admin)

    # ── Server ───────────────────────────────────────────────────────────────

    def _start_server(self):
        def run():
            try:
                self.proc = subprocess.Popen(
                    [sys.executable, str(SERVER_SCRIPT)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as e:
                self.signals.failed.emit(str(e))
                return

            if wait_for_port(PORT):
                self.signals.ready.emit()
            else:
                self.signals.failed.emit("Timeout beim Starten")

        threading.Thread(target=run, daemon=True).start()

    def _stop_server(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
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
