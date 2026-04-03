"""Peripheral bundle generation and consistency verification helpers."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from autoemu.generators.qemu_generator import generate_peripheral_code
from autoemu.generators.test_generator import generate_test_harness
from autoemu.inference.dependency_inference import load_dependency_graph_json
from autoemu.modeling_utils import (
    is_non_mmio_register,
    load_register_blocks_json,
    load_wrapped_model_json,
    normalize_driver_analysis,
    normalize_register_blocks,
)
from autoemu.models.dependency import DependencyGraph, DependencyType
from autoemu.models.interrupt import InterruptModel
from autoemu.models.peripheral import ClockConfig, Peripheral, PeripheralType
from autoemu.models.register import BitField, Register, RegisterBlock
from autoemu.models.state_machine import StateMachine
from autoemu.parsers.driver_parser import DriverAnalysis, RegisterAccess, analyze_driver_file
from autoemu.validators.behavior_validator import validate_behavior
from autoemu.validators.register_validator import validate_register_block


def load_state_machine_json(path: str | Path) -> StateMachine:
    return load_wrapped_model_json(path, StateMachine)


def load_interrupt_model_json(path: str | Path) -> InterruptModel:
    return load_wrapped_model_json(path, InterruptModel)


def build_peripheral_from_models(
    peripheral_name: str,
    register_blocks: dict[str, RegisterBlock] | dict[str, Any],
    *,
    peripheral_type: PeripheralType | str | None = None,
    state_machine: StateMachine | dict[str, Any] | None = None,
    interrupt_model: InterruptModel | dict[str, Any] | None = None,
    dependencies: DependencyGraph | dict[str, Any] | None = None,
    mcu_family: str = "",
) -> Peripheral:
    """Assemble a complete peripheral model from step outputs."""
    blocks = normalize_register_blocks(register_blocks)
    merged_block = merge_register_blocks(blocks, peripheral_name)
    dep_graph = _normalize_dependency_graph(dependencies)
    clock = _derive_clock_config(dep_graph)

    state_machines = []
    if state_machine is not None:
        state_machines.append(_normalize_state_machine(state_machine))

    return Peripheral(
        name=peripheral_name,
        description=f"Auto-assembled peripheral model for {peripheral_name}",
        peripheral_type=_normalize_peripheral_type(peripheral_name, peripheral_type),
        base_address=merged_block.base_address,
        address_size=_infer_address_size(merged_block),
        mcu_family=mcu_family,
        register_block=merged_block,
        state_machines=state_machines,
        interrupt_model=_normalize_interrupt_model(interrupt_model),
        dependencies=dep_graph,
        clock=clock,
    )


def merge_register_blocks(
    register_blocks: dict[str, RegisterBlock] | dict[str, Any],
    peripheral_name: str,
) -> RegisterBlock:
    """Merge one or more register blocks into a single peripheral-local block."""
    blocks = normalize_register_blocks(register_blocks)
    if not blocks:
        return RegisterBlock(name=peripheral_name)

    base_address = min(block.base_address for block in blocks.values())
    registers: list[Register] = []
    for block_name, block in blocks.items():
        for reg in block.registers:
            reg_data = reg.model_dump()
            reg_data["offset"] = block.base_address + reg.offset - base_address
            registers.append(Register.model_validate(reg_data))

    registers.sort(key=lambda reg: reg.offset)
    description = f"Merged register blocks for {peripheral_name}: {', '.join(blocks)}"
    return RegisterBlock(
        name=peripheral_name,
        description=description,
        base_address=base_address,
        registers=registers,
    )


def verify_peripheral_consistency(
    peripheral: Peripheral,
    driver_analysis: DriverAnalysis | dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run register, behavior, and replay-based validation checks."""
    driver_data = normalize_driver_analysis(driver_analysis) if driver_analysis is not None else {}
    register_issues = validate_register_block(peripheral.register_block)
    behavior_issues = validate_behavior(peripheral, driver_data) if driver_data else []
    replay = replay_driver_accesses(
        peripheral,
        driver_data.get("register_accesses", []),
    ) if driver_data else {
        "applied_operations": 0,
        "skipped_operations": 0,
        "mismatches": [],
    }

    issue_count = len(register_issues) + len(behavior_issues) + len(replay["mismatches"])
    return {
        "success": issue_count == 0,
        "register_issues": register_issues,
        "behavior_issues": behavior_issues,
        "driver_replay": replay,
        "issue_count": issue_count,
    }


def replay_driver_accesses(
    peripheral: Peripheral,
    register_accesses: list[RegisterAccess | dict[str, Any]],
) -> dict[str, Any]:
    """Replay driver register accesses against the assembled model."""
    peripheral.reset()
    mismatches: list[dict[str, Any]] = []
    applied = 0
    skipped = 0

    for index, raw_access in enumerate(register_accesses):
        access = _normalize_register_access(raw_access)
        if is_non_mmio_register(access.register):
            skipped += 1
            continue
        register = peripheral.register_block.get_register(access.register)
        if register is None:
            mismatches.append({
                "step": index,
                "register": access.register,
                "reason": "missing_register",
            })
            continue

        offset = register.offset
        current = peripheral.get_register_value(offset)

        match access.access_type:
            case "read":
                peripheral.read_register(offset)
                applied += 1
            case "write":
                value = _resolve_value_expr(register, access.value_expr)
                if value is None:
                    skipped += 1
                    continue
                peripheral.write_register(offset, value)
                applied += 1
            case "set_bit":
                mask = _resolve_mask_expr(register, access.field or access.value_expr)
                if mask == 0:
                    skipped += 1
                    continue
                peripheral.write_register(offset, current | mask)
                applied += 1
            case "clear_bit":
                mask = _resolve_mask_expr(register, access.field or access.value_expr)
                if mask == 0:
                    skipped += 1
                    continue
                peripheral.write_register(offset, current & ~mask)
                applied += 1
            case "modify":
                mask_expr = access.field.removeprefix("mask=") if access.field.startswith("mask=") else access.field
                mask = _resolve_mask_expr(register, mask_expr)
                value = _resolve_value_expr(register, access.value_expr)
                if mask == 0 or value is None:
                    skipped += 1
                    continue
                new_value = (current & ~mask) | (value & mask)
                peripheral.write_register(offset, new_value)
                applied += 1
            case _:
                skipped += 1

    return {
        "applied_operations": applied,
        "skipped_operations": skipped,
        "mismatches": mismatches,
    }


def generate_model_bundle(
    peripheral_name: str,
    register_blocks: dict[str, RegisterBlock] | dict[str, Any],
    *,
    output_dir: str | Path,
    peripheral_type: PeripheralType | str | None = None,
    state_machine: StateMachine | dict[str, Any] | None = None,
    interrupt_model: InterruptModel | dict[str, Any] | None = None,
    dependencies: DependencyGraph | dict[str, Any] | None = None,
    driver_analysis: DriverAnalysis | dict[str, Any] | None = None,
    mcu_family: str = "",
) -> dict[str, Any]:
    """Build a peripheral model, generate artifacts, and verify consistency."""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    peripheral = build_peripheral_from_models(
        peripheral_name,
        register_blocks,
        peripheral_type=peripheral_type,
        state_machine=state_machine,
        interrupt_model=interrupt_model,
        dependencies=dependencies,
        mcu_family=mcu_family,
    )

    snake = _snake(peripheral_name)
    peripheral_json_path = output_path / f"{snake}_peripheral.json"
    peripheral_json_path.write_text(peripheral.model_dump_json(indent=2), encoding="utf-8")

    generated_files = [str(peripheral_json_path)]
    generated_files.extend(generate_peripheral_code(peripheral, output_path))
    generated_files.extend(generate_test_harness(peripheral, output_path))

    validation_report = verify_peripheral_consistency(peripheral, driver_analysis)
    validation_path = output_path / f"{snake}_validation.json"
    validation_path.write_text(json.dumps(validation_report, indent=2), encoding="utf-8")
    generated_files.append(str(validation_path))

    return {
        "peripheral": peripheral.model_dump(),
        "peripheral_json": str(peripheral_json_path),
        "generated_files": generated_files,
        "validation_report": validation_report,
        "validation_json": str(validation_path),
    }


def generate_model_bundle_from_files(
    peripheral_name: str,
    *,
    registers_path: str | Path,
    output_dir: str | Path,
    peripheral_type: PeripheralType | str | None = None,
    state_machine_path: str | Path | None = None,
    interrupt_model_path: str | Path | None = None,
    dependency_graph_path: str | Path | None = None,
    driver_path: str | Path | None = None,
    mcu_family: str = "",
) -> dict[str, Any]:
    """File-based wrapper for the step-5 generation pipeline."""
    register_blocks = load_register_blocks_json(registers_path)
    state_machine = load_state_machine_json(state_machine_path) if state_machine_path else None
    interrupt_model = load_interrupt_model_json(interrupt_model_path) if interrupt_model_path else None
    dependency_graph = load_dependency_graph_json(dependency_graph_path) if dependency_graph_path else None
    driver_analysis = analyze_driver_file(driver_path, peripheral_name) if driver_path else None

    return generate_model_bundle(
        peripheral_name,
        register_blocks,
        output_dir=output_dir,
        peripheral_type=peripheral_type,
        state_machine=state_machine,
        interrupt_model=interrupt_model,
        dependencies=dependency_graph,
        driver_analysis=driver_analysis,
        mcu_family=mcu_family,
    )


def _normalize_state_machine(
    state_machine: StateMachine | dict[str, Any],
) -> StateMachine:
    if isinstance(state_machine, StateMachine):
        return state_machine
    model = state_machine.get("model", state_machine)
    return StateMachine.model_validate(model)


def _normalize_interrupt_model(
    interrupt_model: InterruptModel | dict[str, Any] | None,
) -> InterruptModel | None:
    if interrupt_model is None:
        return None
    if isinstance(interrupt_model, InterruptModel):
        return interrupt_model
    model = interrupt_model.get("model", interrupt_model)
    return InterruptModel.model_validate(model)


def _normalize_dependency_graph(
    dependencies: DependencyGraph | dict[str, Any] | None,
) -> DependencyGraph | None:
    if dependencies is None:
        return None
    if isinstance(dependencies, DependencyGraph):
        return dependencies
    model = dependencies.get("model", dependencies)
    return DependencyGraph.model_validate(model)


def _normalize_register_access(
    access: RegisterAccess | dict[str, Any],
) -> RegisterAccess:
    if isinstance(access, RegisterAccess):
        return access
    return RegisterAccess(**access)


def _normalize_peripheral_type(
    peripheral_name: str,
    peripheral_type: PeripheralType | str | None,
) -> PeripheralType:
    if peripheral_type is None:
        upper = peripheral_name.upper()
        if upper.startswith("DMA"):
            return PeripheralType.DMA
        if "ETH" in upper:
            return PeripheralType.ETH
        if "USB" in upper or "OTG" in upper:
            return PeripheralType.USB
        if "SUBGHZ" in upper or "RADIO" in upper:
            return PeripheralType.RADIO
        return PeripheralType.GENERIC
    if isinstance(peripheral_type, PeripheralType):
        return peripheral_type
    return PeripheralType(peripheral_type)


def _derive_clock_config(dependencies: DependencyGraph | None) -> ClockConfig:
    if dependencies is None:
        return ClockConfig()
    clock_edges = dependencies.get_edges_by_type(DependencyType.CLOCK_GATE)
    if not clock_edges:
        return ClockConfig()
    edge = clock_edges[0]
    bus = ""
    bus_match = re.search(r"\b(AHB[1-4]?|APB[1-4]?)\b", edge.description)
    if bus_match:
        bus = bus_match.group(1)
    return ClockConfig(
        source=edge.source,
        bus=bus,
        enable_register=edge.config_registers[0] if edge.config_registers else "",
    )


def _infer_address_size(register_block: RegisterBlock) -> int:
    if not register_block.registers:
        return 0
    last_register = max(register_block.registers, key=lambda reg: reg.offset + (reg.size // 8))
    return last_register.offset + (last_register.size // 8)


def _resolve_value_expr(register: Register, expr: str) -> int | None:
    constant = _parse_int(expr)
    if constant is not None:
        return constant
    mask = _resolve_mask_expr(register, expr)
    if mask:
        return mask
    return None


def _resolve_mask_expr(register: Register, expr: str) -> int:
    constant = _parse_int(expr)
    if constant is not None:
        return constant

    result = 0
    tokens = re.findall(r"[A-Z][A-Z0-9_]+", expr.upper())
    for token in tokens:
        field_name = token.split("_")[-1]
        field_name = field_name.removesuffix("MSK").removesuffix("MASK").removesuffix("POS")
        field = register.get_field(field_name)
        if field is not None:
            result |= field.mask
    return result


def _parse_int(text: str) -> int | None:
    text = text.strip()
    if not text:
        return None
    candidate = re.sub(r"(?<=\d)[uUlL]+$", "", text)
    if re.fullmatch(r"0[xX][0-9A-Fa-f]+", candidate):
        return int(candidate, 16)
    if re.fullmatch(r"\d+", candidate):
        return int(candidate, 10)
    return None


def _snake(name: str) -> str:
    result = []
    for index, char in enumerate(name):
        if char.isupper() and index > 0 and not name[index - 1].isupper():
            result.append("_")
        result.append(char.lower())
    return "".join(result).replace("__", "_")
