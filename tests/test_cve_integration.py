"""Integration tests for CVE-driven pipeline paths.

These tests exercise the full CVE validation → driver fetch → phase-5 PoC
probe chain with mocked slow/external dependencies (web search, agent,
QEMU build).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from autoemu.agent.runtime import AgentRuntimeConfig, AutoEmuAgentRuntime
from autoemu.cve_validator import run_cve_check


# ---------------------------------------------------------------------------
# CVE validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cve_id", ["CVE-2016-2067", "CVE-2022-25664"])
def test_run_cve_check_qualcomm_adreno(cve_id):
    """CVE should be validated, disclosed, and related to Adreno GPU."""
    result = run_cve_check(cve_id, peripheral_name="GPU", mcu_name="Qualcomm Adreno")
    assert result["valid_format"] is True
    assert result["disclosed"] is True
    assert result["related"] is True
    assert len(result["poc_findings"]) > 0


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
