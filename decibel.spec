# -*- mode: python ; coding: utf-8 -*-
"""
DECIBEL - PyInstaller Build Specification
Build command: pyinstaller decibel.spec --noconfirm
"""

import os
import sys
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

# Collect all necessary data files
datas = []

# Add static folder
static_path = os.path.join(os.getcwd(), 'static')
if os.path.exists(static_path):
    datas.append((static_path, 'static'))

# Add launcher.py as the main script
# launcher.py imports app.py, so both will be included

# Try to find yt-dlp executable for bundling
ytdlp_binaries = []
# Check for yt-dlp.exe in venv
venv_ytdlp = os.path.join(os.getcwd(), 'venv', 'Scripts', 'yt-dlp.exe')
if os.path.exists(venv_ytdlp):
    ytdlp_binaries.append((venv_ytdlp, '.'))
else:
    # Check system PATH
    import shutil
    ytdlp_path = shutil.which('yt-dlp') or shutil.which('yt-dlp.exe')
    if ytdlp_path:
        ytdlp_binaries.append((ytdlp_path, '.'))

# Add yt-dlp executable if found
binaries = ytdlp_binaries

# Hidden imports for modules that PyInstaller might miss
hiddenimports = [
    'ytmusicapi',
    'mutagen',
    'mutagen.id3',
    'mutagen.mp3',
    'mutagen.easyid3',
    'flask_cors',
    'browser_cookie3',
    'google_auth_oauthlib',
    'google.oauth2',
    'google.auth.transport.requests',
    'webview',
    'webview.platforms.qt',
    'proxy_tools',
    'bottle',
    'wsgiref',
    'wsgiref.simple_server',
    'wsgiref.handlers',
    'wsgiref.util',
    'qtpy',
    # Sub-dependencies
    'charset_normalizer',
    'urllib3',
    'certifi',
    'idna',
    'cffi',
    'pycparser',
    'Cryptodome',
    'Cryptodome.Cipher',
    'Cryptodome.PublicKey',
    'rsa',
    'cachetools',
    'xml',
    'xml.dom',
    'xml.etree',
    'xml.parsers',
    'xml.parsers.expat',
]

# Collect ALL yt_dlp submodules automatically
yt_dlp_modules = collect_submodules('yt_dlp')
hiddenimports.extend(yt_dlp_modules)

# Also collect yt_dlp data files
datas.extend(collect_data_files('yt_dlp', include_py_files=False))

a = Analysis(
    ['launcher.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['rthook_ytdlp.py'],
    excludes=[
        'tkinter',
        'unittest',
        'pydoc',
    ],
    noarchive=False,
    optimize=0,  # Disabled optimization to prevent module stripping
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='DECIBEL',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # No console window for desktop app
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='DECIBEL',
)
