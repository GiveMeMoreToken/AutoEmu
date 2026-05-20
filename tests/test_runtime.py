"""Tests for the unified agent runtime pipeline and CLI entry point."""

from __future__ import annotations

import json
from pathlib import Path

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


def test_runtime_config_defaults_to_harness(monkeypatch):
    monkeypatch.delenv("AUTOEMU_AGENT_BACKEND", raising=False)
    monkeypatch.delenv("AUTOEMU_AGENT_MODEL", raising=False)
    monkeypatch.delenv("AUTOEMU_AGENT_MAX_BUDGET_USD", raising=False)
    # Prevent .autoemu.toml in CWD from affecting this test
    monkeypatch.setattr("autoemu.agent.runtime._load_config_file", lambda: {})

    config = AgentRuntimeConfig.load()

    assert config.backend == "harness"
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


# ---------------------------------------------------------------------------
# Runtime validation
# ---------------------------------------------------------------------------


def _valid_qemu_hardware_json() -> dict:
    return {
        "identity": {
            "peripheral_name": "DEMO",
            "qom_type": "demo-device",
            "c_identifier_prefix": "demo_device",
            "type_macro": "TYPE_DEMO_DEVICE",
            "state_struct_name": "DemoDeviceState",
            "kconfig_symbol": "DEMO_DEVICE",
        },
        "file_layout": {
            "source_path": "hw/misc/demo_device.c",
            "header_path": "include/hw/misc/demo_device.h",
            "meson_path": "hw/misc/meson.build",
            "meson_snippet_path": "hw/misc/demo_device.meson.inc",
            "qtest_path": "tests/qtest/demo_device-test.c",
        },
        "mmio_regions": [
            {
                "name": "mmio",
                "base_address": 0x40010000,
                "size": 0x10,
                "register_count": 1,
            }
        ],
        "irq_resources": [],
        "device_tree": {
            "node_name": "demo",
            "unit_address": "40010000",
            "address_cells": 1,
            "size_cells": 1,
            "compatible": ["demo,device"],
            "reg": [
                {
                    "name": "mmio",
                    "base_address": 0x40010000,
                    "size": 0x10,
                }
            ],
            "interrupt_names": [],
            "properties": {},
        },
    }


def _write_valid_qemu_hardware(path: Path) -> None:
    path.write_text(json.dumps(_valid_qemu_hardware_json()), encoding="utf-8")


def _write_nested_c_h_files(base: Path) -> None:
    source = base / "hw" / "misc" / "demo_device.c"
    header = base / "include" / "hw" / "misc" / "demo_device.h"
    source.parent.mkdir(parents=True, exist_ok=True)
    header.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("int demo_device_init(void) { return 0; }\n", encoding="utf-8")
    header.write_text("#pragma once\nint demo_device_init(void);\n", encoding="utf-8")


def test_do_validate_recurses_generated_sources_when_qemu_tree_missing(monkeypatch, tmp_path):
    _write_valid_qemu_hardware(tmp_path / "demo_qemu_hardware.json")
    _write_nested_c_h_files(tmp_path)
    monkeypatch.setattr("autoemu.agent.runtime.find_qemu_include_paths", lambda: [])

    result = AutoEmuAgentRuntime()._do_validate(str(tmp_path))

    assert result["success"] is True
    assert result["generated_source_files"] == 2
    assert result["files_checked"] == 0
    assert any("skipping compilation check" in warning.lower() for warning in result["warnings"])
    assert not any("no c/h files" in warning.lower() for warning in result["warnings"])


def test_do_validate_fails_when_no_generated_sources(monkeypatch, tmp_path):
    _write_valid_qemu_hardware(tmp_path / "demo_qemu_hardware.json")
    monkeypatch.setattr("autoemu.agent.runtime.find_qemu_include_paths", lambda: [])

    result = AutoEmuAgentRuntime()._do_validate(str(tmp_path))

    assert result["success"] is False
    assert result["generated_source_files"] == 0
    assert any("no generated c/h files" in error.lower() for error in result["errors"])


def test_do_validate_fails_on_missing_qemu_hardware_json(monkeypatch, tmp_path):
    _write_nested_c_h_files(tmp_path)
    monkeypatch.setattr("autoemu.agent.runtime.find_qemu_include_paths", lambda: [])

    result = AutoEmuAgentRuntime()._do_validate(str(tmp_path))

    assert result["success"] is False
    assert any("qemu hardware" in error.lower() for error in result["errors"])
    assert any("skipping compilation check" in warning.lower() for warning in result["warnings"])


def test_do_validate_fails_on_empty_qemu_hardware_json_when_compile_skipped(monkeypatch, tmp_path):
    (tmp_path / "demo_qemu_hardware.json").write_text("", encoding="utf-8")
    _write_nested_c_h_files(tmp_path)
    monkeypatch.setattr("autoemu.agent.runtime.find_qemu_include_paths", lambda: [])

    result = AutoEmuAgentRuntime()._do_validate(str(tmp_path))

    assert result["success"] is False
    assert any("empty" in error.lower() for error in result["errors"])
    assert any("qemu hardware" in error.lower() for error in result["errors"])


def test_do_validate_fails_on_incomplete_qemu_hardware_json(monkeypatch, tmp_path):
    (tmp_path / "demo_qemu_hardware.json").write_text(
        json.dumps({"identity": {"peripheral_name": "DEMO"}}),
        encoding="utf-8",
    )
    _write_nested_c_h_files(tmp_path)
    monkeypatch.setattr("autoemu.agent.runtime.find_qemu_include_paths", lambda: [])

    result = AutoEmuAgentRuntime()._do_validate(str(tmp_path))

    assert result["success"] is False
    assert any("incomplete or invalid" in error.lower() for error in result["errors"])
