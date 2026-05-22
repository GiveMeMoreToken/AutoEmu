#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON:-python}"
SKIP_TESTS="${AUTOEMU_SKIP_TESTS:-1}"

echo "[autoemu] Installing build dependencies"
"$PYTHON_BIN" -m pip install -e ".[build]"

if [[ "$SKIP_TESTS" != "1" ]]; then
    echo "[autoemu] Installing test dependencies"
    "$PYTHON_BIN" -m pip install -e ".[dev]"

    echo "[autoemu] Running tests"
    "$PYTHON_BIN" -m pytest
else
    echo "[autoemu] Skipping tests because AUTOEMU_SKIP_TESTS=1"
fi

echo "[autoemu] Building local binary"
rm -f dist/autoemu
"$PYTHON_BIN" -m PyInstaller autoemu.spec --clean --noconfirm

if [[ ! -x dist/autoemu ]]; then
    echo "[autoemu] ERROR: dist/autoemu was not created or is not executable" >&2
    exit 1
fi

echo "[autoemu] Verifying binary"
dist/autoemu --version

echo "[autoemu] Binary ready: $ROOT_DIR/dist/autoemu"
