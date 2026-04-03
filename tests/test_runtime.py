"""Tests for the harness-first agent runtime."""

from __future__ import annotations

import json
from pathlib import Path

from autoemu.agent.orchestrator import FetchResult as AgentFetchResult
from autoemu.agent.orchestrator import ModelingResult
from autoemu.agent.runtime import AgentRuntimeConfig, AutoEmuAgentRuntime


def test_runtime_config_defaults_to_harness(monkeypatch):
    monkeypatch.delenv("AUTOEMU_AGENT_BACKEND", raising=False)
    monkeypatch.delenv("AUTOEMU_AGENT_MODEL", raising=False)
    monkeypatch.delenv("AUTOEMU_AGENT_MAX_BUDGET_USD", raising=False)

    config = AgentRuntimeConfig.from_env()

    assert config.backend == "harness"
    assert config.model is None
    assert config.max_budget_usd == 5.0


def test_agent_fetch_reconstructs_manifest(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    manifest_path = data_dir / "manifests" / "stm32f407vg_eth.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "target_mcu": "STM32F407VG",
                "target_peripheral": "ETH",
                "success": True,
                "artifacts": [],
            }
        ),
        encoding="utf-8",
    )

    async def fake_fetch_input_data(self, task, on_message=None):
        return AgentFetchResult(
            target_mcu=task.target_mcu,
            target_peripheral=task.target_peripheral,
            success=True,
            manifest_files=["manifests/stm32f407vg_eth.json"],
            agent_messages=["fetch complete"],
        )

    monkeypatch.setattr(
        "autoemu.agent.runtime.AutoEmuOrchestrator.fetch_input_data",
        fake_fetch_input_data,
    )

    runtime = AutoEmuAgentRuntime(AgentRuntimeConfig(backend="openai", model="gpt-test"))
    result = runtime.fetch_data(
        target_mcu="STM32F407VG",
        target_peripheral="ETH",
        output_dir=data_dir,
    )

    assert result["target_mcu"] == "STM32F407VG"
    assert result["execution_backend"] == "openai"
    assert result["execution_mode"] == "agent"
    assert result["execution_model"] == "gpt-test"
    assert result["agent_messages"] == ["fetch complete"]


def test_agent_build_reconstructs_generated_bundle(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    output_dir = tmp_path / "bundle"
    target_root = data_dir / "stm32f407vg"
    (target_root / "docs").mkdir(parents=True)
    (target_root / "svd").mkdir(parents=True)
    (target_root / "headers").mkdir(parents=True)
    (target_root / "drivers" / "hal").mkdir(parents=True)
    (data_dir / "manifests").mkdir(parents=True)
    output_dir.mkdir(parents=True)

    reference_manual = target_root / "docs" / "reference_manual.txt"
    svd = target_root / "svd" / "device.svd"
    header = target_root / "headers" / "stm32f407xx.h"
    driver = target_root / "drivers" / "hal" / "stm32f4xx_hal_eth.c"
    manifest_path = data_dir / "manifests" / "stm32f407vg_eth.json"

    reference_manual.write_text("manual", encoding="utf-8")
    svd.write_text("<device></device>", encoding="utf-8")
    header.write_text("#define STM32F407 1\n", encoding="utf-8")
    driver.write_text("void HAL_ETH_Init(void) {}\n", encoding="utf-8")
    manifest_path.write_text(
        json.dumps(
            {
                "target_mcu": "STM32F407VG",
                "target_peripheral": "ETH",
                "artifacts": [
                    {"category": "docs", "status": "downloaded", "local_path": str(reference_manual)},
                    {"category": "svd", "status": "downloaded", "local_path": str(svd)},
                    {"category": "headers", "status": "downloaded", "local_path": str(header)},
                    {"category": "drivers_hal", "status": "downloaded", "local_path": str(driver)},
                ],
            }
        ),
        encoding="utf-8",
    )

    generated_json = {
        "eth_registers.json": {"ETH": {"name": "ETH", "registers": []}},
        "eth_state_machine.json": {"model": {"name": "ETH", "states": [], "transitions": []}},
        "eth_interrupt_model.json": {"model": {"peripheral_name": "ETH", "lines": []}},
        "eth_dependencies.json": {"model": {"mcu_name": "STM32F4", "edges": []}},
        "eth_peripheral.json": {"name": "ETH"},
        "eth_validation.json": {"success": True, "issue_count": 0},
    }
    for name, payload in generated_json.items():
        (output_dir / name).write_text(json.dumps(payload), encoding="utf-8")

    async def fake_model_peripheral(self, task, on_message=None):
        return ModelingResult(
            peripheral_name=task.peripheral_name,
            success=True,
            generated_files=[path.name for path in output_dir.iterdir()],
            agent_messages=["build complete"],
        )

    monkeypatch.setattr(
        "autoemu.agent.runtime.AutoEmuOrchestrator.model_peripheral",
        fake_model_peripheral,
    )

    runtime = AutoEmuAgentRuntime(AgentRuntimeConfig(backend="claude"))
    result = runtime.build_qemu_peripheral(
        target_mcu="STM32F407VG",
        target_peripheral="ETH",
        data_dir=data_dir,
        output_dir=output_dir,
    )

    assert result["target_mcu"] == "STM32F407VG"
    assert result["target_peripheral"] == "ETH"
    assert result["execution_backend"] == "claude"
    assert result["execution_mode"] == "agent"
    assert result["validation_report"]["success"] is True
    assert str(output_dir / "eth_peripheral.json") == result["peripheral_json"]
    assert result["agent_messages"] == ["build complete"]
