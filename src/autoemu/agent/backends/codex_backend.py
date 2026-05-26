"""OpenAI Codex app-server SDK backend implementation.

Supports three SDK generations:
1. Latest: ``openai-codex`` (AsyncCodex high-level API)
2. Intermediate: ``codex-app-server-sdk`` / ``codex_app_server_client`` (CodexClient API)
3. Legacy: ``codex_app_server`` (low-level AsyncCodex + ServiceTier)
"""

from __future__ import annotations

import asyncio
import inspect
import os
import shutil
from pathlib import Path
from typing import Any, AsyncIterator

from autoemu.agent.backend import AgentBackend, AgentEvent, ToolSpec

# ---------------------------------------------------------------------------
# Latest SDK: openai-codex
# ---------------------------------------------------------------------------
_AsyncCodexLatest: Any = None
_AppServerConfigLatest: Any = None
_SandboxModeLatest: Any = None
_ApprovalModeLatest: Any = None
_TextInputLatest: Any = None
try:
    from openai_codex import AsyncCodex as _AsyncCodexLatest
    from openai_codex import AppServerConfig as _AppServerConfigLatest
    from openai_codex import SandboxMode as _SandboxModeLatest
    from openai_codex import ApprovalMode as _ApprovalModeLatest
    from openai_codex import TextInput as _TextInputLatest
except ImportError:  # pragma: no cover - latest SDK is optional
    try:
        from openai_codex.async_client import AsyncCodex as _AsyncCodexLatest
        from openai_codex.client import AppServerConfig as _AppServerConfigLatest
        from openai_codex.generated.v2_all import SandboxMode as _SandboxModeLatest
        from openai_codex._approval_mode import ApprovalMode as _ApprovalModeLatest
        from openai_codex._inputs import TextInput as _TextInputLatest
    except ImportError:
        pass

# ---------------------------------------------------------------------------
# Legacy SDK: codex_app_server
# ---------------------------------------------------------------------------
_AsyncCodexLegacy: Any = None
_AppServerConfigLegacy: Any = None
_ServiceTierLegacy: Any = None
_TextInputLegacy: Any = None
_AgentMessageDeltaNotification: Any = None
_ErrorNotification: Any = None
_TurnCompletedNotification: Any = None
try:
    from codex_app_server import AsyncCodex as _AsyncCodexLegacy
    from codex_app_server import AppServerConfig as _AppServerConfigLegacy
    from codex_app_server import ServiceTier as _ServiceTierLegacy
    from codex_app_server import TextInput as _TextInputLegacy
    from codex_app_server.generated.v2_all import (
        AgentMessageDeltaNotification as _AgentMessageDeltaNotification,
        ErrorNotification as _ErrorNotification,
        TurnCompletedNotification as _TurnCompletedNotification,
    )
except ImportError:  # pragma: no cover - exercised only without optional SDK
    pass

# ---------------------------------------------------------------------------
# Intermediate SDK: codex-app-server-sdk / codex_app_server_client
# ---------------------------------------------------------------------------
_CodexClient: Any = None
_ThreadConfig: Any = None
try:
    from codex_app_server_sdk import CodexClient as _CodexClient
    from codex_app_server_sdk import ThreadConfig as _ThreadConfig
except ImportError:  # pragma: no cover - depends on installed SDK version
    try:
        from codex_app_server_client import CodexClient as _CodexClient
        from codex_app_server_client import ThreadConfig as _ThreadConfig
    except ImportError:  # pragma: no cover - depends on installed SDK version
        pass

AsyncCodexLatest: Any = _AsyncCodexLatest
AppServerConfigLatest: Any = _AppServerConfigLatest
SandboxModeLatest: Any = _SandboxModeLatest
ApprovalModeLatest: Any = _ApprovalModeLatest
TextInputLatest: Any = _TextInputLatest

AsyncCodexLegacy: Any = _AsyncCodexLegacy
AppServerConfigLegacy: Any = _AppServerConfigLegacy
ServiceTierLegacy: Any = _ServiceTierLegacy
# Keep old aliases for backward compatibility with tests and callers
AsyncCodex: Any = AsyncCodexLegacy
AppServerConfig: Any = AppServerConfigLegacy
ServiceTier: Any = ServiceTierLegacy

CodexClient: Any = _CodexClient
ThreadConfig: Any = _ThreadConfig


def _is_transient_transport_error(exc: Exception) -> bool:
    """Return True if *exc* looks like a recoverable stdio/transport failure."""
    msg = str(exc).lower()
    return any(p in msg for p in (
        "stdio",
        "transport",
        "broken pipe",
        "connection reset",
        "eof",
    ))


def _tool_reference(tools: list[ToolSpec]) -> str:
    """Render AutoEmu tools as prompt reference text for Codex."""
    if not tools:
        return ""

    lines = [
        "",
        "AutoEmu tool reference:",
        "The current Codex backend does not expose AutoEmu ToolSpec handlers as native SDK tools.",
        "Use the repository files and working directory directly when you need to perform these tasks.",
    ]
    for spec in tools:
        params = ", ".join(
            f"{name}: {getattr(ptype, '__name__', str(ptype))}"
            for name, ptype in spec.parameters.items()
        )
        lines.append(f"- {spec.name}({params}): {spec.description}")
    return "\n".join(lines)


def _codex_bin(extra: dict[str, Any]) -> str:
    """Resolve a local Codex CLI binary path for SDKs that need one."""
    return (
        str(extra.get("codex_bin") or "").strip()
        or os.getenv("AUTOEMU_CODEX_BIN", "").strip()
        or os.getenv("CODEX_BIN", "").strip()
        or (shutil.which("codex") or "")
    )


def _codex_sandbox(extra: dict[str, Any]) -> str:
    """Resolve sandbox mode for local Codex agent execution."""
    return (
        str(extra.get("sandbox") or "").strip()
        or os.getenv("AUTOEMU_CODEX_SANDBOX", "").strip()
        or os.getenv("CODEX_SANDBOX", "").strip()
        or "danger-full-access"
    )


def _codex_approval_policy(extra: dict[str, Any]) -> str:
    """Resolve approval policy for local Codex agent execution."""
    return (
        str(extra.get("approval_policy") or "").strip()
        or os.getenv("AUTOEMU_CODEX_APPROVAL_POLICY", "").strip()
        or os.getenv("CODEX_APPROVAL_POLICY", "").strip()
        or "never"
    )


def _codex_service_tier(extra: dict[str, Any]) -> str:
    """Resolve a Codex service tier accepted by app-server SDK schemas."""
    tier = (
        str(extra.get("service_tier") or extra.get("serviceTier") or "").strip()
        or os.getenv("AUTOEMU_CODEX_SERVICE_TIER", "").strip()
        or os.getenv("CODEX_SERVICE_TIER", "").strip()
        or "fast"
    ).lower()
    if tier not in {"fast", "flex"}:
        return "fast"
    return tier


def _patch_codex_service_tier_enum() -> None:
    """Tolerate newer app-server serviceTier values in older Python SDKs."""
    if ServiceTier is None or getattr(ServiceTier, "_autoemu_accepts_unknown", False):
        return

    def _missing_(cls, value):
        if isinstance(value, str) and value:
            return getattr(cls, "fast")
        return None

    try:
        ServiceTier._missing_ = classmethod(_missing_)
        ServiceTier._autoemu_accepts_unknown = True
    except Exception:
        return


def _resolve_cwd(cwd: str | None) -> str | None:
    """Normalize agent working directories before passing them to SDKs."""
    return str(Path(cwd).resolve()) if cwd else None


def _filter_thread_config_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Drop kwargs that the installed ThreadConfig class does not accept."""
    if ThreadConfig is None:
        return kwargs
    try:
        sig = inspect.signature(ThreadConfig.__init__)
        params = sig.parameters
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return kwargs
        valid = set(params)
        return {k: v for k, v in kwargs.items() if k in valid}
    except Exception:
        return kwargs


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _codex_config_overrides(extra: dict[str, Any]) -> tuple[str, ...]:
    """Mirror AutoEmu's Codex policy into app-server startup config.

    The app-server reads CLI config before thread_start(), so thread-level
    sandbox settings are too late for hosts where bubblewrap cannot initialize.
    """
    configured = extra.get("config_overrides") or ()
    if isinstance(configured, str):
        overrides = (configured,)
    else:
        overrides = tuple(str(item) for item in configured)

    keys = {
        item.split("=", 1)[0].strip()
        for item in overrides
        if "=" in item
    }
    additions: list[str] = []
    if "sandbox_mode" not in keys:
        additions.append(f"sandbox_mode={_toml_string(_codex_sandbox(extra))}")
    if "approval_policy" not in keys:
        additions.append(f"approval_policy={_toml_string(_codex_approval_policy(extra))}")
    return (*overrides, *additions)


def _codex_app_server_command(codex_bin: str, extra: dict[str, Any]) -> list[str]:
    """Build a Codex CLI command that disables sandboxing before app-server init."""
    command = [codex_bin]
    for override in _codex_config_overrides(extra):
        command.extend(["--config", override])
    command.append("app-server")
    return command


def _codex_config_legacy(extra: dict[str, Any]) -> Any | None:
    """Build old SDK config that can use a locally installed Codex CLI binary."""
    if AppServerConfig is None:
        return None

    codex_bin = _codex_bin(extra)
    return AppServerConfig(
        codex_bin=codex_bin or None,
        config_overrides=_codex_config_overrides(extra),
    )


def _codex_config_latest(extra: dict[str, Any]) -> Any | None:
    """Build latest SDK AppServerConfig."""
    if AppServerConfigLatest is None:
        return None

    codex_bin = _codex_bin(extra)
    return AppServerConfigLatest(
        codex_bin=codex_bin or None,
        config_overrides=_codex_config_overrides(extra),
    )


class CodexSdkBackend(AgentBackend):
    """Backend that delegates to the Codex app-server Python SDK."""

    def __init__(self, **kwargs) -> None:
        self._extra = kwargs

    async def run(
        self,
        prompt: str,
        *,
        system_prompt: str = "",
        tools: list[ToolSpec] | None = None,
        model: str | None = None,
        max_budget_usd: float = 5.0,
        cwd: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        full_prompt = prompt + _tool_reference(tools or [])
        resolved_cwd = _resolve_cwd(cwd)

        # Tests and callers may monkeypatch the legacy alias directly.
        if AsyncCodex is not None and AsyncCodex is not AsyncCodexLegacy:
            async for event in self._run_legacy_sdk(
                full_prompt,
                system_prompt=system_prompt,
                model=model,
                cwd=resolved_cwd,
            ):
                yield event
            return

        # Prefer latest SDK (openai-codex)
        if AsyncCodexLatest is not None:
            async for event in self._run_latest_sdk(
                full_prompt,
                system_prompt=system_prompt,
                model=model,
                cwd=resolved_cwd,
            ):
                yield event
            return

        # Fall back to legacy SDK (codex_app_server)
        if AsyncCodex is not None:
            async for event in self._run_legacy_sdk(
                full_prompt,
                system_prompt=system_prompt,
                model=model,
                cwd=resolved_cwd,
            ):
                yield event
            return

        # Fall back to intermediate SDK (codex-app-server-sdk)
        if CodexClient is not None and ThreadConfig is not None:
            async for event in self._run_intermediate_sdk(
                full_prompt,
                system_prompt=system_prompt,
                model=model,
                cwd=resolved_cwd,
            ):
                yield event
            return

        yield AgentEvent(
            type="error",
            text="No Codex SDK installed. Install: pip install openai-codex",
        )

    async def _run_latest_sdk(
        self,
        prompt: str,
        *,
        system_prompt: str = "",
        model: str | None = None,
        cwd: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Run using ``openai-codex`` (latest high-level AsyncCodex API)."""
        if AsyncCodexLatest is None or TextInputLatest is None:
            yield AgentEvent(type="error", text="openai-codex is not installed")
            return

        sandbox_mode = _codex_sandbox(self._extra)
        approval_policy = _codex_approval_policy(self._extra)
        service_tier = _codex_service_tier(self._extra)

        # Map string sandbox names to SandboxMode enum when available
        sandbox: Any = sandbox_mode
        if SandboxModeLatest is not None:
            try:
                sandbox = getattr(SandboxModeLatest, sandbox_mode.replace("-", "_"))
            except AttributeError:
                pass

        # Map string approval policy to ApprovalMode enum when available
        approval_mode: Any = None
        if ApprovalModeLatest is not None:
            try:
                approval_mode = getattr(
                    ApprovalModeLatest,
                    "deny_all" if approval_policy == "never" else "auto_review",
                )
            except AttributeError:
                pass

        thread_kwargs: dict[str, Any] = {
            "model": model or self._extra.get("model"),
            "cwd": cwd,
            "developer_instructions": system_prompt or None,
            "sandbox": sandbox,
            "service_tier": service_tier,
        }
        if approval_mode is not None:
            thread_kwargs["approval_mode"] = approval_mode
        thread_kwargs = {k: v for k, v in thread_kwargs.items() if v is not None}

        for attempt in range(3):
            try:
                config = _codex_config_latest(self._extra)
                codex_kwargs: dict[str, Any] = {"config": config} if config is not None else {}
                async with AsyncCodexLatest(**codex_kwargs) as codex:
                    thread = await codex.thread_start(**thread_kwargs)
                    result = await thread.run(TextInputLatest(text=prompt))

                final_response = getattr(result, "final_response", None)
                if final_response:
                    yield AgentEvent(type="text", text=str(final_response))
                yield AgentEvent(type="complete", cost_usd=0.0)
                return
            except Exception as exc:
                if attempt < 2 and _is_transient_transport_error(exc):
                    # Longer backoff for stdio transport crashes — the Codex CLI
                    # subprocess may need several seconds to restart fully.
                    await asyncio.sleep(5.0 * (2 ** attempt))
                    continue
                yield AgentEvent(type="error", text=str(exc))
                return

    async def _run_intermediate_sdk(
        self,
        prompt: str,
        *,
        system_prompt: str = "",
        model: str | None = None,
        cwd: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Run using ``codex-app-server-sdk`` / ``codex_app_server_client``."""
        if CodexClient is None or ThreadConfig is None:
            yield AgentEvent(type="error", text="codex-app-server-sdk is not installed")
            return

        codex_bin = _codex_bin(self._extra)
        connect_kwargs: dict[str, Any] = {}
        if codex_bin:
            connect_kwargs["command"] = _codex_app_server_command(codex_bin, self._extra)
        if cwd:
            connect_kwargs["cwd"] = cwd

        thread_config_kwargs = {
            "cwd": cwd,
            "developer_instructions": system_prompt or None,
            "model": model or self._extra.get("model"),
            "sandbox": _codex_sandbox(self._extra),
            "approval_policy": _codex_approval_policy(self._extra),
            "service_tier": _codex_service_tier(self._extra),
        }
        thread_config_kwargs = {
            key: value for key, value in thread_config_kwargs.items() if value is not None
        }
        thread_config_kwargs = _filter_thread_config_kwargs(thread_config_kwargs)

        for attempt in range(3):
            try:
                client = CodexClient.connect_stdio(**connect_kwargs)
                async with client:
                    result = await client.chat_once(
                        prompt,
                        thread_config=ThreadConfig(**thread_config_kwargs),
                    )

                final_text = str(getattr(result, "final_text", "") or "")
                if final_text:
                    yield AgentEvent(type="text", text=final_text)
                yield AgentEvent(type="complete", cost_usd=0.0)
                return
            except Exception as exc:
                if attempt < 2 and _is_transient_transport_error(exc):
                    # Longer backoff for stdio transport crashes — the Codex CLI
                    # subprocess may need several seconds to restart fully.
                    await asyncio.sleep(5.0 * (2 ** attempt))
                    continue
                yield AgentEvent(type="error", text=str(exc))
                return

    async def _run_legacy_sdk(
        self,
        prompt: str,
        *,
        system_prompt: str = "",
        model: str | None = None,
        cwd: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Run using legacy ``codex_app_server`` (AsyncCodex low-level API)."""
        if AsyncCodex is None:
            yield AgentEvent(type="error", text="codex_app_server is not installed")
            return

        thread_kwargs: dict[str, Any] = {
            "model": model or self._extra.get("model"),
            "cwd": cwd,
            "developer_instructions": system_prompt or None,
            "sandbox": _codex_sandbox(self._extra),
            "approval_policy": _codex_approval_policy(self._extra),
            "service_tier": _codex_service_tier(self._extra),
        }
        thread_kwargs = {k: v for k, v in thread_kwargs.items() if v is not None}

        for attempt in range(3):
            try:
                _patch_codex_service_tier_enum()
                config = _codex_config_legacy(self._extra)
                codex_kwargs = {"config": config} if config is not None else {}
                async with AsyncCodex(**codex_kwargs) as codex:
                    thread = await codex.thread_start(**thread_kwargs)
                    # Always use streaming (turn + stream) instead of thread.run().
                    # thread.run() buffers the entire response and is more likely to
                    # trigger stdio transport crashes with large agent outputs.
                    if _TextInputLegacy is None:
                        yield AgentEvent(
                            type="error",
                            text="codex_app_server TextInput is not available",
                        )
                        return
                    handle = await thread.turn(_TextInputLegacy(text=prompt))
                    async for notification in handle.stream():
                        if (
                            _AgentMessageDeltaNotification is not None
                            and isinstance(notification, _AgentMessageDeltaNotification)
                        ):
                            yield AgentEvent(type="text", text=notification.delta)
                        elif (
                            _ErrorNotification is not None
                            and isinstance(notification, _ErrorNotification)
                        ):
                            yield AgentEvent(
                                type="error", text=str(notification.error)
                            )
                            return
                        elif (
                            _TurnCompletedNotification is not None
                            and isinstance(notification, _TurnCompletedNotification)
                        ):
                            yield AgentEvent(type="complete", cost_usd=0.0)
                            return
                    # Stream ended without an explicit completion notification
                    yield AgentEvent(type="complete", cost_usd=0.0)
                    return
            except Exception as exc:
                if attempt < 2 and _is_transient_transport_error(exc):
                    # Longer backoff for stdio transport crashes — the Codex CLI
                    # subprocess may need several seconds to restart fully.
                    await asyncio.sleep(5.0 * (2 ** attempt))
                    continue
                yield AgentEvent(type="error", text=str(exc))
                return
