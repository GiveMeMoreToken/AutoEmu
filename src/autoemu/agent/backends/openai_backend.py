"""OpenAI Agents SDK backend implementation."""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

from agents import Agent, FunctionTool, RunConfig, Runner
from agents.items import (
    MessageOutputItem,
    ToolCallItem,
    ToolCallOutputItem,
)

from autoemu.agent.backend import AgentBackend, AgentEvent, ToolSpec


def _toolspec_to_openai(spec: ToolSpec) -> FunctionTool:
    """Convert a ToolSpec into an openai-agents FunctionTool."""

    # Build a JSON Schema for the parameters.
    _type_map = {str: "string", int: "integer", float: "number", bool: "boolean"}
    properties: dict[str, Any] = {}
    for pname, ptype in spec.parameters.items():
        properties[pname] = {"type": _type_map.get(ptype, "string")}

    params_schema = {
        "type": "object",
        "properties": properties,
        "required": list(spec.parameters.keys()),
        "additionalProperties": False,
    }

    async def _invoke(ctx, input_json: str) -> str:
        """Adapter: parse JSON input, call handler, return text."""
        try:
            args = json.loads(input_json) if input_json else {}
        except json.JSONDecodeError:
            args = {}
        result = await spec.handler(args)
        # Extract text from the standard tool result format.
        content = result.get("content", [])
        texts = [c.get("text", "") for c in content if isinstance(c, dict)]
        return "\n".join(texts)

    return FunctionTool(
        name=spec.name,
        description=spec.description,
        params_json_schema=params_schema,
        on_invoke_tool=_invoke,
        strict_json_schema=False,
    )


class OpenAIAgentsBackend(AgentBackend):
    """Backend that delegates to the openai-agents SDK."""

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
        oai_tools = [_toolspec_to_openai(t) for t in (tools or [])]

        agent = Agent(
            name="autoemu",
            instructions=system_prompt or None,
            tools=oai_tools,
            model=model or self._extra.get("model"),
        )

        run_config = RunConfig(
            tracing_disabled=self._extra.get("tracing_disabled", True),
        )

        result = await Runner.run(
            starting_agent=agent,
            input=prompt,
            run_config=run_config,
            max_turns=self._extra.get("max_turns", 20),
        )

        # Emit events from the collected items.
        for item in result.new_items:
            if isinstance(item, MessageOutputItem):
                text = _extract_message_text(item)
                if text:
                    yield AgentEvent(type="text", text=text)
            elif isinstance(item, ToolCallItem):
                yield AgentEvent(
                    type="tool_call",
                    tool_name=item.raw_item.name if hasattr(item.raw_item, "name") else "",
                    tool_input=item.raw_item.arguments if hasattr(item.raw_item, "arguments") else "",
                )
            elif isinstance(item, ToolCallOutputItem):
                pass  # Tool results are internal; skip.

        # Final output.
        final = result.final_output
        if final and isinstance(final, str):
            yield AgentEvent(type="text", text=final)

        yield AgentEvent(type="complete", cost_usd=0)


def _extract_message_text(item: MessageOutputItem) -> str:
    """Pull plain text from a MessageOutputItem."""
    raw = item.raw_item
    if hasattr(raw, "content"):
        parts = []
        for part in raw.content:
            if hasattr(part, "text"):
                parts.append(part.text)
        return "\n".join(parts)
    return str(raw) if raw else ""
