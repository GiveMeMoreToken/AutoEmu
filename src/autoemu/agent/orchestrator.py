"""Prompt-driven agent orchestrator for the AutoEmu modeling pipeline.

Uses the abstract :class:`AgentBackend` interface so that the pipeline
works identically regardless of whether an SDK or direct API backend is used.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from autoemu.agent.backend import AgentBackend
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
from autoemu.modeling_utils import snake_case as _snake


def _is_transient_backend_error(text: str) -> bool:
    """Return True if *text* looks like a recoverable transport/subprocess failure."""
    msg = text.lower()
    return any(p in msg for p in (
        "failed reading from stdio transport",
        "stdio",
        "transport",
        "broken pipe",
        "connection reset",
    ))


# Tools available during the peripheral modeling phases.
# fetch_data is intentionally excluded: each phase receives the data that was
# already fetched in the dedicated fetch step.  Allowing fetch_data here caused
# the agent to make redundant (and wrong-MCU-family) fetch calls during the
# interrupt-analysis and dependency-graph phases.
_MODEL_TOOLS = [t for t in ALL_TOOLS if t.name != "fetch_data"]


def _write_phase_log(
    output_dir: str,
    phase: str,
    attempt: int,
    prompt: str,
    system_prompt: str,
    events: list[dict[str, Any]],
    error: str = "",
) -> Path:
    """Persist the full agent conversation for a single phase attempt to disk.

    Returns the path to the written log file.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    log_dir = Path(output_dir) / "agent_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{ts}_{phase}_attempt{attempt}.txt"

    lines: list[str] = [
        f"=== AutoEmu Agent Conversation Log ===",
        f"Timestamp: {ts} UTC",
        f"Phase: {phase}",
        f"Attempt: {attempt}",
        "",
        "=== SYSTEM PROMPT ===",
        system_prompt,
        "",
        "=== USER PROMPT ===",
        prompt,
        "",
        "=== EVENTS ===",
    ]

    for ev in events:
        etype = ev.get("type", "unknown")
        detail = ev.get("detail", "")
        lines.append(f"[{etype}] {detail}")

    if error:
        lines.extend(["", f"=== ERROR ===", error])

    lines.append("")
    log_path.write_text("\n".join(lines), encoding="utf-8")
    return log_path


def _mcu_slug(task: "ModelingTask") -> str:
    """Return a lowercase filesystem-safe MCU name for use in generated filenames.

    Prefers ``task.target_mcu`` (exact user input) over the MCU family string,
    so that e.g. "Hikey960" → "hikey960" rather than deriving "kirin960" from
    the family.  Falls back to the family when target_mcu is not set.
    """
    from autoemu.modeling_utils import normalize_name
    raw = task.target_mcu or task.mcu_family
    return normalize_name(raw)


@dataclass
class ModelingTask:
    """A single peripheral modeling task."""

    peripheral_name: str
    mcu_family: str = "STM32F4"
    target_mcu: str = ""         # exact MCU name as typed by the user (e.g. "Hikey960")
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
    if task.data_dir:
        parts.append(
            f"\nFetched input data is located in: {task.data_dir}\n"
            f"Driver sources are in: {task.data_dir}/driver/\n"
            f"Use list_files and read_file to enumerate available files."
        )
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
    if task.data_dir:
        prompt += (
            f"\n\nFetched input data is located in: {task.data_dir}\n"
            f"Use list_files and read_file to find SVD, header, and driver files there."
        )
    prompt += (
        "\n\nPrefer infer_interrupt_model after driver analysis and register extraction. "
        "Use build_interrupt_model only if manual refinement is necessary. "
        "Include all IRQ lines, status flags, enable bits, and event mappings."
    )
    return prompt


def _build_dependency_prompt(task: ModelingTask) -> str:
    prompt = DEPENDENCY_ANALYSIS_PROMPT.format(peripheral_name=task.peripheral_name)
    if task.data_dir:
        prompt += (
            f"\n\nFetched input data is located in: {task.data_dir}\n"
            f"Use list_files and read_file to find available documentation and source files."
        )
    prompt += (
        f"\n\nPrefer infer_dependency_graph after driver analysis, documentation review, "
        f"and interrupt modeling. Use build_dependency_graph only for manual refinement. "
        f"Include DMA, clock, trigger, EXTI, and GPIO dependencies for {task.mcu_family}."
    )
    return prompt


def _build_generation_prompt(task: ModelingTask) -> str:
    prompt = QEMU_GENERATION_PROMPT.format(peripheral_name=task.peripheral_name)
    if task.data_dir:
        prompt += (
            f"\n\nFetched input data is located in: {task.data_dir}\n"
            f"Use list_files and read_file to inspect available SVD, header, and driver files."
        )
    prompt += (
        f"\n\nUse ONLY the available tools to generate code. Do NOT write code "
        f"directly in your response — tool calls are faster and avoid timeouts. "
        f"\n\nStep 1: Read the register model JSON at "
        f"'{task.output_dir}/{_snake(task.peripheral_name)}_register_model.json' (file path) "
        f"to get the register block. "
        f"\nStep 2: Call generate_model_bundle with output_dir='{task.output_dir}', "
        f"passing the JSON FILE PATH as register_blocks_json (do not inline the JSON). "
        f"If state_machine_json or interrupt_model_json files exist in {task.output_dir}, "
        f"pass their file paths as well. "
        f"\nStep 3: If generate_model_bundle is unavailable, use generate_qemu_peripheral "
        f"with the peripheral JSON it produced. "
        f"\n\nGenerate ONLY ONE .c and ONE .h file. Name them "
        f"'{_mcu_slug(task)}_{task.peripheral_name.lower()}.c' and "
        f"'{_mcu_slug(task)}_{task.peripheral_name.lower()}.h'. "
        f"The header must use a local include path "
        f"(e.g. #include \"{_mcu_slug(task)}_{task.peripheral_name.lower()}.h\"), "
        f"not a system path."
    )
    return prompt


def _build_validation_prompt(task: ModelingTask) -> str:
    data_hint = ""
    if task.data_dir:
        data_hint = (
            f"\nFetched source files are in: {task.data_dir}\n"
            f"Generated output is in: {task.output_dir}\n"
        )
    return (
        f"Validate the generated peripheral model for '{task.peripheral_name}':\n"
        f"{data_hint}"
        f"1. Prefer the validation report produced by generate_model_bundle\n"
        f"2. Use validate_register_model to check register consistency\n"
        f"3. Use validate_behavior to check against driver patterns\n"
        f"4. Review the generated QEMU code in '{task.output_dir}/' for correctness\n"
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
        backend: Agent backend name, or an :class:`AgentBackend` instance directly.
        model: Model identifier override.
        max_budget_usd: Spending cap for the run.
        verbose: Whether to emit verbose progress messages.
    """

    def __init__(
        self,
        backend: str | AgentBackend = "claude-sdk",
        model: str | None = None,
        max_budget_usd: float = 5.0,
        verbose: bool = False,
        backend_kwargs: dict[str, Any] | None = None,
    ):
        if isinstance(backend, str):
            self._backend = create_backend(backend, **(backend_kwargs or {}))
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
        phase_delay_seconds: float = 0.0,
        skip_on_failure: bool = True,
    ) -> ModelingResult:
        """Run the full modeling pipeline for a peripheral.

        *on_event(event_type, phase, detail)* is called for every agent
        event so the caller can render progress.

        If a single phase fails with a transient backend error (e.g. stdio
        transport crash), that phase is retried up to 2 times without
        restarting earlier phases.  After retries are exhausted the phase is
        skipped when *skip_on_failure* is True so that later phases can still
        run using the deterministic harness output already on disk.
        """
        result = ModelingResult(peripheral_name=task.peripheral_name)
        total_cost = 0.0
        skipped_phases: list[str] = []

        def _emit(etype: str, phase: str, detail: str) -> None:
            if on_event:
                on_event(etype, phase, detail)

        try:
            for phase in task.phases:
                builder = _PHASE_PROMPT_BUILDERS.get(phase)
                if not builder:
                    continue

                # Small delay between phases to avoid bursting the API and
                # triggering rate limits / concurrent-session caps.
                if phase_delay_seconds > 0:
                    await asyncio.sleep(phase_delay_seconds)

                phase_success = False
                for attempt in range(3):
                    prompt = builder(task)
                    phase_text: list[str] = []
                    event_log: list[dict[str, Any]] = []
                    sys_prompt = build_system_prompt(mode="model", cwd=task.output_dir)

                    _emit("phase_start", phase, prompt[:300])

                    async for event in self._backend.run(
                        prompt,
                        system_prompt=sys_prompt,
                        tools=_MODEL_TOOLS,
                        model=self.model,
                        max_budget_usd=self.max_budget_usd,
                        cwd=task.output_dir,
                    ):
                        if event.type == "text":
                            phase_text.append(event.text)
                            event_log.append({"type": "text", "detail": event.text[:500]})
                            if on_message:
                                on_message(phase, event.text)
                            _emit("text", phase, event.text)
                        elif event.type == "tool_call":
                            detail = f"{event.tool_name}({event.tool_input[:200]})"
                            event_log.append({"type": "tool_call", "detail": detail})
                            _emit("tool_call", phase, detail)
                        elif event.type == "error":
                            total_cost += event.cost_usd
                            event_log.append({"type": "error", "detail": event.text})
                            log_path = _write_phase_log(
                                task.output_dir, phase, attempt, prompt, sys_prompt,
                                event_log, error=event.text,
                            )
                            if _is_transient_backend_error(event.text) and attempt < 2:
                                _emit("text", phase,
                                      f"Transient transport error, retrying phase "
                                      f"({attempt + 1}/3)...")
                                # Longer backoff for stdio transport crashes
                                await asyncio.sleep(5.0 * (2 ** attempt))
                                break
                            result.error = event.text
                            result.total_cost_usd = total_cost
                            _emit("error", phase, event.text)
                            if skip_on_failure:
                                skipped_phases.append(phase)
                                _emit("text", phase,
                                      f"Skipping phase {phase} after failure; "
                                      f"continuing with remaining phases.")
                                result.error = ""
                                break
                            return result
                        elif event.type == "complete":
                            total_cost += event.cost_usd
                            event_log.append({"type": "complete", "detail": f"${event.cost_usd:.4f}"})
                            _emit("phase_done", phase,
                                  f"${event.cost_usd:.4f}")
                    else:
                        # Phase completed normally (no break from error retry)
                        result.phases_completed.append(phase)
                        result.agent_messages.extend(phase_text)
                        phase_success = True
                        _write_phase_log(
                            task.output_dir, phase, attempt, prompt, sys_prompt,
                            event_log,
                        )
                        break

                if not phase_success and skip_on_failure and phase in skipped_phases:
                    # Continue to next phase after logging the skip
                    continue

                if not phase_success:
                    if not result.error:
                        result.error = f"Phase {phase} failed after 3 attempts"
                    result.total_cost_usd = total_cost
                    return result

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
            event_log: list[dict[str, Any]] = []
            sys_prompt = build_system_prompt(mode="fetch", cwd=task.output_dir)
            _emit("phase_start", prompt[:300])

            async for event in self._backend.run(
                prompt,
                system_prompt=sys_prompt,
                tools=ALL_TOOLS,
                model=self.model,
                max_budget_usd=self.max_budget_usd,
                cwd=task.output_dir,
            ):
                if event.type == "text":
                    phase_text.append(event.text)
                    event_log.append({"type": "text", "detail": event.text[:500]})
                    if on_message:
                        on_message("fetch", event.text)
                    _emit("text", event.text)
                elif event.type == "tool_call":
                    detail = f"{event.tool_name}({event.tool_input[:200]})"
                    event_log.append({"type": "tool_call", "detail": detail})
                    _emit("tool_call", detail)
                elif event.type == "error":
                    result.error = event.text
                    total_cost += event.cost_usd
                    event_log.append({"type": "error", "detail": event.text})
                    _write_phase_log(
                        task.output_dir, "fetch", 0, prompt, sys_prompt,
                        event_log, error=event.text,
                    )
                    result.total_cost_usd = total_cost
                    _emit("error", event.text)
                    return result
                elif event.type == "complete":
                    total_cost += event.cost_usd
                    event_log.append({"type": "complete", "detail": f"${event.cost_usd:.4f}"})
                    _emit("phase_done", f"${event.cost_usd:.4f}")

            result.agent_messages.extend(phase_text)
            _write_phase_log(
                task.output_dir, "fetch", 0, prompt, sys_prompt, event_log,
            )
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
