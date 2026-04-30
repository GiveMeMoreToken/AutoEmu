"""Backend implementations for agent execution.

Use :func:`create_backend` to instantiate a backend by name.
"""

from __future__ import annotations

from autoemu.agent.backend import AgentBackend


def create_backend(name: str = "claude", **kwargs) -> AgentBackend:
    """Create an agent backend by name.

    Args:
        name: ``"claude"``, ``"codex"``, or ``"openai"``.
        **kwargs: Passed to the backend constructor.

    Returns:
        An :class:`AgentBackend` instance.

    Raises:
        ValueError: If the backend name is unknown.
        ImportError: If the required SDK is not installed.
    """
    if name == "claude":
        from autoemu.agent.backends.claude_backend import ClaudeBackend
        return ClaudeBackend(**kwargs)
    elif name == "codex":
        from autoemu.agent.backends.codex_backend import CodexBackend
        return CodexBackend(**kwargs)
    elif name == "openai":
        from autoemu.agent.backends.openai_backend import OpenAIAgentsBackend
        return OpenAIAgentsBackend(**kwargs)
    else:
        raise ValueError(
            f"Unknown backend: {name!r}. Choose 'claude', 'codex', or 'openai'."
        )


__all__ = ["create_backend", "AgentBackend"]
