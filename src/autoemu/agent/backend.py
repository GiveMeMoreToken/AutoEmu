"""Abstract backend interface for agent execution.

Defines backend-agnostic tool specifications and event types so that
the orchestrator can work with SDK and direct API backends.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Awaitable


@dataclass
class ToolSpec:
    """Backend-agnostic tool specification.

    Each tool has a name, description, parameter schema (name -> type),
    and an async handler that receives a dict of arguments and returns
    a dict with ``content`` (list of text blocks) and optional ``is_error``.
    """

    name: str
    description: str
    parameters: dict[str, type]
    handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class AgentEvent:
    """A single event emitted by the agent during execution.

    Attributes:
        type: One of ``"text"``, ``"tool_call"``, ``"complete"``, ``"error"``.
        text: Text content (for ``text``/``error`` events).
        tool_name: Name of the tool called (for ``tool_call`` events).
        tool_input: Serialised tool input (for ``tool_call`` events).
        cost_usd: Cumulative cost so far (for ``complete`` events).
    """

    type: str  # "text", "tool_call", "complete", "error"
    text: str = ""
    tool_name: str = ""
    tool_input: str = ""
    cost_usd: float = 0.0


class AgentBackend(ABC):
    """Abstract interface for an LLM agent backend.

    Concrete implementations wrap a specific SDK or provider-style API
    and expose a uniform streaming interface.
    """

    @abstractmethod
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
        """Run a prompt and yield agent events.

        Args:
            prompt: The user prompt to send.
            system_prompt: System instructions for the agent.
            tools: Tools the agent may invoke.
            model: Model identifier override.
            max_budget_usd: Spending cap (backends that support it).
            cwd: Working directory hint (backends that support it).

        Yields:
            AgentEvent instances as the agent works.
        """
        ...  # pragma: no cover
        # Make this an async generator so subclasses can yield.
        yield  # type: ignore[misc]

    async def run_to_text(
        self,
        prompt: str,
        *,
        system_prompt: str = "",
        tools: list[ToolSpec] | None = None,
        model: str | None = None,
        max_budget_usd: float = 5.0,
        cwd: str | None = None,
    ) -> str:
        """Convenience: run a prompt and collect all text into a string."""
        parts: list[str] = []
        async for event in self.run(
            prompt,
            system_prompt=system_prompt,
            tools=tools,
            model=model,
            max_budget_usd=max_budget_usd,
            cwd=cwd,
        ):
            if event.type == "text":
                parts.append(event.text)
            elif event.type == "error":
                parts.append(f"[error] {event.text}")
        return "\n".join(parts)
