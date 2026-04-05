"""Tests for step-5 bundle generation and validation."""

from __future__ import annotations

from pathlib import Path

from autoemu.generators.bundle_generator import (
    build_peripheral_from_models,
    generate_model_bundle,
    verify_peripheral_consistency,
)
from autoemu.inference.dependency_inference import infer_dependency_graph_from_driver_text
from autoemu.inference.interrupt_inference import infer_interrupt_model
from autoemu.models.dependency import DependencyType
from autoemu.models.register import AccessType, BitField, Register, RegisterBlock
from autoemu.parsers.driver_parser import analyze_driver_string


DRIVER_SAMPLE = """\
HAL_StatusTypeDef HAL_USART_Init(UART_HandleTypeDef *huart)
{
    SET_BIT(huart->Instance->CR1, USART_CR1_UE);
    return HAL_OK;
}

void HAL_USART_IRQHandler(UART_HandleTypeDef *huart)
{
    if (__HAL_USART_GET_FLAG(huart, USART_FLAG_TC))
    {
        if (__HAL_USART_GET_IT_SOURCE(huart, USART_IT_TC))
        {
            __HAL_USART_CLEAR_FLAG(huart, USART_FLAG_TC);
            HAL_USART_TxCpltCallback(huart);
        }
    }
}
"""


def _make_registers() -> dict[str, RegisterBlock]:
    return {
        "USART1": RegisterBlock(
            name="USART1",
            base_address=0x40011000,
            registers=[
                Register(
                    name="CR1",
                    offset=0x00,
                    reset_value=0x00,
                    fields=[
                        BitField(name="UE", bit_offset=0, bit_width=1, access=AccessType.RW),
                        BitField(name="TCIE", bit_offset=6, bit_width=1, access=AccessType.RW),
                    ],
                ),
                Register(
                    name="SR",
                    offset=0x04,
                    reset_value=0x00,
                    fields=[
                        BitField(name="TC", bit_offset=6, bit_width=1, access=AccessType.W1C),
                    ],
                ),
            ],
        )
    }


def test_build_peripheral_from_models_attaches_dependencies():
    analysis = analyze_driver_string(DRIVER_SAMPLE, "USART1")
    dependencies = infer_dependency_graph_from_driver_text(
        "__HAL_RCC_USART1_CLK_ENABLE();",
        peripheral_name="USART1",
        mcu_name="STM32F4",
    )
    peripheral = build_peripheral_from_models(
        "USART1",
        _make_registers(),
        state_machine={
            "name": "USART1_SM",
            "states": [
                {"name": "idle", "is_initial": True, "is_final": False},
                {"name": "enabled", "is_initial": False, "is_final": False},
            ],
            "transitions": [
                {
                    "source": "idle",
                    "target": "enabled",
                    "trigger": "reg_write:CR1",
                    "condition": "",
                    "action": "",
                }
            ],
        },
        interrupt_model=infer_interrupt_model(analysis, _make_registers(), peripheral_name="USART1"),
        dependencies=dependencies,
        mcu_family="STM32F4",
    )

    assert peripheral.dependencies is not None
    assert peripheral.clock.source == "RCC"
    assert any(edge.dep_type == DependencyType.CLOCK_GATE for edge in peripheral.dependencies.edges)


def test_generate_model_bundle_writes_artifacts_and_validation(tmp_path):
    analysis = analyze_driver_string(DRIVER_SAMPLE, "USART1")
    interrupt_model = infer_interrupt_model(analysis, _make_registers(), peripheral_name="USART1")
    dependencies = infer_dependency_graph_from_driver_text(
        "__HAL_RCC_USART1_CLK_ENABLE();",
        peripheral_name="USART1",
        mcu_name="STM32F4",
    )

    result = generate_model_bundle(
        "USART1",
        _make_registers(),
        output_dir=tmp_path,
        interrupt_model=interrupt_model,
        dependencies=dependencies,
        driver_analysis=analysis,
        mcu_family="STM32F4",
    )

    generated_names = {Path(path).name for path in result["generated_files"]}
    assert "usart1_peripheral.json" in generated_names
    assert "stm32f4_usart1.c" in generated_names
    assert "stm32f4_usart1.h" in generated_names
    assert "test_stm32f4_usart1.c" in generated_names
    assert "usart1_validation.json" in generated_names
    assert result["validation_report"]["success"]
    assert result["validation_report"]["driver_replay"]["applied_operations"] >= 1


def test_verify_peripheral_consistency_ignores_descriptor_pseudo_registers():
    peripheral = build_peripheral_from_models(
        "ETH",
        {
            "ETH": RegisterBlock(
                name="ETH",
                base_address=0x40028000,
                registers=[
                    Register(name="DMASR", offset=0x14, reset_value=0),
                ],
            )
        },
    )

    report = verify_peripheral_consistency(
        peripheral,
        {
            "register_accesses": [
                {"register": "DESC0", "access_type": "write", "value_expr": "0", "field": ""},
                {"register": "DMASR", "access_type": "read", "value_expr": "", "field": ""},
            ],
            "isr_patterns": [],
            "init_sequences": [],
        },
    )

    assert report["driver_replay"]["mismatches"] == []
    assert report["behavior_issues"] == []
