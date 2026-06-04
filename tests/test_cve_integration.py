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


def _fake_cve_details(cve_id: str) -> dict:
    return {
        "found": True,
        "cve_id": cve_id,
        "description": "Qualcomm Adreno GPU driver vulnerability",
        "references": ["https://example.com/adreno-advisory"],
        "affected_products": ["cpe:2.3:h:qualcomm:adreno:-:*:*:*:*:*:*:*"],
        "published": "2022-10-19T11:15:10.387",
        "error": "",
    }


def _fake_poc_findings(cve_id: str, *args, **kwargs) -> list[dict[str, str]]:
    return [
        {
            "title": f"{cve_id} advisory",
            "url": f"https://example.com/{cve_id.lower()}",
            "category": "advisory",
        }
    ]


# ---------------------------------------------------------------------------
# CVE validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cve_id", ["CVE-2016-2067", "CVE-2022-25664"])
def test_run_cve_check_qualcomm_adreno(monkeypatch, cve_id):
    """CVE should be validated, disclosed, and related to Adreno GPU."""
    monkeypatch.setattr("autoemu.cve_validator.fetch_cve_details", _fake_cve_details)
    monkeypatch.setattr("autoemu.cve_validator.search_cve_poc", _fake_poc_findings)

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
    monkeypatch.setattr("autoemu.cve_validator.fetch_cve_details", _fake_cve_details)
    monkeypatch.setattr("autoemu.cve_validator.search_cve_poc", _fake_poc_findings)
    monkeypatch.setattr(
        "autoemu.cve_validator.fetch_cve_driver_sources",
        lambda *args, **kwargs: {"downloaded": [], "count": 0},
    )

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

    def fake_agent_probe_loop(self, **kw):
        return {"success": True, "skipped": False, "reason": ""}

    import types
    runtime._do_fetch = types.MethodType(fake_do_fetch, runtime)
    runtime._do_build = types.MethodType(fake_do_build, runtime)
    runtime._do_validate = types.MethodType(fake_do_validate, runtime)
    runtime._agent_probe_loop = types.MethodType(fake_agent_probe_loop, runtime)

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
    assert result.probe_result["success"] is True
