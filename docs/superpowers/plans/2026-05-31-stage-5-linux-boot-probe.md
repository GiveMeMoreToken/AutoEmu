# Stage 5 Linux Boot Probe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make AutoEmu stage 5 boot a guest Linux kernel under QEMU and verify real driver probe logs for the generated peripheral.

**Architecture:** Extend `run_qemu_probe()` so a successful generated-source rebuild continues into QEMU system-binary rebuild, Linux boot, and console-log probe analysis. Keep phase 5 soft-fail by returning structured diagnostics instead of raising or failing the whole pipeline.

**Tech Stack:** Python 3, `pytest`, `subprocess`, QEMU build artifacts under `env/build/`, guest artifacts under `env/output/`.

---

## File Structure

- Modify `src/autoemu/validators/qemu_probe_validator.py`
  - Add boot-asset resolution helpers.
  - Add generated QEMU config symbol extraction and binary rebuild target selection.
  - Add guest Linux boot execution.
  - Add probe-log classification.
  - Extend `run_qemu_probe()` result payload.
- Modify `tests/test_qemu_probe_validator.py`
  - Add focused tests for boot asset skips, boot command execution, QEMU startup failures, successful probe logs, and failed probe logs.
- Keep `src/autoemu/agent/runtime.py` unchanged
  - It already treats phase 5 as soft-fail and stores `result.probe_result`.

### Task 1: Boot Asset Resolution

**Files:**
- Modify: `src/autoemu/validators/qemu_probe_validator.py`
- Test: `tests/test_qemu_probe_validator.py`

- [ ] **Step 1: Write the failing test**

Add this test near the existing skip tests:

```python
def test_run_qemu_probe_skips_when_linux_boot_assets_missing(monkeypatch, tmp_path):
    build_env = tmp_path / "env" / "build" / "qemu-aarch64"
    build_env.mkdir(parents=True)
    _write_build_ninja(build_env, "hw/misc", c_name="hikey960_gpu.c")
    qemu_src = tmp_path / "env" / "src" / "qemu-9.2.0"
    (qemu_src / "include" / "qemu").mkdir(parents=True)

    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "gpu_peripheral.json").write_text(
        json.dumps({"name": "GPU", "mcu_family": "HiSilicon Kirin"}),
        encoding="utf-8",
    )
    (output_dir / "meson.build").write_text(
        "system_ss.add(when: 'CONFIG_HIKEY960_GPU', if_true: files('hikey960_gpu.c'))\n",
        encoding="utf-8",
    )
    (output_dir / "hikey960_gpu.c").write_text("int demo(void){return 0;}", encoding="utf-8")
    (output_dir / "hikey960_gpu.h").write_text("#pragma once\n", encoding="utf-8")

    monkeypatch.setattr(
        "autoemu.validators.qemu_probe_validator.shutil.which",
        lambda name: "/usr/bin/ninja" if name == "ninja" else "/usr/bin/python3",
    )

    class FakeCompletedProcess:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(
        "autoemu.validators.qemu_probe_validator.subprocess.run",
        lambda *args, **kwargs: FakeCompletedProcess(),
    )

    result = run_qemu_probe(output_dir, "Hikey960", "GPU", qemu_build_env=build_env)

    assert result["success"] is False
    assert result["skipped"] is True
    assert result["probe_status"] == "skipped"
    assert "Missing Linux boot asset" in result["reason"]
    assert "kernel-aarch64" in result["reason"]
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_qemu_probe_validator.py::test_run_qemu_probe_skips_when_linux_boot_assets_missing -q
```

Expected: FAIL because `run_qemu_probe()` still returns success immediately after ninja succeeds and has no `probe_status` key.

- [ ] **Step 3: Implement boot asset resolution**

Add these helper definitions:

```python
import os
import shlex
import time
from dataclasses import dataclass

DEFAULT_PROBE_TIMEOUT = 45
LINUX_BOOT_MARKERS = (
    "Linux version",
    "Kernel command line:",
    "Freeing unused kernel memory",
    "Run /sbin/init",
)

@dataclass(frozen=True)
class BootAssets:
    arch: str
    qemu_bin: Path
    kernel: Path
    rootfs: Path
    machine: str
    cpu: str
    memory: str
    console: str
    root_dev: str
    drive_if: str
    timeout: int
    extra_args: list[str]
```

Add `_arch_from_build_env()` and `_resolve_boot_assets()`:

```python
def _arch_from_build_env(build_env: Path) -> str:
    name = build_env.name
    if name.startswith("qemu-"):
        return name.removeprefix("qemu-")
    for target in build_env.glob("*-softmmu-config-devices.mak"):
        return target.name.removesuffix("-softmmu-config-devices.mak")
    return "aarch64"

def _resolve_boot_assets(build_env: Path) -> tuple[BootAssets | None, str]:
    arch = _arch_from_build_env(build_env)
    defaults = _boot_defaults(arch)
    env_output = build_env.parent.parent / "output"
    qemu_bin = Path(os.getenv("AUTOEMU_QEMU_BIN", "") or build_env / f"qemu-system-{arch}")
    if not qemu_bin.exists():
        fallback = env_output / f"qemu-system-{arch}"
        if fallback.exists():
            qemu_bin = fallback
    kernel = Path(os.getenv("AUTOEMU_LINUX_KERNEL", "") or env_output / f"kernel-{arch}")
    rootfs = Path(os.getenv("AUTOEMU_LINUX_ROOTFS", "") or env_output / f"rootfs-{arch}.ext4")
    missing = [str(path) for path in (qemu_bin, kernel, rootfs) if not path.exists()]
    if missing:
        return None, "Missing Linux boot asset(s): " + ", ".join(missing)
    timeout = int(os.getenv("AUTOEMU_PROBE_TIMEOUT", str(DEFAULT_PROBE_TIMEOUT)))
    return BootAssets(
        arch=arch,
        qemu_bin=qemu_bin,
        kernel=kernel,
        rootfs=rootfs,
        machine=os.getenv("AUTOEMU_QEMU_MACHINE", defaults["machine"]),
        cpu=os.getenv("AUTOEMU_QEMU_CPU", defaults["cpu"]),
        memory=os.getenv("AUTOEMU_QEMU_MEMORY", "512M"),
        console=defaults["console"],
        root_dev=defaults["root_dev"],
        drive_if=defaults["drive_if"],
        timeout=timeout,
        extra_args=shlex.split(os.getenv("AUTOEMU_QEMU_EXTRA_ARGS", "")),
    ), ""

def _boot_defaults(arch: str) -> dict[str, str]:
    if arch == "x86_64":
        return {"machine": "pc", "cpu": "qemu64", "console": "ttyS0", "root_dev": "/dev/vda", "drive_if": "virtio"}
    if arch == "riscv64":
        return {"machine": "virt", "cpu": "rv64", "console": "ttyS0", "root_dev": "/dev/vda", "drive_if": "virtio"}
    if arch == "mipsel":
        return {"machine": "malta", "cpu": "24Kf", "console": "ttyS0", "root_dev": "/dev/sda", "drive_if": "ide"}
    return {"machine": "virt", "cpu": "cortex-a72", "console": "ttyAMA0", "root_dev": "/dev/vda", "drive_if": "virtio"}
```

- [ ] **Step 4: Wire asset resolution into `run_qemu_probe()`**

After the existing generated-source rebuild succeeds and before PoC handling, call:

```python
boot_assets, boot_error = _resolve_boot_assets(build_env)
if boot_assets is None:
    return {
        "success": False,
        "skipped": True,
        "reason": boot_error,
        "build_log": tail,
        "build_targets": build_targets,
        "poc_results": poc_results,
        "probe_status": "skipped",
        "boot_assets": {},
    }
```

- [ ] **Step 5: Run test to verify it passes**

Run:

```bash
pytest tests/test_qemu_probe_validator.py::test_run_qemu_probe_skips_when_linux_boot_assets_missing -q
```

Expected: PASS.

### Task 2: Probe Tokens and Log Classification

**Files:**
- Modify: `src/autoemu/validators/qemu_probe_validator.py`
- Test: `tests/test_qemu_probe_validator.py`

- [ ] **Step 1: Write failing log-analysis tests**

Add:

```python
from autoemu.validators.qemu_probe_validator import _analyze_probe_log

def test_analyze_probe_log_matches_driver_probe_success(tmp_path):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    data_dir = tmp_path / "data" / "hikey960" / "driver"
    data_dir.mkdir(parents=True)
    (data_dir / "panfrost_device.c").write_text("int probe(void) { return 0; }\n", encoding="utf-8")
    log = "Linux version 6.12.28\npanfrost e82c0000.gpu: mali-t860 id 0x860 major 0 minor 0 status 0\n"

    result = _analyze_probe_log(
        log,
        output_dir=output_dir,
        target_mcu="Hikey960",
        target_peripheral="GPU",
    )

    assert result["probe_status"] == "matched"
    assert result["success"] is True
    assert any("panfrost" in line for line in result["probe_lines"])

def test_analyze_probe_log_reports_driver_probe_failure(tmp_path):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    log = "Linux version 6.12.28\npanfrost e82c0000.gpu: probe failed with error -22\n"

    result = _analyze_probe_log(
        log,
        output_dir=output_dir,
        target_mcu="Hikey960",
        target_peripheral="GPU",
    )

    assert result["probe_status"] == "failed"
    assert result["success"] is False
    assert "probe failed" in result["reason"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_qemu_probe_validator.py::test_analyze_probe_log_matches_driver_probe_success tests/test_qemu_probe_validator.py::test_analyze_probe_log_reports_driver_probe_failure -q
```

Expected: FAIL because `_analyze_probe_log()` does not exist.

- [ ] **Step 3: Implement token extraction and log analysis**

Add helpers:

```python
def _probe_tokens(output_dir: Path, target_mcu: str, target_peripheral: str) -> list[str]:
    tokens = {_snake(target_peripheral), target_peripheral.lower()}
    for peripheral_json in sorted(output_dir.glob("*_peripheral.json")):
        try:
            data = json.loads(peripheral_json.read_text(encoding="utf-8"))
        except Exception:
            continue
        name = str(data.get("name", "")).strip()
        if name:
            tokens.add(name.lower())
        family = str(data.get("mcu_family", "")).strip()
        if family:
            pfx = _snake(family)
            tokens.add(f"{pfx}-{_snake(target_peripheral)}")
        break
    data_root = Path("data") / _snake(target_mcu) / "driver"
    if data_root.exists():
        for source in data_root.rglob("*.[ch]"):
            stem = source.stem.lower()
            if stem:
                tokens.add(stem.split("_")[0])
                tokens.add(stem)
    return sorted(token for token in tokens if len(token) >= 3)

def _analyze_probe_log(
    log: str,
    *,
    output_dir: Path,
    target_mcu: str,
    target_peripheral: str,
) -> dict[str, Any]:
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
        "error",
        "kernel panic",
        "oops",
    )
    for line in probe_lines:
        line_lower = line.lower()
        if any(term in line_lower for term in failure_terms):
            return {
                "success": False,
                "probe_status": "failed",
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
    return {
        "success": False,
        "probe_status": "boot_failed",
        "reason": "Linux boot marker not observed before QEMU exited or timed out",
        "probe_lines": probe_lines,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
pytest tests/test_qemu_probe_validator.py::test_analyze_probe_log_matches_driver_probe_success tests/test_qemu_probe_validator.py::test_analyze_probe_log_reports_driver_probe_failure -q
```

Expected: PASS.

### Task 3: Real QEMU Boot Runner

**Files:**
- Modify: `src/autoemu/validators/qemu_probe_validator.py`
- Test: `tests/test_qemu_probe_validator.py`

- [ ] **Step 1: Write failing tests for QEMU boot command outcomes**

Add:

```python
def _write_boot_assets(build_env: Path, arch: str = "aarch64") -> None:
    env_output = build_env.parent.parent / "output"
    env_output.mkdir(parents=True)
    (build_env / f"qemu-system-{arch}").write_text("#!/bin/sh\n", encoding="utf-8")
    (build_env / f"qemu-system-{arch}").chmod(0o755)
    (env_output / f"kernel-{arch}").write_text("kernel\n", encoding="utf-8")
    (env_output / f"rootfs-{arch}.ext4").write_text("rootfs\n", encoding="utf-8")

def test_run_qemu_probe_boots_linux_and_matches_probe_log(monkeypatch, tmp_path):
    build_env = tmp_path / "env" / "build" / "qemu-aarch64"
    build_env.mkdir(parents=True)
    _write_build_ninja(build_env, "hw/misc", c_name="hikey960_gpu.c")
    _write_boot_assets(build_env)
    qemu_src = tmp_path / "env" / "src" / "qemu-9.2.0"
    (qemu_src / "include" / "qemu").mkdir(parents=True)

    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "gpu_peripheral.json").write_text(
        json.dumps({"name": "GPU", "mcu_family": "HiSilicon Kirin"}),
        encoding="utf-8",
    )
    (output_dir / "meson.build").write_text(
        "system_ss.add(when: 'CONFIG_HIKEY960_GPU', if_true: files('hikey960_gpu.c'))\n",
        encoding="utf-8",
    )
    (output_dir / "hikey960_gpu.c").write_text("int demo(void){return 0;}", encoding="utf-8")
    (output_dir / "hikey960_gpu.h").write_text("#pragma once\n", encoding="utf-8")

    monkeypatch.setattr(
        "autoemu.validators.qemu_probe_validator.shutil.which",
        lambda name: "/usr/bin/ninja" if name == "ninja" else "/usr/bin/python3",
    )

    commands: list[list[str]] = []

    class FakeCompletedProcess:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd, **kwargs):
        commands.append([str(part) for part in cmd])
        if str(cmd[0]).endswith("qemu-system-aarch64"):
            return FakeCompletedProcess(
                stdout="Linux version 6.12.28\npanfrost e82c0000.gpu: probe complete\n"
            )
        return FakeCompletedProcess()

    monkeypatch.setattr("autoemu.validators.qemu_probe_validator.subprocess.run", fake_run)

    result = run_qemu_probe(output_dir, "Hikey960", "GPU", qemu_build_env=build_env)

    assert result["success"] is True
    assert result["probe_status"] == "matched"
    assert any("qemu-system-aarch64" in cmd[0] for cmd in commands)
    assert any("panfrost" in line for line in result["probe_lines"])

def test_run_qemu_probe_reports_qemu_startup_failure(monkeypatch, tmp_path):
    build_env = tmp_path / "env" / "build" / "qemu-aarch64"
    build_env.mkdir(parents=True)
    _write_build_ninja(build_env, "hw/misc", c_name="hikey960_gpu.c")
    _write_boot_assets(build_env)
    qemu_src = tmp_path / "env" / "src" / "qemu-9.2.0"
    (qemu_src / "include" / "qemu").mkdir(parents=True)

    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "gpu_peripheral.json").write_text(
        json.dumps({"name": "GPU", "mcu_family": "HiSilicon Kirin"}),
        encoding="utf-8",
    )
    (output_dir / "meson.build").write_text(
        "system_ss.add(when: 'CONFIG_HIKEY960_GPU', if_true: files('hikey960_gpu.c'))\n",
        encoding="utf-8",
    )
    (output_dir / "hikey960_gpu.c").write_text("int demo(void){return 0;}", encoding="utf-8")
    (output_dir / "hikey960_gpu.h").write_text("#pragma once\n", encoding="utf-8")

    monkeypatch.setattr(
        "autoemu.validators.qemu_probe_validator.shutil.which",
        lambda name: "/usr/bin/ninja" if name == "ninja" else "/usr/bin/python3",
    )

    class FakeCompletedProcess:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd, **kwargs):
        if str(cmd[0]).endswith("qemu-system-aarch64"):
            return FakeCompletedProcess(
                returncode=1,
                stderr="qemu-system-aarch64: -device hikey960-gpu: not a valid device model name\n",
            )
        return FakeCompletedProcess()

    monkeypatch.setattr("autoemu.validators.qemu_probe_validator.subprocess.run", fake_run)

    result = run_qemu_probe(output_dir, "Hikey960", "GPU", qemu_build_env=build_env)

    assert result["success"] is False
    assert result["probe_status"] == "boot_failed"
    assert "not a valid device model name" in result["boot_log"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_qemu_probe_validator.py::test_run_qemu_probe_boots_linux_and_matches_probe_log tests/test_qemu_probe_validator.py::test_run_qemu_probe_reports_qemu_startup_failure -q
```

Expected: FAIL because `run_qemu_probe()` does not run QEMU yet.

- [ ] **Step 3: Implement QEMU boot command helper**

Add:

```python
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
    cmd = _build_qemu_boot_command(assets)
    on_progress(f"Booting guest Linux for driver probe: {' '.join(cmd)}", "compile")
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=assets.timeout,
        )
        boot_log = proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as exc:
        boot_log = (exc.stdout or "") + (exc.stderr or "")
        if isinstance(boot_log, bytes):
            boot_log = boot_log.decode(errors="replace")
    analysis = _analyze_probe_log(
        boot_log,
        output_dir=output_dir,
        target_mcu=target_mcu,
        target_peripheral=target_peripheral,
    )
    return {
        **analysis,
        "qemu_cmd": cmd,
        "boot_log": boot_log[-4000:] if len(boot_log) > 4000 else boot_log,
        "boot_assets": {
            "arch": assets.arch,
            "qemu_bin": str(assets.qemu_bin),
            "kernel": str(assets.kernel),
            "rootfs": str(assets.rootfs),
        },
    }
```

- [ ] **Step 4: Wire boot runner into `run_qemu_probe()`**

After boot asset resolution:

```python
boot_result = _run_guest_linux_probe(
    boot_assets,
    output_dir=gen_dir,
    target_mcu=target_mcu,
    target_peripheral=target_peripheral,
    on_progress=_log,
)
```

Use `boot_result["success"]` for the final phase success. Preserve `poc_results`, `build_log`, and `build_targets`.

- [ ] **Step 5: Run tests to verify they pass**

Run:

```bash
pytest tests/test_qemu_probe_validator.py::test_run_qemu_probe_boots_linux_and_matches_probe_log tests/test_qemu_probe_validator.py::test_run_qemu_probe_reports_qemu_startup_failure -q
```

Expected: PASS.

### Task 4: Rebuild Runnable QEMU Binary

**Files:**
- Modify: `src/autoemu/validators/qemu_probe_validator.py`
- Test: `tests/test_qemu_probe_validator.py`

- [ ] **Step 1: Write failing test for system binary rebuild target**

Add an assertion to `test_run_qemu_probe_boots_linux_and_matches_probe_log`:

```python
assert ["/usr/bin/ninja", "-C", str(build_env), "qemu-system-aarch64"] in commands
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_qemu_probe_validator.py::test_run_qemu_probe_boots_linux_and_matches_probe_log -q
```

Expected: FAIL because only the generated object target is rebuilt.

- [ ] **Step 3: Implement binary rebuild target**

Add:

```python
def _qemu_system_target(build_env: Path) -> str:
    return f"qemu-system-{_arch_from_build_env(build_env)}"
```

After the generated-source rebuild succeeds and before boot asset resolution, run:

```python
system_target = _qemu_system_target(build_env)
_log(f"Rebuilding runnable QEMU binary in {build_env}: {system_target}", "compile")
proc_system = subprocess.run(
    [ninja, "-C", str(build_env), system_target],
    capture_output=True,
    text=True,
    check=False,
    timeout=DEFAULT_BUILD_TIMEOUT,
)
combined += proc_system.stdout + proc_system.stderr
if proc_system.returncode != 0:
    tail = combined[-2000:] if len(combined) > 2000 else combined
    return {
        "success": False,
        "skipped": False,
        "reason": f"QEMU system binary rebuild returned {proc_system.returncode}",
        "build_log": tail,
        "build_targets": [*build_targets, system_target],
        "poc_results": poc_results,
        "probe_status": "boot_failed",
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
pytest tests/test_qemu_probe_validator.py::test_run_qemu_probe_boots_linux_and_matches_probe_log -q
```

Expected: PASS.

### Task 5: Apply Machine Patch and Copy QTest Hook

**Files:**
- Modify: `src/autoemu/validators/qemu_probe_validator.py`
- Test: `tests/test_qemu_probe_validator.py`

- [ ] **Step 1: Write failing test for apply command flags**

In `test_run_qemu_probe_boots_linux_and_matches_probe_log`, assert:

```python
apply_commands = [cmd for cmd in commands if cmd and cmd[0].endswith("python3")]
assert any("--apply-machine-patch" in cmd for cmd in apply_commands)
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_qemu_probe_validator.py::test_run_qemu_probe_boots_linux_and_matches_probe_log -q
```

Expected: FAIL because the current apply command does not pass `--apply-machine-patch`.

- [ ] **Step 3: Update apply command**

In `run_qemu_probe()`, extend the `scripts/apply-to-qemu.py` command:

```python
cmd = [
    shutil.which("python3") or "python",
    str(script),
    str(output_dir),
    "--qemu-src",
    str(qemu_src),
    "--apply-machine-patch",
    "--copy-qtest",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
pytest tests/test_qemu_probe_validator.py::test_run_qemu_probe_boots_linux_and_matches_probe_log -q
```

Expected: PASS.

### Task 6: Regression Suite and Compile Check

**Files:**
- Modify: `tests/test_qemu_probe_validator.py`
- Modify: `src/autoemu/validators/qemu_probe_validator.py`

- [ ] **Step 1: Run focused validator tests**

Run:

```bash
pytest tests/test_qemu_probe_validator.py -q
```

Expected: all tests in `tests/test_qemu_probe_validator.py` pass.

- [ ] **Step 2: Run runtime soft-fail regression**

Run:

```bash
pytest tests/test_runtime.py::test_run_pipeline_soft_fails_when_probe_fails -q
```

Expected: PASS, confirming phase 5 remains soft-fail.

- [ ] **Step 3: Run compileall**

Run:

```bash
python -m compileall -q src tests
```

Expected: exit code 0.

- [ ] **Step 4: Run full pytest**

Run:

```bash
pytest
```

Expected: all tests pass. If unrelated pre-existing tests fail due to the dirty worktree, record exact failures and run the focused phase-5 tests plus compileall before reporting.

