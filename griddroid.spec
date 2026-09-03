# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec per GridDroid

import os
import sys

block_cipher = None

a = Analysis(
    ['griddroid_launcher.py'],
    pathex=[os.path.abspath(SPECPATH)],
    binaries=[],
    datas=[
        ('griddroid/static', 'griddroid/static'),
        ('tools', 'tools'),
    ],
    hiddenimports=[
        'griddroid',
        'griddroid.__main__',
        'griddroid.app',
        'griddroid.config',
        'griddroid.adb_manager',
        'griddroid.device',
        'griddroid.stream_engine',
        'griddroid.input_relay',
        'griddroid.bulk_actions',
        'griddroid.log_manager',
        'uvicorn',
        'uvicorn.logging',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        'fastapi',
        'starlette',
        'websockets',
        'aiofiles',
        'PIL',
        'webview',
        'webview.platforms.winforms',
        'pythonnet',
        'clr',
        'psutil',
    ],
    hookspath=[],
    hooksconfig={},
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
    name='GridDroid',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='logo.ico',
)
