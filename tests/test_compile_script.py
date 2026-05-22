"""Tests for the local binary compilation helper."""

from __future__ import annotations

import ast
import importlib.util
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_compile_script_contract():
    script = ROOT / "compile.sh"

    assert script.is_file()
    assert os.access(script, os.X_OK)

    text = script.read_text(encoding="utf-8")
    assert "set -euo pipefail" in text
    assert 'pip install -e ".[build]"' in text
    assert "pytest" in text
    assert '"$PYTHON_BIN" -m PyInstaller autoemu.spec --clean' in text
    assert "dist/autoemu" in text
    assert "--version" in text


def test_pyinstaller_hidden_imports_exist():
    spec_text = (ROOT / "autoemu.spec").read_text(encoding="utf-8")
    tree = ast.parse(spec_text)
    hiddenimports: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "hiddenimports":
            assert isinstance(node.value, ast.List)
            hiddenimports = [
                item.value
                for item in node.value.elts
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            ]
            break

    assert hiddenimports
    missing = [name for name in hiddenimports if importlib.util.find_spec(name) is None]
    assert missing == []


def test_pyinstaller_collects_codex_sdk_package():
    spec_text = (ROOT / "autoemu.spec").read_text(encoding="utf-8")

    assert "collect_submodules" in spec_text
    assert "codex_app_server" in spec_text
    assert "codex_app_server_client" in spec_text
    assert "codex_app_server_sdk" in spec_text
