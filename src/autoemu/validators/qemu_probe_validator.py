"""Phase-5 validator: attempt a targeted QEMU build of generated hw/ files.

This module is intentionally soft-fail — a missing build environment or a
compile error in the generated device does **not** block the pipeline.  It
only records whether the generated peripheral compiled cleanly inside a real
QEMU source tree so that users can iterate on the model.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from urllib.parse import urlparse

from autoemu.fetchers.generic import _urlopen_with_retry, _check_content


DEFAULT_BUILD_TIMEOUT = 300  # seconds
POC_DOWNLOAD_TIMEOUT = 15  # seconds
DEFAULT_PROBE_TIMEOUT = 45  # seconds
LINUX_BOOT_MARKERS = (
    "Linux version",
    "Kernel command line:",
    "Freeing unused kernel memory",
    "Run /sbin/init",
)
_DTS_MACROS = {
    "GIC_SPI": "0",
    "GIC_PPI": "1",
    "IRQ_TYPE_NONE": "0",
    "IRQ_TYPE_EDGE_RISING": "1",
    "IRQ_TYPE_EDGE_FALLING": "2",
    "IRQ_TYPE_EDGE_BOTH": "3",
    "IRQ_TYPE_LEVEL_HIGH": "4",
    "IRQ_TYPE_LEVEL_LOW": "8",
}


@dataclass(frozen=True)
class BootAssets:
    arch: str
    qemu_bin: Path
    kernel: Path
    rootfs: Path
    dtb: Path | None
    machine: str
    cpu: str
    memory: str
    console: str
    root_dev: str
    drive_if: str
    timeout: int
    extra_args: list[str]


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
    elif os.getenv("AUTOEMU_QEMU_BUILD_DIR", "").strip():
        build_env = Path(os.environ["AUTOEMU_QEMU_BUILD_DIR"].strip())
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
            str(output_dir),
            "--qemu-src", str(qemu_src),
            "--apply-machine-patch",
            "--copy-qtest",
        ]
        _log_stage5_command(_log, cmd)
        try:
            proc_apply = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                env=_stage5_subprocess_env(),
                timeout=60,
            )
            _log_stage5_process(_log, "apply-to-qemu", proc_apply)
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

    target_dir = _infer_qemu_target_dir(gen_dir, target_mcu)
    _log(f"Refreshing QEMU build graph in {build_env} ...", "compile")
    refresh_cmd = [ninja, "-C", str(build_env), "build.ninja"]
    _log_stage5_command(_log, refresh_cmd)
    try:
        proc_refresh = subprocess.run(
            refresh_cmd,
            capture_output=True,
            text=True,
            check=False,
            env=_stage5_subprocess_env(),
            timeout=DEFAULT_BUILD_TIMEOUT,
        )
        _log_stage5_process(_log, "ninja build graph refresh", proc_refresh)
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "skipped": True,
            "reason": f"ninja build graph refresh timed out after {DEFAULT_BUILD_TIMEOUT}s",
        }
    except Exception as exc:
        return {
            "success": False,
            "skipped": True,
            "reason": f"ninja build graph refresh failed: {exc}",
        }

    refresh_log = proc_refresh.stdout + proc_refresh.stderr
    if proc_refresh.returncode != 0:
        tail = refresh_log[-2000:] if len(refresh_log) > 2000 else refresh_log
        return {
            "success": False,
            "skipped": False,
            "reason": f"ninja build graph refresh returned {proc_refresh.returncode}",
            "build_log": tail,
            "poc_results": [],
        }

    hw_c_files = [path for path in c_files if not path.name.startswith("qtest_")]
    build_targets = _infer_generated_ninja_targets(
        build_env=build_env,
        target_dir=target_dir,
        c_files=hw_c_files or c_files,
    )
    if not build_targets:
        generated_names = ", ".join(path.name for path in (hw_c_files or c_files))
        return {
            "success": False,
            "skipped": True,
            "reason": (
                "No QEMU ninja target references generated source(s): "
                f"{generated_names}. Reconfigure/select the generated Kconfig symbol "
                "or apply the model to a build target that includes it."
            ),
            "build_log": refresh_log[-2000:] if len(refresh_log) > 2000 else refresh_log,
            "poc_results": [],
        }

    _log(
        f"Running targeted ninja rebuild in {build_env}: {' '.join(build_targets)}",
        "compile",
    )
    build_cmd = [ninja, "-C", str(build_env), *build_targets]
    _log_stage5_command(_log, build_cmd)
    try:
        proc = subprocess.run(
            build_cmd,
            capture_output=True,
            text=True,
            check=False,
            env=_stage5_subprocess_env(),
            timeout=DEFAULT_BUILD_TIMEOUT,
        )
        _log_stage5_process(_log, "generated object rebuild", proc)
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

    combined = refresh_log + proc.stdout + proc.stderr
    tail = combined[-2000:] if len(combined) > 2000 else combined

    poc_results: list[dict[str, Any]] = []
    if cve_findings and cve_findings.get("poc_findings"):
        poc_results = _test_poc_sources(
            cve_findings["poc_findings"],
            output_dir=gen_dir,
            build_env=build_env,
            on_progress=_log,
        )

    if proc.returncode != 0:
        # Soft-fail — record the error but don't block the pipeline
        return {
            "success": False,
            "skipped": False,
            "reason": f"ninja returned {proc.returncode}",
            "build_log": tail,
            "build_targets": build_targets,
            "poc_results": poc_results,
        }

    _enable_generated_config(gen_dir, build_env, qemu_src, on_progress=_log)
    system_target = _qemu_system_target(build_env)
    all_build_targets = [*build_targets, system_target]
    _log(f"Rebuilding runnable QEMU binary in {build_env}: {system_target}", "compile")
    system_cmd = [ninja, "-C", str(build_env), system_target]
    _log_stage5_command(_log, system_cmd)
    try:
        proc_system = subprocess.run(
            system_cmd,
            capture_output=True,
            text=True,
            check=False,
            env=_stage5_subprocess_env(),
            timeout=DEFAULT_BUILD_TIMEOUT,
        )
        _log_stage5_process(_log, "QEMU system binary rebuild", proc_system)
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "skipped": True,
            "reason": f"QEMU system binary rebuild timed out after {DEFAULT_BUILD_TIMEOUT}s",
            "build_log": tail,
            "build_targets": all_build_targets,
            "poc_results": poc_results,
            "probe_status": "boot_failed",
        }
    except Exception as exc:
        return {
            "success": False,
            "skipped": True,
            "reason": f"QEMU system binary rebuild failed: {exc}",
            "build_log": tail,
            "build_targets": all_build_targets,
            "poc_results": poc_results,
            "probe_status": "boot_failed",
        }

    combined += proc_system.stdout + proc_system.stderr
    tail = combined[-2000:] if len(combined) > 2000 else combined
    if proc_system.returncode != 0:
        return {
            "success": False,
            "skipped": False,
            "reason": f"QEMU system binary rebuild returned {proc_system.returncode}",
            "build_log": tail,
            "build_targets": all_build_targets,
            "poc_results": poc_results,
            "probe_status": "boot_failed",
        }

    boot_assets, boot_error = _resolve_boot_assets(build_env, output_dir=gen_dir)
    if boot_assets is None:
        return {
            "success": False,
            "skipped": True,
            "reason": boot_error,
            "build_log": tail,
            "build_targets": all_build_targets,
            "poc_results": poc_results,
            "probe_status": "skipped",
            "boot_assets": {},
        }

    prepared_dtb = _prepare_probe_dtb(
        boot_assets,
        output_dir=gen_dir,
        target_mcu=target_mcu,
        target_peripheral=target_peripheral,
        on_progress=_log,
    )
    if prepared_dtb is not None:
        boot_assets = replace(boot_assets, dtb=prepared_dtb)

    prepared_rootfs = _prepare_probe_rootfs(
        boot_assets,
        build_env=build_env,
        output_dir=gen_dir,
        target_mcu=target_mcu,
        target_peripheral=target_peripheral,
        on_progress=_log,
    )
    if prepared_rootfs is not None:
        boot_assets = replace(boot_assets, rootfs=prepared_rootfs)

    boot_result = _run_guest_linux_probe(
        boot_assets,
        output_dir=gen_dir,
        target_mcu=target_mcu,
        target_peripheral=target_peripheral,
        on_progress=_log,
    )
    return {
        **boot_result,
        "skipped": False,
        "build_log": tail,
        "build_targets": all_build_targets,
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
                env=_stage5_subprocess_env(),
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


def _format_stage5_command(cmd: list[str] | tuple[str, ...]) -> str:
    return shlex.join(str(part) for part in cmd)


def _log_stage5_command(
    on_progress: Callable[[str, str], None],
    cmd: list[str] | tuple[str, ...],
) -> None:
    on_progress(f"Stage 5 command: {_format_stage5_command(cmd)}", "compile")


def _log_stage5_process(
    on_progress: Callable[[str, str], None],
    label: str,
    proc: subprocess.CompletedProcess[str],
    *,
    stdout_label: str = "stdout",
    stderr_label: str = "stderr",
) -> None:
    kind = "compile" if proc.returncode == 0 else "fail"
    on_progress(f"Stage 5 result: returncode={proc.returncode} ({label})", kind)
    _log_stage5_stream(on_progress, stdout_label, proc.stdout, "compile")
    stderr_kind = "warn" if proc.returncode == 0 else "fail"
    _log_stage5_stream(on_progress, stderr_label, proc.stderr, stderr_kind)


def _log_stage5_stream(
    on_progress: Callable[[str, str], None],
    label: str,
    text: str,
    kind: str,
    *,
    limit: int = 2000,
) -> None:
    if not text:
        return
    trimmed = text[-limit:] if len(text) > limit else text
    on_progress(f"Stage 5 {label}:\n{trimmed.rstrip()}", kind)


def _stage5_subprocess_env() -> dict[str, str]:
    """Return an environment suitable for external toolchain/QEMU commands."""
    env = os.environ.copy()
    pyinstaller_dirs = {
        value
        for key, value in env.items()
        if key.startswith("_PYI") and value
    }

    ld_library_path = env.get("LD_LIBRARY_PATH", "")
    if ld_library_path:
        kept = [
            part for part in ld_library_path.split(os.pathsep)
            if part and not _looks_like_pyinstaller_path(part, pyinstaller_dirs)
        ]
        if kept:
            env["LD_LIBRARY_PATH"] = os.pathsep.join(kept)
        else:
            env.pop("LD_LIBRARY_PATH", None)

    for key in list(env):
        if key.startswith("_PYI") or key.startswith("PYINSTALLER"):
            env.pop(key, None)

    return env


def _looks_like_pyinstaller_path(path: str, pyinstaller_dirs: set[str]) -> bool:
    path_obj = Path(path)
    if any(path == base or path.startswith(f"{base}{os.sep}") for base in pyinstaller_dirs):
        return True
    return path_obj.name.startswith("_MEI")


def _prepare_probe_dtb(
    assets: BootAssets,
    *,
    output_dir: Path,
    target_mcu: str,
    target_peripheral: str,
    on_progress: Callable[[str, str], None],
) -> Path | None:
    """Build an output-local DTB from a generated DTS overlay for Linux probing."""
    dtso = _select_probe_dtso(output_dir, target_peripheral)
    if dtso is None:
        return None
    output_probe_dtb = output_dir / "probe.dtb"
    if assets.dtb is not None and assets.dtb.resolve() != output_probe_dtb.resolve():
        return None

    dtc = shutil.which("dtc")
    fdtoverlay = shutil.which("fdtoverlay")
    missing_tools = [name for name, path in (("dtc", dtc), ("fdtoverlay", fdtoverlay)) if not path]
    if missing_tools:
        on_progress(
            "Cannot build probe DTB; missing tool(s): " + ", ".join(missing_tools),
            "warn",
        )
        return None

    base_dtb = output_dir / "probe-base.dtb"
    overlay_dts = output_dir / "probe-overlay.dts"
    overlay_dtbo = output_dir / "probe.dtbo"
    probe_dtb = output_dir / "probe.dtb"

    on_progress(
        f"Building probe DTB from generated device-tree overlay: {dtso}",
        "compile",
    )
    try:
        overlay_dts.write_text(
            _build_probe_overlay_source(dtso, output_dir, target_mcu, target_peripheral),
            encoding="utf-8",
        )
    except Exception as exc:
        on_progress(f"Cannot prepare probe overlay source: {exc}", "warn")
        return None

    dump_cmd = [
        str(assets.qemu_bin),
        "-M",
        _machine_with_dumpdtb(assets.machine, base_dtb),
        "-machine",
        "accel=tcg",
    ]
    dtc_cmd = [dtc, "-@", "-I", "dts", "-O", "dtb", "-o", str(overlay_dtbo), str(overlay_dts)]
    overlay_cmd = [fdtoverlay, "-i", str(base_dtb), "-o", str(probe_dtb), str(overlay_dtbo)]

    for cmd, label in (
        (dump_cmd, "QEMU base DTB dump"),
        (dtc_cmd, "device-tree overlay compile"),
        (overlay_cmd, "device-tree overlay apply"),
    ):
        _log_stage5_command(on_progress, cmd)
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                env=_stage5_subprocess_env(),
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            on_progress(f"Stage 5 result: timeout after 60s ({label})", "warn")
            return None
        except Exception as exc:
            on_progress(f"Stage 5 {label} failed: {exc}", "warn")
            return None
        _log_stage5_process(on_progress, label, proc)
        if proc.returncode != 0:
            return None

    if not probe_dtb.exists():
        on_progress(f"Probe DTB was not created: {probe_dtb}", "warn")
        return None

    on_progress(f"Using generated probe DTB for Linux boot: {probe_dtb}", "compile")
    return probe_dtb


def _prepare_probe_rootfs(
    assets: BootAssets,
    *,
    build_env: Path,
    output_dir: Path,
    target_mcu: str,
    target_peripheral: str,
    on_progress: Callable[[str, str], None],
) -> Path | None:
    """Create a rootfs copy that loads matching Linux driver modules."""
    linux_build = _linux_build_dir_for_arch(build_env, assets.arch)
    if linux_build is None:
        return None

    modules = _probe_module_closure(linux_build, target_mcu, target_peripheral)
    if not modules:
        return None

    e2mkdir = shutil.which("e2mkdir")
    e2cp = shutil.which("e2cp")
    if not e2mkdir or not e2cp:
        on_progress("Cannot prepare Stage 5 rootfs; e2tools are missing", "warn")
        return None

    stage5_rootfs = output_dir / f"rootfs-{assets.arch}-stage5.ext4"
    source_rootfs = assets.rootfs
    if assets.rootfs.resolve() == stage5_rootfs.resolve():
        base_rootfs = build_env.parent.parent / "output" / f"rootfs-{assets.arch}.ext4"
        if not base_rootfs.exists():
            return None
        source_rootfs = base_rootfs
    try:
        shutil.copy2(source_rootfs, stage5_rootfs)
    except Exception as exc:
        on_progress(f"Cannot copy Stage 5 rootfs: {exc}", "warn")
        return None
    if not _run_rootfs_fsck(stage5_rootfs, on_progress):
        return None

    module_dir = "/lib/modules/autoemu-stage5"
    on_progress(
        f"Preparing Stage 5 rootfs with Linux probe module(s): "
        + ", ".join(path.name for path in modules),
        "compile",
    )

    for directory in ("/lib", "/lib/modules", module_dir, "/etc", "/etc/init.d"):
        _run_e2tool([e2mkdir, f"{stage5_rootfs}:{directory}"], on_progress, "rootfs directory create")

    for module in modules:
        cmd = [e2cp, str(module), f"{stage5_rootfs}:{module_dir}/{module.name}"]
        if not _run_e2tool(cmd, on_progress, "rootfs module copy"):
            return None

    init_script = output_dir / "autoemu-stage5-init"
    init_script.write_text(
        _stage5_module_init_script(module_dir, modules),
        encoding="utf-8",
    )
    init_script.chmod(0o755)
    if not _run_e2tool(
        [e2cp, "-P", "755", str(init_script), f"{stage5_rootfs}:/etc/init.d/S05autoemu-probe"],
        on_progress,
        "rootfs probe init copy",
    ):
        return None

    if not _run_rootfs_fsck(stage5_rootfs, on_progress):
        return None

    return stage5_rootfs


def _run_rootfs_fsck(
    rootfs: Path,
    on_progress: Callable[[str, str], None],
) -> bool:
    e2fsck = shutil.which("e2fsck")
    if not e2fsck:
        return True
    cmd = [e2fsck, "-fy", str(rootfs)]
    _log_stage5_command(on_progress, cmd)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            env=_stage5_subprocess_env(),
            timeout=120,
        )
    except Exception as exc:
        on_progress(f"Stage 5 rootfs fsck failed: {exc}", "warn")
        return False
    kind = "compile" if proc.returncode in (0, 1) else "fail"
    on_progress(f"Stage 5 result: returncode={proc.returncode} (rootfs fsck)", kind)
    _log_stage5_stream(on_progress, "stdout", proc.stdout, "compile")
    stderr_kind = "warn" if proc.returncode in (0, 1) else "fail"
    _log_stage5_stream(on_progress, "stderr", proc.stderr, stderr_kind)
    return proc.returncode in (0, 1)


def _run_e2tool(
    cmd: list[str],
    on_progress: Callable[[str, str], None],
    label: str,
) -> bool:
    _log_stage5_command(on_progress, cmd)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            env=_stage5_subprocess_env(),
            timeout=60,
        )
    except Exception as exc:
        on_progress(f"Stage 5 {label} failed: {exc}", "warn")
        return False
    _log_stage5_process(on_progress, label, proc)
    return proc.returncode == 0 or label == "rootfs directory create"


def _linux_build_dir_for_arch(build_env: Path, arch: str) -> Path | None:
    candidates = [
        build_env.parent / f"linux-{arch}",
        build_env.parent.parent / "build" / f"linux-{arch}",
        Path("env") / "build" / f"linux-{arch}",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _probe_module_closure(
    linux_build: Path,
    target_mcu: str,
    target_peripheral: str,
) -> list[Path]:
    candidates = _probe_module_candidates(linux_build, target_mcu, target_peripheral)
    if not candidates:
        return []

    all_modules = sorted(linux_build.rglob("*.ko"))
    module_index: dict[str, Path] = {}
    for module in all_modules:
        for name in _module_names(module):
            module_index.setdefault(name, module)

    ordered: list[Path] = []
    visiting: set[Path] = set()
    visited: set[Path] = set()

    def add(module: Path) -> None:
        if module in visited or module in visiting:
            return
        visiting.add(module)
        for dep in _module_dependencies(module):
            dep_path = module_index.get(dep) or module_index.get(dep.replace("-", "_"))
            if dep_path is not None:
                add(dep_path)
        visiting.remove(module)
        visited.add(module)
        ordered.append(module)

    for candidate in candidates:
        add(candidate)
    return ordered


def _probe_module_candidates(
    linux_build: Path,
    target_mcu: str,
    target_peripheral: str,
) -> list[Path]:
    prefixes: set[str] = set()
    target_root = Path("data") / _snake(target_mcu) / "driver"
    peripheral = _snake(target_peripheral)
    peripheral_terms = {
        term for term in {peripheral, target_peripheral.lower(), *peripheral.split("_")}
        if len(term) >= 3
    }
    if target_root.exists():
        for path in sorted(target_root.rglob("*.[ch]")):
            stem = path.stem.lower()
            try:
                text = path.read_text(encoding="utf-8", errors="ignore").lower()
            except OSError:
                text = ""
            if peripheral_terms and not any(term in stem or term in text for term in peripheral_terms):
                continue
            if stem:
                prefixes.add(stem.split("_", 1)[0])
    if len(peripheral) >= 4:
        prefixes.add(peripheral)

    candidates: list[Path] = []
    for prefix in sorted(prefixes):
        if len(prefix) < 4:
            continue
        candidates.extend(sorted(linux_build.rglob(f"{prefix}.ko")))
        candidates.extend(sorted(linux_build.rglob(f"{prefix}-*.ko")))
        candidates.extend(sorted(linux_build.rglob(f"{prefix}_*.ko")))
    return _dedupe_paths(candidates)


def _module_names(module: Path) -> set[str]:
    names = {module.stem, module.stem.replace("_", "-"), module.stem.replace("-", "_")}
    modinfo = shutil.which("modinfo")
    if not modinfo:
        return names
    try:
        proc = subprocess.run(
            [modinfo, "-F", "name", str(module)],
            capture_output=True,
            text=True,
            check=False,
            env=_stage5_subprocess_env(),
            timeout=30,
        )
    except Exception:
        return names
    if proc.returncode == 0:
        name = proc.stdout.strip()
        if name:
            names.update({name, name.replace("_", "-"), name.replace("-", "_")})
    return names


def _module_dependencies(module: Path) -> list[str]:
    modinfo = shutil.which("modinfo")
    if not modinfo:
        return []
    try:
        proc = subprocess.run(
            [modinfo, "-F", "depends", str(module)],
            capture_output=True,
            text=True,
            check=False,
            env=_stage5_subprocess_env(),
            timeout=30,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    deps = [item.strip() for item in proc.stdout.strip().split(",") if item.strip()]
    normalized: list[str] = []
    for dep in deps:
        normalized.extend([dep, dep.replace("_", "-"), dep.replace("-", "_")])
    return normalized


def _stage5_module_init_script(module_dir: str, modules: list[Path]) -> str:
    module_paths = " ".join(f"{module_dir}/{module.name}" for module in modules)
    return (
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  start|\"\")\n"
        "    echo \"AutoEmu Stage 5: loading Linux probe modules\"\n"
        f"    for module in {module_paths}; do\n"
        "      [ -e \"$module\" ] || continue\n"
        "      insmod \"$module\" 2>&1 || true\n"
        "    done\n"
        "    ;;\n"
        "esac\n"
        "exit 0\n"
    )


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    result: list[Path] = []
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        result.append(path)
    return result


def _select_probe_dtso(output_dir: Path, target_peripheral: str) -> Path | None:
    peripheral = _snake(target_peripheral)
    preferred = output_dir / f"{peripheral}.dtso"
    if preferred.exists():
        return preferred
    candidates = sorted(output_dir.glob("*.dtso"))
    return candidates[0] if candidates else None


def _machine_with_dumpdtb(machine: str, dtb_path: Path) -> str:
    if "dumpdtb=" in machine:
        return machine
    return f"{machine},dumpdtb={dtb_path}"


def _build_probe_overlay_source(
    dtso_path: Path,
    output_dir: Path,
    target_mcu: str,
    target_peripheral: str,
) -> str:
    """Create a plugin overlay source from generated and fetched evidence."""
    text = _sanitize_dtso_text(dtso_path.read_text(encoding="utf-8", errors="ignore"))
    node = _extract_first_dts_node(text)
    if node is None:
        return _wrap_raw_dtso_as_overlay(text)

    node_name, body = node
    docs = _dts_evidence_for_base(output_dir, target_mcu)
    generated_compatibles = _dts_string_values(_extract_dts_property(text, "compatible"))
    compatibles = _select_probe_compatibles(
        output_dir=output_dir,
        target_mcu=target_mcu,
        target_peripheral=target_peripheral,
        generated_compatibles=generated_compatibles,
        doc_compatibles=docs.get("compatibles", []),
    )
    if not compatibles:
        compatibles = generated_compatibles

    reg_value = _extract_dts_property(text, "reg") or docs.get("reg", "")
    interrupt_value = _normalize_dts_cells(
        _extract_dts_property(text, "interrupts") or docs.get("interrupts", "")
    )
    interrupt_names = _select_probe_interrupt_names(target_mcu, target_peripheral, docs)
    driver_text = "\n".join(_target_driver_source_texts(target_mcu, target_peripheral))
    needs_clock = "devm_clk_get" in driver_text and not _extract_dts_property(body, "clocks")
    supplies = _probe_supply_names(driver_text)

    body_rest = body
    for prop in (
        "compatible",
        "reg",
        "interrupts",
        "interrupt-names",
        "clocks",
        "clock-names",
        "status",
    ):
        body_rest = _remove_dts_property(body_rest, prop)
    for supply in supplies:
        body_rest = _remove_dts_property(body_rest, f"{supply}-supply")

    overlay_lines = [
        "/dts-v1/;",
        "/plugin/;",
        "",
        "/ {",
        "    fragment@0 {",
        "        target-path = \"/\";",
        "        __overlay__ {",
    ]
    if needs_clock:
        overlay_lines.extend([
            "            autoemu_probe_clk: autoemu-probe-clock {",
            "                compatible = \"fixed-clock\";",
            "                #clock-cells = <0>;",
            "                clock-frequency = <100000000>;",
            "            };",
            "",
        ])
    if supplies:
        overlay_lines.extend([
            "            autoemu_probe_regulator: autoemu-probe-regulator {",
            "                compatible = \"regulator-fixed\";",
            "                regulator-name = \"autoemu-probe\";",
            "                regulator-min-microvolt = <1000000>;",
            "                regulator-max-microvolt = <1000000>;",
            "            };",
            "",
        ])

    overlay_lines.append(f"            {node_name} {{")
    if compatibles:
        quoted = ", ".join(f'"{item}"' for item in compatibles)
        overlay_lines.append(f"                compatible = {quoted};")
    if reg_value:
        overlay_lines.append(f"                reg = {_ensure_dts_value(reg_value)};")
    if interrupt_value:
        overlay_lines.append(f"                interrupts = {interrupt_value};")
    if interrupt_names:
        quoted_names = ", ".join(f'"{name}"' for name in interrupt_names)
        overlay_lines.append(f"                interrupt-names = {quoted_names};")
    if needs_clock:
        overlay_lines.append("                clocks = <&autoemu_probe_clk>;")
    for supply in supplies:
        overlay_lines.append(f"                {supply}-supply = <&autoemu_probe_regulator>;")
    overlay_lines.append("                status = \"okay\";")

    for line in body_rest.splitlines():
        stripped = line.strip()
        if stripped:
            overlay_lines.append(f"                {stripped}")

    overlay_lines.extend([
        "            };",
        "        };",
        "    };",
        "};",
        "",
    ])
    return "\n".join(overlay_lines)


def _sanitize_dtso_text(text: str) -> str:
    lines = [line for line in text.splitlines() if line.strip() != "EOF"]
    return "\n".join(lines).strip() + "\n"


def _wrap_raw_dtso_as_overlay(text: str) -> str:
    if "/plugin/;" in text:
        return text
    return "/dts-v1/;\n/plugin/;\n\n" + text


def _extract_first_dts_node(text: str) -> tuple[str, str] | None:
    lines = text.splitlines()
    start = None
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if "{" not in stripped or stripped.startswith("/") or stripped.startswith("fragment@"):
            continue
        if "@" in stripped or ":" in stripped:
            start = idx
            break
    if start is None:
        return None

    node_lines: list[str] = []
    depth = 0
    for line in lines[start:]:
        depth += line.count("{") - line.count("}")
        node_lines.append(line)
        if depth <= 0 and len(node_lines) > 1:
            break

    header = node_lines[0].strip().split("{", 1)[0].strip()
    if ":" in header:
        header = header.split(":", 1)[1].strip()
    body = "\n".join(node_lines[1:-1])
    return header, body


def _extract_dts_property(text: str, prop: str) -> str:
    pattern = re.compile(rf"(?ms)^\s*{re.escape(prop)}\s*=\s*(.*?);")
    match = pattern.search(text)
    return match.group(1).strip() if match else ""


def _remove_dts_property(text: str, prop: str) -> str:
    return re.sub(rf"(?ms)^\s*{re.escape(prop)}\s*=\s*.*?;\s*", "", text)


def _dts_string_values(value: str) -> list[str]:
    return [match.group(1) for match in re.finditer(r'"([^"]+)"', value)]


def _ensure_dts_value(value: str) -> str:
    value = value.strip()
    return value if value.startswith("<") or value.startswith('"') else f"<{value}>"


def _normalize_dts_cells(value: str) -> str:
    if not value:
        return ""
    replaced = value
    for name, replacement in _DTS_MACROS.items():
        replaced = re.sub(rf"\b{re.escape(name)}\b", replacement, replaced)
    cells = re.findall(r"0x[0-9a-fA-F]+|\b\d+\b", replaced)
    if not cells:
        return _ensure_dts_value(value)
    numbers = [str(int(cell, 0)) for cell in cells]
    return "<" + " ".join(numbers) + ">"


def _dts_cell_numbers(value: str) -> list[int]:
    normalized = _normalize_dts_cells(value)
    return [int(cell, 0) for cell in re.findall(r"0x[0-9a-fA-F]+|\b\d+\b", normalized)]


def _dts_evidence_for_base(output_dir: Path, target_mcu: str) -> dict[str, Any]:
    base = _peripheral_base_address(output_dir)
    if base is None:
        return {}
    docs_root = Path("data") / _snake(target_mcu) / "docs"
    if not docs_root.exists():
        return {}
    for path in sorted(list(docs_root.rglob("*.dts")) + list(docs_root.rglob("*.dtsi"))):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for node_name, body in _iter_dts_nodes(text):
            reg = _extract_dts_property(body, "reg")
            if base not in _dts_cell_numbers(reg):
                continue
            return {
                "node": node_name,
                "reg": reg,
                "interrupts": _extract_dts_property(body, "interrupts"),
                "interrupt_names": _dts_string_values(_extract_dts_property(body, "interrupt-names")),
                "compatibles": _dts_string_values(_extract_dts_property(body, "compatible")),
            }
    return {}


def _iter_dts_nodes(text: str) -> list[tuple[str, str]]:
    nodes: list[tuple[str, str]] = []
    lines = _sanitize_dtso_text(text).splitlines()
    idx = 0
    while idx < len(lines):
        stripped = lines[idx].strip()
        if "{" not in stripped or stripped.startswith("/") or stripped.startswith("#"):
            idx += 1
            continue
        if "@" not in stripped and ":" not in stripped:
            idx += 1
            continue
        depth = 0
        node_lines: list[str] = []
        for line in lines[idx:]:
            depth += line.count("{") - line.count("}")
            node_lines.append(line)
            if depth <= 0 and len(node_lines) > 1:
                break
        header = node_lines[0].strip().split("{", 1)[0].strip()
        if ":" in header:
            header = header.split(":", 1)[1].strip()
        nodes.append((header, "\n".join(node_lines[1:-1])))
        idx += max(1, len(node_lines))
    return nodes


def _peripheral_base_address(output_dir: Path) -> int | None:
    for json_path in sorted(output_dir.glob("*.json")):
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        found = _find_json_base_address(data)
        if found is not None:
            return found
    return None


def _find_json_base_address(value: Any) -> int | None:
    if isinstance(value, dict):
        if "base_address" in value:
            parsed = _parse_int(value["base_address"])
            if parsed is not None:
                return parsed
        for item in value.values():
            found = _find_json_base_address(item)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_json_base_address(item)
            if found is not None:
                return found
    return None


def _parse_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 0)
        except ValueError:
            return None
    return None


def _select_probe_compatibles(
    *,
    output_dir: Path,
    target_mcu: str,
    target_peripheral: str,
    generated_compatibles: list[str],
    doc_compatibles: list[str],
) -> list[str]:
    driver_compatibles = _driver_compatible_strings(target_mcu, target_peripheral)
    evidence_words = _probe_evidence_words(output_dir, target_mcu, target_peripheral)
    selected_driver: list[str] = []
    for compatible in driver_compatibles:
        compatible_words = set(re.findall(r"[a-z][a-z0-9]+", compatible.lower()))
        specific_words = compatible_words - {"arm", "mali", "gpu", "vendor", "compatible"}
        if compatible in doc_compatibles or compatible in generated_compatibles:
            selected_driver.append(compatible)
        elif specific_words & evidence_words:
            selected_driver.append(compatible)
    if not selected_driver and len(driver_compatibles) <= 3:
        selected_driver = driver_compatibles

    return _dedupe([*selected_driver, *doc_compatibles, *generated_compatibles])


def _driver_compatible_strings(target_mcu: str, target_peripheral: str) -> list[str]:
    values: list[str] = []
    for text in _target_driver_source_texts(target_mcu, target_peripheral):
        values.extend(re.findall(r"\.compatible\s*=\s*\"([^\"]+)\"", text))
    return _dedupe(values)


def _probe_evidence_words(output_dir: Path, target_mcu: str, target_peripheral: str) -> set[str]:
    words = {part for part in _snake(target_peripheral).split("_") if part}
    docs_root = Path("data") / _snake(target_mcu) / "docs"
    texts: list[str] = []
    for json_path in sorted(output_dir.glob("*.json")):
        try:
            texts.append(json_path.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            pass
    if docs_root.exists():
        for path in sorted(docs_root.rglob("*")):
            if path.suffix.lower() not in (".md", ".txt", ".dts", ".dtsi"):
                continue
            try:
                texts.append(path.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                continue
    combined = "\n".join(texts).lower()
    words.update(re.findall(r"\b(?:bifrost|midgard|valhall|t\d{3,4}|g\d{2,4})\b", combined))
    for match in re.finditer(r"mali[\s_-]+([tg]\d{2,4})", combined):
        words.add(match.group(1))
    return words


def _select_probe_interrupt_names(
    target_mcu: str,
    target_peripheral: str,
    docs: dict[str, Any],
) -> list[str]:
    driver_names = _driver_irq_names(target_mcu, target_peripheral)
    doc_names = [str(name).lower() for name in docs.get("interrupt_names", [])]
    if doc_names and (not driver_names or set(doc_names).issubset(set(driver_names))):
        return doc_names
    return driver_names or doc_names


def _driver_irq_names(target_mcu: str, target_peripheral: str) -> list[str]:
    names: list[str] = []
    for text in _target_driver_source_texts(target_mcu, target_peripheral):
        names.extend(re.findall(r"platform_get_irq_byname\s*\([^;]*?\"([^\"]+)\"", text, re.S))
    return _dedupe(names)


def _probe_supply_names(driver_text: str) -> list[str]:
    names: list[str] = []
    for match in re.finditer(
        r"const\s+char\s+\*\s+const\s+\w*supplies\[\]\s*=\s*\{(?P<body>.*?)\};",
        driver_text,
        re.S,
    ):
        names.extend(
            value for value in re.findall(r'"([^"]+)"', match.group("body"))
            if value.lower() != "null"
        )
    return _dedupe(names[:4])


def _target_driver_source_texts(target_mcu: str, target_peripheral: str) -> list[str]:
    target_root = Path("data") / _snake(target_mcu) / "driver"
    source_paths: list[Path] = []
    prefixes: set[str] = set()
    if target_root.exists():
        for path in sorted(target_root.rglob("*.[ch]")):
            source_paths.append(path)
            stem = path.stem.lower()
            if stem:
                prefixes.add(stem.split("_", 1)[0])

    peripheral = _snake(target_peripheral)
    if peripheral:
        prefixes.add(peripheral)

    linux_root = Path("env") / "src"
    if linux_root.exists():
        for prefix in sorted(prefixes):
            if len(prefix) < 4:
                continue
            for path in sorted(linux_root.glob(f"linux-*/drivers/**/*{prefix}*.[ch]")):
                source_paths.append(path)

    texts: list[str] = []
    seen: set[Path] = set()
    for path in source_paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            texts.append(path.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    return texts


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _boot_defaults(arch: str) -> dict[str, str]:
    if arch == "x86_64":
        return {
            "machine": "pc",
            "cpu": "qemu64",
            "console": "ttyS0",
            "root_dev": "/dev/vda",
            "drive_if": "virtio",
        }
    if arch == "riscv64":
        return {
            "machine": "virt",
            "cpu": "rv64",
            "console": "ttyS0",
            "root_dev": "/dev/vda",
            "drive_if": "virtio",
        }
    if arch == "mipsel":
        return {
            "machine": "malta",
            "cpu": "24Kf",
            "console": "ttyS0",
            "root_dev": "/dev/sda",
            "drive_if": "ide",
        }
    return {
        "machine": "virt",
        "cpu": "cortex-a72",
        "console": "ttyAMA0",
        "root_dev": "/dev/vda",
        "drive_if": "virtio",
    }


def _arch_from_build_env(build_env: Path) -> str:
    """Infer a QEMU architecture name from an AutoEmu build directory."""
    supported = {"aarch64", "arm", "mipsel", "riscv64", "x86_64"}
    name = build_env.name
    if name.startswith("qemu-"):
        candidate = name.removeprefix("qemu-")
        if candidate in supported:
            return candidate
    for target in build_env.glob("*-softmmu-config-devices.mak"):
        candidate = target.name.removesuffix("-softmmu-config-devices.mak")
        if candidate in supported:
            return candidate
    return "aarch64"


def _resolve_boot_assets(
    build_env: Path,
    *,
    output_dir: Path | None = None,
) -> tuple[BootAssets | None, str]:
    """Resolve QEMU, kernel, and rootfs artifacts for a guest Linux probe."""
    arch = _arch_from_build_env(build_env)
    defaults = _boot_defaults(arch)
    env_output = build_env.parent.parent / "output"

    qemu_bin = Path(os.getenv("AUTOEMU_QEMU_BIN", "") or build_env / f"qemu-system-{arch}")
    if not qemu_bin.exists():
        fallback = env_output / f"qemu-system-{arch}"
        if fallback.exists():
            qemu_bin = fallback

    kernel = Path(os.getenv("AUTOEMU_LINUX_KERNEL", "") or env_output / f"kernel-{arch}")
    rootfs_env = os.getenv("AUTOEMU_LINUX_ROOTFS", "").strip()
    if rootfs_env:
        rootfs = Path(rootfs_env)
    else:
        probe_rootfs = output_dir / f"rootfs-{arch}-stage5.ext4" if output_dir else None
        rootfs = probe_rootfs if probe_rootfs and probe_rootfs.exists() else env_output / f"rootfs-{arch}.ext4"

    dtb_env = os.getenv("AUTOEMU_QEMU_DTB", "").strip()
    dtb: Path | None = Path(dtb_env) if dtb_env else None
    if dtb is None and output_dir:
        probe_dtb = output_dir / "probe.dtb"
        if probe_dtb.exists():
            dtb = probe_dtb

    missing = [str(path) for path in (qemu_bin, kernel, rootfs) if not path.exists()]
    if dtb is not None and not dtb.exists():
        missing.append(str(dtb))
    if missing:
        return None, "Missing Linux boot asset(s): " + ", ".join(missing)

    try:
        timeout = int(os.getenv("AUTOEMU_PROBE_TIMEOUT", str(DEFAULT_PROBE_TIMEOUT)))
    except ValueError:
        timeout = DEFAULT_PROBE_TIMEOUT

    return BootAssets(
        arch=arch,
        qemu_bin=qemu_bin,
        kernel=kernel,
        rootfs=rootfs,
        dtb=dtb,
        machine=os.getenv("AUTOEMU_QEMU_MACHINE", defaults["machine"]),
        cpu=os.getenv("AUTOEMU_QEMU_CPU", defaults["cpu"]),
        memory=os.getenv("AUTOEMU_QEMU_MEMORY", "512M"),
        console=defaults["console"],
        root_dev=defaults["root_dev"],
        drive_if=defaults["drive_if"],
        timeout=timeout,
        extra_args=shlex.split(os.getenv("AUTOEMU_QEMU_EXTRA_ARGS", "")),
    ), ""


def _build_qemu_boot_command(assets: BootAssets) -> list[str]:
    cmd = [
        str(assets.qemu_bin),
        "-M",
        assets.machine,
        "-m",
        assets.memory,
        "-cpu",
        assets.cpu,
        "-kernel",
        str(assets.kernel),
        "-drive",
        f"file={assets.rootfs},format=raw,if={assets.drive_if}",
        "-append",
        f"root={assets.root_dev} rw console={assets.console} nographic",
        "-nographic",
        "-no-reboot",
    ]
    if assets.dtb is not None:
        cmd.extend(["-dtb", str(assets.dtb)])
    cmd.extend(assets.extra_args)
    return cmd


def _run_guest_linux_probe(
    assets: BootAssets,
    *,
    output_dir: Path,
    target_mcu: str,
    target_peripheral: str,
    on_progress: Callable[[str, str], None],
) -> dict[str, Any]:
    """Boot guest Linux under QEMU and classify driver probe output."""
    cmd = _build_qemu_boot_command(assets)
    on_progress(f"Booting guest Linux for driver probe: {' '.join(cmd)}", "compile")
    _log_stage5_command(on_progress, cmd)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            env=_stage5_subprocess_env(),
            timeout=assets.timeout,
        )
        _log_stage5_process(
            on_progress,
            "guest Linux probe",
            proc,
            stdout_label="guest stdout",
            stderr_label="guest stderr",
        )
        boot_log = proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        on_progress(f"Stage 5 result: timeout after {assets.timeout}s (guest Linux probe)", "warn")
        _log_stage5_stream(on_progress, "guest stdout", stdout, "compile")
        _log_stage5_stream(on_progress, "guest stderr", stderr, "warn")
        boot_log = stdout + stderr

    analysis = _analyze_probe_log(
        boot_log,
        output_dir=output_dir,
        target_mcu=target_mcu,
        target_peripheral=target_peripheral,
    )
    _log_stage5_probe_summary(on_progress, analysis)
    return {
        **analysis,
        "qemu_cmd": cmd,
        "boot_log": boot_log[-4000:] if len(boot_log) > 4000 else boot_log,
        "boot_assets": {
            "arch": assets.arch,
            "qemu_bin": str(assets.qemu_bin),
            "kernel": str(assets.kernel),
            "rootfs": str(assets.rootfs),
            "dtb": str(assets.dtb) if assets.dtb else "",
        },
    }


def _log_stage5_probe_summary(
    on_progress: Callable[[str, str], None],
    analysis: dict[str, Any],
) -> None:
    """Emit the final guest-probe classification and matched probe lines."""
    status = str(analysis.get("probe_status") or "unknown")
    reason = str(analysis.get("reason") or "").strip()
    kind = "info" if analysis.get("success") else "warn"
    message = f"Stage 5 probe result: {status}"
    if reason:
        message = f"{message} - {reason}"
    on_progress(message, kind)
    for line in analysis.get("probe_lines", [])[:20]:
        clean = str(line).strip()
        if clean:
            on_progress(f"Stage 5 probe line: {clean}", kind)


def _probe_tokens(output_dir: Path, target_mcu: str, target_peripheral: str) -> list[str]:
    """Return lowercase tokens expected in guest probe logs."""
    peripheral_snake = _snake(target_peripheral)
    tokens = {peripheral_snake, target_peripheral.lower(), _snake(target_mcu)}
    family_words: set[str] = set()
    for peripheral_json in sorted(output_dir.glob("*_peripheral.json")):
        try:
            data = json.loads(peripheral_json.read_text(encoding="utf-8"))
        except Exception:
            continue
        name = str(data.get("name", "")).strip()
        if name:
            tokens.add(name.lower())
            peripheral_snake = _snake(name)
        family = str(data.get("mcu_family", "")).strip()
        if family:
            pfx = _snake(family)
            family_words.update(part for part in pfx.split("_") if len(part) >= 3)
            tokens.add(f"{pfx}-{peripheral_snake}")
        break

    data_root = Path("data") / _snake(target_mcu) / "driver"
    if data_root.exists():
        for source in data_root.rglob("*.[ch]"):
            stem = source.stem.lower()
            if not stem:
                continue
            try:
                source_text = source.read_text(encoding="utf-8", errors="ignore").lower()
            except OSError:
                source_text = ""
            if peripheral_snake not in stem and peripheral_snake not in source_text:
                continue
            tokens.add(stem)
            prefix = stem.split("_")[0]
            if prefix not in family_words:
                tokens.add(prefix)

    return sorted(token for token in tokens if len(token) >= 3)


def _analyze_probe_log(
    log: str,
    *,
    output_dir: Path,
    target_mcu: str,
    target_peripheral: str,
) -> dict[str, Any]:
    """Classify Linux console output for the generated device probe."""
    lower = log.lower()
    booted = any(marker.lower() in lower for marker in LINUX_BOOT_MARKERS)
    tokens = _probe_tokens(output_dir, target_mcu, target_peripheral)
    probe_lines = [
        line for line in log.splitlines()
        if any(token in line.lower() for token in tokens)
    ]
    failure_terms = (
        "probe failed",
        "failed to probe",
        "deferred probe timeout",
        "kernel panic",
        "not a valid device model name",
        "oops",
    )
    for line in probe_lines:
        line_lower = line.lower()
        if any(term in line_lower for term in failure_terms) or (
            "error" in line_lower and "error 0" not in line_lower
        ):
            return {
                "success": False,
                "probe_status": "failed" if booted else "boot_failed",
                "reason": line.strip() or "driver probe failed",
                "probe_lines": probe_lines,
            }
    if probe_lines and booted:
        return {
            "success": True,
            "probe_status": "matched",
            "reason": "",
            "probe_lines": probe_lines,
        }
    if booted:
        return {
            "success": False,
            "probe_status": "inconclusive",
            "reason": "Linux booted but no driver probe log matched generated device",
            "probe_lines": [],
        }
    first_line = next((line.strip() for line in log.splitlines() if line.strip()), "")
    return {
        "success": False,
        "probe_status": "boot_failed",
        "reason": first_line or "Linux boot marker not observed before QEMU exited or timed out",
        "probe_lines": probe_lines,
    }


def _qemu_system_target(build_env: Path) -> str:
    return f"qemu-system-{_arch_from_build_env(build_env)}"


def _extract_generated_config_symbol(output_dir: Path) -> str:
    meson_path = output_dir / "meson.build"
    if not meson_path.exists():
        return ""
    text = meson_path.read_text(encoding="utf-8", errors="ignore")
    import re
    match = re.search(r"CONFIG_([A-Z0-9_]+)", text)
    return match.group(1) if match else ""


def _enable_generated_config(
    output_dir: Path,
    build_env: Path,
    qemu_src: Path | None,
    *,
    on_progress: Callable[[str, str], None],
) -> None:
    """Best-effort enablement of the generated QEMU Kconfig symbol."""
    symbol = _extract_generated_config_symbol(output_dir)
    if not symbol:
        return

    arch = _arch_from_build_env(build_env)
    build_config = build_env / f"{arch}-softmmu-config-devices.mak"
    config_line = f"CONFIG_{symbol}=y"
    if build_config.exists():
        text = build_config.read_text(encoding="utf-8", errors="ignore")
        if config_line not in text:
            with build_config.open("a", encoding="utf-8") as f:
                f.write(f"\n{config_line}\n")
            on_progress(f"Enabled generated QEMU config in {build_config}: {config_line}", "compile")

    if qemu_src is None:
        return
    source_config = qemu_src / "configs" / "devices" / f"{arch}-softmmu" / "default.mak"
    if source_config.exists():
        text = source_config.read_text(encoding="utf-8", errors="ignore")
        if config_line not in text:
            with source_config.open("a", encoding="utf-8") as f:
                f.write(f"\n{config_line}\n")


def _infer_qemu_target_dir(output_dir: Path, target_mcu: str) -> str:
    """Infer the QEMU hw/ subdirectory used by apply-to-qemu.py."""
    platform_hint = target_mcu.lower()
    for peripheral_json in sorted(output_dir.glob("*_peripheral.json")):
        try:
            data = json.loads(peripheral_json.read_text(encoding="utf-8"))
        except Exception:
            continue
        platform_hint = (data.get("mcu_family") or target_mcu).lower()
        break

    if "stm32" in platform_hint or "arm" in platform_hint:
        return "hw/arm"
    if "mips" in platform_hint:
        return "hw/mips"
    if "riscv" in platform_hint:
        return "hw/riscv"
    if "x86" in platform_hint:
        return "hw/i386"
    return "hw/misc"


def _infer_generated_ninja_targets(
    build_env: Path,
    target_dir: str,
    c_files: list[Path],
) -> list[str]:
    """Return real ninja targets that compile the generated hardware sources."""
    ninja_file = build_env / "build.ninja"
    try:
        lines = ninja_file.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []

    source_markers = {
        c_file.name: f"{target_dir.rstrip('/')}/{c_file.name}"
        for c_file in c_files
    }
    targets: list[str] = []
    seen: set[str] = set()
    fallback_target = f"{target_dir.rstrip('/')}/all"

    for line in lines:
        if not line.startswith("build "):
            continue
        outputs = _ninja_build_outputs(line)
        if not outputs:
            continue
        for marker in source_markers.values():
            if marker in line:
                target = outputs[0]
                if target not in seen:
                    seen.add(target)
                    targets.append(target)
        if fallback_target in outputs and fallback_target not in seen:
            seen.add(fallback_target)
            targets.append(fallback_target)

    return targets


def _ninja_build_outputs(line: str) -> list[str]:
    """Extract output targets from a simple ``build ...:`` ninja line."""
    if not line.startswith("build "):
        return []
    outputs, sep, _ = line[len("build "):].partition(":")
    if not sep:
        return []
    return outputs.split()
