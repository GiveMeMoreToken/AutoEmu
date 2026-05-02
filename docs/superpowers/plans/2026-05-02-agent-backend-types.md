# Agent Backend Types Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace old agent backend names with `claude-sdk`, `codex-sdk`, `anthropic-api`, and `openai-api` while keeping `harness` as the default deterministic first pass.

**Architecture:** Keep `AgentBackend` as the shared streaming interface. Rename SDK wrappers, add direct API wrappers with local tool-loop execution, update runtime config/UI/docs/tests, and remove old `claude`/`codex`/`openai` names.

**Tech Stack:** Python 3.11+, pytest, `claude-agent-sdk`, `codex-app-server-sdk`, `anthropic`, `openai`.

---

## File Structure

- Modify `src/autoemu/agent/backends/claude_backend.py`: rename class to `ClaudeSdkBackend`, keep alias-free SDK behavior.
- Modify `src/autoemu/agent/backends/codex_backend.py`: rename class to `CodexSdkBackend`, keep SDK execution behavior.
- Delete `src/autoemu/agent/backends/openai_backend.py`: replace old OpenAI Agents SDK backend.
- Create `src/autoemu/agent/backends/openai_api_backend.py`: direct OpenAI-compatible API backend with tool loop.
- Create `src/autoemu/agent/backends/anthropic_api_backend.py`: direct Anthropic-compatible API backend with tool loop.
- Modify `src/autoemu/agent/backends/__init__.py`: register four new backend names and reject old names.
- Modify `src/autoemu/agent/runtime.py`: allow `harness` plus four agent backend names.
- Modify `src/autoemu/tui/app.py`: expose new backend names in settings and label display.
- Modify `tests/test_backend.py`: update factory tests and add mocked API backend tests.
- Modify `tests/test_runtime.py`: update config validation tests.
- Modify `pyproject.toml`: add the `anthropic` dependency and remove `openai-agents`.
- Modify `README.md`: document the new backend names and harness-first strategy.

## Tasks

### Task 1: Lock Backend Names With Failing Tests

- [x] Update `tests/test_backend.py` imports and factory tests to expect `ClaudeSdkBackend`, `CodexSdkBackend`, `AnthropicApiBackend`, and `OpenAIApiBackend`.
- [x] Add parametrized rejection coverage for old names `claude`, `codex`, and `openai`.
- [x] Update `tests/test_runtime.py` to accept the four new agent names and reject old names.
- [x] Run `pytest tests/test_backend.py::TestCreateBackend tests/test_runtime.py::test_runtime_config_accepts_agent_backend -q`; expect failures before implementation.

### Task 2: Rename Existing SDK Backends

- [x] Rename `ClaudeBackend` to `ClaudeSdkBackend` in `claude_backend.py`.
- [x] Rename `CodexBackend` to `CodexSdkBackend` in `codex_backend.py`.
- [x] Update backend factory registration to `claude-sdk` and `codex-sdk`.
- [x] Update orchestrator tests to instantiate `claude-sdk`.
- [x] Run focused factory/runtime tests; expect SDK name tests to pass.

### Task 3: Add Direct API Backends

- [x] Write mocked Anthropic API backend test that returns a text response.
- [x] Write mocked Anthropic API backend test that requests an AutoEmu tool and receives tool result text.
- [x] Write mocked OpenAI API backend test that returns a text response.
- [x] Write mocked OpenAI API backend test that requests an AutoEmu tool and receives tool result text.
- [x] Implement `AnthropicApiBackend`.
- [x] Implement `OpenAIApiBackend`.
- [x] Run focused API backend tests; expect pass.

### Task 4: Update Runtime, UI, Dependencies, And Docs

- [x] Update `SUPPORTED_AGENT_BACKENDS`.
- [x] Update TUI select options and backend labels.
- [x] Update README config examples, backend table, dependency table, and project tree.
- [x] Add `anthropic>=0.66.0` and remove `openai-agents` from `pyproject.toml`.
- [x] Run `rg -n '"claude"|"codex"|"openai"|OpenAIAgentsBackend|ClaudeBackend|CodexBackend|openai-agents' src tests README.md pyproject.toml`; expect only credential/provider references and intentional model text.

### Task 5: Verification

- [x] Run `pytest tests/test_backend.py tests/test_runtime.py -q`.
- [x] Run `pytest -q`.
- [x] Review `git diff --stat` and the changed files.
