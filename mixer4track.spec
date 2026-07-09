# -*- mode: python ; coding: utf-8 -*-
"""
mixer4track.spec
PyInstaller spec ファイル
Windows で python -m PyInstaller mixer4track.spec を実行してください。
"""

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[
        *collect_dynamic_libs('pygame'),
    ],
    datas=[
        *collect_data_files('pygame'),
    ],
    hiddenimports=[
        'numpy',
        'numpy.core',
        'numpy.core._multiarray_umath',
        'scipy',
        'scipy.signal',
        'scipy.signal._signaltools',
        'scipy.signal._lti_conversion',
        'scipy.fft',
        'pygame',
        'pygame.mixer',
        'pygame.mixer_music',
        'pygame.sndarray',
        'PyQt5',
        'PyQt5.QtWidgets',
        'PyQt5.QtCore',
        'PyQt5.QtGui',
        'PyQt5.sip',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'pydub',
        'miniaudio',
        'cffi',
        '_cffi_backend',
        'tkinter',
        'matplotlib',
    ],
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
    name='Mixer4Track',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # ウィンドウアプリ（コンソール非表示）
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,              # アイコンファイルがあれば icon='icon.ico' に変更
)
