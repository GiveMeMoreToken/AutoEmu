# Agent Backends Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a new `codex` backend using the Codex app-server Python SDK while preserving existing `claude` and `openai` behavior.

**Architecture:** Keep `AgentBackend` as the uniform runtime contract. Add `CodexBackend` as a separate backend implementation, register it by name, and extend runtime config/docs/tests so users can select it with `AUTOEMU_AGENT_BACKEND=codex` or `.autoemu.toml`.

**Tech Stack:** Python 3.11, pytest, `claude-agent-sdk`, `openai-agents`, `codex-app-server-sdk`.

---

## File Structure

- Create `src/autoemu/agent/backends/codex_backend.py`: wraps `codex_app_server.AsyncCodex` and emits `AgentEvent` objects.
- Modify `src/autoemu/agent/backends/__init__.py`: register backend name `codex`.
- Modify `src/autoemu/agent/runtime.py`: include `codex` in supported backend validation.
- Modify `tests/test_backend.py`: add factory and mocked streaming tests for `CodexBackend`.
- Modify `tests/test_runtime.py`: add runtime config acceptance test for `codex`.
- Modify `pyproject.toml`: add `codex-app-server-sdk` dependency.
- Modify `README.md`: document `codex` as a backend choice.

### Task 1: Backend Registration Tests

**Files:**
- Modify: `tests/test_backend.py`

- [ ] **Step 1: Write the failing tests**

```python
from autoemu.agent.backends.codex_backend import CodexBackend

def test_create_codex(self):
    assert isinstance(create_backend("codex"), CodexBackend)

def test_all_named_backends_are_agent_backend(self):
    assert isinstance(create_backend("claude"), AgentBackend)
    assert isinstance(create_backend("codex"), AgentBackend)
    assert isinstance(create_backend("openai"), AgentBackend)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_backend.py::TestCreateBackend -v`
Expected: FAIL because `autoemu.agent.backends.codex_backend` or backend name `codex` does not exist.

- [ ] **Step 3: Implement minimal registration**

Create `src/autoemu/agent/backends/codex_backend.py` with a minimal `CodexBackend(AgentBackend)` class, then update `create_backend()` to return it for `name == "codex"`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_backend.py::TestCreateBackend -v`
Expected: PASS.

### Task 2: Runtime Config Test

**Files:**
- Modify: `tests/test_runtime.py`
- Modify: `src/autoemu/agent/runtime.py`

- [ ] **Step 1: Write the failing test**

```python
def test_runtime_config_accepts_codex(monkeypatch):
    monkeypatch.setattr("autoemu.agent.runtime._load_config_file", lambda: {})
    monkeypatch.setenv("AUTOEMU_AGENT_BACKEND", "codex")

    config = AgentRuntimeConfig.load()

    assert config.backend == "codex"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_runtime.py::test_runtime_config_accepts_codex -v`
Expected: FAIL because `codex` is not in `SUPPORTED_AGENT_BACKENDS`.

- [ ] **Step 3: Implement minimal config change**

Change `SUPPORTED_AGENT_BACKENDS = {"harness", "claude", "openai"}` to include `"codex"`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_runtime.py::test_runtime_config_accepts_codex -v`
Expected: PASS.

### Task 3: Codex Backend Execution Test

**Files:**
- Modify: `tests/test_backend.py`
- Modify: `src/autoemu/agent/backends/codex_backend.py`

- [ ] **Step 1: Write the failing test**

```python
class TestCodexBackend:
    @pytest.mark.asyncio
    async def test_emits_text_and_complete_events(self, monkeypatch):
        from autoemu.agent.backends import codex_backend as mod

        class _FakeResult:
            final_response = "hello from codex"
            items = []
            usage = None

        class _FakeThread:
            async def run(self, prompt, **kwargs):
                return _FakeResult()

        class _FakeAsyncCodex:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def thread_start(self, **kwargs):
                return _FakeThread()

        monkeypatch.setattr(mod, "AsyncCodex", _FakeAsyncCodex)

        events = []
        async for ev in mod.CodexBackend().run(
            "Say hello",
            system_prompt="system",
            tools=[],
            model="gpt-5.4",
            cwd="/tmp",
        ):
            events.append(ev)

        assert [e.type for e in events] == ["text", "complete"]
        assert events[0].text == "hello from codex"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_backend.py::TestCodexBackend::test_emits_text_and_complete_events -v`
Expected: FAIL until `CodexBackend.run()` calls `AsyncCodex`, starts a thread, runs the prompt, and emits events.

- [ ] **Step 3: Implement minimal Codex backend**

Use `AsyncCodex` from `codex_app_server`. Start a thread with `model`, `cwd`, and `developer_instructions` when supported by the SDK call signature. Run a combined prompt that includes tool descriptions when tools are supplied. Emit a `text` event when `result.final_response` exists and always emit `complete`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_backend.py::TestCodexBackend::test_emits_text_and_complete_events -v`
Expected: PASS.

### Task 4: Dependency and Documentation

**Files:**
- Modify: `pyproject.toml`
- Modify: `README.md`

- [ ] **Step 1: Update dependency metadata**

Add `"codex-app-server-sdk>=0.2.0"` to `[project].dependencies`.

- [ ] **Step 2: Update README backend lists**

Change examples and tables so supported backend values are `harness`, `claude`, `codex`, and `openai`. State that `codex` uses the OpenAI Codex app-server SDK and `openai` uses OpenAI Agents SDK with OpenAI-compatible URL/key support.

- [ ] **Step 3: Run focused docs/config tests**

Run: `pytest tests/test_backend.py tests/test_runtime.py -v`
Expected: PASS.

### Task 5: Final Verification

**Files:**
- All changed files

- [ ] **Step 1: Run focused backend/runtime tests**

Run: `pytest tests/test_backend.py tests/test_runtime.py -v`
Expected: PASS.

- [ ] **Step 2: Run the full test suite**

Run: `pytest -q`
Expected: PASS, or document any unrelated environmental failure with exact output.

- [ ] **Step 3: Review changed files**

Run: `git diff --stat && git diff -- src/autoemu/agent/backends/__init__.py src/autoemu/agent/backends/codex_backend.py src/autoemu/agent/runtime.py tests/test_backend.py tests/test_runtime.py pyproject.toml README.md`
Expected: Diff only contains the approved backend/config/docs changes.
