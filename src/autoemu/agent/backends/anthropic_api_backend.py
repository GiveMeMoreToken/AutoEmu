"""Anthropic-compatible direct API backend implementation."""

from __future__ import annotations

import os
from typing import Any, AsyncIterator

try:
    from anthropic import AsyncAnthropic
except ImportError:  # pragma: no cover - exercised only without optional SDK
    AsyncAnthropic = None  # type: ignore[assignment]

from autoemu.agent.backend import AgentBackend, AgentEvent, ToolSpec

_DEFAULT_MODEL = "claude-sonnet-4-20250514"


def _json_type(ptype: type) -> str:
    type_map = {str: "string", int: "integer", float: "number", bool: "boolean"}
    return type_map.get(ptype, "string")


def _toolspec_to_anthropic(spec: ToolSpec) -> dict[str, Any]:
    """Convert a ToolSpec into an Anthropic Messages API tool schema."""
    properties = {
        name: {"type": _json_type(ptype)}
        for name, ptype in spec.parameters.items()
    }
    return {
        "name": spec.name,
        "description": spec.description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": list(spec.parameters.keys()),
            "additionalProperties": False,
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


def _content_block_to_dict(block: Any) -> dict[str, Any]:
    block_type = getattr(block, "type", "")
    if block_type == "text":
        return {"type": "text", "text": getattr(block, "text", "")}
    if block_type == "tool_use":
        return {
            "type": "tool_use",
            "id": getattr(block, "id", ""),
            "name": getattr(block, "name", ""),
            "input": getattr(block, "input", {}) or {},
        }
    return {"type": block_type or "unknown"}


def _estimate_cost(usage: Any) -> float:
    if not usage:
        return 0.0
    input_tokens = getattr(usage, "input_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or 0
    return input_tokens * 0.000003 + output_tokens * 0.000015


class AnthropicApiBackend(AgentBackend):
    """Backend that calls an Anthropic-compatible Messages API directly."""

    def __init__(self, **kwargs: Any) -> None:
        self._extra = kwargs

    def _client_kwargs(self) -> dict[str, Any]:
        api_key = self._extra.get("api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
        base_url = self._extra.get("base_url") or os.environ.get("ANTHROPIC_BASE_URL", "")
        kwargs: dict[str, Any] = {}
        if api_key:
            kwargs["api_key"] = api_key
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
        if AsyncAnthropic is None:
            yield AgentEvent(type="error", text="anthropic SDK is not installed")
            return

        tool_specs = {spec.name: spec for spec in (tools or [])}
        api_tools = [_toolspec_to_anthropic(spec) for spec in tool_specs.values()]
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]

        try:
            client = AsyncAnthropic(**self._client_kwargs())
            cost_usd = 0.0
            max_turns = int(self._extra.get("max_turns", 20))

            for _ in range(max_turns):
                request: dict[str, Any] = {
                    "model": model or self._extra.get("model") or _DEFAULT_MODEL,
                    "max_tokens": int(self._extra.get("max_tokens", 4096)),
                    "messages": messages,
                }
                if system_prompt:
                    request["system"] = system_prompt
                if api_tools:
                    request["tools"] = api_tools

                response = await client.messages.create(**request)
                cost_usd += _estimate_cost(getattr(response, "usage", None))
                content_blocks = list(getattr(response, "content", []) or [])
                tool_uses = [
                    block for block in content_blocks
                    if getattr(block, "type", "") == "tool_use"
                ]

                if tool_uses:
                    messages.append({
                        "role": "assistant",
                        "content": [_content_block_to_dict(block) for block in content_blocks],
                    })
                    tool_results: list[dict[str, Any]] = []
                    for block in tool_uses:
                        name = getattr(block, "name", "")
                        args = getattr(block, "input", {}) or {}
                        yield AgentEvent(
                            type="tool_call",
                            tool_name=name,
                            tool_input=str(args)[:500],
                        )

                        spec = tool_specs.get(name)
                        if spec is None:
                            content = f"Unknown tool: {name}"
                            is_error = True
                        else:
                            result = await spec.handler(args)
                            content = _tool_result_text(result)
                            is_error = bool(result.get("is_error"))
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": getattr(block, "id", ""),
                            "content": content,
                            "is_error": is_error,
                        })
                    messages.append({"role": "user", "content": tool_results})
                    continue

                text_parts = [
                    getattr(block, "text", "")
                    for block in content_blocks
                    if getattr(block, "type", "") == "text"
                ]
                text = "\n".join(part for part in text_parts if part)
                if text:
                    yield AgentEvent(type="text", text=text)
                yield AgentEvent(type="complete", cost_usd=cost_usd)
                return

            yield AgentEvent(type="error", text=f"Max turns exceeded: {max_turns}")
        except Exception as exc:
            yield AgentEvent(type="error", text=str(exc))


__all__ = ["AnthropicApiBackend", "_toolspec_to_anthropic"]
