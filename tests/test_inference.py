"""Tests for automatic state-machine inference."""

from __future__ import annotations

import json

from autoemu.inference.state_machine_inference import infer_state_machine
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


def test_infer_state_machine_from_driver_analysis():
    analysis = analyze_driver_string(DRIVER_SAMPLE, "UART")
    sm = infer_state_machine(analysis)

    states = {state.name for state in sm.states}
    assert {"reset", "ready", "enabled", "transmitting", "complete", "error", "disabled"} <= states
    assert sm.current_state == "reset"

    transitions = {(t.source, t.target, t.trigger) for t in sm.transitions}
    assert ("reset", "ready", "call:HAL_UART_Init") in transitions
    assert ("ready", "enabled", "call:HAL_UART_Enable") in transitions
    assert ("enabled", "transmitting", "call:HAL_UART_Transmit_IT") in transitions
    assert ("transmitting", "complete", "callback:HAL_UART_TxCpltCallback") in transitions
    assert ("transmitting", "error", "callback:HAL_UART_ErrorCallback") in transitions


def test_infer_state_machine_uses_documentation_text():
    analysis = analyze_driver_string(DRIVER_SAMPLE, "UART")
    documentation = (
        "The peripheral remains idle after initialization. "
        "During transmit, the hardware is busy sending data. "
        "On transfer complete it returns to idle. "
        "An overrun condition places the peripheral in error state."
    )
    sm = infer_state_machine(analysis, documentation_text=documentation)

    descriptions = {state.name: state.description for state in sm.states}
    assert "idle after initialization" in descriptions["ready"].lower()
    assert "busy sending data" in descriptions["transmitting"].lower()
    assert "error state" in descriptions["error"].lower()
