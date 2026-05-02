# Agent Backend Types Design

## Goal

Keep AutoEmu's harness-first pipeline unchanged: local deterministic fetch/build/validate runs first, and an agent backend is only used as an optional fallback/enhancement. Replace the old agent backend names with four explicit backend types:

- `claude-sdk`
- `codex-sdk`
- `anthropic-api`
- `openai-api`

The old names `claude`, `codex`, and `openai` are removed from config, docs, tests, and UI labels.

## Architecture

`AgentBackend` remains the shared streaming contract for all agent fallbacks. The runtime still treats `harness` as a top-level mode and skips agent enhancement when `backend == "harness"`.

The backend factory registers only agent backends. It accepts the four new names and returns a concrete implementation:

- `claude-sdk` -> `ClaudeSdkBackend`
- `codex-sdk` -> `CodexSdkBackend`
- `anthropic-api` -> `AnthropicApiBackend`
- `openai-api` -> `OpenAIApiBackend`

SDK backends represent local agent runtimes. API backends represent provider-compatible HTTP APIs with local AutoEmu tool-loop execution.

## Backend Behavior

`ClaudeSdkBackend` uses `claude-agent-sdk` with the existing in-process MCP conversion for `ToolSpec` handlers. It keeps working-directory, model, system prompt, and budget support.

`CodexSdkBackend` uses `codex_app_server.AsyncCodex`. It starts a Codex thread with model, cwd, and developer instructions, runs the prompt, emits the final response, and completes. AutoEmu tools remain documented in the prompt because the Codex SDK path does not expose the same direct Python `ToolSpec` execution surface.

`AnthropicApiBackend` uses the Anthropic Messages API through the `anthropic` Python SDK. It converts `ToolSpec` into Anthropic tool schemas, sends messages, executes returned tool calls locally, appends tool results, and repeats until the model returns text or the turn limit is reached.

`OpenAIApiBackend` uses `openai.AsyncOpenAI` against OpenAI-compatible Chat Completions endpoints. It converts `ToolSpec` into function tools, executes returned tool calls locally, appends tool results, and repeats until the model returns text or the turn limit is reached. Chat Completions is used for compatibility with OpenAI-style proxy APIs.

## Configuration

`AgentRuntimeConfig.backend` defaults to `harness`. `SUPPORTED_AGENT_BACKENDS` contains:

- `harness`
- `claude-sdk`
- `codex-sdk`
- `anthropic-api`
- `openai-api`

Environment variables and `.autoemu.toml` keep the existing credential names:

- `ANTHROPIC_API_KEY`
- `ANTHROPIC_BASE_URL`
- `OPENAI_API_KEY`
- `OPENAI_BASE_URL`

## Testing

Tests cover:

- factory registration for the four new agent backend names;
- rejection of old names;
- runtime config accepting new names and rejecting old names;
- mocked execution for `codex-sdk`;
- mocked local tool-loop behavior for `anthropic-api` and `openai-api`;
- docs and TUI references no longer advertising old names.
