# -*- mode: python ; coding: utf-8 -*-

# NOC Toolkit - PyInstaller Specification File
# This file configures how PyInstaller packages the toolkit

a = Analysis(
    ['noc-toolkit.py'],
    pathex=[],
    binaries=[],
    datas=[
        # Include all tool directories with their Python files and docs
        ('tools', 'tools'),
        # Include configuration templates
        ('.env.example', '.'),
        ('requirements.txt', '.'),
        # Include documentation
        ('README.md', '.'),
        ('README_RU.md', '.'),
    ],
    hiddenimports=[
        # Explicitly include dependencies that might not be auto-detected
        'requests',
        'python-dotenv',
        'dotenv',
        'urllib3',
        'certifi',
        'charset_normalizer',
        'idna',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Exclude unnecessary modules to reduce size
        'matplotlib',
        'numpy',
        'pandas',
        'scipy',
        'tkinter',
        'test',
        'unittest',
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='NOC-Toolkit',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
