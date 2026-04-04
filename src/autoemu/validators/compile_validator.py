"""Compilation validation for generated QEMU peripheral code."""

from __future__ import annotations

import re
import subprocess
import shutil
from pathlib import Path
from typing import Any

QEMU_SOURCE_DIR = Path("build/qemu-src/qemu-9.2.4")


# ---------------------------------------------------------------------------
# Meson.build validation
# ---------------------------------------------------------------------------

def validate_meson_build(meson_path: str | Path) -> dict[str, Any]:
    """Validate a meson.build snippet for structural correctness.

    Checks:
    - File exists and is non-empty.
    - Contains ``system_ss.add(`` call.
    - Contains ``when:`` and ``if_true: files(`` patterns.
    - The referenced ``.c`` file(s) exist in the same directory.

    Returns ``{"valid": bool, "errors": list[str], "warnings": list[str]}``.
    """
    path = Path(meson_path)
    errors: list[str] = []
    warnings: list[str] = []

    if not path.exists():
        errors.append(f"File does not exist: {path}")
        return {"valid": False, "errors": errors, "warnings": warnings}

    content = path.read_text(encoding="utf-8", errors="replace").strip()
    if not content:
        errors.append(f"File is empty: {path}")
        return {"valid": False, "errors": errors, "warnings": warnings}

    if "system_ss.add(" not in content:
        errors.append("Missing 'system_ss.add(' call")

    if "when:" not in content:
        errors.append("Missing 'when:' keyword")

    if not re.search(r"if_true:\s*files\(", content):
        errors.append("Missing 'if_true: files(' pattern")

    # Check that referenced .c files exist next to the meson.build
    parent = path.parent
    for m in re.finditer(r"files\(\s*'([^']+\.c)'", content):
        c_filename = m.group(1)
        if not (parent / c_filename).exists():
            errors.append(f"Referenced source file not found: {c_filename}")

    return {"valid": len(errors) == 0, "errors": errors, "warnings": warnings}


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
        # Validate meson.build files structurally
        if path.name == "meson.build":
            meson_result = validate_meson_build(path)
            files_checked += 1
            if not meson_result["valid"]:
                results_errors.append({
                    "file": str(path),
                    "returncode": 1,
                    "stderr": "; ".join(meson_result["errors"]),
                })
            warnings.extend(meson_result["warnings"])
            continue
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
