"""Agent orchestrator using claude-agent-sdk.

This is the core of AutoEmu's LLM-driven modeling pipeline. It creates
a Claude agent with MCP tools for parsing, analysis, generation, and
validation, then drives the modeling workflow through structured prompts.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    create_sdk_mcp_server,
    query,
)

from autoemu.agent.prompts import (
    SYSTEM_PROMPT,
    REGISTER_EXTRACTION_PROMPT,
    BEHAVIOR_INFERENCE_PROMPT,
    INTERRUPT_ANALYSIS_PROMPT,
    DEPENDENCY_ANALYSIS_PROMPT,
    QEMU_GENERATION_PROMPT,
)
from autoemu.agent.tools import ALL_TOOLS, TOOL_NAMES


@dataclass
class ModelingTask:
    """A single peripheral modeling task."""

    peripheral_name: str
    mcu_family: str = "STM32F4"
    svd_path: str = ""
    header_path: str = ""
    driver_paths: list[str] = field(default_factory=list)
    output_dir: str = "output"
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


def _build_extraction_prompt(task: ModelingTask) -> str:
    """Build the prompt for register extraction phase."""
    parts = [REGISTER_EXTRACTION_PROMPT.format(peripheral_name=task.peripheral_name)]

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
    """Build the prompt for driver analysis phase."""
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
        "\nAfter analysis, use build_state_machine to create the "
        "peripheral's state machine model."
    )
    return "\n".join(parts)


def _build_interrupt_prompt(task: ModelingTask) -> str:
    """Build the prompt for interrupt analysis phase."""
    prompt = INTERRUPT_ANALYSIS_PROMPT.format(peripheral_name=task.peripheral_name)
    prompt += (
        "\n\nUse build_interrupt_model to create the interrupt model. "
        "Include all IRQ lines, status flags, enable bits, and event mappings."
    )
    return prompt


def _build_dependency_prompt(task: ModelingTask) -> str:
    """Build the prompt for dependency analysis phase."""
    prompt = DEPENDENCY_ANALYSIS_PROMPT.format(peripheral_name=task.peripheral_name)
    prompt += (
        f"\n\nUse build_dependency_graph to create the dependency graph "
        f"for {task.mcu_family}. Include DMA, clock, trigger, and GPIO dependencies."
    )
    return prompt


def _build_generation_prompt(task: ModelingTask) -> str:
    """Build the prompt for code generation phase."""
    prompt = QEMU_GENERATION_PROMPT.format(peripheral_name=task.peripheral_name)
    prompt += (
        f"\n\nUse generate_qemu_peripheral with output_dir='{task.output_dir}' "
        f"to generate the QEMU C code. Then use generate_test_harness to "
        f"create validation tests."
    )
    return prompt


def _build_validation_prompt(task: ModelingTask) -> str:
    """Build the prompt for validation phase."""
    return (
        f"Validate the generated peripheral model for '{task.peripheral_name}':\n"
        f"1. Use validate_register_model to check register consistency\n"
        f"2. Use validate_behavior to check against driver patterns\n"
        f"3. Review the generated QEMU code for correctness\n"
        f"4. Report any issues found and suggest fixes\n"
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
    """Orchestrates the LLM-driven peripheral modeling pipeline."""

    def __init__(
        self,
        model: str | None = None,
        max_budget_usd: float = 5.0,
        verbose: bool = False,
    ):
        self.model = model
        self.max_budget_usd = max_budget_usd
        self.verbose = verbose
        self._mcp_server = create_sdk_mcp_server(
            "autoemu",
            version="0.1.0",
            tools=ALL_TOOLS,
        )

    def _build_options(self, cwd: str | None = None) -> ClaudeAgentOptions:
        opts = ClaudeAgentOptions(
            system_prompt=SYSTEM_PROMPT,
            mcp_servers={"autoemu": self._mcp_server},
            allowed_tools=TOOL_NAMES,
            permission_mode="bypassPermissions",
            max_budget_usd=self.max_budget_usd,
        )
        if self.model:
            opts.model = self.model
        if cwd:
            opts.cwd = cwd
        return opts

    async def model_peripheral(
        self,
        task: ModelingTask,
        on_message: Any | None = None,
    ) -> ModelingResult:
        """Run the full modeling pipeline for a peripheral.

        Args:
            task: The modeling task specification.
            on_message: Optional callback(phase, text) for progress updates.

        Returns:
            ModelingResult with generated files and validation results.
        """
        result = ModelingResult(peripheral_name=task.peripheral_name)
        options = self._build_options(cwd=task.output_dir)
        total_cost = 0.0

        try:
            for phase in task.phases:
                if self.verbose and on_message:
                    on_message(phase, f"Starting phase: {phase}")

                builder = _PHASE_PROMPT_BUILDERS.get(phase)
                if not builder:
                    continue

                prompt = builder(task)
                phase_text: list[str] = []

                async for msg in query(prompt=prompt, options=options):
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock):
                                phase_text.append(block.text)
                                if on_message:
                                    on_message(phase, block.text)
                            elif isinstance(block, ToolUseBlock):
                                if self.verbose and on_message:
                                    on_message(
                                        phase,
                                        f"[tool] {block.name}({json.dumps(block.input)[:200]}...)"
                                    )
                    elif isinstance(msg, ResultMessage):
                        if msg.total_cost_usd:
                            total_cost += msg.total_cost_usd
                        if msg.is_error:
                            result.error = msg.result or "Unknown error"
                            result.total_cost_usd = total_cost
                            return result

                result.phases_completed.append(phase)
                result.agent_messages.extend(phase_text)

        except Exception as e:
            result.error = str(e)

        # Collect generated files
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
        options = self._build_options(cwd=task.output_dir)

        for phase in task.phases:
            yield (phase, f"=== Phase: {phase} ===")

            builder = _PHASE_PROMPT_BUILDERS.get(phase)
            if not builder:
                continue

            prompt = builder(task)

            async for msg in query(prompt=prompt, options=options):
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            yield (phase, block.text)
                        elif isinstance(block, ToolUseBlock):
                            yield (phase, f"[tool:{block.name}]")
                elif isinstance(msg, ResultMessage):
                    cost = msg.total_cost_usd or 0
                    yield (phase, f"[phase complete, cost: ${cost:.4f}]")
                    if msg.is_error:
                        yield (phase, f"[error] {msg.result}")
                        return

    async def single_query(self, prompt: str) -> str:
        """Run a single free-form query against the agent."""
        options = self._build_options()
        result_parts: list[str] = []

        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        result_parts.append(block.text)
            elif isinstance(msg, ResultMessage):
                break

        return "\n".join(result_parts)

    async def batch_model(
        self,
        tasks: list[ModelingTask],
        max_concurrent: int = 2,
    ) -> list[ModelingResult]:
        """Model multiple peripherals, with limited concurrency."""
        semaphore = asyncio.Semaphore(max_concurrent)
        results: list[ModelingResult] = []

        async def _run(task: ModelingTask) -> ModelingResult:
            async with semaphore:
                return await self.model_peripheral(task)

        coros = [_run(t) for t in tasks]
        results = await asyncio.gather(*coros)
        return list(results)
