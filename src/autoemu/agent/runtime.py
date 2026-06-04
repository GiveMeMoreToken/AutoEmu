"""Harness-first runtime for the public AutoEmu workflows."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from autoemu.agent.orchestrator import (
    AutoEmuOrchestrator,
    FetchTask,
    ModelingTask,
    _write_phase_log,
)
from autoemu.fetchers.generic import (
    GenericDataFetcher,
    _check_content,
    collect_cached_input_artifacts,
    infer_stm32_mcu_family,
    resolve_fetched_input_bundle,
)
from autoemu.modeling_utils import normalize_name as _snake
from autoemu.pipeline import run_target_model_pipeline
from autoemu.validators.compile_validator import (
    find_qemu_include_paths,
    qemu_source_hint,
    validate_compile,
)

logger = logging.getLogger(__name__)

SUPPORTED_AGENT_BACKENDS = {
    "claude-sdk",
    "codex-sdk",
    "anthropic-api",
    "openai-api",
}


# ---------------------------------------------------------------------------
# Phase descriptors for the unified pipeline
# ---------------------------------------------------------------------------

MAX_PROBE_FIX_ATTEMPTS = 3
_MAX_PROBE_FIX_CODE_BYTES = 8_000  # per-file truncation limit
_MAX_PROBE_FIX_PROMPT_BYTES = 30_000  # total prompt size guard
_PROBE_FIX_TIMEOUT_S = 300  # 5 minutes per agent call
_PROBE_SUBPROCESS_TIMEOUT_S = 600  # 10 minutes per probe subprocess

PIPELINE_PHASES = [
    "Detecting platform",
    "Fetching input data",
    "Building QEMU peripheral model",
    "Validating generated code",
    "Testing driver probing",
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
class AgentResult:
    """Mutable container for agent probe-fix results."""

    text: str = ""
    files: dict[str, str] = field(default_factory=dict)
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
    probe_result: dict[str, Any] = field(default_factory=dict)
    generated_files: list[str] = field(default_factory=list)
    test_commands: list[str] = field(default_factory=list)
    cve_findings: dict[str, Any] = field(default_factory=dict)
    success: bool = False
    error: str = ""


def _build_test_commands(
    generated_files: list[str],
    output_dir: str = "output",
    qemu_src: str = "",
) -> list[str]:
    """Return actionable commands to test and validate generated artifacts."""
    commands: list[str] = []
    out = Path(output_dir)

    # Compile validation against QEMU headers
    c_files = [f for f in generated_files if f.endswith(".c") and not Path(f).name.startswith("qtest_")]
    if c_files:
        if qemu_src:
            commands.append("# Validate compilation against QEMU headers")
            commands.append(f"AUTOEMU_QEMU_SRC={qemu_src} python -m autoemu.validators.compile_validator {' '.join(c_files)}")
        else:
            commands.append("# Validate compilation (set AUTOEMU_QEMU_SRC to enable full QEMU header checks)")
            commands.append(f"python -m autoemu.validators.compile_validator {' '.join(c_files)}")

    # Apply to QEMU tree
    commands.append("# Apply generated peripheral to QEMU source tree")
    if qemu_src:
        commands.append(f"./scripts/apply-to-qemu.py {out} --qemu-src {qemu_src}")
    else:
        commands.append(f"./scripts/apply-to-qemu.py {out} --qemu-src /path/to/qemu")

    # QTest in-tree
    qtest_files = [f for f in generated_files if Path(f).name.startswith("qtest_") and f.endswith(".c")]
    if qtest_files:
        commands.append("# Build and run QTest inside QEMU (requires in-tree placement first)")
        commands.append("# cd <qemu-src>/build && make check-qtest-arm  # or appropriate target")

    return commands


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

    Resolution order: environment variables → .autoemu.toml in CWD → defaults.
    """

    backend: str = "codex-sdk"
    model: str | None = None
    max_budget_usd: float = 5.0
    anthropic_api_key: str = ""
    anthropic_base_url: str = ""
    openai_api_key: str = ""
    openai_base_url: str = ""
    qemu_src: str = ""
    agent_phase_delay: float = 30.0

    @classmethod
    def load(cls) -> AgentRuntimeConfig:
        """Build config from environment variables, .autoemu.toml, then defaults."""
        file_cfg = _load_config_file()
        agent_cfg = file_cfg.get("agent", {})
        validation_cfg = file_cfg.get("validation", {})

        def _setting(file_key: str, env_key: str, default: Any = "") -> Any:
            env_value = os.getenv(env_key)
            if env_value:
                return env_value
            return agent_cfg.get(file_key, default)

        backend = str(
            _setting("backend", "AUTOEMU_AGENT_BACKEND", "codex-sdk")
        ).strip().lower() or "codex-sdk"
        if backend not in SUPPORTED_AGENT_BACKENDS:
            raise ValueError(
                "AUTOEMU_AGENT_BACKEND must be one of: "
                + ", ".join(sorted(SUPPORTED_AGENT_BACKENDS))
            )

        model = str(_setting("model", "AUTOEMU_AGENT_MODEL", "")).strip() or None

        budget_raw = str(
            _setting("max_budget_usd", "AUTOEMU_AGENT_MAX_BUDGET_USD", "")
        ).strip()
        max_budget_usd = float(budget_raw) if budget_raw else 5.0

        anthropic_key = str(
            _setting("anthropic_api_key", "ANTHROPIC_API_KEY", "")
        ).strip()
        anthropic_base = str(
            _setting("anthropic_base_url", "ANTHROPIC_BASE_URL", "")
        ).strip()
        openai_key = str(
            _setting("openai_api_key", "OPENAI_API_KEY", "")
        ).strip()
        openai_base = str(
            _setting("openai_base_url", "OPENAI_BASE_URL", "")
        ).strip()
        qemu_src = str(
            os.getenv("AUTOEMU_QEMU_SRC", "").strip()
            or validation_cfg.get("qemu_src", "")
        ).strip()

        delay_raw = str(
            _setting("agent_phase_delay", "AUTOEMU_AGENT_PHASE_DELAY", "")
        ).strip()
        agent_phase_delay = float(delay_raw) if delay_raw else 5.0

        # Inject into environment so SDKs pick them up
        if anthropic_key:
            os.environ["ANTHROPIC_API_KEY"] = anthropic_key
        if anthropic_base:
            os.environ["ANTHROPIC_BASE_URL"] = anthropic_base
        if openai_key:
            os.environ["OPENAI_API_KEY"] = openai_key
        if openai_base:
            os.environ["OPENAI_BASE_URL"] = openai_base

        return cls(
            backend=backend,
            model=model,
            max_budget_usd=max_budget_usd,
            anthropic_api_key=anthropic_key,
            anthropic_base_url=anthropic_base,
            openai_api_key=openai_key,
            openai_base_url=openai_base,
            qemu_src=qemu_src,
            agent_phase_delay=agent_phase_delay,
        )

    # Keep the old name as alias
    from_env = load


def _is_stdio_transport_error(msg: str) -> bool:
    return "failed reading from stdio transport" in msg.lower()


def _is_retryable_agent_result(result: Any) -> bool:
    """Return True if an agent result dict/ object contains a transient stdio error."""
    msg = ""
    if isinstance(result, dict):
        msg = str(result.get("agent_error", "")) + str(result.get("error", ""))
    else:
        msg = str(getattr(result, "error", "") or "")
    return _is_stdio_transport_error(msg)


def _run_async_factory_with_retry(
    coro_factory: Callable[[], Any],
    max_retries: int = 3,
    delay: float = 5.0,
) -> Any:
    """Run a coroutine factory with retry on transient stdio transport failures.

    Retries both when asyncio.run raises an exception AND when the returned
    result object carries a retryable agent error, because the Codex backend
    catches transport crashes internally and surfaces them as result.error.
    """
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            result = asyncio.run(coro_factory())
            if _is_retryable_agent_result(result) and attempt < max_retries:
                backoff = delay * (2 ** (attempt - 1))
                logger.warning(
                    "Agent returned transient error (attempt %d/%d), retrying in %.1fs ...",
                    attempt,
                    max_retries,
                    backoff,
                )
                time.sleep(backoff)
                continue
            return result
        except Exception as exc:
            last_exc = exc
            msg = str(exc)
            if _is_stdio_transport_error(msg) and attempt < max_retries:
                backoff = delay * (2 ** (attempt - 1))
                logger.warning(
                    "Agent stdio transport failed (attempt %d/%d), retrying in %.1fs ...",
                    attempt,
                    max_retries,
                    backoff,
                )
                time.sleep(backoff)
                continue
            raise
    raise last_exc or RuntimeError("Agent failed after retries")


def _cleanup_stale_files(data_dir: str, _log: Callable) -> None:
    """Remove files in data_dir that fail content validation.

    Targets the svd/, header/, and driver/ subdirectories that GenericDataFetcher
    writes to.  Any file whose content doesn't match its declared category (e.g.
    HTML saved as .c) is deleted before the agent picks it up.
    """
    _cat_map = {
        "svd":    "svd",
        "header": "header",
        "driver": "driver",
        "docs":   "docs",
    }
    base = Path(data_dir)
    if not base.is_dir():
        return

    removed: list[str] = []
    for subdir, category in _cat_map.items():
        sub = base / subdir
        if not sub.is_dir():
            continue
        for fpath in sub.iterdir():
            if not fpath.is_file():
                continue
            try:
                raw = fpath.read_bytes()
            except OSError:
                continue
            reason = _check_content(raw, category, fpath.name)
            if reason:
                fpath.unlink(missing_ok=True)
                removed.append(fpath.name)
                _log(f"  Removed stale file: {fpath.name} ({reason})", "warn")

    if not removed:
        _log("  No stale files found", "info")


def _fix_nested_data_dir(data_dir: str, mcu_slug: str, _log: Callable) -> None:
    """Migrate files from a double-nested MCU directory and remove it.

    Old versions of _fetch_data appended the MCU slug to an already-MCU-specific
    output_dir, producing e.g. data/hikey960/hikey960/{docs,svd,...} instead of
    data/hikey960/{docs,svd,...}.  This function moves any files found in the
    nested subtree to their correct siblings, then removes the nested directory.
    """
    import shutil

    base = Path(data_dir)
    nested = base / mcu_slug
    if not nested.is_dir():
        return

    moved = 0
    for src_file in nested.rglob("*"):
        if not src_file.is_file():
            continue
        # Compute destination relative to nested/ and place it under base/
        rel = src_file.relative_to(nested)
        dst = base / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists():
            shutil.move(str(src_file), dst)
            moved += 1
        # If dst already exists (duplicate), just leave src to be deleted below

    shutil.rmtree(nested, ignore_errors=True)
    if moved:
        _log(f"  Migrated {moved} file(s) from nested {nested.name}/ to correct location", "warn")
    else:
        _log(f"  Removed empty nested directory: {nested.name}/", "warn")


def _clear_output_dir(output_dir: str, _emit: Callable[[int, str, str], None]) -> None:
    """Remove generated C/H/JSON files from a previous run in output_dir.

    Keeps subdirectories (e.g. hw/misc/) so the agent can still write there,
    but removes all top-level C, H, JSON, and build files that were created by
    a prior pipeline run.  This prevents stale artifacts from a different
    target from polluting the compile validator.
    """
    import shutil
    out = Path(output_dir)
    if not out.exists():
        return
    _stale_exts = {".c", ".h", ".json", ".txt", ".build", ".snippet"}
    removed = 0
    for fpath in out.iterdir():
        if _preserve_output_item(fpath):
            continue
        if fpath.is_file() and fpath.suffix.lower() in _stale_exts:
            fpath.unlink(missing_ok=True)
            removed += 1
        elif fpath.is_dir():
            # Remove entire subdirectories generated by agent (hw/, tests/, gpu/, etc.)
            shutil.rmtree(fpath, ignore_errors=True)
            removed += 1
    if removed:
        _emit(3, f"Cleared {removed} stale item(s) from {output_dir}", "info")


def _preserve_output_item(path: Path) -> bool:
    """Return true for output artifacts that are reused by later phases."""
    name = path.name
    if name == "agent_logs":
        return True
    if name == "probe.dtb":
        return True
    if name.startswith("probe-") and path.suffix.lower() in {".dtb", ".dtbo", ".dts"}:
        return True
    return name.startswith("rootfs-") and name.endswith("-stage5.ext4")


def _noop_progress(message: str, kind: str = "info") -> None:
    return None


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
        cve_id: str = "",
        on_progress: Callable[[PipelineProgress], None] | None = None,
    ) -> PipelineResult:
        """Run the full pipeline: detect → cve (optional) → fetch → build → validate.

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

            # Optional CVE phase — validate, check relation, search PoC
            if cve_id:
                from autoemu.cve_validator import run_cve_check
                _emit(1, f"Validating CVE: {cve_id} ...", "search")
                cve_summary = run_cve_check(
                    cve_id,
                    peripheral_name=target_peripheral,
                    mcu_name=target_mcu,
                )
                result.cve_findings = cve_summary
                if not cve_summary["valid_format"]:
                    _emit(1, f"CVE warning: {cve_summary['warnings'][0]}", "warn")
                elif not cve_summary["disclosed"]:
                    _emit(1, f"CVE warning: {cve_summary['warnings'][0]}", "warn")
                elif not cve_summary["related"]:
                    _emit(1, f"CVE warning: {cve_summary['warnings'][0]}", "warn")
                else:
                    _emit(1, f"CVE {cve_id} is related to {target_peripheral}", "info")
                poc_count = len(cve_summary.get("poc_findings", []))
                if poc_count:
                    _emit(1, f"Found {poc_count} PoC / exploit / advisory reference(s)", "info")
                # Persist findings to output for later inspection
                try:
                    out_path = Path(output_dir)
                    out_path.mkdir(parents=True, exist_ok=True)
                    (out_path / "cve_findings.json").write_text(
                        json.dumps(cve_summary, indent=2), encoding="utf-8"
                    )
                except Exception:
                    pass

                # Fetch CVE-related driver sources for modeling enrichment
                if cve_summary.get("related"):
                    from autoemu.cve_validator import fetch_cve_driver_sources
                    _emit(1, f"Fetching CVE-related driver sources for {target_peripheral} ...", "search")
                    cve_driver_dir = f"{data_dir}/driver/cve"
                    cve_driver_result = fetch_cve_driver_sources(
                        cve_id,
                        peripheral_name=target_peripheral,
                        mcu_name=target_mcu,
                        output_dir=cve_driver_dir,
                    )
                    if cve_driver_result.get("downloaded"):
                        _emit(1, f"Downloaded {len(cve_driver_result['downloaded'])} CVE driver file(s)", "info")
                    else:
                        _emit(1, "No CVE driver sources found (continuing with existing inputs)", "warn")

            # Phase 2 — fetch
            _emit(2, "Searching the web for input data ...", "search")

            def _emit_fetch(msg: str, kind: str = "info") -> None:
                _emit(2, msg, kind)

            fetch_result = self._do_fetch(
                target_mcu=target_mcu,
                target_peripheral=target_peripheral,
                platform_name=platform_name,
                output_dir=data_dir,
                on_progress=_emit_fetch,
            )
            result.fetch_result = fetch_result
            _emit(2, f"Fetch complete ({self._count_fetched(fetch_result)} artifacts)")

            # Phase 3 — build (clear stale output files first)
            _clear_output_dir(output_dir, _emit)
            _emit(3, "Running modeling pipeline ...", "agent_thinking")

            def _emit_build(msg: str, kind: str = "info") -> None:
                _emit(3, msg, kind)

            build_result = self._do_build(
                target_mcu=target_mcu,
                target_peripheral=target_peripheral,
                platform_name=platform_name,
                data_dir=data_dir,
                output_dir=output_dir,
                on_progress=_emit_build,
            )
            result.build_result = build_result
            generated = build_result.get("generated_files", [])
            result.generated_files = generated
            _emit(3, f"Generated {len(generated)} file(s)", "agent_text")

            result.test_commands = _build_test_commands(
                generated,
                output_dir=output_dir,
                qemu_src=self.config.qemu_src,
            )

            # Phase 4 — validate
            _emit(4, "Validating generated code ...", "compile")

            def _emit_validate(msg: str, kind: str = "compile") -> None:
                _emit(4, msg, kind)

            validation = self._do_validate(
                output_dir,
                on_progress=_emit_validate,
            )
            result.validation_result = validation
            ok = validation.get("success")
            has_errors = bool(validation.get("errors"))
            if ok:
                _emit(4, "Validation: PASS", "info")
            elif has_errors:
                _emit(4, "Validation: ISSUES FOUND", "fail")
            else:
                _emit(4, "Validation: FAILED", "fail")

            agent_errors = _phase_agent_errors(fetch_result, build_result)
            for agent_error in agent_errors:
                _emit(4, agent_error, "warn" if ok else "fail")

            # Phase 5 — test driver probing (soft-fail: warn on failure)
            _emit(5, "Testing driver probing ...", "compile")

            def _emit_test(msg: str, kind: str = "info") -> None:
                _emit(5, msg, kind)

            probe_result = self._agent_probe_loop(
                target_mcu=target_mcu,
                target_peripheral=target_peripheral,
                output_dir=output_dir,
                build_result=build_result,
                cve_findings=result.cve_findings,
                on_progress=_emit_test,
            )
            result.probe_result = probe_result
            if probe_result.get("success"):
                _emit(5, "Probe test: PASS", "info")
            elif probe_result.get("skipped"):
                _emit(5, f"Probe test: SKIPPED — {probe_result.get('reason', '')}", "warn")
            else:
                _emit(5, f"Probe test: WARN — {probe_result.get('reason', '')}", "warn")

            failure_reasons: list[str] = []
            if not ok:
                failure_reasons.append(_validation_failure_summary(validation))
                failure_reasons.extend(agent_errors)

            result.success = not failure_reasons
            result.error = "; ".join(reason for reason in failure_reasons if reason)

        except Exception as exc:
            result.error = str(exc)
            logger.exception("Pipeline failed for %s / %s", target_mcu, target_peripheral)
            if on_progress:
                on_progress(PipelineProgress(
                    phase=0, phase_name="error", detail=str(exc),
                    finished=True, error=str(exc),
                ))

        if on_progress:
            on_progress(PipelineProgress(finished=True, error=result.error))

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
        _log = on_progress or _noop_progress

        # Use local web search first; optionally enhance with agent
        fetcher = GenericDataFetcher()
        _log("Running web search queries ...", "search")
        candidates = fetcher.discover_candidates(target_mcu, target_peripheral)
        _log(f"Found {len(candidates)} candidate(s)", "search")
        select_candidates = getattr(fetcher, "select_candidates", None)
        selected = (
            select_candidates(candidates, limit=10)
            if select_candidates
            else candidates[:10]
        )
        for c in selected:
            _log(f"  [{c.category}] {c.title}  (score {c.score})", "search")

        if selected:
            _log(f"Downloading {len(selected)} selected candidate(s) across file types ...", "download")
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

        # Remove stale files from previous runs that fail content validation
        _cleanup_stale_files(output_dir, _log)
        # Migrate and remove double-nested MCU directory from old _fetch_data bug.
        # The old code produced data/hikey960/hikey960/{docs,svd,...} instead of
        # data/hikey960/{docs,svd,...}.  Move any files found there to the correct
        # location, then delete the stale nested subtree.
        _fix_nested_data_dir(output_dir, _snake(target_mcu), _log)
        downloaded_paths = {
            str(Path(d["local_path"]))
            for d in fetch_result.downloaded
            if d.get("local_path")
        }
        reused = [
            artifact
            for artifact in collect_cached_input_artifacts(
                target_mcu=target_mcu,
                target_peripheral=target_peripheral,
                data_dir=output_dir,
            )
            if str(Path(artifact["local_path"])) not in downloaded_paths
        ]
        for d in reused:
            _log(f"  Reused cached: {Path(d['local_path']).name} ({d['category']})", "download")

        base_result = {
            "target_mcu": target_mcu,
            "target_peripheral": target_peripheral,
            "output_dir": output_dir,
            "platform": platform_name,
            "downloaded": fetch_result.downloaded,
            "reused": reused,
            "errors": fetch_result.errors,
            "success": bool(fetch_result.downloaded or reused),
        }

        try:
            _log("Enhancing search with AI agent ...", "agent_thinking")
            orchestrator = AutoEmuOrchestrator(
                backend=self.config.backend,
                model=self.config.model,
                max_budget_usd=self.config.max_budget_usd,
            )

            def _on_fetch_event(etype: str, phase: str, detail: str) -> None:
                _emit_agent_event(_log, etype, phase, detail)

            agent_result = _run_async_factory_with_retry(
                lambda: orchestrator.fetch_input_data(
                    FetchTask(
                        target_mcu=target_mcu,
                        target_peripheral=target_peripheral,
                        output_dir=output_dir,
                    ),
                    on_event=_on_fetch_event,
                ),
                max_retries=3,
                delay=1.0,
            )
            if agent_result.error:
                agent_error = _sanitize_agent_error(agent_result.error)
                _log(f"Agent fetch failed: {agent_error}", "warn")
                base_result["agent_error"] = agent_error
            else:
                _log("Agent fetch completed", "agent_text")
                base_result["agent_messages"] = agent_result.agent_messages
        except Exception as exc:
            agent_error = _sanitize_agent_error(str(exc))
            _log(f"Agent fetch unavailable: {agent_error}", "warn")
            base_result["agent_error"] = agent_error

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
        _log = on_progress or _noop_progress

        # Clear stale artifacts from previous runs so we don't validate or
        # report old files as "generated" this run.
        out_path = Path(output_dir)
        if out_path.exists():
            for item in out_path.iterdir():
                if _preserve_output_item(item):
                    continue
                if item.is_file():
                    item.unlink()
                elif item.is_dir():
                    import shutil
                    shutil.rmtree(item)

        # Run the local pipeline first (gracefully skip if no inputs)
        try:
            build_result = run_target_model_pipeline(
                target_mcu=target_mcu,
                target_peripheral=target_peripheral,
                data_dir=data_dir,
                output_dir=output_dir,
                qemu_src=self.config.qemu_src,
            )
        except ValueError as exc:
            # No SVD/headers/drivers fetched yet — skip local pipeline and rely on agent
            _log(f"Local pipeline skipped: {exc}", "warn")
            build_result = {
                "target_mcu": target_mcu,
                "target_peripheral": target_peripheral,
                "generated_files": [],
                "success": False,
                "skipped": True,
            }

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

            model_kwargs: dict[str, Any] = {"on_event": _on_build_event}
            model_sig = inspect.signature(orchestrator.model_peripheral)
            if (
                "phase_delay_seconds" in model_sig.parameters
                or any(
                    param.kind == inspect.Parameter.VAR_KEYWORD
                    for param in model_sig.parameters.values()
                )
            ):
                model_kwargs["phase_delay_seconds"] = self.config.agent_phase_delay

            result = _run_async_factory_with_retry(
                lambda: orchestrator.model_peripheral(
                    ModelingTask(
                        peripheral_name=target_peripheral,
                        mcu_family=infer_stm32_mcu_family(target_mcu),
                        target_mcu=target_mcu,
                        svd_path=inputs.svd_path,
                        header_path=inputs.header_path,
                        driver_paths=list(inputs.driver_paths),
                        output_dir=output_dir,
                        data_dir=data_dir,
                    ),
                    **model_kwargs,
                ),
                max_retries=3,
                delay=1.0,
            )
            if result.error:
                agent_error = _sanitize_agent_error(result.error)
                _log(f"Agent build failed: {agent_error}", "warn")
                build_result["agent_error"] = agent_error
            else:
                _log("Agent enhancement completed", "agent_text")
                build_result["agent_messages"] = result.agent_messages
        except Exception as exc:
            agent_error = _sanitize_agent_error(str(exc))
            _log(f"Agent build unavailable, using local output: {agent_error}", "warn")
            build_result["agent_error"] = agent_error

        # If the agent extracted a better register model (e.g. correct base address)
        # but the generate phase failed, regenerate the bundle from the agent model
        # so that QEMU code uses the corrected values.
        agent_register_model = out_path / f"{_snake(target_peripheral)}_register_model.json"
        if agent_register_model.exists():
            try:
                agent_data = json.loads(agent_register_model.read_text(encoding="utf-8"))
                agent_rb = agent_data.get("register_block")
                if agent_rb:
                    from autoemu.generators.bundle_generator import generate_model_bundle
                    from autoemu.models.register import RegisterBlock
                    from autoemu.models.state_machine import StateMachine
                    from autoemu.models.interrupt import InterruptModel
                    from autoemu.models.dependency import DependencyGraph

                    register_blocks = {
                        target_peripheral: RegisterBlock.model_validate(agent_rb)
                    }
                    state_machine = None
                    sm_path = out_path / f"{_snake(target_peripheral)}_state_machine.json"
                    if sm_path.exists():
                        try:
                            sm_data = json.loads(sm_path.read_text(encoding="utf-8"))
                            state_machine = StateMachine.model_validate(sm_data.get("model", sm_data))
                        except Exception:
                            pass
                    interrupt_model = None
                    im_path = out_path / f"{_snake(target_peripheral)}_interrupt_model.json"
                    if im_path.exists():
                        try:
                            im_data = json.loads(im_path.read_text(encoding="utf-8"))
                            interrupt_model = InterruptModel.model_validate(im_data.get("model", im_data))
                        except Exception:
                            pass
                    deps = None
                    dep_path = out_path / f"{_snake(target_peripheral)}_dependencies.json"
                    if dep_path.exists():
                        try:
                            dep_data = json.loads(dep_path.read_text(encoding="utf-8"))
                            deps = DependencyGraph.model_validate(dep_data.get("model", dep_data))
                        except Exception:
                            pass

                    _log(
                        f"Regenerating bundle from agent register model "
                        f"(base=0x{agent_rb.get('base_address', 0):X}) ...",
                        "agent_thinking",
                    )
                    bundle = generate_model_bundle(
                        target_peripheral,
                        register_blocks,
                        output_dir=out_path,
                        state_machine=state_machine,
                        interrupt_model=interrupt_model,
                        dependencies=deps,
                        mcu_family=infer_stm32_mcu_family(target_mcu),
                        qemu_src=self.config.qemu_src,
                    )
                    build_result["generated_files"] = bundle["generated_files"]
                    _log("Bundle regenerated from agent model", "agent_text")
            except Exception as e:
                _log(f"Could not regenerate bundle from agent model: {e}", "warn")

        return build_result

    def _do_validate(
        self,
        output_dir: str,
        on_progress: Callable[[str, str], None] | None = None,
    ) -> dict[str, Any]:
        _log = on_progress or _noop_progress
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

        # Check QEMU source availability before logging per-file messages so we
        # don't emit "Compiling: foo.c" lines when nothing will actually be compiled.
        qemu_src = self.config.qemu_src or None
        if not find_qemu_include_paths(qemu_src):
            _log(
                f"Skipping compilation check ({len(files)} file(s)) — "
                f"QEMU source tree not found ({qemu_source_hint(qemu_src)})",
                "warn",
            )
            all_warnings = [
                f"QEMU source tree not found ({qemu_source_hint(qemu_src)}); "
                "skipping compilation check"
            ] + extra_warnings
            for w in all_warnings:
                _log(f"  WARN: {w}", "warn")
            result: dict[str, Any] = {"success": True, "files_checked": 0, "errors": [], "warnings": all_warnings}
            if extra_warnings:
                result["success"] = False
            return result

        qemu_label = qemu_src or "auto-discovered QEMU"
        _log(f"Checking {len(files)} file(s) against {qemu_label} headers ...", "compile")
        for f in files:
            _log(f"  Compiling: {f.name}", "compile")

        compile_result: dict[str, Any] = validate_compile(files, qemu_src=qemu_src)
        for err in compile_result.get("errors", []):
            fname = Path(err.get("file", "")).name
            stderr_line = err.get("stderr", "").split("\n")[0][:120]
            _log(f"  FAIL: {fname} — {stderr_line}", "fail")
        all_warnings = compile_result.get("warnings", []) + extra_warnings
        for w in all_warnings:
            _log(f"  WARN: {w}", "warn")
        ok = compile_result.get("files_checked", 0) - len(compile_result.get("errors", []))
        _log(f"  {ok} passed, {len(compile_result.get('errors', []))} failed, {len(all_warnings)} warning(s)", "compile")
        compile_result["warnings"] = all_warnings
        # An empty register model produces a non-functional QEMU device —
        # treat it as a validation failure so the TUI shows a red status.
        if extra_warnings:
            compile_result["success"] = False
        return compile_result

    def _do_test(
        self,
        target_mcu: str,
        target_peripheral: str,
        output_dir: str = "output",
        cve_findings: dict[str, Any] | None = None,
        on_progress: Callable[[str, str], None] | None = None,
    ) -> dict[str, Any]:
        """Phase 5: attempt a QEMU driver-probing build (soft-fail)."""
        from autoemu.validators.qemu_probe_validator import run_qemu_probe

        return run_qemu_probe(
            output_dir=output_dir,
            target_mcu=target_mcu,
            target_peripheral=target_peripheral,
            qemu_build_env=None,
            cve_findings=cve_findings,
            on_progress=on_progress,
        )

    def _count_fetched(self, fetch_result: dict[str, Any]) -> int:
        if "downloaded" in fetch_result:
            return len(fetch_result["downloaded"])
        artifacts = fetch_result.get("artifacts", [])
        return sum(1 for a in artifacts if a.get("status") in ("downloaded", "cached"))

    def _agent_probe_loop(
        self,
        *,
        target_mcu: str,
        target_peripheral: str,
        output_dir: str,
        build_result: dict[str, Any],
        cve_findings: dict[str, Any] | None = None,
        on_progress: Callable[[str, str], None] | None = None,
    ) -> dict[str, Any]:
        """Run probe in a subprocess, retry with agent fixes on failure.

        Each iteration:
        1. Spawn ``scripts/probe_runner.py`` in its own process group.
        2. If the subprocess hangs, kill the entire process group.
        3. If probe succeeds → return.
        4. Build a feedback prompt with the probe analysis and current code.
        5. Ask the agent to fix the generated code.
        6. Write fixed files back to the output directory.
        7. Loop.
        """
        def _emit(msg: str, kind: str = "info") -> None:
            if on_progress:
                on_progress(msg, kind)

        probe_result: dict[str, Any] = {}

        for attempt in range(MAX_PROBE_FIX_ATTEMPTS):
            if attempt > 0:
                _emit(f"Probe fix attempt {attempt + 1}/{MAX_PROBE_FIX_ATTEMPTS} ...", "agent_thinking")

            _emit(
                f"Running QEMU probe (subprocess, attempt {attempt + 1}) ...",
                "compile",
            )
            probe_result = _run_probe_subprocess(
                target_mcu=target_mcu,
                target_peripheral=target_peripheral,
                output_dir=output_dir,
                cve_findings=cve_findings,
            )

            if probe_result.get("success"):
                if attempt > 0:
                    _emit(f"Probe succeeded after {attempt} fix attempt(s)", "info")
                return probe_result

            probe_status = probe_result.get("probe_status", "unknown")
            probe_reason = probe_result.get("reason", "")
            _emit(f"Probe {probe_status}: {probe_reason}", "warn")

            if attempt >= MAX_PROBE_FIX_ATTEMPTS - 1:
                _emit("Max probe fix attempts reached", "warn")
                return probe_result

            # Ask the agent to fix the code.
            _emit("Asking agent to fix generated code ...", "agent_thinking")

            sys_prompt = _build_probe_fix_system_prompt()
            user_msg = _build_probe_fix_user_prompt(
                target_mcu=target_mcu,
                target_peripheral=target_peripheral,
                probe_result=probe_result,
                build_result=build_result,
                output_dir=output_dir,
            )

            # Guard against oversized prompts.
            prompt_bytes = len(user_msg.encode("utf-8"))
            if prompt_bytes > _MAX_PROBE_FIX_PROMPT_BYTES:
                _emit(
                    f"Probe fix prompt too large ({prompt_bytes} bytes), "
                    f"truncating to {_MAX_PROBE_FIX_PROMPT_BYTES}",
                    "warn",
                )
                user_msg = user_msg[:_MAX_PROBE_FIX_PROMPT_BYTES] + "\n\n---\n[Prompt truncated]\n"

            result = AgentResult()
            _run_agent_task(
                self.config.backend,
                sys_prompt,
                user_msg,
                result,
                on_progress=on_progress,
                phase_name="probe_fix",
                output_dir=output_dir,
            )

            # Write agent log for the probe fix attempt.
            try:
                log_path = _write_phase_log(
                    output_dir, "probe_fix", attempt,
                    user_msg, sys_prompt, [], error=result.error or None,
                )
                _emit(f"Agent log written: {log_path}", "agent_text")
            except Exception:
                pass

            # Save agent text output for debugging.
            if result.text:
                try:
                    txt_path = Path(output_dir) / f"probe_fix_agent_text_{attempt}.txt"
                    txt_path.write_text(result.text, encoding="utf-8")
                except Exception:
                    pass

            if result.error:
                _emit(f"Agent probe fix failed: {result.error}, retrying ...", "warn")
                continue

            written = len(result.files)
            if written:
                _emit(f"Agent updated {written} file(s), re-probing ...", "agent_text")
            else:
                _emit("Agent returned no file changes, retrying ...", "warn")
                continue

        return probe_result


# ---------------------------------------------------------------------------
# Subprocess-based probe runner
# ---------------------------------------------------------------------------

def _run_probe_subprocess(
    *,
    target_mcu: str,
    target_peripheral: str,
    output_dir: str,
    cve_findings: dict[str, Any] | None = None,
    timeout_s: float = _PROBE_SUBPROCESS_TIMEOUT_S,
) -> dict[str, Any]:
    """Run the QEMU probe in a child process with timeout + kill.

    Spawns ``scripts/probe_runner.py`` as a subprocess in its own process
    group.  If the probe hangs (e.g. QEMU boot stalls), the entire process
    group is killed after *timeout_s* seconds.

    Returns the probe result dict on success, or a synthetic failure dict
    on timeout / crash.
    """
    import subprocess as _sp
    import sys as _sys

    probe_script = str(Path(__file__).resolve().parent.parent.parent.parent / "scripts" / "probe_runner.py")
    cmd = [
        _sys.executable,
        probe_script,
        "--output-dir", output_dir,
        "--target-mcu", target_mcu,
        "--target-peripheral", target_peripheral,
    ]
    if cve_findings:
        cmd.extend(["--cve-findings", json.dumps(cve_findings)])

    try:
        proc = _sp.Popen(
            cmd,
            stdout=_sp.PIPE,
            stderr=_sp.PIPE,
            start_new_session=True,  # own process group for clean kill
        )
    except OSError as exc:
        return {
            "success": False,
            "reason": f"Failed to start probe subprocess: {exc}",
            "probe_status": "error",
        }

    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except _sp.TimeoutExpired:
        # Kill the entire process group.
        try:
            os.killpg(os.getpgid(proc.pid), 15)  # SIGTERM
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except _sp.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), 9)  # SIGKILL
            except OSError:
                pass
            proc.wait(timeout=5)
        return {
            "success": False,
            "reason": f"Probe subprocess timed out after {timeout_s}s",
            "probe_status": "timeout",
        }

    if proc.returncode != 0:
        stderr_tail = stderr.decode("utf-8", errors="replace").strip().split("\n")[-10:]
        return {
            "success": False,
            "reason": f"Probe subprocess exited with code {proc.returncode}",
            "probe_status": "error",
            "probe_log_tail": stderr_tail,
        }

    try:
        result = json.loads(stdout.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return {
            "success": False,
            "reason": f"Failed to parse probe result: {exc}",
            "probe_status": "error",
        }

    return result


async def _run_probe_fix_agent(
    backend_name: str,
    system_prompt: str,
    user_message: str,
    output_dir: str,
    *,
    model: str | None = None,
    max_budget_usd: float = 5.0,
    timeout_s: float = _PROBE_FIX_TIMEOUT_S,
) -> dict[str, Any]:
    """Run a minimal agent loop with a ``write_file`` tool for probe fixes.

    Returns ``{"files": dict, "text": str, "error": str}``.
    """
    from autoemu.agent.backends import create_backend

    out_path = Path(output_dir)
    written_files: dict[str, str] = {}

    async def _handle_write_file(args: dict[str, Any]) -> dict[str, Any]:
        rel_path = str(args.get("file_path", "")).strip()
        content = str(args.get("content", ""))
        if not rel_path:
            return {"content": [{"type": "text", "text": "Missing file_path"}], "is_error": True}
        target = out_path / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written_files[rel_path] = content
        return {"content": [{"type": "text", "text": f"Wrote {rel_path}"}]}

    from autoemu.agent.backend import ToolSpec

    write_tool = ToolSpec(
        name="write_file",
        description="Write content to a file (relative to the output directory).",
        parameters={"file_path": str, "content": str},
        handler=_handle_write_file,
    )

    try:
        backend = create_backend(backend_name, model=model)
    except Exception as exc:
        return {"files": {}, "text": "", "error": str(exc)}

    text_parts: list[str] = []
    error = ""

    async def _run() -> None:
        nonlocal error
        async for event in backend.run(
            user_message,
            system_prompt=system_prompt,
            tools=[write_tool],
            model=model,
            max_budget_usd=max_budget_usd,
        ):
            if event.type == "text":
                text_parts.append(event.text)
            elif event.type == "error":
                error = event.text

    try:
        await asyncio.wait_for(_run(), timeout=timeout_s)
    except TimeoutError:
        error = f"Agent timed out after {timeout_s}s"
    except Exception as exc:
        error = str(exc)

    # If the agent did not use the tool, try to parse files from the text.
    if not written_files and text_parts:
        combined = "\n".join(text_parts)
        code_blocks = re.findall(r"```(?:c|dtso)?\n(.*?)```", combined, re.DOTALL)
        generated_paths = [
            p.name for p in sorted(out_path.glob("*.c"))
            if p.name not in ("probe_log.txt",)
        ] + [p.name for p in sorted(out_path.glob("*.h"))]
        for idx, block in enumerate(code_blocks):
            if idx < len(generated_paths):
                rel = generated_paths[idx]
                target = out_path / rel
                target.write_text(block, encoding="utf-8")
                written_files[rel] = block

    if not written_files and not error:
        error = "Agent returned no file changes"
    elif written_files:
        error = ""

    return {"files": written_files, "text": "\n".join(text_parts), "error": error}


def _run_agent_task(
    backend_name: str,
    system_prompt: str,
    user_message: str,
    result: AgentResult,
    *,
    on_progress: Callable[[str, str], None] | None = None,
    phase_name: str = "",
    model: str | None = None,
    max_budget_usd: float = 5.0,
    output_dir: str = "",
) -> None:
    """Synchronous wrapper that runs :func:`_run_probe_fix_agent`."""
    agent_result = asyncio.run(
        _run_probe_fix_agent(
            backend_name,
            system_prompt,
            user_message,
            output_dir,
            model=model,
            max_budget_usd=max_budget_usd,
        )
    )
    result.text = agent_result.get("text", "")
    result.files = agent_result.get("files", {})
    result.error = agent_result.get("error", "")


def _phase_agent_errors(*phase_results: dict[str, Any]) -> list[str]:
    """Return user-visible agent backend failures from phase result dicts."""
    phase_names = ("fetch", "build")
    errors: list[str] = []
    for phase_name, phase_result in zip(phase_names, phase_results):
        agent_error = str(phase_result.get("agent_error", "")).strip()
        if agent_error:
            errors.append(f"{phase_name} agent failed: {agent_error}")
    return errors


def _validation_failure_summary(validation: dict[str, Any]) -> str:
    """Summarize validation errors or blocking warnings for PipelineResult.error."""
    errors = validation.get("errors", []) or []
    if errors:
        summaries: list[str] = []
        for err in errors[:3]:
            if isinstance(err, dict):
                fname = Path(err.get("file", "")).name or "generated file"
                stderr = str(err.get("stderr", "")).split("\n")[0].strip()
                summaries.append(f"{fname}: {stderr}" if stderr else fname)
            else:
                summaries.append(str(err))
        return "validation failed: " + "; ".join(summaries)

    warnings = validation.get("warnings", []) or []
    if warnings:
        return "validation failed: " + "; ".join(str(w) for w in warnings[:3])
    return "validation failed"


def _sanitize_agent_error(msg: str) -> str:
    """Return a clean, English-only summary of an agent error message.

    Proxy vendors sometimes embed Chinese-language status pages, JSON blobs,
    or QQ group links inside HTTP 500 responses.  We strip those and keep only
    the HTTP status code and a short English description.
    """
    import re as _re
    # Extract just the HTTP status code + first English phrase if present
    code_match = _re.search(r"Error code:\s*(\d+)", msg)
    code = code_match.group(1) if code_match else None
    # Detect non-ASCII (CJK etc.) content
    has_cjk = bool(_re.search(r"[\u4e00-\u9fff\u3040-\u30ff]", msg))
    if has_cjk and code:
        return f"HTTP {code} error from API proxy (server busy or rate-limited)"
    if has_cjk:
        return "API proxy returned a non-English error (server busy or rate-limited)"
    # Truncate very long technical blobs
    if len(msg) > 200:
        return msg[:197] + "..."
    return msg


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
        # Sanitize proxy-specific error blobs (e.g. Chinese-language 500 messages)
        sanitized = _sanitize_agent_error(detail)
        _log(f"  Agent error: {sanitized}", "fail")


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


# ---------------------------------------------------------------------------
# Probe-fix prompt builders
# ---------------------------------------------------------------------------

def _build_probe_fix_system_prompt() -> str:
    return (
        "You are an expert embedded-systems engineer debugging a QEMU "
        "peripheral model.  A Linux driver probe was attempted inside a QEMU "
        "VM and failed.  Analyse the probe feedback, identify the root cause, "
        "and fix the generated C/H/DTSO files so that the driver probes "
        "successfully.\n\n"
        "Guidelines:\n"
        "- Focus on the specific errors reported; do not rewrite unrelated code.\n"
        "- If a compile error is present, fix it first — the probe cannot succeed "
        "if the QEMU build fails.\n"
        "- If the driver does not bind, verify that the device-tree compatible "
        "string, MMIO base/size, and register offsets match the driver's "
        "expectations.\n"
        "- If the driver probes but crashes, check register read/write handlers, "
        "reset values, and interrupt wiring.\n"
        "- Preserve the existing code structure and style."
    )


def _extract_code_summary(content: str, max_bytes: int) -> str:
    """Extract the most relevant parts of a generated C/H file.

    Strategy:
    - Always include the first block (includes, top-level defines, struct).
    - Skip repetitive ``#define *_OFFSET`` lines after the first few.
    - Collapse long runs of trivial ``case`` / ``return s->field`` pairs.
    - Include function signatures and non-trivial function bodies.
    - Truncate to *max_bytes*.
    """
    lines = content.splitlines(keepends=True)
    out: list[str] = []
    total = 0
    offset_defines_seen = 0
    trivial_case_run = 0

    for line in lines:
        stripped = line.strip()

        # Skip repetitive register-offset defines after the first few.
        if stripped.startswith("#define ") and "_OFFSET" in stripped:
            offset_defines_seen += 1
            if offset_defines_seen <= 20:
                out.append(line)
                total += len(line.encode("utf-8"))
            elif offset_defines_seen == 21:
                out.append("    /* ... additional _OFFSET defines omitted ... */\n")
                total += 60
            continue

        # Collapse long runs of trivial case+return pairs.
        if stripped.startswith("case ") and stripped.endswith(":"):
            trivial_case_run += 1
            if trivial_case_run <= 10:
                encoded_len = len(line.encode("utf-8"))
                if total + encoded_len > max_bytes:
                    break
                out.append(line)
                total += encoded_len
            elif trivial_case_run == 11:
                out.append("        /* ... additional trivial cases omitted ... */\n")
                total += 60
            continue

        if trivial_case_run > 10 and stripped.startswith("return s->"):
            continue  # skip trivial return after collapsed cases

        trivial_case_run = 0

        encoded_len = len(line.encode("utf-8"))
        if total + encoded_len > max_bytes:
            out.append(f"\n/* ... remaining code omitted ({len(content)} bytes total) ... */\n")
            break
        out.append(line)
        total += encoded_len

    return "".join(out)


def _build_probe_fix_user_prompt(
    *,
    target_mcu: str,
    target_peripheral: str,
    probe_result: dict[str, Any],
    build_result: dict[str, Any],
    output_dir: str,
) -> str:
    probe_status = probe_result.get("probe_status", "unknown")
    probe_reason = probe_result.get("reason", "")
    probe_lines = probe_result.get("probe_log_tail", [])[:20]
    compile_errors = build_result.get("compile_errors", [])

    sections = [
        f"## Probe feedback\n"
        f"- MCU: {target_mcu}\n"
        f"- Peripheral: {target_peripheral}\n"
        f"- Status: {probe_status}\n"
        f"- Reason: {probe_reason}",
    ]

    if probe_lines:
        sections.append(
            "## QEMU console log (tail)\n```\n"
            + "\n".join(probe_lines)
            + "\n```"
        )

    if compile_errors:
        err_lines = []
        for entry in compile_errors[:5]:
            if isinstance(entry, dict):
                fname = Path(entry.get("file", "")).name
                stderr = str(entry.get("stderr", "")).strip()
                err_lines.append(f"{fname}: {stderr}" if stderr else fname)
            else:
                err_lines.append(str(entry))
        sections.append(
            "## Compile errors\n```\n"
            + "\n".join(err_lines)
            + "\n```"
        )

    # Include current generated code (smart extraction for large files).
    out_path = Path(output_dir)
    code_blocks: list[str] = []
    for ext in ("*.c", "*.h", "*.dtso"):
        for fpath in sorted(out_path.glob(ext)):
            rel = fpath.relative_to(out_path)
            content = fpath.read_text(encoding="utf-8")
            if len(content) > _MAX_PROBE_FIX_CODE_BYTES:
                content = _extract_code_summary(content, _MAX_PROBE_FIX_CODE_BYTES)
            code_blocks.append(f"### {rel}\n```c\n{content}\n```")
    if code_blocks:
        sections.append("## Current generated code\n" + "\n\n".join(code_blocks))

    sections.append(
        "## Task\n"
        "Diagnose why the driver probe failed and fix the generated code.  "
        "Use the `write_file` tool to write corrected files to the output directory.  "
        "Focus on the specific error — do not rewrite code that is already correct."
    )

    return "\n\n".join(sections)


__all__ = [
    "AgentRuntimeConfig",
    "AutoEmuAgentRuntime",
    "PipelineProgress",
    "PipelineResult",
    "PIPELINE_PHASES",
    "SUPPORTED_AGENT_BACKENDS",
    "MAX_PROBE_FIX_ATTEMPTS",
]
