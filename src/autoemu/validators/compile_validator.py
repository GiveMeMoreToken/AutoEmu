"""Compilation validation for generated QEMU peripheral code."""

from __future__ import annotations

import subprocess
import shutil
from pathlib import Path
from typing import Any

QEMU_SOURCE_DIR = Path("build/qemu-src/qemu-9.2.4")


def find_qemu_include_paths(qemu_src: Path | None = None) -> list[str]:
    """Discover QEMU header include paths."""
    src = qemu_src or QEMU_SOURCE_DIR
    if not src.exists():
        return []
    paths = [
        str(src / "include"),
        str(src / "include" / "qemu"),
    ]
    # Also check for a build directory with generated headers
    build_dir = src / "build"
    if build_dir.exists():
        paths.append(str(build_dir))
    return [p for p in paths if Path(p).exists()]


def validate_compile(
    source_files: list[str | Path],
    *,
    qemu_src: Path | None = None,
    cc: str = "cc",
) -> dict[str, Any]:
    """Compile generated files with -fsyntax-only against QEMU headers.

    Returns dict with:
      - success: bool
      - files_checked: int
      - errors: list[dict] with file, returncode, stderr
      - warnings: list[str]
    """
    compiler = shutil.which(cc) or shutil.which("gcc") or shutil.which("clang")
    if not compiler:
        return {
            "success": False,
            "files_checked": 0,
            "errors": [],
            "warnings": ["No C compiler found (tried cc, gcc, clang)"],
        }

    include_paths = find_qemu_include_paths(qemu_src)
    # Build include flags
    include_flags = []
    for p in include_paths:
        include_flags.extend(["-I", p])

    results_errors: list[dict[str, Any]] = []
    warnings: list[str] = []
    files_checked = 0

    if not include_paths:
        warnings.append(
            f"QEMU source tree not found at {qemu_src or QEMU_SOURCE_DIR}; "
            "skipping compilation check"
        )
        return {
            "success": True,
            "files_checked": 0,
            "errors": [],
            "warnings": warnings,
        }

    for file_path in source_files:
        path = Path(file_path)
        if path.suffix not in (".c", ".h"):
            continue
        files_checked += 1

        cmd = [
            compiler,
            "-fsyntax-only",
            "-std=gnu11",
            *include_flags,
            "-DNEED_CPU_H=0",  # Skip CPU-specific headers
            str(path),
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                results_errors.append({
                    "file": str(path),
                    "returncode": result.returncode,
                    "stderr": result.stderr.strip(),
                })
        except subprocess.TimeoutExpired:
            results_errors.append({
                "file": str(path),
                "returncode": -1,
                "stderr": "Compilation timed out after 30s",
            })
        except Exception as exc:
            results_errors.append({
                "file": str(path),
                "returncode": -1,
                "stderr": str(exc),
            })

    return {
        "success": len(results_errors) == 0,
        "files_checked": files_checked,
        "errors": results_errors,
        "warnings": warnings,
    }
