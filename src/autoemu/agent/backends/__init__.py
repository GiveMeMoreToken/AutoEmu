"""Backend implementations for agent execution.

Use :func:`create_backend` to instantiate a backend by name.
"""

from __future__ import annotations

from autoemu.agent.backend import AgentBackend


def create_backend(name: str = "claude-sdk", **kwargs) -> AgentBackend:
    """Create an agent backend by name.

    Args:
        name: ``"claude-sdk"``, ``"codex-sdk"``,
            ``"anthropic-api"``, or ``"openai-api"``.
        **kwargs: Passed to the backend constructor.

    Returns:
        An :class:`AgentBackend` instance.

    Raises:
        ValueError: If the backend name is unknown.
        ImportError: If the required SDK is not installed.
    """
    if name == "claude-sdk":
        from autoemu.agent.backends.claude_backend import ClaudeSdkBackend
        return ClaudeSdkBackend(**kwargs)
    elif name == "codex-sdk":
        from autoemu.agent.backends.codex_backend import CodexSdkBackend
        return CodexSdkBackend(**kwargs)
    elif name == "anthropic-api":
        from autoemu.agent.backends.anthropic_api_backend import AnthropicApiBackend
        return AnthropicApiBackend(**kwargs)
    elif name == "openai-api":
        from autoemu.agent.backends.openai_api_backend import OpenAIApiBackend
        return OpenAIApiBackend(**kwargs)
    else:
        raise ValueError(
            "Unknown backend: "
            f"{name!r}. Choose 'claude-sdk', 'codex-sdk', "
            "'anthropic-api', or 'openai-api'."
        )


__all__ = ["create_backend", "AgentBackend"]
