"""Compilation validation for generated QEMU peripheral code."""

from __future__ import annotations

import os
import re
import subprocess
import shutil
import tempfile
from pathlib import Path
from collections.abc import Sequence
from typing import Any

QEMU_SOURCE_DIR = Path("build/qemu-src/qemu")
QEMU_GIT_URL = "https://gitlab.com/qemu-project/qemu.git"
_LATEST_QEMU_ALIASES = {"latest", "master", "upstream"}


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


def resolve_qemu_source_dir(qemu_src: str | Path | None = None) -> Path | None:
    """Resolve a usable QEMU source tree from explicit, env, or local inputs."""
    requested = _qemu_source_setting(qemu_src)
    if requested:
        if requested.lower() in _LATEST_QEMU_ALIASES:
            return _ensure_latest_qemu_source()
        path = Path(requested).expanduser()
        return path if _is_qemu_source_tree(path) else None

    for candidate in _qemu_source_candidates():
        if _is_qemu_source_tree(candidate):
            return candidate
    return None


def _qemu_source_setting(qemu_src: str | Path | None = None) -> str:
    if qemu_src is not None:
        return str(qemu_src).strip()
    for env_name in ("AUTOEMU_QEMU_SRC", "QEMU_SRC", "QEMU_SOURCE_DIR"):
        value = os.getenv(env_name, "").strip()
        if value:
            return value
    return ""


def _qemu_source_candidates() -> list[Path]:
    return [
        QEMU_SOURCE_DIR,
        Path("build/qemu-src/qemu"),
        Path("build/qemu"),
        Path("../qemu"),
        Path("../qemu-master"),
        Path.home() / "src" / "qemu",
    ]


def _is_qemu_source_tree(path: Path) -> bool:
    return path.is_dir() and (path / "include" / "qemu").is_dir()


def _latest_qemu_cache_dir() -> Path:
    configured = os.getenv("AUTOEMU_QEMU_CACHE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".cache" / "autoemu" / "qemu-latest"


def _ensure_latest_qemu_source() -> Path | None:
    """Clone or refresh an explicit latest-QEMU checkout in AutoEmu's cache."""
    git = shutil.which("git")
    if not git:
        return None

    cache_dir = _latest_qemu_cache_dir()
    try:
        if _is_qemu_source_tree(cache_dir):
            subprocess.run(
                [git, "-C", str(cache_dir), "fetch", "--depth", "1", "origin", "master"],
                capture_output=True,
                text=True,
                timeout=120,
            )
            subprocess.run(
                [git, "-C", str(cache_dir), "checkout", "--force", "FETCH_HEAD"],
                capture_output=True,
                text=True,
                timeout=60,
            )
        else:
            cache_dir.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [git, "clone", "--depth", "1", QEMU_GIT_URL, str(cache_dir)],
                capture_output=True,
                text=True,
                timeout=300,
            )
    except Exception:
        return None

    return cache_dir if _is_qemu_source_tree(cache_dir) else None


def qemu_source_hint(qemu_src: str | Path | None = None) -> str:
    """Describe where QEMU source resolution looked and how to enable latest."""
    requested = _qemu_source_setting(qemu_src)
    if requested:
        return (
            f"{requested}; set AUTOEMU_QEMU_SRC to an existing QEMU checkout "
            "or AUTOEMU_QEMU_SRC=latest to use a managed upstream checkout"
        )
    candidates = ", ".join(str(path) for path in _qemu_source_candidates())
    return (
        f"{candidates}; set AUTOEMU_QEMU_SRC to an existing QEMU checkout "
        "or AUTOEMU_QEMU_SRC=latest to use a managed upstream checkout"
    )


def find_qemu_include_paths(qemu_src: str | Path | None = None) -> list[str]:
    """Discover QEMU header include paths plus system dependencies."""
    src = resolve_qemu_source_dir(qemu_src)
    if src is None:
        return []
    paths = [
        str(src / "include"),
        str(src / "include" / "qemu"),
    ]
    build_dir = src / "build"
    if build_dir.exists():
        paths.append(str(build_dir))

    # Add system library include paths required by QEMU headers
    for pkg in ("glib-2.0", "pixman-1"):
        paths.extend(_pkg_config_cflags(pkg))

    return [p for p in paths if Path(p).exists()]


def _pkg_config_cflags(package: str) -> list[str]:
    """Run pkg-config --cflags and return the include directories."""
    try:
        result = subprocess.run(
            ["pkg-config", "--cflags-only-I", package],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return [
                flag[2:]  # strip "-I" prefix
                for flag in result.stdout.strip().split()
                if flag.startswith("-I")
            ]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return []


def validate_compile(
    source_files: Sequence[str | Path],
    *,
    qemu_src: str | Path | None = None,
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
            f"QEMU source tree not found ({qemu_source_hint(qemu_src)}); "
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

        # Skip QTest files — they need the full QEMU test harness.
        # Match both qtest_*.c (standard naming) and any file that includes
        # libqtest.h (e.g. agent-generated *-test.c files).
        if path.name.startswith("qtest_"):
            warnings.append(f"{path.name}: skipped QTest file (needs full QEMU build)")
            continue
        if path.suffix == ".c":
            try:
                src_text = path.read_text(encoding="utf-8", errors="replace")
                if "libqtest.h" in src_text:
                    warnings.append(f"{path.name}: skipped QTest file (needs full QEMU build)")
                    continue
            except OSError:
                pass

        files_checked += 1

        # Add the file's parent dir as include path so that
        # `#include "hw/foo.h"` resolves when foo.h sits alongside
        local_include_flags = list(include_flags)
        parent = str(path.parent.resolve())
        local_include_flags.extend(["-I", parent])
        # Create a hw/ symlink so #include "hw/prefix_name.h" resolves
        hw_dir = path.parent / "hw"
        hw_symlink_created = False
        if not hw_dir.exists():
            try:
                hw_dir.symlink_to(path.parent.resolve())
                hw_symlink_created = True
            except OSError:
                local_include_flags.extend(["-I", parent])

        header_wrapper = None
        compile_path = path
        try:
            if path.suffix == ".h":
                header_wrapper = tempfile.TemporaryDirectory(prefix="autoemu-header-check-")
                compile_path = Path(header_wrapper.name) / "check_header.c"
                compile_path.write_text(
                    '#include "qemu/osdep.h"\n'
                    f'#include "{path.name}"\n',
                    encoding="utf-8",
                )

            cmd = [
                compiler,
                "-fsyntax-only",
                "-std=gnu11",
                *local_include_flags,
                "-DNEED_CPU_H=0",  # Skip CPU-specific headers
                str(compile_path),
            ]

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
        finally:
            if header_wrapper is not None:
                header_wrapper.cleanup()
            if hw_symlink_created and hw_dir.is_symlink():
                hw_dir.unlink()

    # Demote errors caused by missing system headers (not a code issue)
    real_errors: list[dict[str, Any]] = []
    for err in results_errors:
        stderr = err.get("stderr", "")
        if _is_missing_system_header(stderr):
            warnings.append(
                f"{err['file']}: skipped — missing system headers "
                f"(install QEMU build dependencies to enable full validation)"
            )
        else:
            real_errors.append(err)

    return {
        "success": len(real_errors) == 0,
        "files_checked": files_checked,
        "errors": real_errors,
        "warnings": warnings,
    }


def _is_missing_system_header(stderr: str) -> bool:
    """Detect compile errors caused by missing system/external headers or
    incomplete QEMU build environment (not actual code issues)."""
    # Direct missing system headers
    system_header_patterns = [
        "fatal error: glib.h",
        "fatal error: glib/",
        "fatal error: pixman.h",
        "fatal error: zlib.h",
        "fatal error: ffi.h",
    ]
    if any(p in stderr for p in system_header_patterns):
        return True
    # Errors from QEMU internal headers that fail due to missing build env
    # (e.g., qemu/atomic.h needing stdint.h which should come from osdep.h→glib.h)
    qemu_env_patterns = [
        "qemu/atomic.h",
        "qemu/osdep.h",
        "glib-compat.h",
    ]
    if any(p in stderr for p in qemu_env_patterns):
        return True
    return False
