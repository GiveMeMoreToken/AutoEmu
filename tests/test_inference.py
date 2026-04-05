"""Tests for inference functions (happy path + hardening)."""

from __future__ import annotations

from autoemu.inference.state_machine_inference import infer_state_machine
from autoemu.inference.interrupt_inference import infer_interrupt_model
from autoemu.inference.dependency_inference import infer_dependency_graph
from autoemu.models.state_machine import StateMachine
from autoemu.models.interrupt import InterruptModel
from autoemu.models.dependency import DependencyGraph
from autoemu.parsers.driver_parser import analyze_driver_string


DRIVER_SAMPLE = """\
HAL_StatusTypeDef HAL_UART_Init(UART_HandleTypeDef *huart)
{
    SET_BIT(huart->Instance->CR1, USART_CR1_UE);
    return HAL_OK;
}

void HAL_UART_Enable(UART_HandleTypeDef *huart)
{
    SET_BIT(huart->Instance->CR1, USART_CR1_TE);
}

HAL_StatusTypeDef HAL_UART_Transmit_IT(UART_HandleTypeDef *huart)
{
    SET_BIT(huart->Instance->CR1, USART_CR1_TXEIE);
    return HAL_OK;
}

void HAL_UART_IRQHandler(UART_HandleTypeDef *huart)
{
    if (__HAL_UART_GET_FLAG(huart, UART_FLAG_TC))
    {
        __HAL_UART_CLEAR_FLAG(huart, UART_FLAG_TC);
        HAL_UART_TxCpltCallback(huart);
    }
    if (__HAL_UART_GET_FLAG(huart, UART_FLAG_ORE))
    {
        __HAL_UART_CLEAR_FLAG(huart, UART_FLAG_ORE);
        HAL_UART_ErrorCallback(huart);
    }
}

void HAL_UART_Disable(UART_HandleTypeDef *huart)
{
    CLEAR_BIT(huart->Instance->CR1, USART_CR1_UE);
}
"""


# ---------------------------------------------------------------------------
# State machine inference
# ---------------------------------------------------------------------------

def test_infer_state_machine_from_driver():
    analysis = analyze_driver_string(DRIVER_SAMPLE, "UART")
    sm = infer_state_machine(analysis)

    states = {s.name for s in sm.states}
    assert {"reset", "ready", "enabled", "transmitting", "complete", "error", "disabled"} <= states
    assert sm.current_state == "reset"

    transitions = {(t.source, t.target, t.trigger) for t in sm.transitions}
    assert ("reset", "ready", "call:HAL_UART_Init") in transitions
    assert ("ready", "enabled", "call:HAL_UART_Enable") in transitions
    assert ("enabled", "transmitting", "call:HAL_UART_Transmit_IT") in transitions


def test_infer_state_machine_with_docs():
    analysis = analyze_driver_string(DRIVER_SAMPLE, "UART")
    docs = "The peripheral remains idle after initialization. During transmit, the hardware is busy sending data."
    sm = infer_state_machine(analysis, documentation_text=docs)
    descriptions = {s.name: s.description for s in sm.states}
    assert "idle after initialization" in descriptions["ready"].lower()


def test_infer_state_machine_empty_input():
    result = infer_state_machine({})
    assert isinstance(result, StateMachine)
    assert any(s.is_initial for s in result.states)


def test_infer_state_machine_none_input():
    result = infer_state_machine(None)  # type: ignore[arg-type]
    assert isinstance(result, StateMachine)


# ---------------------------------------------------------------------------
# Interrupt inference
# ---------------------------------------------------------------------------

def test_infer_interrupt_model_empty():
    result = infer_interrupt_model({})
    assert isinstance(result, InterruptModel)
    assert result.lines == []


def test_infer_interrupt_model_none():
    result = infer_interrupt_model(None)  # type: ignore[arg-type]
    assert isinstance(result, InterruptModel)


# ---------------------------------------------------------------------------
# Dependency inference
# ---------------------------------------------------------------------------

def test_infer_dependency_graph_empty():
    result = infer_dependency_graph([])
    assert isinstance(result, DependencyGraph)
    assert result.edges == []


def test_infer_dependency_graph_none():
    result = infer_dependency_graph(None)  # type: ignore[arg-type]
    assert isinstance(result, DependencyGraph)


def test_infer_dependency_graph_preserves_mcu_name():
    result = infer_dependency_graph([], mcu_name="STM32F4")
    assert result.mcu_name == "STM32F4"
