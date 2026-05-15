# -*- mode: python ; coding: utf-8 -*-
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

a = Analysis(
    ['gui.py'],
    pathex=['.'],
    binaries=[],
    datas=(
        collect_data_files('mutagen') +
        collect_data_files('cryptography') +
        collect_data_files('qrcode')
    ),
    hiddenimports=(
        collect_submodules('mutagen') +
        collect_submodules('cryptography') +
        collect_submodules('qrcode') +
        ['PIL', 'PIL.Image', 'PIL.ImageDraw', 'PIL.ImageFont',
         'PyQt5.QtCore', 'PyQt5.QtGui', 'PyQt5.QtWidgets',
         'PyQt5.sip']
    ),
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='ownly-audio-pocket',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,       # no terminal window
    icon=None,
)
