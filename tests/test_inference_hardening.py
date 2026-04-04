"""Tests for inference hardening -- each inference function must return a valid
model even when given empty, None, or malformed inputs."""

from __future__ import annotations

from autoemu.inference.state_machine_inference import infer_state_machine
from autoemu.inference.interrupt_inference import infer_interrupt_model
from autoemu.inference.dependency_inference import infer_dependency_graph
from autoemu.models.state_machine import StateMachine
from autoemu.models.interrupt import InterruptModel
from autoemu.models.dependency import DependencyGraph


# ---------------------------------------------------------------------------
# State machine inference
# ---------------------------------------------------------------------------


def test_infer_state_machine_empty_dict():
    result = infer_state_machine({})
    assert isinstance(result, StateMachine)
    assert any(s.is_initial for s in result.states)


def test_infer_state_machine_none():
    """None input should be caught by the try/except and return a fallback."""
    result = infer_state_machine(None)  # type: ignore[arg-type]
    assert isinstance(result, StateMachine)
    assert any(s.is_initial for s in result.states)


def test_infer_state_machine_minimal_analysis():
    analysis = {
        "peripheral_name": "SPI",
        "register_accesses": [],
        "init_sequences": [],
        "isr_patterns": [],
    }
    result = infer_state_machine(analysis)
    assert isinstance(result, StateMachine)
    assert result.name == "SPI_behavior"


# ---------------------------------------------------------------------------
# Interrupt inference
# ---------------------------------------------------------------------------


def test_infer_interrupt_model_empty_dict():
    result = infer_interrupt_model({})
    assert isinstance(result, InterruptModel)
    assert result.lines == []


def test_infer_interrupt_model_empty_dict_none_blocks():
    result = infer_interrupt_model({}, None)
    assert isinstance(result, InterruptModel)
    assert result.lines == []


def test_infer_interrupt_model_none():
    """None input should be caught by the try/except and return a fallback."""
    result = infer_interrupt_model(None)  # type: ignore[arg-type]
    assert isinstance(result, InterruptModel)
    assert result.lines == []


# ---------------------------------------------------------------------------
# Dependency inference
# ---------------------------------------------------------------------------


def test_infer_dependency_graph_empty_list():
    result = infer_dependency_graph([])
    assert isinstance(result, DependencyGraph)
    assert result.edges == []


def test_infer_dependency_graph_none():
    """None input should be caught and return an empty graph."""
    result = infer_dependency_graph(None)  # type: ignore[arg-type]
    assert isinstance(result, DependencyGraph)
    assert result.edges == []


def test_infer_dependency_graph_empty_dict():
    result = infer_dependency_graph({})
    assert isinstance(result, DependencyGraph)


def test_infer_dependency_graph_preserves_mcu_name():
    result = infer_dependency_graph([], mcu_name="STM32F4")
    assert result.mcu_name == "STM32F4"


def test_infer_dependency_graph_none_source_texts():
    result = infer_dependency_graph({}, source_texts=None)
    assert isinstance(result, DependencyGraph)
