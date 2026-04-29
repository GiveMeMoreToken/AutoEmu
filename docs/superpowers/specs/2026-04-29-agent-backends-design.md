# Agent Backends Design

## Goal

AutoEmu should support four agent backend choices:

- `harness`: deterministic local pipeline only.
- `claude`: Anthropic Claude Agent SDK backend.
- `codex`: OpenAI Codex app-server Python SDK backend.
- `openai`: OpenAI Agents SDK backend for OpenAI-compatible URL/key use.

The existing `openai` behavior remains intact. `codex` is a new backend name so users can choose Codex without breaking OpenAI Agents SDK users.

## Architecture

The existing `AgentBackend` abstraction remains the shared contract. A new `CodexBackend` implements that contract in `src/autoemu/agent/backends/codex_backend.py` using `codex_app_server.AsyncCodex`.

`create_backend()` registers `codex`. `AgentRuntimeConfig` accepts `codex` through `AUTOEMU_AGENT_BACKEND` and `.autoemu.toml`.

## Backend Behavior

`ClaudeBackend` continues to use `claude-agent-sdk` and in-process MCP tools.

`OpenAIAgentsBackend` continues to use `openai-agents`. When `OPENAI_BASE_URL` is a custom endpoint, it uses `OpenAIChatCompletionsModel` with an `openai.AsyncOpenAI` client so OpenAI-compatible proxies work.

`CodexBackend` uses the Codex app-server SDK. It creates an async Codex client, starts a thread with the requested model and working directory, runs the prompt, emits the final response as a text event, and emits a complete event. The current public Codex quick path does not expose the same AutoEmu `ToolSpec` execution API as the Claude MCP and OpenAI Agents paths, so native tool execution remains on `claude` and `openai`.

## Configuration

`.autoemu.toml` and environment variables continue to support Anthropic-style and OpenAI-style API credentials:

- `ANTHROPIC_API_KEY`
- `ANTHROPIC_BASE_URL`
- `OPENAI_API_KEY`
- `OPENAI_BASE_URL`

`AUTOEMU_AGENT_BACKEND` accepts `harness`, `claude`, `codex`, or `openai`.

## Testing

Tests should cover:

- backend factory registration for `codex`;
- runtime config validation accepting `codex`;
- a mocked `CodexBackend.run()` that emits text and complete events;
- README/config documentation listing the new backend.
