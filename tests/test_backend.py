"""Tests for the backend abstraction layer."""

from __future__ import annotations

import json
import pytest

from agents.items import MessageOutputItem, ToolCallItem

from autoemu.agent.backend import AgentBackend, AgentEvent, ToolSpec
from autoemu.agent.backends import create_backend
from autoemu.agent.backends.claude_backend import ClaudeBackend
from autoemu.agent.backends.openai_backend import OpenAIAgentsBackend
from autoemu.agent.tools import ALL_TOOLS, TOOL_NAMES
from autoemu.agent.orchestrator import AutoEmuOrchestrator, ModelingTask, ModelingResult


# ------------------------------------------------------------------ ToolSpec

class TestToolSpec:
    def test_all_tools_are_toolspec(self):
        for t in ALL_TOOLS:
            assert isinstance(t, ToolSpec)

    @pytest.mark.parametrize("name", [
        "fetch_data", "extract_register_structure", "infer_state_machine",
        "infer_interrupt_model", "infer_dependency_graph",
        "generate_model_bundle", "run_model_pipeline",
    ])
    def test_key_tools_registered(self, name):
        assert name in TOOL_NAMES

    @pytest.mark.asyncio
    async def test_read_file_tool(self, tmp_path):
        tool = next(t for t in ALL_TOOLS if t.name == "read_file")
        f = tmp_path / "hello.txt"
        f.write_text("hello world")
        result = await tool.handler({"file_path": str(f)})
        assert "hello world" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_write_file_tool(self, tmp_path):
        tool = next(t for t in ALL_TOOLS if t.name == "write_file")
        f = tmp_path / "out.txt"
        result = await tool.handler({"file_path": str(f), "content": "test content"})
        assert not result.get("is_error")
        assert f.read_text() == "test content"

    @pytest.mark.asyncio
    async def test_list_files_tool(self, tmp_path):
        (tmp_path / "a.txt").touch()
        (tmp_path / "b.txt").touch()
        tool = next(t for t in ALL_TOOLS if t.name == "list_files")
        result = await tool.handler({"directory": str(tmp_path), "pattern": "*.txt"})
        text = result["content"][0]["text"]
        assert "a.txt" in text and "b.txt" in text


# ------------------------------------------------------------------ Factory

class TestCreateBackend:
    def test_create_claude(self):
        assert isinstance(create_backend("claude"), ClaudeBackend)

    def test_create_openai(self):
        assert isinstance(create_backend("openai"), OpenAIAgentsBackend)

    def test_create_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown backend"):
            create_backend("unknown")

    def test_both_are_agent_backend(self):
        assert isinstance(create_backend("claude"), AgentBackend)
        assert isinstance(create_backend("openai"), AgentBackend)


# ------------------------------------------------------------------ Orchestrator

class TestOrchestrator:
    def test_init_with_string_backend(self):
        o = AutoEmuOrchestrator(backend="claude")
        assert isinstance(o.backend, ClaudeBackend)

    def test_init_with_backend_instance(self):
        b = create_backend("openai")
        o = AutoEmuOrchestrator(backend=b)
        assert o.backend is b

    def test_modeling_task_defaults(self):
        t = ModelingTask(peripheral_name="DMA1")
        assert t.mcu_family == "STM32F4"
        assert len(t.phases) == 6


# -------------------------------------------------- Backend tool conversion

class TestBackendConversion:
    def test_toolspec_to_claude(self):
        from autoemu.agent.backends.claude_backend import _toolspec_to_claude

        async def dummy(args):
            return {"content": [{"type": "text", "text": "ok"}]}

        spec = ToolSpec(name="test", description="A test", parameters={"x": str}, handler=dummy)
        assert _toolspec_to_claude(spec) is not None

    def test_toolspec_to_openai(self):
        from autoemu.agent.backends.openai_backend import _toolspec_to_openai

        async def dummy(args):
            return {"content": [{"type": "text", "text": "ok"}]}

        spec = ToolSpec(name="test", description="A test", parameters={"x": str}, handler=dummy)
        ft = _toolspec_to_openai(spec)
        assert ft.name == "test"
        assert "x" in ft.params_json_schema["properties"]

    @pytest.mark.asyncio
    async def test_openai_tool_invoke(self):
        from autoemu.agent.backends.openai_backend import _toolspec_to_openai

        async def echo(args):
            return {"content": [{"type": "text", "text": f"got: {args.get('msg', '')}"}]}

        spec = ToolSpec(name="echo", description="Echo", parameters={"msg": str}, handler=echo)
        ft = _toolspec_to_openai(spec)
        result = await ft.on_invoke_tool(None, json.dumps({"msg": "hello"}))
        assert "got: hello" in result


# -------------------------------- OpenAI backend streaming (mock Runner)

class _FakeRawItem:
    def __init__(self, name="", arguments="", content=None):
        self.name = name
        self.arguments = arguments
        self.content = content or []


class _FakeContent:
    def __init__(self, text: str):
        self.text = text


class _FakeStreamingResult:
    """Minimal stub for RunResultStreaming."""

    def __init__(self, events):
        self._events = events
        self.raw_responses = []

    async def stream_events(self):
        for ev in self._events:
            yield ev


class _FakeRunItemEvent:
    type = "run_item_stream_event"

    def __init__(self, item):
        self.item = item


class _FakeOtherEvent:
    type = "raw_response_event"


class TestOpenAIBackendStreaming:
    """Tests that OpenAIAgentsBackend.run() emits correct AgentEvents."""

    def _make_msg_item(self, text: str) -> MessageOutputItem:
        raw = _FakeRawItem(content=[_FakeContent(text)])
        item = object.__new__(MessageOutputItem)
        item.raw_item = raw
        return item

    def _make_tool_item(self, name: str, args: str) -> ToolCallItem:
        raw = _FakeRawItem(name=name, arguments=args)
        item = object.__new__(ToolCallItem)
        item.raw_item = raw
        item.title = name
        return item

    @pytest.mark.asyncio
    async def test_emits_text_events(self, monkeypatch):
        from autoemu.agent.backends import openai_backend as mod

        msg_item = self._make_msg_item("hello from agent")
        fake_result = _FakeStreamingResult([
            _FakeRunItemEvent(msg_item),
        ])

        monkeypatch.setattr(mod.Runner, "run_streamed", lambda **kw: fake_result)

        backend = OpenAIAgentsBackend()
        events = []
        async for ev in backend.run("test", system_prompt="", tools=[], model="gpt-4o"):
            events.append(ev)

        text_events = [e for e in events if e.type == "text"]
        complete_events = [e for e in events if e.type == "complete"]
        assert any("hello from agent" in e.text for e in text_events)
        assert len(complete_events) == 1

    @pytest.mark.asyncio
    async def test_emits_tool_call_events(self, monkeypatch):
        from autoemu.agent.backends import openai_backend as mod

        tool_item = self._make_tool_item("read_file", '{"file_path": "/tmp/x"}')
        fake_result = _FakeStreamingResult([
            _FakeRunItemEvent(tool_item),
        ])

        monkeypatch.setattr(mod.Runner, "run_streamed", lambda **kw: fake_result)

        backend = OpenAIAgentsBackend()
        events = []
        async for ev in backend.run("test", system_prompt="", tools=[], model="gpt-4o"):
            events.append(ev)

        tool_events = [e for e in events if e.type == "tool_call"]
        assert len(tool_events) == 1
        assert tool_events[0].tool_name == "read_file"
        assert "file_path" in tool_events[0].tool_input

    @pytest.mark.asyncio
    async def test_skips_non_run_item_events(self, monkeypatch):
        from autoemu.agent.backends import openai_backend as mod

        fake_result = _FakeStreamingResult([_FakeOtherEvent()])
        monkeypatch.setattr(mod.Runner, "run_streamed", lambda **kw: fake_result)

        backend = OpenAIAgentsBackend()
        events = []
        async for ev in backend.run("test", system_prompt="", tools=[], model="gpt-4o"):
            events.append(ev)

        assert all(e.type != "text" for e in events)
        assert any(e.type == "complete" for e in events)

    @pytest.mark.asyncio
    async def test_handles_max_turns_exceeded(self, monkeypatch):
        from autoemu.agent.backends import openai_backend as mod
        from agents.exceptions import MaxTurnsExceeded

        async def _bad_stream():
            raise MaxTurnsExceeded("too many turns")
            yield  # make it a generator

        class _BadResult:
            raw_responses = []
            async def stream_events(self):
                raise MaxTurnsExceeded("too many turns")
                yield

        monkeypatch.setattr(mod.Runner, "run_streamed", lambda **kw: _BadResult())

        backend = OpenAIAgentsBackend()
        events = []
        async for ev in backend.run("test", system_prompt="", tools=[], model="gpt-4o"):
            events.append(ev)

        error_events = [e for e in events if e.type == "error"]
        assert len(error_events) == 1
        assert "turns" in error_events[0].text.lower()

    @pytest.mark.asyncio
    async def test_handles_generic_exception(self, monkeypatch):
        from autoemu.agent.backends import openai_backend as mod

        class _ErrorResult:
            raw_responses = []
            async def stream_events(self):
                raise ValueError("connection refused")
                yield

        monkeypatch.setattr(mod.Runner, "run_streamed", lambda **kw: _ErrorResult())

        backend = OpenAIAgentsBackend()
        events = []
        async for ev in backend.run("test", system_prompt="", tools=[], model="gpt-4o"):
            events.append(ev)

        error_events = [e for e in events if e.type == "error"]
        assert len(error_events) == 1
        assert "connection refused" in error_events[0].text
