"""OpenAI Codex app-server SDK backend implementation."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, AsyncIterator

from autoemu.agent.backend import AgentBackend, AgentEvent, ToolSpec

_AsyncCodex: Any
_AppServerConfig: Any
_ServiceTier: Any
_CodexClient: Any
_ThreadConfig: Any
try:
    from codex_app_server import AsyncCodex as _AsyncCodex
    from codex_app_server import AppServerConfig as _AppServerConfig
    from codex_app_server import ServiceTier as _ServiceTier
except ImportError:  # pragma: no cover - exercised only without optional SDK
    _AsyncCodex = None
    _AppServerConfig = None
    _ServiceTier = None
try:
    from codex_app_server_sdk import CodexClient as _CodexClient
    from codex_app_server_sdk import ThreadConfig as _ThreadConfig
except ImportError:  # pragma: no cover - depends on installed SDK version
    try:
        from codex_app_server_client import CodexClient as _CodexClient
        from codex_app_server_client import ThreadConfig as _ThreadConfig
    except ImportError:  # pragma: no cover - depends on installed SDK version
        _CodexClient = None
        _ThreadConfig = None

AsyncCodex: Any = _AsyncCodex
AppServerConfig: Any = _AppServerConfig
ServiceTier: Any = _ServiceTier
CodexClient: Any = _CodexClient
ThreadConfig: Any = _ThreadConfig


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


def _codex_config(extra: dict[str, Any]) -> Any | None:
    """Build old SDK config that can use a locally installed Codex CLI binary."""
    if AppServerConfig is None:
        return None

    codex_bin = _codex_bin(extra)
    return AppServerConfig(
        codex_bin=codex_bin or None,
        config_overrides=_codex_config_overrides(extra),
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
        if AsyncCodex is None and CodexClient is None:
            yield AgentEvent(
                type="error",
                text="codex-app-server-sdk is not installed",
            )
            return

        full_prompt = prompt + _tool_reference(tools or [])
        resolved_cwd = _resolve_cwd(cwd)

        if AsyncCodex is None:
            async for event in self._run_new_sdk(
                full_prompt,
                system_prompt=system_prompt,
                model=model,
                cwd=resolved_cwd,
            ):
                yield event
            return

        thread_kwargs: dict[str, Any] = {
            "model": model or self._extra.get("model"),
            "cwd": resolved_cwd,
            "developer_instructions": system_prompt or None,
            "sandbox": _codex_sandbox(self._extra),
            "approval_policy": _codex_approval_policy(self._extra),
            "service_tier": _codex_service_tier(self._extra),
        }
        thread_kwargs = {k: v for k, v in thread_kwargs.items() if v is not None}

        try:
            _patch_codex_service_tier_enum()
            config = _codex_config(self._extra)
            codex_kwargs = {"config": config} if config is not None else {}
            async with AsyncCodex(**codex_kwargs) as codex:
                thread = await codex.thread_start(**thread_kwargs)
                result = await thread.run(full_prompt)

            if result.final_response:
                yield AgentEvent(type="text", text=result.final_response)

            yield AgentEvent(type="complete", cost_usd=0.0)
        except Exception as exc:
            yield AgentEvent(type="error", text=str(exc))

    async def _run_new_sdk(
        self,
        prompt: str,
        *,
        system_prompt: str = "",
        model: str | None = None,
        cwd: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Run using codex-app-server-sdk >=0.3.x (CodexClient API)."""
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
        except Exception as exc:
            yield AgentEvent(type="error", text=str(exc))
