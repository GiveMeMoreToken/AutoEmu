"""Phase-5 validator: attempt a targeted QEMU build of generated hw/ files.

This module is intentionally soft-fail — a missing build environment or a
compile error in the generated device does **not** block the pipeline.  It
only records whether the generated peripheral compiled cleanly inside a real
QEMU source tree so that users can iterate on the model.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from urllib.parse import urlparse

from autoemu.fetchers.generic import _urlopen_with_retry, _check_content


DEFAULT_BUILD_TIMEOUT = 300  # seconds
POC_DOWNLOAD_TIMEOUT = 15  # seconds


def _resolve_qemu_src(build_env: Path) -> Path | None:
    """Locate the QEMU source tree that corresponds to *build_env*.

    Tries several strategies in order:
    1. Sibling ``env/src/qemu-*`` directories.
    2. ``build.ninja`` ``builddir`` reference.
    3. Parent-parent heuristic ``../../src/qemu-*``.
    """
    # Strategy 1: sibling env/src/qemu-*
    src_dir = build_env.parent.parent / "src"
    if src_dir.is_dir():
        for cand in sorted(src_dir.glob("qemu-*")):
            if (cand / "include" / "qemu").is_dir():
                return cand

    # Strategy 2: inspect build.ninja for source references
    ninja_file = build_env / "build.ninja"
    if ninja_file.exists():
        text = ninja_file.read_text(encoding="utf-8", errors="ignore")
        # Look for builddir = ... or references to source paths
        for line in text.splitlines():
            if line.startswith("builddir ="):
                _, _, val = line.partition("=")
                referenced = build_env / val.strip()
                if (referenced / "include" / "qemu").is_dir():
                    return referenced
                break

    # Strategy 3: relative from env/build/<name> -> env/src/<name>
    rel_src = build_env.parent.parent / "src" / build_env.name
    if (rel_src / "include" / "qemu").is_dir():
        return rel_src

    return None


def run_qemu_probe(
    output_dir: str | Path,
    target_mcu: str,
    target_peripheral: str,
    qemu_build_env: str | Path | None = None,
    cve_findings: dict[str, Any] | None = None,
    on_progress: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    """Run a targeted ninja rebuild of the generated peripheral in QEMU.

    Parameters
    ----------
    output_dir
        Directory containing the generated ``*.c`` / ``*.h`` files.
    target_mcu
        MCU / board name (used to derive the QEMU machine slug).
    target_peripheral
        Peripheral name (used for ``--peripheral`` in ``apply-to-qemu.py``).
    qemu_build_env
        Path to a working QEMU build directory (the one with ``build.ninja``).
        If *None*, ``env/build/qemu-<mcu_slug>`` is tried, followed by a
        glob of ``env/build/qemu-*``.
    cve_findings
        Optional CVE findings dict (from :func:`autoemu.cve_validator.run_cve_check`).
        When provided and ``poc_findings`` is non-empty, PoC sources are
        downloaded and compiled against QEMU headers as part of the probe.
    on_progress
        Optional ``(message, kind)`` callback for log emission.

    Returns
    -------
    dict
        Keys: ``success`` (bool), ``skipped`` (bool), ``reason`` (str),
        ``build_log`` (str), ``poc_results`` (list[dict]).
    """

    def _log(msg: str, kind: str = "info") -> None:
        if on_progress:
            on_progress(msg, kind)

    mcu_slug = _snake(target_mcu)

    # ------------------------------------------------------------------
    # Resolve QEMU build environment
    # ------------------------------------------------------------------
    build_env: Path | None = None
    if qemu_build_env:
        build_env = Path(qemu_build_env)
    else:
        candidates = [
            Path(f"env/build/qemu-{mcu_slug}"),
            Path(f"env/build/qemu-{mcu_slug}-softmmu"),
        ]
        for cand in candidates:
            if cand.exists():
                build_env = cand
                break
        if build_env is None:
            for cand in sorted(Path("env/build").glob("qemu-*")):
                build_env = cand
                break

    if build_env is None or not build_env.exists():
        return {
            "success": False,
            "skipped": True,
            "reason": "QEMU build environment not found in env/build/",
        }

    build_ninja = build_env / "build.ninja"
    if not build_ninja.exists():
        return {
            "success": False,
            "skipped": True,
            "reason": f"No build.ninja in {build_env}",
        }

    # ------------------------------------------------------------------
    # Ensure there are generated files to probe
    # ------------------------------------------------------------------
    gen_dir = Path(output_dir)
    c_files = list(gen_dir.glob("*.c"))
    h_files = list(gen_dir.glob("*.h"))
    if not c_files and not h_files:
        return {
            "success": False,
            "skipped": True,
            "reason": "No generated C/H files to probe",
        }

    # ------------------------------------------------------------------
    # Resolve QEMU source tree from build environment
    # ------------------------------------------------------------------
    qemu_src = _resolve_qemu_src(build_env)
    if qemu_src is None:
        _log("Could not locate QEMU source tree for build env — skipping copy", "warn")
    else:
        _log(f"Resolved QEMU source tree: {qemu_src}")

    # ------------------------------------------------------------------
    # Copy generated files into QEMU source tree
    # ------------------------------------------------------------------
    script = Path("scripts/apply-to-qemu.py")
    if script.exists() and qemu_src is not None:
        _log(f"Copying generated files into QEMU source tree ...", "info")
        cmd = [
            shutil.which("python3") or "python",
            str(script),
            "--output-dir", str(output_dir),
            "--qemu-src", str(qemu_src),
            "--peripheral", target_peripheral,
        ]
        try:
            proc_apply = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            if proc_apply.returncode != 0:
                _log(f"apply-to-qemu.py exited {proc_apply.returncode}", "warn")
                if proc_apply.stdout:
                    _log(f"apply-to-qemu.py stdout:\n{proc_apply.stdout}", "warn")
                if proc_apply.stderr:
                    _log(f"apply-to-qemu.py stderr:\n{proc_apply.stderr}", "warn")
            else:
                if proc_apply.stdout:
                    _log(proc_apply.stdout.strip(), "info")
        except Exception as exc:
            _log(f"apply-to-qemu.py failed: {exc}", "warn")
    else:
        if not script.exists():
            _log("scripts/apply-to-qemu.py not found — skipping copy", "warn")

    # ------------------------------------------------------------------
    # Targeted ninja rebuild
    # ------------------------------------------------------------------
    ninja = shutil.which("ninja") or shutil.which("ninja-build")
    if not ninja:
        return {
            "success": False,
            "skipped": True,
            "reason": "ninja not found in PATH",
        }

    _log(f"Running targeted ninja rebuild in {build_env} ...", "compile")
    target = f"hw/{mcu_slug}/all"
    try:
        proc = subprocess.run(
            [ninja, "-C", str(build_env), target],
            capture_output=True,
            text=True,
            check=False,
            timeout=DEFAULT_BUILD_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "skipped": True,
            "reason": f"ninja rebuild timed out after {DEFAULT_BUILD_TIMEOUT}s",
        }
    except Exception as exc:
        return {
            "success": False,
            "skipped": True,
            "reason": f"ninja rebuild failed: {exc}",
        }

    combined = proc.stdout + proc.stderr
    tail = combined[-2000:] if len(combined) > 2000 else combined

    poc_results: list[dict[str, Any]] = []
    if cve_findings and cve_findings.get("poc_findings"):
        poc_results = _test_poc_sources(
            cve_findings["poc_findings"],
            output_dir=gen_dir,
            build_env=build_env,
            on_progress=_log,
        )

    if proc.returncode == 0:
        return {
            "success": True,
            "skipped": False,
            "reason": "",
            "build_log": tail,
            "poc_results": poc_results,
        }

    # Soft-fail — record the error but don't block the pipeline
    return {
        "success": False,
        "skipped": False,
        "reason": f"ninja returned {proc.returncode}",
        "build_log": tail,
        "poc_results": poc_results,
    }


def _test_poc_sources(
    poc_findings: list[dict[str, str]],
    output_dir: Path,
    build_env: Path,
    on_progress: Callable[[str, str], None],
) -> list[dict[str, Any]]:
    """Download PoC source files and attempt compilation against QEMU headers.

    Returns a list of result dicts with keys:
    ``title``, ``url``, ``success`` (bool), ``path`` (str), ``reason`` (str).
    """
    results: list[dict[str, Any]] = []
    poc_dir = output_dir / "poc"
    poc_dir.mkdir(parents=True, exist_ok=True)

    seen_urls: set[str] = set()
    for poc in poc_findings:
        url = poc.get("url", "")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)

        parsed = urlparse(url)
        path_str = parsed.path
        # Convert GitHub blob URLs to raw URLs
        if "github.com" in parsed.netloc and "/blob/" in path_str:
            raw_url = url.replace("/blob/", "/raw/", 1)
        else:
            raw_url = url

        ext = Path(path_str).suffix.lower()
        if ext not in (".c", ".h"):
            on_progress(f"PoC skipped (not a C/H source): {url}", "warn")
            results.append({
                "title": poc.get("title", ""),
                "url": url,
                "success": False,
                "path": "",
                "reason": "not a C/H source file",
            })
            continue

        filename = Path(path_str).name
        dest = poc_dir / filename
        try:
            from urllib.request import Request
            request = Request(raw_url, headers={"User-Agent": "AutoEmu/0.1"})
            with _urlopen_with_retry(request, timeout=POC_DOWNLOAD_TIMEOUT) as response:
                data = response.read()
        except Exception as exc:
            on_progress(f"PoC download failed: {url} — {exc}", "warn")
            results.append({
                "title": poc.get("title", ""),
                "url": url,
                "success": False,
                "path": "",
                "reason": f"download failed: {exc}",
            })
            continue

        reason = _check_content(data, "poc", filename)
        if reason:
            on_progress(f"PoC content rejected: {filename} — {reason}", "warn")
            results.append({
                "title": poc.get("title", ""),
                "url": url,
                "success": False,
                "path": "",
                "reason": f"content rejected: {reason}",
            })
            continue

        dest.write_bytes(data)

        # Attempt compile against QEMU headers
        cc = shutil.which("gcc") or shutil.which("cc")
        if not cc:
            on_progress(f"PoC compile skipped (no C compiler): {filename}", "warn")
            results.append({
                "title": poc.get("title", ""),
                "url": url,
                "success": False,
                "path": str(dest),
                "reason": "no C compiler found",
            })
            continue

        # Gather QEMU include paths from the build env
        include_paths: list[str] = []
        qemu_include = build_env / "include"
        if qemu_include.exists():
            include_paths.append(str(qemu_include))
        qemu_include2 = build_env.parent / "src" / "qemu-9.2.0" / "include"
        if qemu_include2.exists():
            include_paths.append(str(qemu_include2))
        # Also include generated output dir for the model headers
        include_paths.append(str(output_dir))

        cmd = [cc, "-fsyntax-only", "-c", str(dest)]
        for inc in include_paths:
            cmd.extend(["-I", inc])

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        except Exception as exc:
            on_progress(f"PoC compile error: {filename} — {exc}", "warn")
            results.append({
                "title": poc.get("title", ""),
                "url": url,
                "success": False,
                "path": str(dest),
                "reason": f"compile error: {exc}",
            })
            continue

        if proc.returncode == 0:
            on_progress(f"PoC compiled OK: {filename}", "info")
            results.append({
                "title": poc.get("title", ""),
                "url": url,
                "success": True,
                "path": str(dest),
                "reason": "",
            })
        else:
            stderr_line = proc.stderr.split("\n")[0][:120] if proc.stderr else ""
            on_progress(f"PoC compile failed: {filename} — {stderr_line}", "warn")
            results.append({
                "title": poc.get("title", ""),
                "url": url,
                "success": False,
                "path": str(dest),
                "reason": stderr_line or f"gcc returned {proc.returncode}",
            })

    return results


def _snake(name: str) -> str:
    """Normalise a name to snake_case / slug form."""
    from autoemu.modeling_utils import normalize_name
    return normalize_name(name)
