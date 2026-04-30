"""OpenAI Codex app-server SDK backend implementation."""

from __future__ import annotations

from typing import AsyncIterator

try:
    from codex_app_server import AsyncCodex
except ImportError:  # pragma: no cover - exercised only without optional SDK
    AsyncCodex = None  # type: ignore[assignment]

from autoemu.agent.backend import AgentBackend, AgentEvent, ToolSpec


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


class CodexBackend(AgentBackend):
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
        if AsyncCodex is None:
            yield AgentEvent(
                type="error",
                text="codex-app-server-sdk is not installed",
            )
            return

        full_prompt = prompt + _tool_reference(tools or [])
        thread_kwargs = {
            "model": model or self._extra.get("model"),
            "cwd": cwd,
            "developer_instructions": system_prompt or None,
        }
        thread_kwargs = {k: v for k, v in thread_kwargs.items() if v is not None}

        try:
            async with AsyncCodex() as codex:
                thread = await codex.thread_start(**thread_kwargs)
                result = await thread.run(full_prompt)

            if result.final_response:
                yield AgentEvent(type="text", text=result.final_response)

            yield AgentEvent(type="complete", cost_usd=0.0)
        except Exception as exc:
            yield AgentEvent(type="error", text=str(exc))
