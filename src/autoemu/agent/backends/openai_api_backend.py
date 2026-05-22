"""OpenAI-compatible direct API backend implementation."""

from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator

from autoemu.agent.backend import AgentBackend, AgentEvent, ToolSpec

_AsyncOpenAI: Any
try:
    from openai import AsyncOpenAI as _AsyncOpenAI
except ImportError:  # pragma: no cover - exercised only without optional SDK
    _AsyncOpenAI = None

AsyncOpenAI: Any = _AsyncOpenAI

_DEFAULT_MODEL = "gpt-5.5"


def _json_type(ptype: type) -> str:
    type_map = {str: "string", int: "integer", float: "number", bool: "boolean"}
    return type_map.get(ptype, "string")


def _toolspec_to_openai(spec: ToolSpec) -> dict[str, Any]:
    """Convert a ToolSpec into an OpenAI Chat Completions function tool."""
    properties = {
        name: {"type": _json_type(ptype)}
        for name, ptype in spec.parameters.items()
    }
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(spec.parameters.keys()),
                "additionalProperties": False,
            },
        },
    }


def _tool_result_text(result: dict[str, Any]) -> str:
    content = result.get("content", [])
    texts = [
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    text = "\n".join(t for t in texts if t)
    if text:
        return text
    if result.get("is_error"):
        return "Tool returned an error without text output."
    return ""


def _tool_call_to_message(call: Any) -> dict[str, Any]:
    function = getattr(call, "function", None)
    return {
        "id": getattr(call, "id", ""),
        "type": getattr(call, "type", "function") or "function",
        "function": {
            "name": getattr(function, "name", ""),
            "arguments": getattr(function, "arguments", "") or "{}",
        },
    }


def _estimate_cost(usage: Any) -> float:
    if not usage:
        return 0.0
    prompt_tokens = getattr(usage, "prompt_tokens", 0) or getattr(usage, "input_tokens", 0)
    completion_tokens = (
        getattr(usage, "completion_tokens", 0) or getattr(usage, "output_tokens", 0)
    )
    return prompt_tokens * 0.0000005 + completion_tokens * 0.0000015


class OpenAIApiBackend(AgentBackend):
    """Backend that calls an OpenAI-compatible Chat Completions API directly."""

    def __init__(self, **kwargs: Any) -> None:
        self._extra = kwargs

    def _client_kwargs(self) -> dict[str, Any]:
        api_key = self._extra.get("api_key") or os.environ.get("OPENAI_API_KEY", "")
        base_url = self._extra.get("base_url") or os.environ.get("OPENAI_BASE_URL", "")
        kwargs: dict[str, Any] = {}
        if api_key:
            kwargs["api_key"] = api_key
        elif base_url:
            kwargs["api_key"] = "sk-placeholder"
        if base_url:
            kwargs["base_url"] = base_url
        return kwargs

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
        if AsyncOpenAI is None:
            yield AgentEvent(type="error", text="openai SDK is not installed")
            return

        tool_specs = {spec.name: spec for spec in (tools or [])}
        api_tools = [_toolspec_to_openai(spec) for spec in tool_specs.values()]
        messages: list[dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        try:
            client = AsyncOpenAI(**self._client_kwargs())
            cost_usd = 0.0
            max_turns = int(self._extra.get("max_turns", 20))

            for _ in range(max_turns):
                request: dict[str, Any] = {
                    "model": model or self._extra.get("model") or _DEFAULT_MODEL,
                    "messages": messages,
                }
                if api_tools:
                    request["tools"] = api_tools
                    request["tool_choice"] = "auto"

                response = await client.chat.completions.create(**request)
                cost_usd += _estimate_cost(getattr(response, "usage", None))
                choice = response.choices[0]
                message = choice.message
                tool_calls = list(getattr(message, "tool_calls", None) or [])

                if tool_calls:
                    messages.append({
                        "role": "assistant",
                        "content": getattr(message, "content", None),
                        "tool_calls": [_tool_call_to_message(call) for call in tool_calls],
                    })
                    for call in tool_calls:
                        function = getattr(call, "function", None)
                        name = getattr(function, "name", "")
                        raw_args = getattr(function, "arguments", "") or "{}"
                        yield AgentEvent(
                            type="tool_call",
                            tool_name=name,
                            tool_input=raw_args[:500],
                        )
                        try:
                            args = json.loads(raw_args)
                        except json.JSONDecodeError:
                            args = {}

                        spec = tool_specs.get(name)
                        if spec is None:
                            content = f"Unknown tool: {name}"
                        else:
                            content = _tool_result_text(await spec.handler(args))
                        messages.append({
                            "role": "tool",
                            "tool_call_id": getattr(call, "id", ""),
                            "content": content,
                        })
                    continue

                text = getattr(message, "content", "") or ""
                if text:
                    yield AgentEvent(type="text", text=text)
                yield AgentEvent(type="complete", cost_usd=cost_usd)
                return

            yield AgentEvent(type="error", text=f"Max turns exceeded: {max_turns}")
        except Exception as exc:
            yield AgentEvent(type="error", text=str(exc))


__all__ = ["OpenAIApiBackend", "_toolspec_to_openai"]
