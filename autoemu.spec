# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['src/autoemu/main.py'],
    pathex=['src'],
    binaries=[],
    datas=[],
    hiddenimports=[
        'autoemu.agent.backends.claude_backend',
        'autoemu.agent.backends.codex_backend',
        'autoemu.agent.backends.openai_backend',
        'autoemu.agent.runtime',
        'autoemu.tui',
        'autoemu.tui.app',
        'autoemu.tui.widgets',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Test / dev dependencies
        'pytest',
        'pytest_asyncio',
        '_pytest',
        'pyinstaller',
    ],
    noarchive=False,
    optimize=2,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='autoemu',
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,
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
