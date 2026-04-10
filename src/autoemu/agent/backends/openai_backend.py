"""OpenAI Agents SDK backend implementation."""

from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator

import openai
from agents import Agent, FunctionTool, OpenAIChatCompletionsModel, RunConfig, Runner
from agents.exceptions import AgentsException, MaxTurnsExceeded
from agents.items import (
    MessageOutputItem,
    ReasoningItem,
    ToolCallItem,
    ToolCallOutputItem,
)
from agents.stream_events import RunItemStreamEvent

from autoemu.agent.backend import AgentBackend, AgentEvent, ToolSpec

# The default OpenAI base URL — used to detect when a custom proxy is configured.
_OPENAI_DEFAULT_BASE = "https://api.openai.com/v1"


def _toolspec_to_openai(spec: ToolSpec) -> FunctionTool:
    """Convert a ToolSpec into an openai-agents FunctionTool."""

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

    async def _invoke(ctx: Any, input_json: str) -> str:
        """Adapter: parse JSON input, call handler, return text."""
        try:
            args = json.loads(input_json) if input_json else {}
        except json.JSONDecodeError:
            args = {}
        result = await spec.handler(args)
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


def _extract_message_text(item: MessageOutputItem) -> str:
    """Pull plain text from a MessageOutputItem."""
    raw = item.raw_item
    if hasattr(raw, "content"):
        parts: list[str] = []
        for part in raw.content:
            if hasattr(part, "text"):
                parts.append(part.text)
        return "\n".join(parts)
    return str(raw) if raw else ""


def _extract_tool_name_args(item: ToolCallItem) -> tuple[str, str]:
    """Extract tool name and arguments from a ToolCallItem."""
    raw = item.raw_item
    name = getattr(raw, "name", "") or (item.title or "")
    args = getattr(raw, "arguments", "")
    if not isinstance(args, str):
        try:
            args = json.dumps(args)
        except Exception:
            args = str(args)
    return name, args


def _estimate_cost(raw_responses: list) -> float:
    """Estimate total cost from raw model responses (very rough)."""
    total_input = 0
    total_output = 0
    for resp in raw_responses:
        usage = getattr(resp, "usage", None)
        if usage:
            total_input += getattr(usage, "input_tokens", 0)
            total_output += getattr(usage, "output_tokens", 0)
    return total_input * 0.0000005 + total_output * 0.0000015


def _build_model(model_name: str | None) -> "str | OpenAIChatCompletionsModel | None":
    """Return an appropriate model object.

    When a custom OPENAI_BASE_URL is configured (i.e. a third-party proxy),
    the openai-agents SDK must use OpenAIChatCompletionsModel because the
    default OpenAIResponsesModel targets the Responses API endpoint
    (``/v1/responses``) which most proxies do not implement.

    With the official OpenAI API the string model name is returned unchanged so
    the SDK can use its default Responses API path.
    """
    api_key = os.environ.get("OPENAI_API_KEY", "")
    base_url = os.environ.get("OPENAI_BASE_URL", "").rstrip("/")

    using_proxy = bool(base_url) and base_url != _OPENAI_DEFAULT_BASE.rstrip("/")

    if not using_proxy:
        return model_name  # let the SDK handle it (Responses API)

    # Build a chat-completions client pointed at the custom proxy.
    client = openai.AsyncOpenAI(
        api_key=api_key or "sk-placeholder",
        base_url=base_url,
    )
    return OpenAIChatCompletionsModel(
        model=model_name or "gpt-4o",
        openai_client=client,
    )


class OpenAIAgentsBackend(AgentBackend):
    """Backend that delegates to the openai-agents SDK.

    Automatically selects ``OpenAIChatCompletionsModel`` when a custom
    ``OPENAI_BASE_URL`` is set, because most third-party proxies only support
    the Chat Completions API and not the OpenAI Responses API.

    Uses ``Runner.run_streamed()`` so events are emitted as the agent works.
    """

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

        resolved_model = _build_model(model or self._extra.get("model"))

        agent = Agent(
            name="autoemu",
            instructions=system_prompt or None,
            tools=oai_tools,
            model=resolved_model,
        )

        run_config = RunConfig(
            tracing_disabled=self._extra.get("tracing_disabled", True),
        )

        got_any_event = False
        try:
            result_streaming = Runner.run_streamed(
                starting_agent=agent,
                input=prompt,
                run_config=run_config,
                max_turns=self._extra.get("max_turns", 20),
            )

            async for event in result_streaming.stream_events():
                if event.type != "run_item_stream_event":
                    continue

                item = event.item

                if isinstance(item, MessageOutputItem):
                    text = _extract_message_text(item)
                    if text:
                        got_any_event = True
                        yield AgentEvent(type="text", text=text)

                elif isinstance(item, ReasoningItem):
                    raw = item.raw_item
                    summary = getattr(raw, "summary", None)
                    if summary:
                        parts = [
                            getattr(s, "text", str(s))
                            for s in (summary if isinstance(summary, list) else [summary])
                        ]
                        text = " ".join(parts)
                        if text:
                            got_any_event = True
                            yield AgentEvent(type="text", text=f"[thinking] {text}")

                elif isinstance(item, ToolCallItem):
                    name, args = _extract_tool_name_args(item)
                    got_any_event = True
                    yield AgentEvent(
                        type="tool_call",
                        tool_name=name,
                        tool_input=args[:500],
                    )

            cost_usd = _estimate_cost(getattr(result_streaming, "raw_responses", []))

            if not got_any_event:
                # The agent returned no output — surface this as a warning so
                # the runtime can log it rather than silently claiming success.
                yield AgentEvent(
                    type="text",
                    text="[agent produced no output — API may be unavailable or model unsupported]",
                )

            yield AgentEvent(type="complete", cost_usd=cost_usd)

        except MaxTurnsExceeded as exc:
            yield AgentEvent(type="error", text=f"Max turns exceeded: {exc}")
        except AgentsException as exc:
            yield AgentEvent(type="error", text=f"Agent error: {exc}")
        except Exception as exc:
            yield AgentEvent(type="error", text=str(exc))
