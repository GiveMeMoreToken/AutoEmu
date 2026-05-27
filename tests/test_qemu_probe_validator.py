"""Tests for the phase-5 QEMU probe validator."""

from __future__ import annotations

from pathlib import Path

import pytest

from autoemu.validators.qemu_probe_validator import run_qemu_probe


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
    """A successful ninja rebuild should report success."""
    build_env = tmp_path / "env" / "build" / "qemu-stm32f407vg"
    build_env.mkdir(parents=True)
    (build_env / "build.ninja").write_text("# ninja\n", encoding="utf-8")

    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "demo.c").write_text("int demo(void){return 0;}", encoding="utf-8")

    monkeypatch.setattr(
        "autoemu.validators.qemu_probe_validator.shutil.which",
        lambda name: "/usr/bin/ninja",
    )

    class FakeCompletedProcess:
        returncode = 0
        stdout = "[1/1] Compiling hw/stm32f407vg/demo.c\n"
        stderr = ""

    monkeypatch.setattr(
        "autoemu.validators.qemu_probe_validator.subprocess.run",
        lambda *args, **kwargs: FakeCompletedProcess(),
    )

    result = run_qemu_probe(
        output_dir=output_dir,
        target_mcu="STM32F407VG",
        target_peripheral="ETH",
        qemu_build_env=build_env,
    )
    assert result["success"] is True
    assert result["skipped"] is False


def test_run_qemu_probe_warns_on_ninja_failure(monkeypatch, tmp_path):
    """A ninja failure should be a soft-fail (not skipped, but success=False)."""
    build_env = tmp_path / "env" / "build" / "qemu-stm32f407vg"
    build_env.mkdir(parents=True)
    (build_env / "build.ninja").write_text("# ninja\n", encoding="utf-8")

    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "demo.c").write_text("int demo(void){return 0;}", encoding="utf-8")

    monkeypatch.setattr(
        "autoemu.validators.qemu_probe_validator.shutil.which",
        lambda name: "/usr/bin/ninja",
    )

    class FakeCompletedProcess:
        returncode = 1
        stdout = ""
        stderr = "ninja: error: unknown target 'hw/stm32f407vg/all'\n"

    monkeypatch.setattr(
        "autoemu.validators.qemu_probe_validator.subprocess.run",
        lambda *args, **kwargs: FakeCompletedProcess(),
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


def test_run_qemu_probe_emits_progress(monkeypatch, tmp_path):
    """Progress callback should receive messages during probing."""
    build_env = tmp_path / "env" / "build" / "qemu-stm32f407vg"
    build_env.mkdir(parents=True)
    (build_env / "build.ninja").write_text("# ninja\n", encoding="utf-8")

    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "demo.c").write_text("int demo(void){return 0;}", encoding="utf-8")

    monkeypatch.setattr(
        "autoemu.validators.qemu_probe_validator.shutil.which",
        lambda name: "/usr/bin/ninja",
    )

    class FakeCompletedProcess:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(
        "autoemu.validators.qemu_probe_validator.subprocess.run",
        lambda *args, **kwargs: FakeCompletedProcess(),
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


def test_run_qemu_probe_includes_poc_results_when_cve_findings(monkeypatch, tmp_path):
    """When cve_findings with poc_findings are passed, poc_results should be populated."""
    build_env = tmp_path / "env" / "build" / "qemu-stm32f407vg"
    build_env.mkdir(parents=True)
    (build_env / "build.ninja").write_text("# ninja\n", encoding="utf-8")

    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "demo.c").write_text("int demo(void){return 0;}", encoding="utf-8")

    monkeypatch.setattr(
        "autoemu.validators.qemu_probe_validator.shutil.which",
        lambda name: "/usr/bin/ninja" if name == "ninja" else "/usr/bin/gcc",
    )

    class FakeCompletedProcess:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(
        "autoemu.validators.qemu_probe_validator.subprocess.run",
        lambda *args, **kwargs: FakeCompletedProcess(),
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
    (build_env / "build.ninja").write_text("# ninja\n", encoding="utf-8")

    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "demo.c").write_text("int demo(void){return 0;}", encoding="utf-8")

    monkeypatch.setattr(
        "autoemu.validators.qemu_probe_validator.shutil.which",
        lambda name: "/usr/bin/ninja",
    )

    class FakeCompletedProcess:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(
        "autoemu.validators.qemu_probe_validator.subprocess.run",
        lambda *args, **kwargs: FakeCompletedProcess(),
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
    (build_env / "build.ninja").write_text("# ninja\n", encoding="utf-8")

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
