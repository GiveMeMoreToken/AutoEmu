"""Tests for the backend abstraction layer."""

from __future__ import annotations

from enum import Enum
import json
from pathlib import Path

import pytest

from autoemu.agent.backend import ToolSpec
from autoemu.agent.backends import create_backend
from autoemu.agent.backends.anthropic_api_backend import AnthropicApiBackend
from autoemu.agent.backends.claude_backend import ClaudeSdkBackend
from autoemu.agent.backends.codex_backend import CodexSdkBackend
from autoemu.agent.backends.openai_api_backend import OpenAIApiBackend
from autoemu.agent.prompts import QEMU_GENERATION_PROMPT, SYSTEM_PROMPT, build_system_prompt
from autoemu.agent.tools import ALL_TOOLS, TOOL_NAMES
from autoemu.agent.orchestrator import AutoEmuOrchestrator, ModelingTask


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


class TestPrompts:
    def test_prompts_target_latest_qemu_not_v924(self):
        prompt = SYSTEM_PROMPT + "\n" + QEMU_GENERATION_PROMPT

        assert "latest upstream QEMU" in prompt
        assert "9.2.4" not in prompt
        assert "v9.2.4" not in prompt

    def test_assembled_prompt_does_not_reintroduce_v924_from_constraints(self):
        prompt = build_system_prompt(mode="model", cwd=str(Path.cwd()))

        assert "latest upstream QEMU" in prompt
        assert "9.2.4" not in prompt
        assert "v9.2.4" not in prompt


# ------------------------------------------------------------------ Factory

class TestCreateBackend:
    @pytest.mark.parametrize(
        ("name", "klass"),
        [
            ("claude-sdk", ClaudeSdkBackend),
            ("codex-sdk", CodexSdkBackend),
            ("anthropic-api", AnthropicApiBackend),
            ("openai-api", OpenAIApiBackend),
        ],
    )
    def test_create_named_backend(self, name, klass):
        assert isinstance(create_backend(name), klass)

    def test_create_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown backend"):
            create_backend("unknown")

    @pytest.mark.parametrize("old_name", ["claude", "codex", "openai"])
    def test_old_backend_names_are_rejected(self, old_name):
        with pytest.raises(ValueError, match="Unknown backend"):
            create_backend(old_name)

# ------------------------------------------------------------------ Orchestrator

class TestOrchestrator:
    def test_init_with_string_backend(self):
        o = AutoEmuOrchestrator(backend="claude-sdk")
        assert isinstance(o.backend, ClaudeSdkBackend)

    def test_init_with_backend_instance(self):
        b = create_backend("openai-api")
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
        from autoemu.agent.backends.openai_api_backend import _toolspec_to_openai

        async def dummy(args):
            return {"content": [{"type": "text", "text": "ok"}]}

        spec = ToolSpec(name="test", description="A test", parameters={"x": str}, handler=dummy)
        tool = _toolspec_to_openai(spec)
        assert tool["type"] == "function"
        assert tool["function"]["name"] == "test"
        assert "x" in tool["function"]["parameters"]["properties"]

    def test_toolspec_to_anthropic(self):
        from autoemu.agent.backends.anthropic_api_backend import _toolspec_to_anthropic

        async def dummy(args):
            return {"content": [{"type": "text", "text": "ok"}]}

        spec = ToolSpec(name="test", description="A test", parameters={"x": str}, handler=dummy)
        tool = _toolspec_to_anthropic(spec)
        assert tool["name"] == "test"
        assert "x" in tool["input_schema"]["properties"]


class TestCodexSdkBackend:
    """Tests that CodexSdkBackend.run() emits AutoEmu AgentEvents."""

    def test_patches_legacy_service_tier_enum_to_accept_new_sdk_values(self, monkeypatch):
        from autoemu.agent.backends import codex_backend as mod

        class _FakeServiceTier(Enum):
            fast = "fast"
            flex = "flex"

        monkeypatch.setattr(mod, "ServiceTier", _FakeServiceTier, raising=False)

        with pytest.raises(ValueError):
            _FakeServiceTier("priority")

        mod._patch_codex_service_tier_enum()

        assert _FakeServiceTier("priority") is _FakeServiceTier.fast

    @pytest.mark.asyncio
    async def test_emits_text_and_complete_events(self, monkeypatch):
        from autoemu.agent.backends import codex_backend as mod

        captured: dict[str, object] = {}

        class _FakeResult:
            final_response = "hello from codex"
            items = []
            usage = None

        class _FakeThread:
            async def run(self, prompt, **kwargs):
                captured["prompt"] = prompt
                captured["run_kwargs"] = kwargs
                return _FakeResult()

        class _FakeAsyncCodex:
            def __init__(self, config=None):
                captured["config"] = config

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def thread_start(self, **kwargs):
                captured["thread_kwargs"] = kwargs
                return _FakeThread()

        monkeypatch.setattr(mod, "AsyncCodex", _FakeAsyncCodex)

        events = []
        async for ev in mod.CodexSdkBackend().run(
            "Say hello",
            system_prompt="system",
            tools=[],
            model="gpt-5.4",
            cwd="/tmp",
        ):
            events.append(ev)

        assert [e.type for e in events] == ["text", "complete"]
        assert events[0].text == "hello from codex"
        assert captured["prompt"] == "Say hello"
        assert captured["thread_kwargs"] == {
            "model": "gpt-5.4",
            "cwd": "/tmp",
            "developer_instructions": "system",
            "sandbox": "danger-full-access",
            "approval_policy": "never",
            "service_tier": "fast",
        }

    @pytest.mark.asyncio
    async def test_uses_system_codex_binary_when_sdk_runtime_package_is_absent(self, monkeypatch):
        from autoemu.agent.backends import codex_backend as mod

        captured: dict[str, object] = {}

        class _FakeConfig:
            def __init__(self, *, codex_bin=None, config_overrides=()):
                self.codex_bin = codex_bin
                self.config_overrides = config_overrides

        class _FakeResult:
            final_response = ""

        class _FakeThread:
            async def run(self, prompt, **kwargs):
                return _FakeResult()

        class _FakeAsyncCodex:
            def __init__(self, config=None):
                captured["config"] = config

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def thread_start(self, **kwargs):
                return _FakeThread()

        monkeypatch.setattr(mod, "AsyncCodex", _FakeAsyncCodex)
        monkeypatch.setattr(mod, "AppServerConfig", _FakeConfig, raising=False)
        monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/codex" if name == "codex" else None)

        events = []
        async for ev in mod.CodexSdkBackend().run("Say hello", tools=[]):
            events.append(ev)

        assert [e.type for e in events] == ["complete"]
        assert captured["config"].codex_bin == "/usr/local/bin/codex"
        assert captured["config"].config_overrides == (
            'sandbox_mode="danger-full-access"',
            'approval_policy="never"',
        )

    @pytest.mark.asyncio
    async def test_supports_new_codex_app_server_sdk_client_api(self, monkeypatch):
        from autoemu.agent.backends import codex_backend as mod

        captured: dict[str, object] = {}

        class _FakeThreadConfig:
            def __init__(self, **kwargs):
                captured["thread_config"] = kwargs

        class _FakeResult:
            final_text = "hello from new sdk"

        class _FakeClient:
            @classmethod
            def connect_stdio(cls, **kwargs):
                captured["connect_kwargs"] = kwargs
                return cls()

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def chat_once(self, prompt, **kwargs):
                captured["prompt"] = prompt
                captured["chat_kwargs"] = kwargs
                return _FakeResult()

        monkeypatch.setattr(mod, "AsyncCodex", None)
        monkeypatch.setattr(mod, "CodexClient", _FakeClient, raising=False)
        monkeypatch.setattr(mod, "ThreadConfig", _FakeThreadConfig, raising=False)
        monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/codex" if name == "codex" else None)

        events = []
        async for ev in mod.CodexSdkBackend().run(
            "Say hello",
            system_prompt="system",
            tools=[],
            model="gpt-5.5",
            cwd="output",
        ):
            events.append(ev)

        expected_cwd = str(Path("output").resolve())
        assert [e.type for e in events] == ["text", "complete"]
        assert events[0].text == "hello from new sdk"
        assert captured["connect_kwargs"] == {
            "command": [
                "/usr/local/bin/codex",
                "--config",
                'sandbox_mode="danger-full-access"',
                "--config",
                'approval_policy="never"',
                "app-server",
            ],
            "cwd": expected_cwd,
        }
        assert captured["thread_config"] == {
            "cwd": expected_cwd,
            "developer_instructions": "system",
            "model": "gpt-5.5",
            "sandbox": "danger-full-access",
            "approval_policy": "never",
            "service_tier": "fast",
        }
        assert captured["chat_kwargs"]["thread_config"] is not None


class _FakeFunction:
    def __init__(self, name: str, arguments: str):
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    type = "function"

    def __init__(self, name: str, arguments: str, call_id: str = "call_1"):
        self.id = call_id
        self.function = _FakeFunction(name, arguments)


class _FakeOpenAIMessage:
    def __init__(self, content: str = "", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class _FakeOpenAIChoice:
    def __init__(self, message: _FakeOpenAIMessage):
        self.message = message


class _FakeUsage:
    input_tokens = 0
    output_tokens = 0
    prompt_tokens = 0
    completion_tokens = 0


class _FakeOpenAIResponse:
    def __init__(self, message: _FakeOpenAIMessage):
        self.choices = [_FakeOpenAIChoice(message)]
        self.usage = _FakeUsage()


class _FakeOpenAICompletions:
    def __init__(self, responses, captured):
        self._responses = list(responses)
        self._captured = captured

    async def create(self, **kwargs):
        self._captured.append(kwargs)
        return self._responses.pop(0)


class _FakeOpenAIChat:
    def __init__(self, responses, captured):
        self.completions = _FakeOpenAICompletions(responses, captured)


class _FakeOpenAIClient:
    def __init__(self, responses, captured):
        self.chat = _FakeOpenAIChat(responses, captured)


class TestOpenAIApiBackend:
    @pytest.mark.asyncio
    async def test_emits_text_and_complete_events(self, monkeypatch):
        from autoemu.agent.backends import openai_api_backend as mod

        captured = []
        responses = [_FakeOpenAIResponse(_FakeOpenAIMessage(content="hello from api"))]
        monkeypatch.setattr(
            mod,
            "AsyncOpenAI",
            lambda **kwargs: _FakeOpenAIClient(responses, captured),
        )

        events = []
        async for ev in mod.OpenAIApiBackend().run(
            "Say hello",
            system_prompt="system",
            tools=[],
            model="gpt-test",
        ):
            events.append(ev)

        assert [e.type for e in events] == ["text", "complete"]
        assert events[0].text == "hello from api"
        assert captured[0]["model"] == "gpt-test"
        assert captured[0]["messages"][0] == {"role": "system", "content": "system"}

    @pytest.mark.asyncio
    async def test_executes_tool_calls(self, monkeypatch):
        from autoemu.agent.backends import openai_api_backend as mod

        async def echo(args):
            return {"content": [{"type": "text", "text": f"tool saw {args['msg']}"}]}

        spec = ToolSpec(name="echo", description="Echo", parameters={"msg": str}, handler=echo)
        captured = []
        responses = [
            _FakeOpenAIResponse(
                _FakeOpenAIMessage(
                    tool_calls=[_FakeToolCall("echo", json.dumps({"msg": "hi"}))]
                )
            ),
            _FakeOpenAIResponse(_FakeOpenAIMessage(content="final answer")),
        ]
        monkeypatch.setattr(
            mod,
            "AsyncOpenAI",
            lambda **kwargs: _FakeOpenAIClient(responses, captured),
        )

        events = []
        async for ev in mod.OpenAIApiBackend().run("Use tool", tools=[spec], model="gpt-test"):
            events.append(ev)

        assert [e.type for e in events] == ["tool_call", "text", "complete"]
        assert events[0].tool_name == "echo"
        assert captured[0]["tools"][0]["function"]["name"] == "echo"
        second_messages = captured[1]["messages"]
        assert any(m.get("role") == "tool" and "tool saw hi" in m.get("content", "") for m in second_messages)


class _FakeAnthropicText:
    type = "text"

    def __init__(self, text: str):
        self.text = text


class _FakeAnthropicToolUse:
    type = "tool_use"

    def __init__(self, name: str, input_: dict, block_id: str = "toolu_1"):
        self.id = block_id
        self.name = name
        self.input = input_


class _FakeAnthropicUsage:
    input_tokens = 0
    output_tokens = 0


class _FakeAnthropicResponse:
    def __init__(self, content):
        self.content = content
        self.usage = _FakeAnthropicUsage()


class _FakeAnthropicMessages:
    def __init__(self, responses, captured):
        self._responses = list(responses)
        self._captured = captured

    async def create(self, **kwargs):
        self._captured.append(kwargs)
        return self._responses.pop(0)


class _FakeAnthropicClient:
    def __init__(self, responses, captured):
        self.messages = _FakeAnthropicMessages(responses, captured)


class TestAnthropicApiBackend:
    @pytest.mark.asyncio
    async def test_emits_text_and_complete_events(self, monkeypatch):
        from autoemu.agent.backends import anthropic_api_backend as mod

        captured = []
        responses = [_FakeAnthropicResponse([_FakeAnthropicText("hello from anthropic")])]
        monkeypatch.setattr(
            mod,
            "AsyncAnthropic",
            lambda **kwargs: _FakeAnthropicClient(responses, captured),
        )

        events = []
        async for ev in mod.AnthropicApiBackend().run(
            "Say hello",
            system_prompt="system",
            tools=[],
            model="claude-test",
        ):
            events.append(ev)

        assert [e.type for e in events] == ["text", "complete"]
        assert events[0].text == "hello from anthropic"
        assert captured[0]["model"] == "claude-test"
        assert captured[0]["system"] == "system"

    @pytest.mark.asyncio
    async def test_executes_tool_calls(self, monkeypatch):
        from autoemu.agent.backends import anthropic_api_backend as mod

        async def echo(args):
            return {"content": [{"type": "text", "text": f"tool saw {args['msg']}"}]}

        spec = ToolSpec(name="echo", description="Echo", parameters={"msg": str}, handler=echo)
        captured = []
        responses = [
            _FakeAnthropicResponse([_FakeAnthropicToolUse("echo", {"msg": "hi"})]),
            _FakeAnthropicResponse([_FakeAnthropicText("final answer")]),
        ]
        monkeypatch.setattr(
            mod,
            "AsyncAnthropic",
            lambda **kwargs: _FakeAnthropicClient(responses, captured),
        )

        events = []
        async for ev in mod.AnthropicApiBackend().run("Use tool", tools=[spec], model="claude-test"):
            events.append(ev)

        assert [e.type for e in events] == ["tool_call", "text", "complete"]
        assert events[0].tool_name == "echo"
        assert captured[0]["tools"][0]["name"] == "echo"
        second_messages = captured[1]["messages"]
        assert any(
            block.get("type") == "tool_result" and "tool saw hi" in block.get("content", "")
            for message in second_messages
            for block in (message.get("content") if isinstance(message.get("content"), list) else [])
        )
