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
    openai_api_key: str = ""

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
        openai_key = (
            agent_cfg.get("openai_api_key")
            or os.getenv("OPENAI_API_KEY", "")
        )

        # Inject keys into the environment so SDKs pick them up
        if anthropic_key:
            os.environ.setdefault("ANTHROPIC_API_KEY", anthropic_key)
        if openai_key:
            os.environ.setdefault("OPENAI_API_KEY", openai_key)

        return cls(
            backend=backend,
            model=model,
            max_budget_usd=max_budget_usd,
            anthropic_api_key=anthropic_key,
            openai_api_key=openai_key,
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

        def _emit(phase: int, detail: str = "") -> None:
            if on_progress:
                on_progress(PipelineProgress(
                    phase=phase,
                    phase_name=PIPELINE_PHASES[phase - 1],
                    detail=detail,
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
            _emit(2, "Searching the web for input data ...")
            fetch_result = self._do_fetch(
                target_mcu=target_mcu,
                target_peripheral=target_peripheral,
                platform_name=platform_name,
                output_dir=data_dir,
                on_progress=lambda msg: _emit(2, msg),
            )
            result.fetch_result = fetch_result
            _emit(2, f"Fetch complete ({self._count_fetched(fetch_result)} artifacts)")

            # Phase 3 — build
            _emit(3, "Running modeling pipeline ...")
            build_result = self._do_build(
                target_mcu=target_mcu,
                target_peripheral=target_peripheral,
                platform_name=platform_name,
                data_dir=data_dir,
                output_dir=output_dir,
            )
            result.build_result = build_result
            generated = build_result.get("generated_files", [])
            result.generated_files = generated
            _emit(3, f"Generated {len(generated)} file(s)")

            # Phase 4 — validate
            _emit(4, "Validating generated code ...")
            validation = self._do_validate(
                output_dir,
                on_progress=lambda msg: _emit(4, msg),
            )
            result.validation_result = validation
            _emit(4, f"Validation: {'PASS' if validation.get('success') else 'ISSUES FOUND'}")

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
        on_progress: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        _log = on_progress or (lambda msg: None)

        if self.config.backend != "harness":
            return self._do_fetch_agent(
                target_mcu=target_mcu,
                target_peripheral=target_peripheral,
                output_dir=output_dir,
            )

        fetcher = GenericDataFetcher()
        _log("Running web search queries ...")
        candidates = fetcher.discover_candidates(target_mcu, target_peripheral)
        _log(f"Found {len(candidates)} candidate(s)")
        for c in candidates[:10]:
            _log(f"  [{c.category}] {c.title}  (score {c.score})")

        selected = candidates[:10]
        if selected:
            _log(f"Downloading top {len(selected)} candidate(s) ...")
        fetch_result = fetcher.fetch_selected(
            selected,
            output_dir,
            target_mcu=target_mcu,
            target_peripheral=target_peripheral,
        )
        for d in fetch_result.downloaded:
            _log(f"  Downloaded: {Path(d['local_path']).name} ({d['category']})")
        for e in fetch_result.errors:
            _log(f"  Failed: {e}")
        return {
            "target_mcu": target_mcu,
            "target_peripheral": target_peripheral,
            "output_dir": output_dir,
            "platform": platform_name,
            "downloaded": fetch_result.downloaded,
            "errors": fetch_result.errors,
            "success": len(fetch_result.downloaded) > 0,
        }

    def _do_fetch_agent(
        self,
        *,
        target_mcu: str,
        target_peripheral: str,
        output_dir: str,
    ) -> dict[str, Any]:

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
                    output_dir=output_dir,
                )
            )
        )
        if result.error:
            raise RuntimeError(result.error)
        bundle = resolve_fetched_input_bundle(
            target_mcu=target_mcu,
            target_peripheral=target_peripheral,
            data_dir=output_dir,
        )
        manifest_path = Path(bundle.manifest_path)
        if manifest_path.exists():
            return json.loads(manifest_path.read_text(encoding="utf-8"))
        return {"success": True, "agent_messages": result.agent_messages}

    def _do_build(
        self,
        *,
        target_mcu: str,
        target_peripheral: str,
        platform_name: str,
        data_dir: str,
        output_dir: str,
    ) -> dict[str, Any]:
        if self.config.backend == "harness":
            return run_target_model_pipeline(
                target_mcu=target_mcu,
                target_peripheral=target_peripheral,
                data_dir=data_dir,
                output_dir=output_dir,
            )

        inputs = resolve_fetched_input_bundle(
            target_mcu=target_mcu,
            target_peripheral=target_peripheral,
            data_dir=data_dir,
        )
        # For generic targets without SVD/headers, driver-only analysis is fine
        if not inputs.svd_path and not inputs.header_path and not inputs.driver_paths:
            raise ValueError(
                f"No inputs found for {target_mcu}/{target_peripheral}. "
                f"Fetch may have failed — no SVD, headers, or driver sources available."
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
                    output_dir=output_dir,
                )
            )
        )
        if result.error:
            raise RuntimeError(result.error)

        output_path = Path(output_dir)
        generated = [str(f) for f in sorted(output_path.rglob("*")) if f.is_file()]
        return {
            "peripheral_name": target_peripheral,
            "generated_files": generated,
            "target_mcu": target_mcu,
        }

    def _do_validate(
        self,
        output_dir: str,
        on_progress: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        _log = on_progress or (lambda msg: None)
        source_dir = Path(output_dir)
        if not source_dir.exists():
            _log("No output directory found")
            return {"success": True, "files_checked": 0, "errors": [], "warnings": ["No output directory"]}
        files = list(source_dir.glob("*.c")) + list(source_dir.glob("*.h"))
        if not files:
            _log("No C/H files to validate")
            return {"success": True, "files_checked": 0, "errors": [], "warnings": ["No C/H files to validate"]}

        _log(f"Checking {len(files)} file(s) against QEMU v9.2.4 headers ...")
        for f in files:
            _log(f"  Compiling: {f.name}")

        result = validate_compile(files)
        for err in result.get("errors", []):
            fname = Path(err.get("file", "")).name
            stderr_line = err.get("stderr", "").split("\n")[0][:120]
            _log(f"  FAIL: {fname} — {stderr_line}")
        for w in result.get("warnings", []):
            _log(f"  WARN: {w}")
        ok = result.get("files_checked", 0) - len(result.get("errors", []))
        _log(f"  {ok} passed, {len(result.get('errors', []))} failed, {len(result.get('warnings', []))} warning(s)")
        return result

    def _count_fetched(self, fetch_result: dict[str, Any]) -> int:
        if "downloaded" in fetch_result:
            return len(fetch_result["downloaded"])
        artifacts = fetch_result.get("artifacts", [])
        return sum(1 for a in artifacts if a.get("status") in ("downloaded", "cached"))


__all__ = [
    "AgentRuntimeConfig",
    "AutoEmuAgentRuntime",
    "PipelineProgress",
    "PipelineResult",
    "PIPELINE_PHASES",
    "SUPPORTED_AGENT_BACKENDS",
]
