"""Harness-first runtime for the public AutoEmu workflows."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from autoemu.agent.orchestrator import AutoEmuOrchestrator, FetchTask, ModelingTask
from autoemu.fetchers.generic import (
    GenericDataFetcher,
    infer_stm32_mcu_family,
    resolve_fetched_input_bundle,
)
from autoemu.modeling_utils import normalize_name as _snake
from autoemu.pipeline import run_target_model_pipeline
from autoemu.platforms import detect_platform
from autoemu.validators.compile_validator import validate_compile

logger = logging.getLogger(__name__)

SUPPORTED_AGENT_BACKENDS = {"harness", "claude", "openai"}


# ---------------------------------------------------------------------------
# Phase descriptors for the unified pipeline
# ---------------------------------------------------------------------------

PIPELINE_PHASES = [
    "Detecting platform",
    "Fetching input data",
    "Building QEMU peripheral model",
    "Validating generated code",
]


@dataclass
class PipelineProgress:
    """Tracks progress of the unified pipeline."""

    phase: int = 0
    total_phases: int = len(PIPELINE_PHASES)
    phase_name: str = ""
    detail: str = ""
    finished: bool = False
    error: str = ""
    # kind: "info" | "agent_thinking" | "agent_tool" | "agent_text" |
    #       "download" | "search" | "compile" | "warn" | "fail"
    kind: str = "info"


@dataclass
class PipelineResult:
    """Full result of a unified pipeline run."""

    target_mcu: str = ""
    target_peripheral: str = ""
    platform: str = ""
    fetch_result: dict[str, Any] = field(default_factory=dict)
    build_result: dict[str, Any] = field(default_factory=dict)
    validation_result: dict[str, Any] = field(default_factory=dict)
    generated_files: list[str] = field(default_factory=list)
    success: bool = False
    error: str = ""


CONFIG_FILENAME = ".autoemu.toml"


def _load_config_file() -> dict[str, Any]:
    """Load .autoemu.toml from the current directory (if it exists)."""
    config_path = Path(CONFIG_FILENAME)
    if not config_path.is_file():
        return {}
    try:
        import tomllib
        return tomllib.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("Failed to parse %s", CONFIG_FILENAME)
        return {}


@dataclass(frozen=True)
class AgentRuntimeConfig:
    """Execution settings for the public agent runtime.

    Resolution order: .autoemu.toml in CWD → environment variables → defaults.
    """

    backend: str = "harness"
    model: str | None = None
    max_budget_usd: float = 5.0
    anthropic_api_key: str = ""
    anthropic_base_url: str = ""
    openai_api_key: str = ""
    openai_base_url: str = ""

    @classmethod
    def load(cls) -> AgentRuntimeConfig:
        """Build config from .autoemu.toml (CWD) then environment variables."""
        file_cfg = _load_config_file()
        agent_cfg = file_cfg.get("agent", {})

        backend = (
            agent_cfg.get("backend")
            or os.getenv("AUTOEMU_AGENT_BACKEND")
            or "harness"
        ).strip().lower()
        if backend not in SUPPORTED_AGENT_BACKENDS:
            raise ValueError(
                "AUTOEMU_AGENT_BACKEND must be one of: "
                + ", ".join(sorted(SUPPORTED_AGENT_BACKENDS))
            )

        model = (
            agent_cfg.get("model")
            or os.getenv("AUTOEMU_AGENT_MODEL")
            or ""
        ).strip() or None

        budget_raw = (
            str(agent_cfg.get("max_budget_usd", ""))
            or os.getenv("AUTOEMU_AGENT_MAX_BUDGET_USD", "")
        ).strip()
        max_budget_usd = float(budget_raw) if budget_raw else 5.0

        anthropic_key = (
            agent_cfg.get("anthropic_api_key")
            or os.getenv("ANTHROPIC_API_KEY", "")
        )
        anthropic_base = (
            agent_cfg.get("anthropic_base_url")
            or os.getenv("ANTHROPIC_BASE_URL", "")
        )
        openai_key = (
            agent_cfg.get("openai_api_key")
            or os.getenv("OPENAI_API_KEY", "")
        )
        openai_base = (
            agent_cfg.get("openai_base_url")
            or os.getenv("OPENAI_BASE_URL", "")
        )

        # Inject into environment so SDKs pick them up
        if anthropic_key:
            os.environ.setdefault("ANTHROPIC_API_KEY", anthropic_key)
        if anthropic_base:
            os.environ.setdefault("ANTHROPIC_BASE_URL", anthropic_base)
        if openai_key:
            os.environ.setdefault("OPENAI_API_KEY", openai_key)
        if openai_base:
            os.environ.setdefault("OPENAI_BASE_URL", openai_base)

        return cls(
            backend=backend,
            model=model,
            max_budget_usd=max_budget_usd,
            anthropic_api_key=anthropic_key,
            anthropic_base_url=anthropic_base,
            openai_api_key=openai_key,
            openai_base_url=openai_base,
        )

    # Keep the old name as alias
    from_env = load


class AutoEmuAgentRuntime:
    """Unified runtime: fetch → build → validate in one call."""

    def __init__(self, config: AgentRuntimeConfig | None = None) -> None:
        self.config = config or AgentRuntimeConfig.from_env()

    # ------------------------------------------------------------------
    # Unified pipeline — the single public entry point
    # ------------------------------------------------------------------

    def run_pipeline(
        self,
        *,
        target_mcu: str,
        target_peripheral: str,
        on_progress: Callable[[PipelineProgress], None] | None = None,
    ) -> PipelineResult:
        """Run the full pipeline: detect → fetch → build → validate.

        Only *target_mcu* and *target_peripheral* are required; everything
        else (platform, directories, validation) is inferred automatically.
        """
        result = PipelineResult(
            target_mcu=target_mcu,
            target_peripheral=target_peripheral,
        )

        def _emit(phase: int, detail: str = "", kind: str = "info") -> None:
            if on_progress:
                on_progress(PipelineProgress(
                    phase=phase,
                    phase_name=PIPELINE_PHASES[phase - 1],
                    detail=detail,
                    kind=kind,
                ))

        try:
            # Phase 1 — detect platform
            from autoemu.platforms import analyze_target
            _emit(1, f"Analysing target: {target_mcu}")
            board = analyze_target(target_mcu)
            platform_name = board.platform
            result.platform = f"{board.vendor}/{board.family}" if board.vendor != "unknown" else platform_name
            mcu_slug = _snake(target_mcu)
            data_dir = f"data/{mcu_slug}"
            output_dir = "output"
            _emit(1, f"Vendor: {board.vendor}, arch: {board.arch}, family: {board.family}")

            # Phase 2 — fetch
            _emit(2, "Searching the web for input data ...", "search")
            fetch_result = self._do_fetch(
                target_mcu=target_mcu,
                target_peripheral=target_peripheral,
                platform_name=platform_name,
                output_dir=data_dir,
                on_progress=lambda msg, k="info": _emit(2, msg, k),
            )
            result.fetch_result = fetch_result
            _emit(2, f"Fetch complete ({self._count_fetched(fetch_result)} artifacts)")

            # Phase 3 — build
            _emit(3, "Running modeling pipeline ...", "agent_thinking")
            build_result = self._do_build(
                target_mcu=target_mcu,
                target_peripheral=target_peripheral,
                platform_name=platform_name,
                data_dir=data_dir,
                output_dir=output_dir,
                on_progress=lambda msg, k="info": _emit(3, msg, k),
            )
            result.build_result = build_result
            generated = build_result.get("generated_files", [])
            result.generated_files = generated
            _emit(3, f"Generated {len(generated)} file(s)", "agent_text")

            # Phase 4 — validate
            _emit(4, "Validating generated code ...", "compile")
            validation = self._do_validate(
                output_dir,
                on_progress=lambda msg, k="compile": _emit(4, msg, k),
            )
            result.validation_result = validation
            ok = validation.get("success")
            _emit(4, f"Validation: {'PASS' if ok else 'ISSUES FOUND'}", "info" if ok else "fail")

            result.success = True

        except Exception as exc:
            result.error = str(exc)
            logger.exception("Pipeline failed for %s / %s", target_mcu, target_peripheral)
            if on_progress:
                on_progress(PipelineProgress(
                    phase=0, phase_name="error", detail=str(exc),
                    finished=True, error=str(exc),
                ))

        if on_progress:
            on_progress(PipelineProgress(finished=True))

        return result

    # ------------------------------------------------------------------
    # Internal phase implementations
    # ------------------------------------------------------------------

    def _do_fetch(
        self,
        *,
        target_mcu: str,
        target_peripheral: str,
        platform_name: str,
        output_dir: str,
        on_progress: Callable[[str, str], None] | None = None,
    ) -> dict[str, Any]:
        _log = on_progress or (lambda msg, kind="info": None)

        # Use local web search first; optionally enhance with agent
        fetcher = GenericDataFetcher()
        _log("Running web search queries ...", "search")
        candidates = fetcher.discover_candidates(target_mcu, target_peripheral)
        _log(f"Found {len(candidates)} candidate(s)", "search")
        for c in candidates[:10]:
            _log(f"  [{c.category}] {c.title}  (score {c.score})", "search")

        selected = candidates[:10]
        if selected:
            _log(f"Downloading top {len(selected)} candidate(s) ...", "download")
        fetch_result = fetcher.fetch_selected(
            selected,
            output_dir,
            target_mcu=target_mcu,
            target_peripheral=target_peripheral,
        )
        for d in fetch_result.downloaded:
            _log(f"  Downloaded: {Path(d['local_path']).name} ({d['category']})", "download")
        for e in fetch_result.errors:
            _log(f"  Failed: {e}", "fail")

        base_result = {
            "target_mcu": target_mcu,
            "target_peripheral": target_peripheral,
            "output_dir": output_dir,
            "platform": platform_name,
            "downloaded": fetch_result.downloaded,
            "errors": fetch_result.errors,
            "success": len(fetch_result.downloaded) > 0,
        }

        # If an agent backend is configured, try to enhance fetch via AI
        if self.config.backend != "harness":
            try:
                _log("Enhancing search with AI agent ...", "agent_thinking")
                orchestrator = AutoEmuOrchestrator(
                    backend=self.config.backend,
                    model=self.config.model,
                    max_budget_usd=self.config.max_budget_usd,
                )

                def _on_fetch_event(etype: str, phase: str, detail: str) -> None:
                    _emit_agent_event(_log, etype, phase, detail)

                agent_result = asyncio.run(
                    orchestrator.fetch_input_data(
                        FetchTask(
                            target_mcu=target_mcu,
                            target_peripheral=target_peripheral,
                            output_dir=output_dir,
                        ),
                        on_event=_on_fetch_event,
                    )
                )
                if agent_result.error:
                    _log(f"Agent fetch failed: {agent_result.error}", "warn")
                else:
                    _log("Agent fetch completed", "agent_text")
                    base_result["agent_messages"] = agent_result.agent_messages
            except Exception as exc:
                _log(f"Agent fetch unavailable: {exc}", "warn")

        return base_result

    def _do_build(
        self,
        *,
        target_mcu: str,
        target_peripheral: str,
        platform_name: str,
        data_dir: str,
        output_dir: str,
        on_progress: Callable[[str, str], None] | None = None,
    ) -> dict[str, Any]:
        _log = on_progress or (lambda msg, kind="info": None)

        # Always run the local harness pipeline first
        harness_result = run_target_model_pipeline(
            target_mcu=target_mcu,
            target_peripheral=target_peripheral,
            data_dir=data_dir,
            output_dir=output_dir,
        )

        # If an agent backend is configured, try to enhance with AI
        if self.config.backend != "harness":
            inputs = resolve_fetched_input_bundle(
                target_mcu=target_mcu,
                target_peripheral=target_peripheral,
                data_dir=data_dir,
            )
            try:
                _log("Enhancing model with AI agent ...", "agent_thinking")
                orchestrator = AutoEmuOrchestrator(
                    backend=self.config.backend,
                    model=self.config.model,
                    max_budget_usd=self.config.max_budget_usd,
                )

                def _on_build_event(etype: str, phase: str, detail: str) -> None:
                    _emit_agent_event(_log, etype, phase, detail)

                result = asyncio.run(
                    orchestrator.model_peripheral(
                        ModelingTask(
                            peripheral_name=target_peripheral,
                            mcu_family=infer_stm32_mcu_family(target_mcu),
                            svd_path=inputs.svd_path,
                            header_path=inputs.header_path,
                            driver_paths=list(inputs.driver_paths),
                            output_dir=output_dir,
                            data_dir=data_dir,
                        ),
                        on_event=_on_build_event,
                    )
                )
                if result.error:
                    _log(f"Agent build failed: {result.error}", "warn")
                else:
                    _log("Agent enhancement completed", "agent_text")
                    harness_result["agent_messages"] = result.agent_messages
            except Exception as exc:
                _log(f"Agent build unavailable, using harness output: {exc}", "warn")

        return harness_result

    def _do_validate(
        self,
        output_dir: str,
        on_progress: Callable[[str, str], None] | None = None,
    ) -> dict[str, Any]:
        _log = on_progress or (lambda msg, kind="compile": None)
        source_dir = Path(output_dir)
        extra_warnings: list[str] = []

        if not source_dir.exists():
            _log("No output directory found", "warn")
            return {"success": False, "files_checked": 0, "errors": [], "warnings": ["No output directory"]}

        # Check for empty peripheral models (zero registers = unusable QEMU device)
        for model_file in source_dir.glob("*_peripheral.json"):
            try:
                model = json.loads(model_file.read_text(encoding="utf-8"))
                regs = model.get("register_block", {}).get("registers", [])
                base = model.get("base_address", 0)
                if not regs:
                    extra_warnings.append(
                        f"{model_file.name} has 0 registers — generated MMIO device will reject all accesses"
                    )
                if not base:
                    extra_warnings.append(
                        f"{model_file.name} has base_address=0 — likely incorrect"
                    )
            except Exception:
                pass

        files = list(source_dir.glob("*.c")) + list(source_dir.glob("*.h"))
        if not files:
            _log("No C/H files to validate", "warn")
            return {"success": True, "files_checked": 0, "errors": [], "warnings": ["No C/H files to validate"] + extra_warnings}

        _log(f"Checking {len(files)} file(s) against QEMU v9.2.4 headers ...", "compile")
        for f in files:
            _log(f"  Compiling: {f.name}", "compile")

        result = validate_compile(files)
        for err in result.get("errors", []):
            fname = Path(err.get("file", "")).name
            stderr_line = err.get("stderr", "").split("\n")[0][:120]
            _log(f"  FAIL: {fname} — {stderr_line}", "fail")
        all_warnings = result.get("warnings", []) + extra_warnings
        for w in all_warnings:
            _log(f"  WARN: {w}", "warn")
        ok = result.get("files_checked", 0) - len(result.get("errors", []))
        _log(f"  {ok} passed, {len(result.get('errors', []))} failed, {len(all_warnings)} warning(s)", "compile")
        result["warnings"] = all_warnings
        # An empty register model produces a non-functional QEMU device —
        # treat it as a validation failure so the TUI shows a red status.
        if extra_warnings:
            result["success"] = False
        return result

    def _count_fetched(self, fetch_result: dict[str, Any]) -> int:
        if "downloaded" in fetch_result:
            return len(fetch_result["downloaded"])
        artifacts = fetch_result.get("artifacts", [])
        return sum(1 for a in artifacts if a.get("status") in ("downloaded", "cached"))


def _emit_agent_event(
    _log: Callable[[str, str], None],
    etype: str,
    phase: str,
    detail: str,
) -> None:
    """Translate orchestrator events into styled log messages."""
    # Escape brackets so Rich markup doesn't eat the phase name.
    safe_phase = phase.replace("[", "(").replace("]", ")")

    if etype == "phase_start":
        prompt_preview = detail.replace("\n", " ").strip()
        if len(prompt_preview) > 150:
            prompt_preview = prompt_preview[:147] + "..."
        _log(f"Agent phase ({safe_phase}) prompt: {prompt_preview}", "agent_thinking")
    elif etype == "text":
        for line in detail.split("\n"):
            line = line.strip()
            if not line:
                continue
            if _is_agent_filler(line):
                continue
            if len(line) > 200:
                line = line[:197] + "..."
            _log(f"  {line}", "agent_text")
    elif etype == "tool_call":
        _log(f"  Tool call: {detail}", "agent_tool")
    elif etype == "phase_done":
        _log(f"  Agent phase ({safe_phase}) done (cost: {detail})", "agent_text")
    elif etype == "error":
        _log(f"  Agent error: {detail}", "fail")


_FILLER_PATTERNS = [
    "i don't yet have",
    "i don't have the manifest",
    "i don't have the",
    "if you want, i can",
    "if you'd like, i can",
    "if you want, you can",
    "let me know if",
    "shall i",
    "would you like me to",
    "i can also",
    "i'll now",
    "i will now",
    "here's what i",
    "here is what i",
    "i'm going to",
    "i am going to",
    "sure, i",
    "certainly,",
    "of course,",
    "you can send either",
    "you can either",
    "please provide",
    "to proceed, i need",
    "i need at least",
    "i need either",
    "next steps:",
    "recommended next step",
    "if you provide",
    "once you provide",
]


def _is_agent_filler(line: str) -> bool:
    """Return True if the line is conversational filler, not real output."""
    lower = line.lower()
    return any(lower.startswith(p) for p in _FILLER_PATTERNS)


__all__ = [
    "AgentRuntimeConfig",
    "AutoEmuAgentRuntime",
    "PipelineProgress",
    "PipelineResult",
    "PIPELINE_PHASES",
    "SUPPORTED_AGENT_BACKENDS",
]
