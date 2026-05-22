# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Kivy desktop app (Windows + Linux)
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

if sys.platform == 'win32':
    from kivy_deps import sdl2, glew
    _win_trees = [Tree(p) for p in sdl2.dep_bins + glew.dep_bins]
else:
    _win_trees = []

a = Analysis(
    ['kivy_app.py'],
    pathex=['.'],
    binaries=[],
    datas=(
        collect_data_files('kivy') +
        [('icon.png', '.')]
    ),
    hiddenimports=(
        collect_submodules('kivy') +
        [
            'kivy.core.audio.audio_sdl2',
            'kivy.core.audio.audio_gstreamer',
            'kivy.core.window.window_sdl2',
            'kivy.core.text.text_sdl2',
            'kivy.core.image.img_sdl2',
            'kivy.core.image.img_pil',
            'kivy.core.clipboard.clipboard_sdl2',
            'kivy.graphics.cgl_backend.cgl_glew',
            'kivy.graphics.cgl_backend.cgl_sdl2',
        ]
    ),
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6'],
    cipher=None,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    *_win_trees,
    name='ownly-audio-pocket',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    icon='icon.png',
)
