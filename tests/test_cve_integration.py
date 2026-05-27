"""Integration tests for CVE-driven pipeline paths.

These tests exercise the full CVE validation → driver fetch → phase-5 PoC
probe chain with mocked slow/external dependencies (web search, agent,
QEMU build).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from autoemu.agent.runtime import AgentRuntimeConfig, AutoEmuAgentRuntime
from autoemu.cve_validator import fetch_cve_driver_sources, run_cve_check
from autoemu.validators.qemu_probe_validator import run_qemu_probe


class FakeCompletedProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def fake_qemu_build_env(tmp_path):
    """Create a minimal QEMU build directory with build.ninja."""
    env = tmp_path / "env" / "build" / "qemu-qualcomm_adreno"
    env.mkdir(parents=True)
    (env / "build.ninja").write_text("# ninja\n", encoding="utf-8")
    return env


@pytest.fixture
def generated_output_dir(tmp_path):
    """Create an output directory with fake generated C/H files."""
    out = tmp_path / "output"
    out.mkdir()
    (out / "adreno_gpu.c").write_text("int adreno_gpu_init(void){return 0;}\n", encoding="utf-8")
    (out / "adreno_gpu.h").write_text("#ifndef ADRENO_GPU_H\n#define ADRENO_GPU_H\n#endif\n", encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# CVE validation
# ---------------------------------------------------------------------------

def test_run_cve_check_qualcomm_adreno_cve_2016_2067():
    """CVE-2016-2067 should be validated, disclosed, and related to Adreno GPU."""
    result = run_cve_check("CVE-2016-2067", peripheral_name="GPU", mcu_name="Qualcomm Adreno")
    assert result["valid_format"] is True
    assert result["disclosed"] is True
    assert result["related"] is True
    assert len(result["poc_findings"]) > 0


def test_run_cve_check_qualcomm_adreno_cve_2022_25664():
    """CVE-2022-25664 should be validated, disclosed, and related to Adreno GPU."""
    result = run_cve_check("CVE-2022-25664", peripheral_name="GPU", mcu_name="Qualcomm Adreno")
    assert result["valid_format"] is True
    assert result["disclosed"] is True
    assert result["related"] is True
    assert len(result["poc_findings"]) > 0


# ---------------------------------------------------------------------------
# CVE driver source fetching
# ---------------------------------------------------------------------------

def test_fetch_cve_driver_sources_cve_2022_25664_downloads_adreno_files(tmp_path):
    """CVE-2022-25664 should yield Adreno driver source files."""
    result = fetch_cve_driver_sources(
        "CVE-2022-25664",
        peripheral_name="GPU",
        mcu_name="Qualcomm Adreno",
        output_dir=tmp_path,
    )
    assert isinstance(result, dict)
    assert "downloaded" in result
    # At least one .c file should be found (GitHub Security Lab hosts them)
    c_files = [d for d in result["downloaded"] if d["url"].endswith(".c")]
    assert len(c_files) >= 1


def test_fetch_cve_driver_sources_cve_2016_2067_returns_empty_or_dict(tmp_path):
    """CVE-2016-2067 may return zero driver files (advisory-only search results)."""
    result = fetch_cve_driver_sources(
        "CVE-2016-2067",
        peripheral_name="GPU",
        mcu_name="Qualcomm Adreno",
        output_dir=tmp_path,
    )
    assert isinstance(result, dict)
    assert "downloaded" in result
    assert "count" in result
    # Zero files is acceptable for older CVEs without public driver sources
    assert result["count"] >= 0


# ---------------------------------------------------------------------------
# Phase 5: PoC probe with CVE findings
# ---------------------------------------------------------------------------

def test_run_qemu_probe_with_cve_poc_findings(monkeypatch, tmp_path, fake_qemu_build_env, generated_output_dir):
    """When CVE findings contain PoC URLs, phase 5 should attempt to download and compile them."""
    cve_findings = run_cve_check("CVE-2022-25664", peripheral_name="GPU", mcu_name="Qualcomm Adreno")
    assert len(cve_findings["poc_findings"]) > 0

    monkeypatch.setattr(
        "autoemu.validators.qemu_probe_validator.shutil.which",
        lambda name: "/usr/bin/ninja" if name == "ninja" else "/usr/bin/gcc",
    )
    monkeypatch.setattr(
        "autoemu.validators.qemu_probe_validator.subprocess.run",
        lambda *args, **kwargs: FakeCompletedProcess(),
    )

    result = run_qemu_probe(
        output_dir=generated_output_dir,
        target_mcu="Qualcomm Adreno",
        target_peripheral="GPU",
        qemu_build_env=fake_qemu_build_env,
        cve_findings=cve_findings,
    )

    assert result["success"] is True
    assert result["skipped"] is False
    assert "poc_results" in result
    # PoC results should be populated (one entry per PoC finding)
    assert len(result["poc_results"]) > 0
    # Most PoC URLs are not .c/.h files, so most should be skipped
    skipped = [pr for pr in result["poc_results"] if "not a C/H source" in pr.get("reason", "")]
    assert len(skipped) >= 1


# ---------------------------------------------------------------------------
# Full pipeline with CVE (mocked slow parts)
# ---------------------------------------------------------------------------

def test_pipeline_with_cve_2022_25664_records_poc_results(monkeypatch, tmp_path):
    """The full pipeline should record CVE findings and phase-5 PoC results."""
    runtime = AutoEmuAgentRuntime(AgentRuntimeConfig(backend="codex-sdk"))

    out = tmp_path / "output"
    out.mkdir()
    (out / "adreno_gpu.c").write_text("int adreno_gpu_init(void){return 0;}\n", encoding="utf-8")
    (out / "adreno_gpu.h").write_text("#ifndef ADRENO_GPU_H\n#define ADRENO_GPU_H\n#endif\n", encoding="utf-8")

    def fake_do_fetch(self, **kw):
        return {"success": True, "downloaded": []}

    def fake_do_build(self, output_dir, **kw):
        # Copy files to the requested output_dir
        import shutil
        for f in out.iterdir():
            shutil.copy(f, Path(output_dir) / f.name)
        return {"success": True, "generated_files": [str(out / "adreno_gpu.c"), str(out / "adreno_gpu.h")]}

    def fake_do_validate(self, output_dir, **kw):
        return {"success": True, "files_checked": 2, "errors": [], "warnings": []}

    import types
    runtime._do_fetch = types.MethodType(fake_do_fetch, runtime)
    runtime._do_build = types.MethodType(fake_do_build, runtime)
    runtime._do_validate = types.MethodType(fake_do_validate, runtime)

    # Create a fake QEMU build env so phase 5 doesn't skip
    env = tmp_path / "env" / "build" / "qemu-qualcomm_adreno"
    env.mkdir(parents=True)
    (env / "build.ninja").write_text("# ninja\n", encoding="utf-8")

    result = runtime.run_pipeline(
        target_mcu="Qualcomm Adreno",
        target_peripheral="GPU",
        cve_id="CVE-2022-25664",
    )

    assert result.success is True
    assert result.cve_findings["valid_format"] is True
    assert result.cve_findings["related"] is True
    assert len(result.cve_findings["poc_findings"]) > 0
    # Probe should run and record poc_results
    assert "poc_results" in result.probe_result
