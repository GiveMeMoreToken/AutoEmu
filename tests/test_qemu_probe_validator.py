"""Tests for the phase-5 QEMU probe validator."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoemu.validators.qemu_probe_validator import _analyze_probe_log, run_qemu_probe


def _write_build_ninja(build_env: Path, target_dir: str, c_name: str = "demo.c") -> str:
    """Create a minimal build graph with one generated-source object target."""
    target = f"libcommon.a.p/{target_dir.replace('/', '_')}_{c_name}.o"
    (build_env / "build.ninja").write_text(
        f"build {target}: c_COMPILER ../../src/qemu-9.2.0/{target_dir}/{c_name}\n",
        encoding="utf-8",
    )
    return target


def _write_boot_assets(build_env: Path, arch: str = "aarch64") -> None:
    """Create minimal files that satisfy QEMU Linux boot asset discovery."""
    env_output = build_env.parent.parent / "output"
    env_output.mkdir(parents=True)
    qemu_bin = build_env / f"qemu-system-{arch}"
    qemu_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    qemu_bin.chmod(0o755)
    (env_output / f"kernel-{arch}").write_text("kernel\n", encoding="utf-8")
    (env_output / f"rootfs-{arch}.ext4").write_text("rootfs\n", encoding="utf-8")


def test_run_qemu_probe_skips_when_no_build_env(monkeypatch, tmp_path):
    """Missing build environment should be a soft skip, not a hard failure."""
    monkeypatch.setattr(
        "autoemu.validators.qemu_probe_validator.Path.exists",
        lambda self: False,
    )
    result = run_qemu_probe(
        output_dir=tmp_path,
        target_mcu="STM32F407VG",
        target_peripheral="ETH",
    )
    assert result["skipped"] is True
    assert result["success"] is False
    assert "QEMU build environment not found" in result["reason"]


def test_run_qemu_probe_skips_when_no_generated_files(monkeypatch, tmp_path):
    """No C/H files means nothing to probe."""
    # Create a fake build env with build.ninja
    build_env = tmp_path / "env" / "build" / "qemu-stm32f407vg"
    build_env.mkdir(parents=True)
    (build_env / "build.ninja").write_text("# ninja\n", encoding="utf-8")

    # output_dir has no .c/.h files
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    result = run_qemu_probe(
        output_dir=output_dir,
        target_mcu="STM32F407VG",
        target_peripheral="ETH",
        qemu_build_env=build_env,
    )
    assert result["skipped"] is True
    assert "No generated C/H files to probe" in result["reason"]


def test_run_qemu_probe_skips_when_no_ninja(monkeypatch, tmp_path):
    """Missing ninja binary should be a soft skip."""
    build_env = tmp_path / "env" / "build" / "qemu-stm32f407vg"
    build_env.mkdir(parents=True)
    (build_env / "build.ninja").write_text("# ninja\n", encoding="utf-8")

    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "demo.c").write_text("int demo(void){return 0;}", encoding="utf-8")

    monkeypatch.setattr(
        "autoemu.validators.qemu_probe_validator.shutil.which",
        lambda name: None,
    )

    result = run_qemu_probe(
        output_dir=output_dir,
        target_mcu="STM32F407VG",
        target_peripheral="ETH",
        qemu_build_env=build_env,
    )
    assert result["skipped"] is True
    assert "ninja not found" in result["reason"]


def test_run_qemu_probe_passes_on_ninja_success(monkeypatch, tmp_path):
    """A successful rebuild plus Linux driver probe should report success."""
    build_env = tmp_path / "env" / "build" / "qemu-stm32f407vg"
    build_env.mkdir(parents=True)
    _write_build_ninja(build_env, "hw/arm")
    _write_boot_assets(build_env)

    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "demo.c").write_text("int demo(void){return 0;}", encoding="utf-8")

    monkeypatch.setattr(
        "autoemu.validators.qemu_probe_validator.shutil.which",
        lambda name: "/usr/bin/ninja",
    )

    class FakeCompletedProcess:
        def __init__(self, stdout="[1/1] Compiling hw/stm32f407vg/demo.c\n"):
            self.returncode = 0
            self.stdout = stdout
            self.stderr = ""

    def fake_run(cmd, **kwargs):
        if str(cmd[0]).endswith("qemu-system-aarch64"):
            return FakeCompletedProcess(stdout="Linux version 6.12.28\neth0: probe complete\n")
        return FakeCompletedProcess()

    monkeypatch.setattr(
        "autoemu.validators.qemu_probe_validator.subprocess.run",
        fake_run,
    )

    result = run_qemu_probe(
        output_dir=output_dir,
        target_mcu="STM32F407VG",
        target_peripheral="ETH",
        qemu_build_env=build_env,
    )
    assert result["success"] is True
    assert result["skipped"] is False
    assert result["probe_status"] == "matched"


def test_run_qemu_probe_warns_on_ninja_failure(monkeypatch, tmp_path):
    """A ninja failure should be a soft-fail (not skipped, but success=False)."""
    build_env = tmp_path / "env" / "build" / "qemu-stm32f407vg"
    build_env.mkdir(parents=True)
    _write_build_ninja(build_env, "hw/arm")

    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "demo.c").write_text("int demo(void){return 0;}", encoding="utf-8")

    monkeypatch.setattr(
        "autoemu.validators.qemu_probe_validator.shutil.which",
        lambda name: "/usr/bin/ninja",
    )

    class FakeRefreshProcess:
        returncode = 0
        stdout = ""
        stderr = ""

    class FakeBuildProcess:
        returncode = 1
        stdout = ""
        stderr = "cc: error: generated model failed\n"

    def fake_run(cmd, **kwargs):
        if cmd[-1] == "build.ninja":
            return FakeRefreshProcess()
        return FakeBuildProcess()

    monkeypatch.setattr(
        "autoemu.validators.qemu_probe_validator.subprocess.run",
        fake_run,
    )

    result = run_qemu_probe(
        output_dir=output_dir,
        target_mcu="STM32F407VG",
        target_peripheral="ETH",
        qemu_build_env=build_env,
    )
    assert result["success"] is False
    assert result["skipped"] is False
    assert "ninja returned 1" in result["reason"]


def test_run_qemu_probe_skips_when_linux_boot_assets_missing(monkeypatch, tmp_path):
    """A successful rebuild without boot artifacts should be a clear soft skip."""
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


def test_run_qemu_probe_skips_when_generated_source_not_in_qemu_build_graph(monkeypatch, tmp_path):
    """Missing generated-source targets should be a clear soft skip."""
    build_env = tmp_path / "env" / "build" / "qemu-aarch64"
    build_env.mkdir(parents=True)
    (build_env / "build.ninja").write_text("# ninja\n", encoding="utf-8")
    qemu_src = tmp_path / "env" / "src" / "qemu-9.2.0"
    (qemu_src / "include" / "qemu").mkdir(parents=True)

    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "gpu_peripheral.json").write_text(
        json.dumps(
            {
                "name": "GPU",
                "peripheral_type": "generic",
                "mcu_family": "HiSilicon Kirin",
                "register_block": {"name": "GPU", "registers": []},
            }
        ),
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
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        commands.append([str(part) for part in cmd])
        return FakeCompletedProcess()

    monkeypatch.setattr(
        "autoemu.validators.qemu_probe_validator.subprocess.run",
        fake_run,
    )

    result = run_qemu_probe(
        output_dir=output_dir,
        target_mcu="Hikey960",
        target_peripheral="GPU",
        qemu_build_env=build_env,
    )

    assert result["success"] is False
    assert result["skipped"] is True
    assert "No QEMU ninja target references generated source" in result["reason"]
    assert ["/usr/bin/ninja", "-C", str(build_env), "hw/misc/all"] not in commands


def test_run_qemu_probe_builds_generated_source_object_target(monkeypatch, tmp_path):
    """The rebuild should target the generated C object when QEMU exposes one."""
    build_env = tmp_path / "env" / "build" / "qemu-aarch64"
    build_env.mkdir(parents=True)
    _write_boot_assets(build_env)
    (build_env / "build.ninja").write_text(
        "build libcommon.a.p/hw_misc_hikey960_gpu.c.o: "
        "c_COMPILER ../../src/qemu-9.2.0/hw/misc/hikey960_gpu.c\n",
        encoding="utf-8",
    )
    qemu_src = tmp_path / "env" / "src" / "qemu-9.2.0"
    (qemu_src / "include" / "qemu").mkdir(parents=True)

    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "gpu_peripheral.json").write_text(
        json.dumps(
            {
                "name": "GPU",
                "peripheral_type": "generic",
                "mcu_family": "HiSilicon Kirin",
                "register_block": {"name": "GPU", "registers": []},
            }
        ),
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
        def __init__(self, stdout=""):
            self.returncode = 0
            self.stdout = stdout
            self.stderr = ""

    def fake_run(cmd, **kwargs):
        commands.append([str(part) for part in cmd])
        if str(cmd[0]).endswith("qemu-system-aarch64"):
            return FakeCompletedProcess(stdout="Linux version 6.12.28\npanfrost e82c0000.gpu: probe complete\n")
        return FakeCompletedProcess()

    monkeypatch.setattr(
        "autoemu.validators.qemu_probe_validator.subprocess.run",
        fake_run,
    )

    result = run_qemu_probe(
        output_dir=output_dir,
        target_mcu="Hikey960",
        target_peripheral="GPU",
        qemu_build_env=build_env,
    )

    assert result["success"] is True
    assert ["/usr/bin/ninja", "-C", str(build_env), "hw/misc/all"] not in commands
    assert [
        "/usr/bin/ninja",
        "-C",
        str(build_env),
        "libcommon.a.p/hw_misc_hikey960_gpu.c.o",
    ] in commands


def test_analyze_probe_log_matches_driver_probe_success(tmp_path):
    """Probe log analysis should accept a booted guest with target driver output."""
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    log = (
        "Linux version 6.12.28\n"
        "panfrost e82c0000.gpu: mali-t860 id 0x860 major 0 minor 0 status 0\n"
    )

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
    """Probe failure lines near target tokens should soft-fail the probe."""
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


def test_analyze_probe_log_ignores_broad_soc_family_matches(tmp_path):
    """Generic SoC family words must not count as peripheral driver probes."""
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "gpu_peripheral.json").write_text(
        json.dumps({"name": "GPU", "mcu_family": "HiSilicon Kirin"}),
        encoding="utf-8",
    )
    log = (
        "Linux version 6.12.28\n"
        "hns3: Hisilicon Ethernet Network Driver for Hip08 Family - version\n"
    )

    result = _analyze_probe_log(
        log,
        output_dir=output_dir,
        target_mcu="Hikey960",
        target_peripheral="GPU",
    )

    assert result["probe_status"] == "inconclusive"
    assert result["success"] is False
    assert result["probe_lines"] == []


def test_run_qemu_probe_boots_linux_and_matches_probe_log(monkeypatch, tmp_path):
    """A successful phase-5 probe rebuilds QEMU, boots Linux, and matches probe logs."""
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
    assert ["/usr/bin/ninja", "-C", str(build_env), "qemu-system-aarch64"] in commands
    apply_commands = [cmd for cmd in commands if cmd and cmd[0].endswith("python3")]
    assert any("--apply-machine-patch" in cmd for cmd in apply_commands)
    assert any("panfrost" in line for line in result["probe_lines"])


def test_run_qemu_probe_uses_output_probe_boot_artifacts(monkeypatch, tmp_path):
    """Output-local probe DTB/rootfs artifacts should be used when present."""
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
    (output_dir / "probe.dtb").write_text("dtb\n", encoding="utf-8")
    (output_dir / "rootfs-aarch64-stage5.ext4").write_text("probe rootfs\n", encoding="utf-8")

    monkeypatch.setattr(
        "autoemu.validators.qemu_probe_validator.shutil.which",
        lambda name: "/usr/bin/ninja" if name == "ninja" else "/usr/bin/python3",
    )

    commands: list[list[str]] = []

    class FakeCompletedProcess:
        def __init__(self, stdout=""):
            self.returncode = 0
            self.stdout = stdout
            self.stderr = ""

    def fake_run(cmd, **kwargs):
        commands.append([str(part) for part in cmd])
        if str(cmd[0]).endswith("qemu-system-aarch64"):
            return FakeCompletedProcess(
                stdout="Linux version 6.12.28\npanfrost e82c0000.gpu: mali-g71 id 0x6000\n"
            )
        return FakeCompletedProcess()

    monkeypatch.setattr("autoemu.validators.qemu_probe_validator.subprocess.run", fake_run)

    result = run_qemu_probe(output_dir, "Hikey960", "GPU", qemu_build_env=build_env)

    qemu_cmd = next(cmd for cmd in commands if cmd[0].endswith("qemu-system-aarch64"))
    assert result["success"] is True
    assert f"file={output_dir / 'rootfs-aarch64-stage5.ext4'},format=raw,if=virtio" in qemu_cmd
    assert "-dtb" in qemu_cmd
    assert str(output_dir / "probe.dtb") in qemu_cmd
    assert result["boot_assets"]["dtb"] == str(output_dir / "probe.dtb")


def test_run_qemu_probe_reports_qemu_startup_failure(monkeypatch, tmp_path):
    """QEMU startup failures should be reported as phase-5 soft failures."""
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


def test_run_qemu_probe_emits_progress(monkeypatch, tmp_path):
    """Progress callback should receive messages during probing."""
    build_env = tmp_path / "env" / "build" / "qemu-stm32f407vg"
    build_env.mkdir(parents=True)
    _write_build_ninja(build_env, "hw/arm")
    _write_boot_assets(build_env)

    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "demo.c").write_text("int demo(void){return 0;}", encoding="utf-8")

    monkeypatch.setattr(
        "autoemu.validators.qemu_probe_validator.shutil.which",
        lambda name: "/usr/bin/ninja",
    )

    class FakeCompletedProcess:
        def __init__(self, stdout=""):
            self.returncode = 0
            self.stdout = stdout
            self.stderr = ""

    def fake_run(cmd, **kwargs):
        if str(cmd[0]).endswith("qemu-system-aarch64"):
            return FakeCompletedProcess(stdout="Linux version 6.12.28\neth0: probe complete\n")
        return FakeCompletedProcess()

    monkeypatch.setattr(
        "autoemu.validators.qemu_probe_validator.subprocess.run",
        fake_run,
    )

    log: list[tuple[str, str]] = []
    result = run_qemu_probe(
        output_dir=output_dir,
        target_mcu="STM32F407VG",
        target_peripheral="ETH",
        qemu_build_env=build_env,
        on_progress=lambda msg, kind: log.append((msg, kind)),
    )
    assert result["success"] is True
    assert any("ninja" in m.lower() for m, _ in log)
    assert any("booting guest linux" in m.lower() for m, _ in log)


def test_run_qemu_probe_emits_stage5_command_and_output_logs(monkeypatch, tmp_path):
    """Stage 5 should expose command lines and command output through progress logs."""
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
        def __init__(self, stdout="", stderr=""):
            self.returncode = 0
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd, **kwargs):
        cmd_text = " ".join(str(part) for part in cmd)
        if "build.ninja" in cmd_text:
            return FakeCompletedProcess(stdout="refresh ok\n")
        if "libcommon.a.p/hw_misc_hikey960_gpu.c.o" in cmd_text:
            return FakeCompletedProcess(stdout="object rebuilt\n")
        if cmd_text.endswith("qemu-system-aarch64"):
            return FakeCompletedProcess(stdout="system rebuilt\n")
        if str(cmd[0]).endswith("qemu-system-aarch64"):
            return FakeCompletedProcess(
                stdout="Linux version 6.12.28\npanfrost e82c0000.gpu: probe complete\n",
                stderr="guest stderr line\n",
            )
        return FakeCompletedProcess(stdout="apply ok\n")

    monkeypatch.setattr("autoemu.validators.qemu_probe_validator.subprocess.run", fake_run)

    log: list[tuple[str, str]] = []
    result = run_qemu_probe(
        output_dir=output_dir,
        target_mcu="Hikey960",
        target_peripheral="GPU",
        qemu_build_env=build_env,
        on_progress=lambda msg, kind: log.append((msg, kind)),
    )

    messages = "\n".join(msg for msg, _ in log)
    assert result["success"] is True
    assert "Stage 5 command: /usr/bin/ninja -C" in messages
    assert "Stage 5 result: returncode=0" in messages
    assert "Stage 5 stdout:" in messages
    assert "refresh ok" in messages
    assert "object rebuilt" in messages
    assert "system rebuilt" in messages
    assert "Stage 5 guest stdout:" in messages
    assert "panfrost e82c0000.gpu: probe complete" in messages
    assert "Stage 5 guest stderr:" in messages
    assert "guest stderr line" in messages
    assert "Stage 5 probe result: matched" in messages
    assert "Stage 5 probe line: panfrost e82c0000.gpu: probe complete" in messages


def test_run_qemu_probe_includes_poc_results_when_cve_findings(monkeypatch, tmp_path):
    """When cve_findings with poc_findings are passed, poc_results should be populated."""
    build_env = tmp_path / "env" / "build" / "qemu-stm32f407vg"
    build_env.mkdir(parents=True)
    _write_build_ninja(build_env, "hw/arm")
    _write_boot_assets(build_env)

    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "demo.c").write_text("int demo(void){return 0;}", encoding="utf-8")

    monkeypatch.setattr(
        "autoemu.validators.qemu_probe_validator.shutil.which",
        lambda name: "/usr/bin/ninja" if name == "ninja" else "/usr/bin/gcc",
    )

    class FakeCompletedProcess:
        def __init__(self, stdout=""):
            self.returncode = 0
            self.stdout = stdout
            self.stderr = ""

    def fake_run(cmd, **kwargs):
        if str(cmd[0]).endswith("qemu-system-aarch64"):
            return FakeCompletedProcess(stdout="Linux version 6.12.28\neth0: probe complete\n")
        return FakeCompletedProcess()

    monkeypatch.setattr(
        "autoemu.validators.qemu_probe_validator.subprocess.run",
        fake_run,
    )

    monkeypatch.setattr(
        "autoemu.validators.qemu_probe_validator._urlopen_with_retry",
        lambda request, timeout: _FakeResponse(b"int poc(void){return 0;}\n"),
    )

    monkeypatch.setattr(
        "autoemu.validators.qemu_probe_validator._check_content",
        lambda data, kind, filename: "",
    )

    cve_findings = {
        "poc_findings": [
            {"title": "PoC 1", "url": "https://github.com/user/repo/raw/main/poc.c", "category": "poc"},
        ],
    }

    result = run_qemu_probe(
        output_dir=output_dir,
        target_mcu="STM32F407VG",
        target_peripheral="ETH",
        qemu_build_env=build_env,
        cve_findings=cve_findings,
    )

    assert result["success"] is True
    assert "poc_results" in result
    assert len(result["poc_results"]) == 1
    assert result["poc_results"][0]["success"] is True
    assert result["poc_results"][0]["url"] == "https://github.com/user/repo/raw/main/poc.c"


def test_run_qemu_probe_poc_skips_non_source_urls(monkeypatch, tmp_path):
    """PoC URLs that are not .c/.h should be skipped with a reason."""
    build_env = tmp_path / "env" / "build" / "qemu-stm32f407vg"
    build_env.mkdir(parents=True)
    _write_build_ninja(build_env, "hw/arm")
    _write_boot_assets(build_env)

    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "demo.c").write_text("int demo(void){return 0;}", encoding="utf-8")

    monkeypatch.setattr(
        "autoemu.validators.qemu_probe_validator.shutil.which",
        lambda name: "/usr/bin/ninja",
    )

    class FakeCompletedProcess:
        def __init__(self, stdout=""):
            self.returncode = 0
            self.stdout = stdout
            self.stderr = ""

    def fake_run(cmd, **kwargs):
        if str(cmd[0]).endswith("qemu-system-aarch64"):
            return FakeCompletedProcess(stdout="Linux version 6.12.28\neth0: probe complete\n")
        return FakeCompletedProcess()

    monkeypatch.setattr(
        "autoemu.validators.qemu_probe_validator.subprocess.run",
        fake_run,
    )

    cve_findings = {
        "poc_findings": [
            {"title": "Advisory PDF", "url": "https://example.com/advisory.pdf", "category": "advisory"},
        ],
    }

    result = run_qemu_probe(
        output_dir=output_dir,
        target_mcu="STM32F407VG",
        target_peripheral="ETH",
        qemu_build_env=build_env,
        cve_findings=cve_findings,
    )

    assert result["success"] is True
    assert len(result["poc_results"]) == 1
    assert result["poc_results"][0]["success"] is False
    assert "not a C/H source" in result["poc_results"][0]["reason"]


def test_run_qemu_probe_poc_compile_failure(monkeypatch, tmp_path):
    """PoC compile errors should be recorded but not fail the phase."""
    build_env = tmp_path / "env" / "build" / "qemu-stm32f407vg"
    build_env.mkdir(parents=True)
    _write_build_ninja(build_env, "hw/arm")
    _write_boot_assets(build_env)

    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "demo.c").write_text("int demo(void){return 0;}", encoding="utf-8")

    monkeypatch.setattr(
        "autoemu.validators.qemu_probe_validator.shutil.which",
        lambda name: "/usr/bin/ninja" if name == "ninja" else "/usr/bin/gcc",
    )

    class FakeNinjaProcess:
        returncode = 0
        stdout = ""
        stderr = ""

    class FakeCompileProcess:
        returncode = 1
        stdout = ""
        stderr = "poc.c:1:5: error: unknown type name 'foo'\n"

    def fake_subprocess_run(cmd, **kwargs):
        if cmd[0] == "/usr/bin/ninja":
            return FakeNinjaProcess()
        if str(cmd[0]).endswith("qemu-system-aarch64"):
            proc = FakeNinjaProcess()
            proc.stdout = "Linux version 6.12.28\neth0: probe complete\n"
            return proc
        return FakeCompileProcess()

    monkeypatch.setattr(
        "autoemu.validators.qemu_probe_validator.subprocess.run",
        fake_subprocess_run,
    )

    monkeypatch.setattr(
        "autoemu.validators.qemu_probe_validator._urlopen_with_retry",
        lambda request, timeout: _FakeResponse(b"int poc(void){return 0;}\n"),
    )

    monkeypatch.setattr(
        "autoemu.validators.qemu_probe_validator._check_content",
        lambda data, kind, filename: "",
    )

    cve_findings = {
        "poc_findings": [
            {"title": "Bad PoC", "url": "https://example.com/poc.c", "category": "poc"},
        ],
    }

    result = run_qemu_probe(
        output_dir=output_dir,
        target_mcu="STM32F407VG",
        target_peripheral="ETH",
        qemu_build_env=build_env,
        cve_findings=cve_findings,
    )

    assert result["success"] is True  # ninja succeeded, phase is soft-fail
    assert len(result["poc_results"]) == 1
    assert result["poc_results"][0]["success"] is False
    assert "unknown type name" in result["poc_results"][0]["reason"]


class _FakeResponse:
    def __init__(self, data: bytes):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass
