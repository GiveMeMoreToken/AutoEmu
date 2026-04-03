# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['src/autoemu/main.py'],
    pathex=['src'],
    binaries=[],
    datas=[('AGENTS.md', '.')],
    hiddenimports=[
        'autoemu.agent.backends.claude_backend',
        'autoemu.agent.backends.openai_backend',
        'autoemu.agent.runtime',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name='autoemu',
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
