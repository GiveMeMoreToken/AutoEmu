"""Tests for the backend abstraction layer."""

from __future__ import annotations

import asyncio
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

    def test_tool_names_match(self):
        assert TOOL_NAMES == [t.name for t in ALL_TOOLS]

    def test_tool_count(self):
        assert len(ALL_TOOLS) == 23

    def test_fetch_tool_registered(self):
        assert "fetch_data" in TOOL_NAMES

    def test_register_extraction_tool_registered(self):
        assert "extract_register_structure" in TOOL_NAMES

    def test_state_machine_inference_tool_registered(self):
        assert "infer_state_machine" in TOOL_NAMES

    def test_interrupt_model_inference_tool_registered(self):
        assert "infer_interrupt_model" in TOOL_NAMES

    def test_dependency_graph_inference_tool_registered(self):
        assert "infer_dependency_graph" in TOOL_NAMES

    def test_model_bundle_generation_tool_registered(self):
        assert "generate_model_bundle" in TOOL_NAMES

    def test_pipeline_tool_registered(self):
        assert "run_model_pipeline" in TOOL_NAMES

    def test_tool_has_handler(self):
        for t in ALL_TOOLS:
            assert callable(t.handler)

    def test_tool_has_parameters(self):
        for t in ALL_TOOLS:
            assert isinstance(t.parameters, dict)
            for k, v in t.parameters.items():
                assert isinstance(k, str)
                assert v in (str, int, float, bool)

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
        assert "a.txt" in text
        assert "b.txt" in text


# ------------------------------------------------------------------ Factory

class TestCreateBackend:
    def test_create_claude(self):
        b = create_backend("claude")
        assert isinstance(b, ClaudeBackend)

    def test_create_openai(self):
        b = create_backend("openai")
        assert isinstance(b, OpenAIAgentsBackend)

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

    def test_init_with_openai_backend(self):
        o = AutoEmuOrchestrator(backend="openai")
        assert isinstance(o.backend, OpenAIAgentsBackend)

    def test_init_with_backend_instance(self):
        b = create_backend("openai")
        o = AutoEmuOrchestrator(backend=b)
        assert o.backend is b

    def test_modeling_task_defaults(self):
        t = ModelingTask(peripheral_name="DMA1")
        assert t.mcu_family == "STM32F4"
        assert t.output_dir == "output"
        assert len(t.phases) == 6

    def test_modeling_result_defaults(self):
        r = ModelingResult(peripheral_name="DMA1")
        assert not r.success
        assert r.total_cost_usd == 0.0
        assert r.error == ""


# -------------------------------------------------- Claude backend tool conversion

class TestClaudeBackendConversion:
    def test_toolspec_to_claude(self):
        from autoemu.agent.backends.claude_backend import _toolspec_to_claude

        async def dummy(args):
            return {"content": [{"type": "text", "text": "ok"}]}

        spec = ToolSpec(name="test", description="A test", parameters={"x": str}, handler=dummy)
        ct = _toolspec_to_claude(spec)
        # Should be some callable/tool object (exact type depends on SDK version)
        assert ct is not None


# -------------------------------------------------- OpenAI backend tool conversion

class TestOpenAIBackendConversion:
    def test_toolspec_to_openai(self):
        from autoemu.agent.backends.openai_backend import _toolspec_to_openai

        async def dummy(args):
            return {"content": [{"type": "text", "text": "ok"}]}

        spec = ToolSpec(name="test", description="A test", parameters={"x": str}, handler=dummy)
        ft = _toolspec_to_openai(spec)
        assert ft.name == "test"
        assert ft.description == "A test"
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


# -------------------------------------------------- AgentEvent

class TestAgentEvent:
    def test_text_event(self):
        e = AgentEvent(type="text", text="hello")
        assert e.type == "text"
        assert e.text == "hello"

    def test_tool_call_event(self):
        e = AgentEvent(type="tool_call", tool_name="parse_svd", tool_input='{"file_path": "x"}')
        assert e.tool_name == "parse_svd"

    def test_complete_event(self):
        e = AgentEvent(type="complete", cost_usd=0.5)
        assert e.cost_usd == 0.5
