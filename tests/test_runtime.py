"""Tests for the unified agent runtime pipeline and CLI entry point."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from autoemu.main import cli
from autoemu.agent.runtime import (
    AgentRuntimeConfig,
    AutoEmuAgentRuntime,
    PipelineProgress,
)


@pytest.fixture(autouse=True)
def isolate_runtime_config(monkeypatch):
    """Keep runtime tests independent from local .autoemu.toml and env vars."""
    monkeypatch.delenv("AUTOEMU_AGENT_BACKEND", raising=False)
    monkeypatch.delenv("AUTOEMU_AGENT_MODEL", raising=False)
    monkeypatch.delenv("AUTOEMU_AGENT_MAX_BUDGET_USD", raising=False)
    monkeypatch.delenv("AUTOEMU_QEMU_SRC", raising=False)
    monkeypatch.setattr("autoemu.agent.runtime._load_config_file", lambda: {})


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_version():
    result = CliRunner().invoke(cli, ["--version"])
    assert result.exit_code == 0 and "0.1.0" in result.output


def test_cli_help():
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0 and "AutoEmu" in result.output


# ---------------------------------------------------------------------------
# Runtime config
# ---------------------------------------------------------------------------


def test_runtime_config_defaults_to_codex_sdk(monkeypatch):
    monkeypatch.delenv("AUTOEMU_AGENT_BACKEND", raising=False)
    monkeypatch.delenv("AUTOEMU_AGENT_MODEL", raising=False)
    monkeypatch.delenv("AUTOEMU_AGENT_MAX_BUDGET_USD", raising=False)
    # Prevent .autoemu.toml in CWD from affecting this test
    monkeypatch.setattr("autoemu.agent.runtime._load_config_file", lambda: {})

    config = AgentRuntimeConfig.load()

    assert config.backend == "codex-sdk"
    assert config.model is None
    assert config.max_budget_usd == 5.0


@pytest.mark.parametrize(
    "backend",
    ["claude-sdk", "codex-sdk", "anthropic-api", "openai-api"],
)
def test_runtime_config_accepts_agent_backend(monkeypatch, backend):
    monkeypatch.setattr("autoemu.agent.runtime._load_config_file", lambda: {})
    monkeypatch.setenv("AUTOEMU_AGENT_BACKEND", backend)

    config = AgentRuntimeConfig.load()

    assert config.backend == backend


@pytest.mark.parametrize("old_backend", ["claude", "codex", "openai"])
def test_runtime_config_rejects_old_agent_backend_names(monkeypatch, old_backend):
    monkeypatch.setattr("autoemu.agent.runtime._load_config_file", lambda: {})
    monkeypatch.setenv("AUTOEMU_AGENT_BACKEND", old_backend)

    with pytest.raises(ValueError, match="AUTOEMU_AGENT_BACKEND"):
        AgentRuntimeConfig.load()


def test_runtime_config_environment_overrides_file(monkeypatch):
    monkeypatch.setattr(
        "autoemu.agent.runtime._load_config_file",
        lambda: {
            "agent": {
                "backend": "claude-sdk",
                "model": "file-model",
                "max_budget_usd": 2.0,
                "openai_api_key": "file-openai-key",
                "openai_base_url": "https://file.example/v1",
            }
        },
    )
    monkeypatch.setenv("AUTOEMU_AGENT_BACKEND", "openai-api")
    monkeypatch.setenv("AUTOEMU_AGENT_MODEL", "env-model")
    monkeypatch.setenv("AUTOEMU_AGENT_MAX_BUDGET_USD", "9.5")
    monkeypatch.setenv("OPENAI_API_KEY", "env-openai-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://env.example/v1")

    config = AgentRuntimeConfig.load()

    assert config.backend == "openai-api"
    assert config.model == "env-model"
    assert config.max_budget_usd == 9.5
    assert config.openai_api_key == "env-openai-key"
    assert config.openai_base_url == "https://env.example/v1"


def test_runtime_config_loads_validation_qemu_src(monkeypatch):
    monkeypatch.setattr(
        "autoemu.agent.runtime._load_config_file",
        lambda: {"validation": {"qemu_src": "/opt/qemu"}},
    )

    config = AgentRuntimeConfig.load()

    assert config.qemu_src == "/opt/qemu"


def test_runtime_config_qemu_src_environment_overrides_file(monkeypatch):
    monkeypatch.setattr(
        "autoemu.agent.runtime._load_config_file",
        lambda: {"validation": {"qemu_src": "/opt/qemu-from-file"}},
    )
    monkeypatch.setenv("AUTOEMU_QEMU_SRC", "latest")

    config = AgentRuntimeConfig.load()

    assert config.qemu_src == "latest"


def test_run_pipeline_calls_phases(monkeypatch, tmp_path):
    """Verify the unified pipeline calls fetch, build, validate in order."""
    phases_seen: list[str] = []

    def fake_do_fetch(self, **kwargs):
        phases_seen.append("fetch")
        return {"success": True, "artifacts": [], "downloaded": []}

    def fake_do_build(self, **kwargs):
        phases_seen.append("build")
        return {"generated_files": ["test.c"], "target_mcu": "STM32F407VG"}

    def fake_do_validate(self, output_dir, **kwargs):
        phases_seen.append("validate")
        return {"success": True, "files_checked": 0, "errors": [], "warnings": []}

    monkeypatch.setattr(AutoEmuAgentRuntime, "_do_fetch", fake_do_fetch)
    monkeypatch.setattr(AutoEmuAgentRuntime, "_do_build", fake_do_build)
    monkeypatch.setattr(AutoEmuAgentRuntime, "_do_validate", fake_do_validate)

    runtime = AutoEmuAgentRuntime()
    result = runtime.run_pipeline(
        target_mcu="STM32F407VG",
        target_peripheral="ETH",
    )

    assert result.success
    assert "stm" in result.platform.lower()
    assert phases_seen == ["fetch", "build", "validate"]


def test_run_pipeline_emits_progress(monkeypatch):
    """Verify the on_progress callback is called with phase info."""
    progress_log: list[PipelineProgress] = []

    def fake_do_fetch(self, **kwargs):
        return {"success": True, "artifacts": []}

    def fake_do_build(self, **kwargs):
        return {"generated_files": []}

    def fake_do_validate(self, output_dir, **kwargs):
        return {"success": True, "files_checked": 0, "errors": [], "warnings": []}

    monkeypatch.setattr(AutoEmuAgentRuntime, "_do_fetch", fake_do_fetch)
    monkeypatch.setattr(AutoEmuAgentRuntime, "_do_build", fake_do_build)
    monkeypatch.setattr(AutoEmuAgentRuntime, "_do_validate", fake_do_validate)

    runtime = AutoEmuAgentRuntime()
    result = runtime.run_pipeline(
        target_mcu="STM32F407VG",
        target_peripheral="ETH",
        on_progress=progress_log.append,
    )

    assert result.success
    # Should have progress for each phase + finished
    phase_numbers = [p.phase for p in progress_log if not p.finished]
    assert 1 in phase_numbers  # detect
    assert 2 in phase_numbers  # fetch
    assert 3 in phase_numbers  # build
    assert 4 in phase_numbers  # validate
    assert any(p.finished for p in progress_log)


def test_run_pipeline_handles_fetch_error(monkeypatch):
    """Pipeline handles errors gracefully."""

    def fake_do_fetch(self, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(AutoEmuAgentRuntime, "_do_fetch", fake_do_fetch)

    runtime = AutoEmuAgentRuntime()
    result = runtime.run_pipeline(
        target_mcu="STM32F407VG",
        target_peripheral="ETH",
    )

    assert not result.success
    assert "network down" in result.error


def test_run_pipeline_fails_when_validation_fails(monkeypatch):
    """Validation failure must determine the final pipeline status."""

    def fake_do_fetch(self, **kwargs):
        return {"success": True, "downloaded": [{"file": "driver.c"}]}

    def fake_do_build(self, **kwargs):
        return {"success": True, "generated_files": ["output/gpu_peripheral.json"]}

    def fake_do_validate(self, output_dir, **kwargs):
        return {
            "success": False,
            "files_checked": 0,
            "errors": [],
            "warnings": ["gpu_peripheral.json has base_address=0 - likely incorrect"],
        }

    monkeypatch.setattr(AutoEmuAgentRuntime, "_do_fetch", fake_do_fetch)
    monkeypatch.setattr(AutoEmuAgentRuntime, "_do_build", fake_do_build)
    monkeypatch.setattr(AutoEmuAgentRuntime, "_do_validate", fake_do_validate)

    runtime = AutoEmuAgentRuntime()
    result = runtime.run_pipeline(
        target_mcu="Hikey960",
        target_peripheral="GPU",
    )

    assert not result.success
    assert "base_address=0" in result.error
    assert result.validation_result["success"] is False


def test_run_pipeline_falls_back_on_agent_errors_when_local_valid(monkeypatch):
    """Configured agent failures should not fail a valid deterministic local run."""

    def fake_do_fetch(self, **kwargs):
        return {"success": True, "downloaded": [{"file": "driver.c"}]}

    def fake_do_build(self, **kwargs):
        return {
            "success": True,
            "generated_files": ["output/hikey960_gpu.c"],
            "agent_error": "codex-app-server-sdk is not installed",
        }

    def fake_do_validate(self, output_dir, **kwargs):
        return {"success": True, "files_checked": 1, "errors": [], "warnings": []}

    monkeypatch.setattr(AutoEmuAgentRuntime, "_do_fetch", fake_do_fetch)
    monkeypatch.setattr(AutoEmuAgentRuntime, "_do_build", fake_do_build)
    monkeypatch.setattr(AutoEmuAgentRuntime, "_do_validate", fake_do_validate)

    runtime = AutoEmuAgentRuntime(AgentRuntimeConfig(backend="codex-sdk"))
    result = runtime.run_pipeline(
        target_mcu="Hikey960",
        target_peripheral="GPU",
    )

    assert result.success
    assert result.generated_files == ["output/hikey960_gpu.c"]
    assert result.error == ""


def test_run_pipeline_generic_platform_detection(monkeypatch):
    """Non-STM32 targets get 'generic' platform and per-MCU data dir."""
    captured: dict = {}

    def fake_do_fetch(self, **kwargs):
        captured.update(kwargs)
        return {"success": True, "downloaded": [{"file": "test.c"}]}

    def fake_do_build(self, **kwargs):
        captured["build_data_dir"] = kwargs.get("data_dir", "")
        return {"generated_files": []}

    def fake_do_validate(self, output_dir, **kwargs):
        return {"success": True, "files_checked": 0, "errors": [], "warnings": []}

    monkeypatch.setattr(AutoEmuAgentRuntime, "_do_fetch", fake_do_fetch)
    monkeypatch.setattr(AutoEmuAgentRuntime, "_do_build", fake_do_build)
    monkeypatch.setattr(AutoEmuAgentRuntime, "_do_validate", fake_do_validate)

    runtime = AutoEmuAgentRuntime()
    result = runtime.run_pipeline(
        target_mcu="kirin960",
        target_peripheral="gpu",
    )

    assert result.success
    assert "hisilicon" in result.platform.lower()
    # Data dir should be per-MCU, not "data/generic"
    assert captured["output_dir"] == "data/kirin960"
    assert captured["build_data_dir"] == "data/kirin960"


def test_run_pipeline_per_mcu_data_dirs(monkeypatch):
    """Each MCU gets its own data folder."""
    dirs: list[str] = []

    def fake_do_fetch(self, **kwargs):
        dirs.append(kwargs.get("output_dir", ""))
        return {"success": True, "downloaded": []}

    def fake_do_build(self, **kwargs):
        dirs.append(kwargs.get("data_dir", ""))
        return {"generated_files": []}

    def fake_do_validate(self, output_dir, **kwargs):
        return {"success": True, "files_checked": 0, "errors": [], "warnings": []}

    monkeypatch.setattr(AutoEmuAgentRuntime, "_do_fetch", fake_do_fetch)
    monkeypatch.setattr(AutoEmuAgentRuntime, "_do_build", fake_do_build)
    monkeypatch.setattr(AutoEmuAgentRuntime, "_do_validate", fake_do_validate)

    runtime = AutoEmuAgentRuntime()

    runtime.run_pipeline(target_mcu="ESP32", target_peripheral="wifi")
    assert dirs[-2] == "data/esp32"  # fetch
    assert dirs[-1] == "data/esp32"  # build

    runtime.run_pipeline(target_mcu="STM32F407VG", target_peripheral="ETH")
    assert dirs[-2] == "data/stm32f407vg"  # fetch
    assert dirs[-1] == "data/stm32f407vg"  # build


def test_do_fetch_records_agent_fetch_error(monkeypatch, tmp_path):
    """Agent fetch failures should be returned to the caller."""

    class FakeFetcher:
        def discover_candidates(self, target_mcu, target_peripheral):
            return []

        def fetch_selected(self, selected, output_dir, *, target_mcu, target_peripheral):
            return SimpleNamespace(downloaded=[], errors=[])

    class FakeOrchestrator:
        def __init__(self, **kwargs):
            pass

        async def fetch_input_data(self, task, on_event=None):
            return SimpleNamespace(
                error="codex-app-server-sdk is not installed",
                agent_messages=[],
            )

    monkeypatch.setattr("autoemu.agent.runtime.GenericDataFetcher", FakeFetcher)
    monkeypatch.setattr("autoemu.agent.runtime.AutoEmuOrchestrator", FakeOrchestrator)

    runtime = AutoEmuAgentRuntime(AgentRuntimeConfig(backend="codex-sdk"))
    result = runtime._do_fetch(
        target_mcu="Hikey960",
        target_peripheral="GPU",
        platform_name="generic",
        output_dir=str(tmp_path),
    )

    assert result["agent_error"] == "codex-app-server-sdk is not installed"


def test_do_build_records_agent_build_error_without_discarding_local_output(monkeypatch, tmp_path):
    """Agent build failures should be explicit while preserving local artifacts."""

    class FakeOrchestrator:
        def __init__(self, **kwargs):
            pass

        async def model_peripheral(self, task, on_event=None):
            return SimpleNamespace(
                error="codex-app-server-sdk is not installed",
                agent_messages=[],
            )

    def fake_run_target_model_pipeline(**kwargs):
        return {
            "success": True,
            "generated_files": ["output/hikey960_gpu.c"],
        }

    def fake_resolve_fetched_input_bundle(**kwargs):
        return SimpleNamespace(svd_path="", header_path="", driver_paths=())

    monkeypatch.setattr("autoemu.agent.runtime.AutoEmuOrchestrator", FakeOrchestrator)
    monkeypatch.setattr("autoemu.agent.runtime.run_target_model_pipeline", fake_run_target_model_pipeline)
    monkeypatch.setattr("autoemu.agent.runtime.resolve_fetched_input_bundle", fake_resolve_fetched_input_bundle)

    runtime = AutoEmuAgentRuntime(AgentRuntimeConfig(backend="codex-sdk"))
    result = runtime._do_build(
        target_mcu="Hikey960",
        target_peripheral="GPU",
        platform_name="generic",
        data_dir=str(tmp_path),
        output_dir=str(tmp_path / "output"),
    )

    assert result["generated_files"] == ["output/hikey960_gpu.c"]
    assert result["agent_error"] == "codex-app-server-sdk is not installed"


def test_do_validate_uses_configured_qemu_source(monkeypatch, tmp_path):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "demo.c").write_text("int demo(void) { return 0; }\n", encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_find_qemu_include_paths(qemu_src=None):
        captured["find_qemu_src"] = qemu_src
        return [str(tmp_path / "qemu" / "include")]

    def fake_validate_compile(files, *, qemu_src=None):
        captured["validate_qemu_src"] = qemu_src
        captured["files"] = [str(f) for f in files]
        return {"success": True, "files_checked": 1, "errors": [], "warnings": []}

    monkeypatch.setattr("autoemu.agent.runtime.find_qemu_include_paths", fake_find_qemu_include_paths)
    monkeypatch.setattr("autoemu.agent.runtime.validate_compile", fake_validate_compile)

    runtime = AutoEmuAgentRuntime(AgentRuntimeConfig(qemu_src="/opt/qemu"))
    result = runtime._do_validate(str(output_dir))

    assert result["success"] is True
    assert captured["find_qemu_src"] == "/opt/qemu"
    assert captured["validate_qemu_src"] == "/opt/qemu"
    assert captured["files"] == [str(output_dir / "demo.c")]
