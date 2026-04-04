"""Harness-first runtime for the public AutoEmu workflows."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autoemu.agent.orchestrator import AutoEmuOrchestrator, FetchTask, ModelingTask
from autoemu.fetchers.stm32 import (
    STM32DataFetcher,
    infer_stm32_mcu_family,
    resolve_fetched_input_bundle,
)
from autoemu.pipeline import run_target_model_pipeline


SUPPORTED_AGENT_BACKENDS = {"harness", "claude", "openai"}


@dataclass(frozen=True)
class AgentRuntimeConfig:
    """Execution settings for the public agent runtime."""

    backend: str = "harness"
    model: str | None = None
    max_budget_usd: float = 5.0

    @classmethod
    def from_env(cls) -> "AgentRuntimeConfig":
        """Build a runtime config from environment variables."""
        backend = (os.getenv("AUTOEMU_AGENT_BACKEND", "harness") or "harness").strip().lower()
        if backend not in SUPPORTED_AGENT_BACKENDS:
            raise ValueError(
                "AUTOEMU_AGENT_BACKEND must be one of: "
                + ", ".join(sorted(SUPPORTED_AGENT_BACKENDS))
            )

        model = (os.getenv("AUTOEMU_AGENT_MODEL") or "").strip() or None
        budget_text = (os.getenv("AUTOEMU_AGENT_MAX_BUDGET_USD") or "").strip()
        max_budget_usd = 5.0
        if budget_text:
            max_budget_usd = float(budget_text)

        return cls(
            backend=backend,
            model=model,
            max_budget_usd=max_budget_usd,
        )


class AutoEmuAgentRuntime:
    """Run the public workflows through one harness-oriented entrypoint."""

    def __init__(self, config: AgentRuntimeConfig | None = None) -> None:
        self.config = config or AgentRuntimeConfig.from_env()

    def fetch_data(
        self,
        *,
        target_mcu: str,
        target_peripheral: str,
        output_dir: str | Path = "data/stm32",
        refresh: bool = False,
        offline: bool = False,
    ) -> dict[str, Any]:
        """Fetch source inputs using the configured execution backend."""
        if self.config.backend == "harness":
            fetcher = STM32DataFetcher(offline=offline)
            result = fetcher.fetch_data(
                target_mcu=target_mcu,
                target_peripheral=target_peripheral,
                output_dir=output_dir,
                refresh=refresh,
            )
            return _with_execution_metadata(result.to_manifest(), self.config, mode="harness")

        orchestrator = AutoEmuOrchestrator(
            backend=self.config.backend,
            model=self.config.model,
            max_budget_usd=self.config.max_budget_usd,
        )
        result = asyncio.run(
            orchestrator.fetch_input_data(
                FetchTask(
                    target_mcu=target_mcu,
                    target_peripheral=target_peripheral,
                    output_dir=str(output_dir),
                    refresh=refresh,
                )
            )
        )
        if result.error:
            raise RuntimeError(result.error)

        manifest = _load_fetch_manifest(
            target_mcu=target_mcu,
            target_peripheral=target_peripheral,
            output_dir=output_dir,
        )
        manifest["agent_messages"] = result.agent_messages
        return _with_execution_metadata(manifest, self.config, mode="agent")

    def build_qemu_peripheral(
        self,
        *,
        target_mcu: str,
        target_peripheral: str,
        data_dir: str | Path = "data/stm32",
        output_dir: str | Path = "output",
        offline: bool = False,
    ) -> dict[str, Any]:
        """Build a QEMU-ready peripheral model using the configured execution backend."""
        if self.config.backend == "harness":
            result = run_target_model_pipeline(
                target_mcu=target_mcu,
                target_peripheral=target_peripheral,
                data_dir=data_dir,
                output_dir=output_dir,
            )
            return _with_execution_metadata(result, self.config, mode="harness")

        inputs = resolve_fetched_input_bundle(
            target_mcu=target_mcu,
            target_peripheral=target_peripheral,
            data_dir=data_dir,
        )
        if not inputs.svd_path and not inputs.header_path:
            raise ValueError(
                f"No SVD or header input found for {target_mcu} / {target_peripheral}. "
                f"Run fetch-data first or provide fetched register sources under {data_dir}."
            )
        if not inputs.driver_paths:
            raise ValueError(
                f"No driver sources found for {target_mcu} / {target_peripheral}. "
                f"Run fetch-data first or provide fetched drivers under {data_dir}."
            )
        orchestrator = AutoEmuOrchestrator(
            backend=self.config.backend,
            model=self.config.model,
            max_budget_usd=self.config.max_budget_usd,
        )
        result = asyncio.run(
            orchestrator.model_peripheral(
                ModelingTask(
                    peripheral_name=target_peripheral,
                    mcu_family=infer_stm32_mcu_family(target_mcu),
                    svd_path=inputs.svd_path,
                    header_path=inputs.header_path,
                    driver_paths=list(inputs.driver_paths),
                    output_dir=str(output_dir),
                )
            )
        )
        if result.error:
            raise RuntimeError(result.error)

        payload = _collect_generated_bundle(
            target_mcu=target_mcu,
            target_peripheral=target_peripheral,
            data_dir=data_dir,
            output_dir=output_dir,
        )
        payload["agent_messages"] = result.agent_messages
        return _with_execution_metadata(payload, self.config, mode="agent")


def _collect_generated_bundle(
    *,
    target_mcu: str,
    target_peripheral: str,
    data_dir: str | Path,
    output_dir: str | Path,
) -> dict[str, Any]:
    output_path = Path(output_dir)
    snake = _snake(target_peripheral)
    file_map = {
        "registers_json": output_path / f"{snake}_registers.json",
        "state_machine_json": output_path / f"{snake}_state_machine.json",
        "interrupt_model_json": output_path / f"{snake}_interrupt_model.json",
        "dependency_graph_json": output_path / f"{snake}_dependencies.json",
        "peripheral_json": output_path / f"{snake}_peripheral.json",
        "validation_json": output_path / f"{snake}_validation.json",
    }
    missing = [
        name
        for name, path in file_map.items()
        if not path.exists()
    ]
    if missing:
        raise RuntimeError(
            "Agent run completed without the expected generated files: "
            + ", ".join(sorted(missing))
        )

    inputs = resolve_fetched_input_bundle(
        target_mcu=target_mcu,
        target_peripheral=target_peripheral,
        data_dir=data_dir,
    )
    validation_report = json.loads(file_map["validation_json"].read_text(encoding="utf-8"))
    generated_files = [
        str(path)
        for path in sorted(output_path.rglob("*"))
        if path.is_file()
    ]

    return {
        "peripheral_name": target_peripheral,
        "mcu_family": infer_stm32_mcu_family(target_mcu),
        "driver_files": list(inputs.driver_paths),
        "documentation_files": list(inputs.documentation_paths),
        "registers_json": str(file_map["registers_json"]),
        "state_machine_json": str(file_map["state_machine_json"]),
        "interrupt_model_json": str(file_map["interrupt_model_json"]),
        "dependency_graph_json": str(file_map["dependency_graph_json"]),
        "peripheral_json": str(file_map["peripheral_json"]),
        "generated_files": generated_files,
        "validation_report": validation_report,
        "validation_json": str(file_map["validation_json"]),
        "target_mcu": target_mcu,
        "target_peripheral": target_peripheral,
        "data_manifest": inputs.manifest_path,
    }


def _load_fetch_manifest(
    *,
    target_mcu: str,
    target_peripheral: str,
    output_dir: str | Path,
) -> dict[str, Any]:
    bundle = resolve_fetched_input_bundle(
        target_mcu=target_mcu,
        target_peripheral=target_peripheral,
        data_dir=output_dir,
    )
    manifest_path = Path(bundle.manifest_path)
    if not manifest_path.exists():
        raise RuntimeError(f"Agent run completed without a manifest at {manifest_path}.")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _with_execution_metadata(
    payload: dict[str, Any],
    config: AgentRuntimeConfig,
    *,
    mode: str,
) -> dict[str, Any]:
    result = dict(payload)
    result["execution_backend"] = config.backend
    result["execution_mode"] = mode
    if config.model:
        result["execution_model"] = config.model
    return result


def _snake(name: str) -> str:
    normalized = "".join(ch.lower() if ch.isalnum() else "_" for ch in name)
    while "__" in normalized:
        normalized = normalized.replace("__", "_")
    return normalized.strip("_")


__all__ = [
    "AgentRuntimeConfig",
    "AutoEmuAgentRuntime",
    "SUPPORTED_AGENT_BACKENDS",
]
