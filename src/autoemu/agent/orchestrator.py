"""Prompt-driven agent orchestrator for the AutoEmu modeling pipeline.

Uses the abstract :class:`AgentBackend` interface so that the pipeline
works identically regardless of whether claude-agent-sdk or openai-agents
is used underneath.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from autoemu.agent.backend import AgentBackend, AgentEvent
from autoemu.agent.backends import create_backend
from autoemu.agent.prompts import (
    REGISTER_EXTRACTION_PROMPT,
    BEHAVIOR_INFERENCE_PROMPT,
    INTERRUPT_ANALYSIS_PROMPT,
    DEPENDENCY_ANALYSIS_PROMPT,
    QEMU_GENERATION_PROMPT,
    build_system_prompt,
)
from autoemu.agent.tools import ALL_TOOLS

# Tools available during the peripheral modeling phases.
# fetch_data is intentionally excluded: each phase receives the data that was
# already fetched in the dedicated fetch step.  Allowing fetch_data here caused
# the agent to make redundant (and wrong-MCU-family) fetch calls during the
# interrupt-analysis and dependency-graph phases.
_MODEL_TOOLS = [t for t in ALL_TOOLS if t.name != "fetch_data"]


@dataclass
class ModelingTask:
    """A single peripheral modeling task."""

    peripheral_name: str
    mcu_family: str = "STM32F4"
    svd_path: str = ""
    header_path: str = ""
    driver_paths: list[str] = field(default_factory=list)
    output_dir: str = "output"
    data_dir: str = ""          # directory where fetched input data lives
    phases: list[str] = field(default_factory=lambda: [
        "extract", "analyze", "infer", "connect", "generate", "validate"
    ])


@dataclass
class ModelingResult:
    """Result of a modeling run."""

    peripheral_name: str
    success: bool = False
    phases_completed: list[str] = field(default_factory=list)
    generated_files: list[str] = field(default_factory=list)
    validation_issues: list[dict[str, str]] = field(default_factory=list)
    total_cost_usd: float = 0.0
    error: str = ""
    agent_messages: list[str] = field(default_factory=list)


@dataclass
class FetchTask:
    """Source-data fetch task."""

    target_mcu: str
    target_peripheral: str
    output_dir: str = "data/stm32"
    refresh: bool = False


@dataclass
class FetchResult:
    """Result of a source-data fetch run."""

    target_mcu: str
    target_peripheral: str
    success: bool = False
    fetched_files: list[str] = field(default_factory=list)
    manifest_files: list[str] = field(default_factory=list)
    total_cost_usd: float = 0.0
    error: str = ""
    agent_messages: list[str] = field(default_factory=list)


def _build_extraction_prompt(task: ModelingTask) -> str:
    parts = [REGISTER_EXTRACTION_PROMPT.format(peripheral_name=task.peripheral_name)]
    if task.data_dir:
        parts.append(
            f"\nFetched input data is located in: {task.data_dir}\n"
            f"Search subdirectories: {task.data_dir}/svd/, {task.data_dir}/header/, "
            f"{task.data_dir}/driver/, {task.data_dir}/docs/\n"
            f"Use list_files and read_file to find and read available files before parsing."
        )
    if task.svd_path:
        parts.append(
            f"\nSVD file available at: {task.svd_path}\n"
            f"Use the parse_svd tool to extract register maps."
        )
    if task.header_path:
        parts.append(
            f"\nCMSIS header available at: {task.header_path}\n"
            f"Use the parse_header tool to extract register structures."
        )
    if not task.svd_path and not task.header_path:
        parts.append(
            "\nNo SVD or header files provided. Use your knowledge of "
            f"{task.mcu_family} {task.peripheral_name} to build the register model."
        )
    parts.append(
        f"\nAfter extraction, use build_peripheral_model to create the model "
        f"with peripheral_type appropriate for {task.peripheral_name}."
    )
    return "\n".join(parts)


def _build_analysis_prompt(task: ModelingTask) -> str:
    parts = [BEHAVIOR_INFERENCE_PROMPT.format(peripheral_name=task.peripheral_name)]
    if task.driver_paths:
        for dp in task.driver_paths:
            parts.append(f"\nDriver source file: {dp}")
        parts.append(
            "\nUse analyze_driver to extract register access patterns, "
            "ISR logic, and init sequences."
        )
    else:
        parts.append(
            f"\nNo driver files provided. Use your knowledge of STM32 HAL "
            f"driver patterns for {task.peripheral_name} to infer behavior."
        )
    parts.append(
        "\nAfter analysis, prefer infer_state_machine to derive the "
        "peripheral's state machine model from the driver analysis. "
        "Use build_state_machine only if manual refinement is necessary."
    )
    return "\n".join(parts)


def _build_interrupt_prompt(task: ModelingTask) -> str:
    prompt = INTERRUPT_ANALYSIS_PROMPT.format(peripheral_name=task.peripheral_name)
    prompt += (
        "\n\nPrefer infer_interrupt_model after driver analysis and register extraction. "
        "Use build_interrupt_model only if manual refinement is necessary. "
        "Include all IRQ lines, status flags, enable bits, and event mappings."
    )
    return prompt


def _build_dependency_prompt(task: ModelingTask) -> str:
    prompt = DEPENDENCY_ANALYSIS_PROMPT.format(peripheral_name=task.peripheral_name)
    prompt += (
        f"\n\nPrefer infer_dependency_graph after driver analysis, documentation review, "
        f"and interrupt modeling. Use build_dependency_graph only for manual refinement. "
        f"Include DMA, clock, trigger, EXTI, and GPIO dependencies for {task.mcu_family}."
    )
    return prompt


def _build_generation_prompt(task: ModelingTask) -> str:
    prompt = QEMU_GENERATION_PROMPT.format(peripheral_name=task.peripheral_name)
    prompt += (
        f"\n\nIf you have raw SVD/header/driver inputs and need the whole flow from scratch, "
        f"prefer run_model_pipeline with output_dir='{task.output_dir}'. "
        f"\n\nPrefer generate_model_bundle with output_dir='{task.output_dir}' "
        f"to assemble the peripheral, emit QEMU artifacts, and write validation reports. "
        f"Use generate_qemu_peripheral and generate_test_harness only when manual refinement "
        f"is required."
    )
    return prompt


def _build_validation_prompt(task: ModelingTask) -> str:
    return (
        f"Validate the generated peripheral model for '{task.peripheral_name}':\n"
        f"1. Prefer the validation report produced by generate_model_bundle\n"
        f"2. Use validate_register_model to check register consistency\n"
        f"3. Use validate_behavior to check against driver patterns\n"
        f"4. Review the generated QEMU code for correctness\n"
        f"5. Report any issues found and suggest fixes\n"
    )


def _build_fetch_prompt(task: FetchTask) -> str:
    return (
        "Collect hardware documentation and driver source data for the requested MCU and peripheral.\n"
        f"Target MCU: {task.target_mcu}\n"
        f"Target peripheral: {task.target_peripheral}\n"
        f"Output directory: {task.output_dir}\n"
        f"Refresh existing files: {task.refresh}\n\n"
        "Use fetch_data to perform the acquisition. "
        f"Pass target_mcu='{task.target_mcu}', "
        f"target_peripheral='{task.target_peripheral}', "
        f"output_dir='{task.output_dir}', refresh={str(task.refresh)}.\n"
        "After the tool runs, list the files that were successfully downloaded. "
        "Do not fabricate file paths or manifest entries — only report what the tool actually returned."
    )


_PHASE_PROMPT_BUILDERS = {
    "extract": _build_extraction_prompt,
    "analyze": _build_analysis_prompt,
    "infer": _build_interrupt_prompt,
    "connect": _build_dependency_prompt,
    "generate": _build_generation_prompt,
    "validate": _build_validation_prompt,
}


class AutoEmuOrchestrator:
    """Orchestrates the LLM-driven peripheral modeling pipeline.

    Args:
        backend: Backend name (``"claude"`` or ``"openai"``), or an
                 :class:`AgentBackend` instance directly.
        model: Model identifier override.
        max_budget_usd: Spending cap for the run.
        verbose: Whether to emit verbose progress messages.
    """

    def __init__(
        self,
        backend: str | AgentBackend = "claude",
        model: str | None = None,
        max_budget_usd: float = 5.0,
        verbose: bool = False,
    ):
        if isinstance(backend, str):
            self._backend = create_backend(backend)
        else:
            self._backend = backend
        self.model = model
        self.max_budget_usd = max_budget_usd
        self.verbose = verbose

    @property
    def backend(self) -> AgentBackend:
        return self._backend

    async def model_peripheral(
        self,
        task: ModelingTask,
        on_message: Any | None = None,
        on_event: Any | None = None,
    ) -> ModelingResult:
        """Run the full modeling pipeline for a peripheral.

        *on_event(event_type, phase, detail)* is called for every agent
        event so the caller can render progress.
        """
        result = ModelingResult(peripheral_name=task.peripheral_name)
        total_cost = 0.0

        def _emit(etype: str, phase: str, detail: str) -> None:
            if on_event:
                on_event(etype, phase, detail)

        try:
            for phase in task.phases:
                builder = _PHASE_PROMPT_BUILDERS.get(phase)
                if not builder:
                    continue

                prompt = builder(task)
                phase_text: list[str] = []

                _emit("phase_start", phase, prompt[:300])

                async for event in self._backend.run(
                    prompt,
                    system_prompt=build_system_prompt(mode="model", cwd=task.output_dir),
                    tools=_MODEL_TOOLS,
                    model=self.model,
                    max_budget_usd=self.max_budget_usd,
                    cwd=task.output_dir,
                ):
                    if event.type == "text":
                        phase_text.append(event.text)
                        if on_message:
                            on_message(phase, event.text)
                        _emit("text", phase, event.text)
                    elif event.type == "tool_call":
                        _emit("tool_call", phase,
                              f"{event.tool_name}({event.tool_input[:200]})")
                    elif event.type == "error":
                        result.error = event.text
                        total_cost += event.cost_usd
                        result.total_cost_usd = total_cost
                        _emit("error", phase, event.text)
                        return result
                    elif event.type == "complete":
                        total_cost += event.cost_usd
                        _emit("phase_done", phase,
                              f"${event.cost_usd:.4f}")

                result.phases_completed.append(phase)
                result.agent_messages.extend(phase_text)

        except Exception as e:
            result.error = str(e)

        output_path = Path(task.output_dir)
        if output_path.exists():
            result.generated_files = [
                str(f.relative_to(output_path))
                for f in output_path.rglob("*")
                if f.is_file()
            ]

        result.success = not result.error
        result.total_cost_usd = total_cost
        return result

    async def model_peripheral_interactive(
        self,
        task: ModelingTask,
    ) -> AsyncIterator[tuple[str, str]]:
        """Run modeling with interactive streaming output.

        Yields (phase, text) tuples as the agent works.
        """
        for phase in task.phases:
            yield (phase, f"=== Phase: {phase} ===")

            builder = _PHASE_PROMPT_BUILDERS.get(phase)
            if not builder:
                continue

            prompt = builder(task)

            async for event in self._backend.run(
                prompt,
                system_prompt=build_system_prompt(mode="model", cwd=task.output_dir),
                tools=_MODEL_TOOLS,
                model=self.model,
                max_budget_usd=self.max_budget_usd,
                cwd=task.output_dir,
            ):
                if event.type == "text":
                    yield (phase, event.text)
                elif event.type == "tool_call":
                    yield (phase, f"[tool:{event.tool_name}]")
                elif event.type == "complete":
                    yield (phase, f"[phase complete, cost: ${event.cost_usd:.4f}]")
                elif event.type == "error":
                    yield (phase, f"[error] {event.text}")
                    return

    async def single_query(self, prompt: str) -> str:
        """Run a single free-form query against the agent."""
        return await self._backend.run_to_text(
            prompt,
            system_prompt=build_system_prompt(mode="model"),
            tools=ALL_TOOLS,
            model=self.model,
            max_budget_usd=self.max_budget_usd,
        )

    async def fetch_input_data(
        self,
        task: FetchTask,
        on_message: Any | None = None,
        on_event: Any | None = None,
    ) -> FetchResult:
        """Run the input-data collection flow through the agent backend."""
        result = FetchResult(
            target_mcu=task.target_mcu,
            target_peripheral=task.target_peripheral,
        )
        total_cost = 0.0

        def _emit(etype: str, detail: str) -> None:
            if on_event:
                on_event(etype, "fetch", detail)

        try:
            prompt = _build_fetch_prompt(task)
            phase_text: list[str] = []
            _emit("phase_start", prompt[:300])

            async for event in self._backend.run(
                prompt,
                system_prompt=build_system_prompt(mode="fetch", cwd=task.output_dir),
                tools=ALL_TOOLS,
                model=self.model,
                max_budget_usd=self.max_budget_usd,
                cwd=task.output_dir,
            ):
                if event.type == "text":
                    phase_text.append(event.text)
                    if on_message:
                        on_message("fetch", event.text)
                    _emit("text", event.text)
                elif event.type == "tool_call":
                    _emit("tool_call",
                          f"{event.tool_name}({event.tool_input[:200]})")
                elif event.type == "error":
                    result.error = event.text
                    total_cost += event.cost_usd
                    result.total_cost_usd = total_cost
                    _emit("error", event.text)
                    return result
                elif event.type == "complete":
                    total_cost += event.cost_usd
                    _emit("phase_done", f"${event.cost_usd:.4f}")

            result.agent_messages.extend(phase_text)
        except Exception as e:
            result.error = str(e)

        output_path = Path(task.output_dir)
        if output_path.exists():
            result.fetched_files = [
                str(path.relative_to(output_path))
                for path in output_path.rglob("*")
                if path.is_file()
            ]
            manifests_dir = output_path / "manifests"
            if manifests_dir.exists():
                result.manifest_files = [
                    str(path.relative_to(output_path))
                    for path in manifests_dir.glob("*.json")
                    if path.is_file()
                ]

        result.success = not result.error
        result.total_cost_usd = total_cost
        return result

    async def batch_model(
        self,
        tasks: list[ModelingTask],
        max_concurrent: int = 2,
    ) -> list[ModelingResult]:
        """Model multiple peripherals, with limited concurrency."""
        semaphore = asyncio.Semaphore(max_concurrent)

        async def _run(task: ModelingTask) -> ModelingResult:
            async with semaphore:
                return await self.model_peripheral(task)

        coros = [_run(t) for t in tasks]
        results = await asyncio.gather(*coros)
        return list(results)
