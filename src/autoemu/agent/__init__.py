"""Agent orchestration for automated peripheral modeling.

Provides a backend-agnostic interface supporting claude-agent-sdk,
codex-app-server-sdk, and openai-agents SDKs.
"""

from autoemu.agent.backend import AgentBackend, AgentEvent, ToolSpec
from autoemu.agent.backends import create_backend
from autoemu.agent.orchestrator import AutoEmuOrchestrator, ModelingTask, ModelingResult
from autoemu.agent.runtime import AgentRuntimeConfig, AutoEmuAgentRuntime

__all__ = [
    "AgentBackend",
    "AgentEvent",
    "ToolSpec",
    "create_backend",
    "AutoEmuOrchestrator",
    "ModelingTask",
    "ModelingResult",
    "AgentRuntimeConfig",
    "AutoEmuAgentRuntime",
]
