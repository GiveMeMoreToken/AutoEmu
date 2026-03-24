"""Claude Agent SDK backend implementation."""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
    tool as claude_tool,
)

from autoemu.agent.backend import AgentBackend, AgentEvent, ToolSpec


def _toolspec_to_claude(spec: ToolSpec):
    """Convert a ToolSpec into a claude-agent-sdk MCP tool."""
    return claude_tool(
        spec.name,
        spec.description,
        {k: v for k, v in spec.parameters.items()},
    )(spec.handler)


class ClaudeBackend(AgentBackend):
    """Backend that delegates to claude-agent-sdk's ``query()``."""

    def __init__(self, **kwargs: Any) -> None:
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
        claude_tools = [_toolspec_to_claude(t) for t in (tools or [])]
        tool_names = [t.name for t in (tools or [])]

        mcp_server = create_sdk_mcp_server(
            "autoemu",
            version="0.1.0",
            tools=claude_tools,
        )

        opts = ClaudeAgentOptions(
            system_prompt=system_prompt,
            mcp_servers={"autoemu": mcp_server},
            allowed_tools=tool_names,
            permission_mode="bypassPermissions",
            max_budget_usd=max_budget_usd,
        )
        if model:
            opts.model = model
        if cwd:
            opts.cwd = cwd

        async for msg in query(prompt=prompt, options=opts):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        yield AgentEvent(type="text", text=block.text)
                    elif isinstance(block, ToolUseBlock):
                        yield AgentEvent(
                            type="tool_call",
                            tool_name=block.name,
                            tool_input=json.dumps(block.input)[:500],
                        )
            elif isinstance(msg, ResultMessage):
                if msg.is_error:
                    yield AgentEvent(
                        type="error",
                        text=msg.result or "Unknown error",
                        cost_usd=msg.total_cost_usd or 0,
                    )
                yield AgentEvent(
                    type="complete",
                    cost_usd=msg.total_cost_usd or 0,
                )
