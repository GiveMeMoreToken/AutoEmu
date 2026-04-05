"""Tests for the backend abstraction layer."""

from __future__ import annotations

import json
import pytest

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
