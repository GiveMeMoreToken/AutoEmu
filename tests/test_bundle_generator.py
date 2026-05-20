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
    generated_paths = {Path(path).relative_to(tmp_path).as_posix() for path in result["generated_files"]}
    assert "usart1_peripheral.json" in generated_names
    assert "usart1_qemu_hardware.json" in generated_names
    assert "stm32f4_usart1.c" in generated_names
    assert "stm32f4_usart1.h" in generated_names
    assert "test_stm32f4_usart1.c" in generated_names
    assert "usart1_validation.json" in generated_names
    assert Path(result["qemu_hardware_json"]).name == "usart1_qemu_hardware.json"
    assert Path(result["qemu_hardware_json"]).exists()
    assert "qemu_tree_files" in result
    assert set(result["qemu_tree_files"]).issubset(set(result["generated_files"]))
    assert "hw/misc/stm32f4_usart1.c" in generated_paths
    assert "include/hw/misc/stm32f4_usart1.h" in generated_paths
    assert "tests/qtest/stm32f4_usart1-test.c" in generated_paths
    qemu_hardware = Path(result["qemu_hardware_json"]).read_text(encoding="utf-8")
    assert '"qom_type": "stm32f4-usart1"' in qemu_hardware
    assert result["validation_report"]["success"]
    assert result["validation_report"]["driver_replay"]["applied_operations"] >= 1


def test_verify_peripheral_consistency_rejects_empty_hardware_structure():
    peripheral = build_peripheral_from_models(
        "EMPTY",
        {"EMPTY": RegisterBlock(name="EMPTY", base_address=0x40010000, registers=[])},
        mcu_family="DemoSoC",
    )

    report = verify_peripheral_consistency(peripheral)

    assert report["success"] is False
    assert report["issue_count"] >= 1
    assert any(issue["severity"] == "error" for issue in report["register_issues"])
    hardware_messages = " ".join(issue["message"].lower() for issue in report["hardware_issues"])
    assert "mmio" in hardware_messages
    assert "size" in hardware_messages
    assert "register_count" in hardware_messages


def test_generate_model_bundle_reports_empty_tree_artifacts(monkeypatch, tmp_path):
    def fake_tree_artifacts(peripheral, output_dir, **kwargs):
        empty_tree_source = Path(output_dir) / "hw" / "misc" / "empty_device.c"
        empty_tree_source.parent.mkdir(parents=True, exist_ok=True)
        empty_tree_source.write_text("", encoding="utf-8")
        return [str(empty_tree_source)]

    monkeypatch.setattr(
        "autoemu.generators.bundle_generator.generate_qemu_tree_artifacts",
        fake_tree_artifacts,
    )

    result = generate_model_bundle("USART1", _make_registers(), output_dir=tmp_path)

    assert result["validation_report"]["success"] is False
    assert any(
        issue["severity"] == "error"
        and issue.get("path", "").endswith("hw/misc/empty_device.c")
        and "empty" in issue["message"].lower()
        for issue in result["validation_report"]["artifact_issues"]
    )


def test_generate_model_bundle_fails_validation_when_compile_fails(monkeypatch, tmp_path):
    compile_result = {
        "success": False,
        "files_checked": 1,
        "errors": [
            {
                "file": "hw/misc/stm32f4_usart1.c",
                "returncode": 1,
                "stderr": "syntax error",
            }
        ],
        "warnings": ["one warning"],
    }

    monkeypatch.setattr(
        "autoemu.generators.bundle_generator.validate_compile",
        lambda source_files: compile_result,
    )

    result = generate_model_bundle("USART1", _make_registers(), output_dir=tmp_path)

    report = result["validation_report"]
    assert report["success"] is False
    assert report["issue_count"] == len(compile_result["errors"])
    assert report["compile_validation"] == compile_result


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
