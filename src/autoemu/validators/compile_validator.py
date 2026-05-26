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
    else:
        # Search for pre-built QEMU trees in common relative locations
        # e.g. env/src/qemu-9.2.0 -> env/build/qemu-aarch64/config-host.h
        for candidate in (src.parent.parent / "build", src.parent / "build"):
            if candidate.exists():
                for subdir in sorted(candidate.iterdir()):
                    if subdir.is_dir() and (subdir / "config-host.h").exists():
                        paths.append(str(subdir))
                        break
                break

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


def _is_missing_system_header(stderr: str) -> bool:
    """Detect compile errors caused by missing system/external headers
    (glib, pixman, zlib, ffi) — not actual code issues."""
    system_header_patterns = [
        "fatal error: glib.h",
        "fatal error: glib/",
        "fatal error: pixman.h",
        "fatal error: zlib.h",
        "fatal error: ffi.h",
        "fatal error: glib-compat.h",
        "fatal error: config-host.h",
    ]
    return any(p in stderr for p in system_header_patterns)


def _probe_qemu_build_env(compiler: str, include_flags: list[str]) -> tuple[bool, bool]:
    """Quick compile probe. Returns (usable, missing_system_deps).

    *usable* is True when the probe compiled cleanly.
    *missing_system_deps* is True when the probe failed specifically because
    system headers (glib, pixman) are missing — in that case we should skip
    the whole batch rather than fail every file.
    """
    try:
        with tempfile.TemporaryDirectory(prefix="autoemu-probe-") as td:
            probe = Path(td) / "probe.c"
            probe.write_text('#include "qemu/osdep.h"\nint main(void){return 0;}\n')
            cmd = [
                compiler, "-fsyntax-only", "-std=gnu11",
                *include_flags, "-DNEED_CPU_H=0",
                str(probe),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if result.returncode == 0:
                return (True, False)
            stderr = result.stderr or ""
            return (False, _is_missing_system_header(stderr))
    except Exception:
        return (False, False)


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
    include_flags: list[str] = []
    for p in include_paths:
        include_flags.extend(["-I", p])

    if not include_paths:
        return {
            "success": True,
            "files_checked": 0,
            "errors": [],
            "warnings": [
                f"QEMU source tree not found ({qemu_source_hint(qemu_src)}); "
                "skipping compilation check"
            ],
        }

    # Pre-flight: verify the build environment can actually compile QEMU headers.
    # If system deps (glib, pixman) are missing, every file would fail with the
    # same error. Detect it once and emit a single warning.
    usable, missing_system_deps = _probe_qemu_build_env(compiler, include_flags)
    if missing_system_deps:
        return {
            "success": True,
            "files_checked": 0,
            "errors": [],
            "warnings": [
                "Missing QEMU build dependencies (glib-2.0, pixman-1); "
                "skipping compilation check"
            ],
        }

    results_errors: list[dict[str, Any]] = []
    warnings: list[str] = []
    files_checked = 0

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

        # QTest files need the full QEMU test framework; skip silently.
        if path.name.startswith("qtest_"):
            continue
        if path.suffix == ".c":
            try:
                src_text = path.read_text(encoding="utf-8", errors="replace")
                if "libqtest.h" in src_text:
                    continue
            except OSError:
                pass

        files_checked += 1

        # Add the file's parent dir as include path so that
        # `#include "foo.h"` resolves when foo.h sits alongside the .c file
        local_include_flags = list(include_flags)
        parent = str(path.parent.resolve())
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

    # Any remaining errors at this point are real code issues, not environment.
    return {
        "success": len(results_errors) == 0,
        "files_checked": files_checked,
        "errors": results_errors,
        "warnings": warnings,
    }


