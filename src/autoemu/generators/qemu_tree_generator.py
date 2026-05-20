"""Generate QEMU tree-style hardware integration artifacts."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from autoemu.generators.qemu_generator import (
    QEMU_TARGET_VERSION,
    _generate_header,
    _generate_qtest,
    _generate_source,
)
from autoemu.modeling_utils import normalize_name, snake_case, upper_case
from autoemu.models.peripheral import Peripheral
from autoemu.models.qemu import QEMUHardwareModel, build_qemu_hardware_model


def generate_qemu_tree_artifacts(
    peripheral: Peripheral,
    output_dir: str | Path,
    hardware_model: QEMUHardwareModel | None = None,
    driver_analysis: Any = None,
) -> list[str]:
    """Generate QEMU tree-relative artifacts for a peripheral hardware model."""
    model = hardware_model or build_qemu_hardware_model(peripheral, driver_analysis)
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    source_path = _tree_path(output_path, model.file_layout.source_path)
    header_path = _tree_path(output_path, model.file_layout.header_path)
    meson_path = _tree_path(
        output_path,
        model.file_layout.meson_snippet_path or model.file_layout.meson_path,
    )
    qtest_path = _tree_path(output_path, model.file_layout.qtest_path)
    kconfig_path = _tree_path(output_path, _derive_kconfig_path(model))
    dts_path = _tree_path(output_path, _derive_device_tree_path(model))

    source_path.write_text(_tree_source(peripheral, model), encoding="utf-8")
    header_path.write_text(_tree_header(peripheral, model), encoding="utf-8")
    meson_path.write_text(_generate_meson_snippet(model), encoding="utf-8")
    kconfig_path.write_text(_generate_kconfig_snippet(model), encoding="utf-8")
    qtest_path.write_text(_tree_qtest(peripheral, model), encoding="utf-8")
    dts_path.write_text(_generate_device_tree_snippet(model), encoding="utf-8")

    return [
        str(source_path),
        str(header_path),
        str(meson_path),
        str(kconfig_path),
        str(qtest_path),
        str(dts_path),
    ]


def _tree_path(output_dir: Path, tree_relative_path: str) -> Path:
    path = Path(tree_relative_path)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"QEMU tree artifact path must be relative: {tree_relative_path}")
    resolved = output_dir / path
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def _tree_source(peripheral: Peripheral, model: QEMUHardwareModel) -> str:
    source = _rewrite_generated_identity(_generate_source(peripheral), peripheral, model)
    qemu_header = _qemu_include_path(model.file_layout.header_path)
    include_lines = [line for line in source.splitlines() if line.startswith('#include "hw/')]
    if not include_lines:
        return source
    return source.replace(include_lines[0], f'#include "{qemu_header}"', 1)


def _tree_header(peripheral: Peripheral, model: QEMUHardwareModel) -> str:
    return _rewrite_generated_identity(_generate_header(peripheral), peripheral, model)


def _tree_qtest(peripheral: Peripheral, model: QEMUHardwareModel) -> str:
    return _rewrite_generated_identity(_generate_qtest(peripheral), peripheral, model)


def _rewrite_generated_identity(
    content: str,
    peripheral: Peripheral,
    model: QEMUHardwareModel,
) -> str:
    generated_identity = build_qemu_hardware_model(peripheral).identity
    replacements = _identity_replacements(generated_identity, model.identity)
    if not replacements:
        return content
    replacement_map: dict[str, str] = {}
    for old_value, new_value in replacements:
        replacement_map.setdefault(old_value, new_value)
    pattern = re.compile("|".join(re.escape(old_value) for old_value, _ in replacements))
    return pattern.sub(lambda match: replacement_map[match.group(0)], content)


def _identity_replacements(old: Any, new: Any) -> list[tuple[str, str]]:
    old_peripheral_snake = normalize_name(snake_case(old.peripheral_name))
    new_peripheral_snake = normalize_name(snake_case(new.peripheral_name))
    old_peripheral_upper = upper_case(old.peripheral_name)
    new_peripheral_upper = upper_case(new.peripheral_name)
    old_display_prefix = _display_prefix(old.c_identifier_prefix, old_peripheral_snake)
    old_qtest_path = f"/{old.qom_type.replace('-', '/')}"
    new_qtest_path = f"/{new.qom_type.replace('-', '/')}"

    pairs = [
        (old_qtest_path, new_qtest_path),
        (
            f"{old_display_prefix.upper()} {old.peripheral_name}",
            f"{new.kconfig_symbol} {new.peripheral_name}",
        ),
        (old.state_struct_name, new.state_struct_name),
        (old.type_macro, new.type_macro),
        (old.kconfig_symbol, new.kconfig_symbol),
        (old.c_identifier_prefix, new.c_identifier_prefix),
        (old.qom_type, new.qom_type),
        (old_peripheral_upper, new_peripheral_upper),
        (old.peripheral_name, new.peripheral_name),
        (old_peripheral_snake, new_peripheral_snake),
    ]
    return sorted(
        [(old_value, new_value) for old_value, new_value in pairs if old_value != new_value],
        key=lambda pair: len(pair[0]),
        reverse=True,
    )


def _display_prefix(c_identifier_prefix: str, peripheral_snake: str) -> str:
    suffix = f"_{peripheral_snake}"
    if c_identifier_prefix.endswith(suffix):
        return c_identifier_prefix[: -len(suffix)]
    return c_identifier_prefix


def _qemu_include_path(header_path: str) -> str:
    parts = Path(header_path).parts
    if parts and parts[0] == "include":
        return str(Path(*parts[1:]))
    return header_path


def _generate_meson_snippet(model: QEMUHardwareModel) -> str:
    source_basename = Path(model.file_layout.source_path).name
    symbol = model.identity.kconfig_symbol
    return "\n".join(
        [
            f"# AutoEmu QEMU hardware model for {model.identity.qom_type}",
            f"# Target: QEMU {QEMU_TARGET_VERSION}",
            f"system_ss.add(when: 'CONFIG_{symbol}', if_true: files('{source_basename}'))",
            "",
        ]
    )


def _generate_kconfig_snippet(model: QEMUHardwareModel) -> str:
    source_basename = Path(model.file_layout.source_path).name
    symbol = model.identity.kconfig_symbol
    return "\n".join(
        [
            f"# AutoEmu QEMU hardware model for {model.identity.qom_type}",
            f"# Source: {source_basename}",
            f"config {symbol}",
            "    bool",
            "    help",
            f"      AutoEmu generated QEMU model for {model.identity.qom_type}.",
            "",
        ]
    )


def _generate_device_tree_snippet(model: QEMUHardwareModel) -> str:
    dt = model.device_tree
    node_name = dt.node_name or model.identity.peripheral_name
    unit_address = dt.unit_address
    lines = [
        "/ {",
        f"    #address-cells = <{dt.address_cells}>;",
        f"    #size-cells = <{dt.size_cells}>;",
        f"    {node_name}@{unit_address} {{",
        f"        compatible = {_format_string_list(dt.compatible)};",
    ]

    if dt.reg:
        reg_cells: list[int] = []
        for region in dt.reg:
            reg_cells.extend(_cell_values(region.base_address, dt.address_cells))
            reg_cells.extend(_cell_values(region.size, dt.size_cells))
        lines.append(f"        reg = <{_format_cells(reg_cells)}>;")

    if dt.interrupt_names:
        lines.append(f"        interrupt-names = {_format_string_list(dt.interrupt_names)};")

    irq_numbers = [irq.irq_number for irq in model.irq_resources if irq.irq_number is not None]
    if irq_numbers:
        lines.append(f"        interrupts = <{_format_decimal_cells(irq_numbers)}>;")

    for name, value in sorted(dt.properties.items()):
        lines.extend(_format_property(name, value))

    lines.extend(["    };", "};", ""])
    return "\n".join(lines)


def _format_property(name: str, value: Any) -> list[str]:
    if value is True:
        return [f"        {name};"]
    if value is False or value is None:
        return []
    if isinstance(value, str):
        return [f"        {name} = \"{_escape_dt_string(value)}\";"]
    if isinstance(value, int):
        return [f"        {name} = <{value}>;"]
    if isinstance(value, list):
        if all(isinstance(item, str) for item in value):
            return [f"        {name} = {_format_string_list(value)};"]
        if all(isinstance(item, int) for item in value):
            return [f"        {name} = <{_format_decimal_cells(value)}>;"]
    return [f"        {name} = \"{_escape_dt_string(str(value))}\";"]


def _format_string_list(values: list[str]) -> str:
    return ", ".join(f"\"{_escape_dt_string(value)}\"" for value in values)


def _escape_dt_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _cell_values(value: int, cell_count: int) -> list[int]:
    return [
        (value >> (32 * shift)) & 0xFFFFFFFF
        for shift in reversed(range(cell_count))
    ]


def _format_cells(values: list[int]) -> str:
    return " ".join(f"0x{value:08x}" for value in values)


def _format_decimal_cells(values: list[int]) -> str:
    return " ".join(str(value) for value in values)


def _derive_kconfig_path(model: QEMUHardwareModel) -> str:
    source_path = Path(model.file_layout.source_path)
    return str(source_path.with_suffix(".kconfig"))


def _derive_device_tree_path(model: QEMUHardwareModel) -> str:
    source_path = Path(model.file_layout.source_path)
    return str(source_path.with_suffix(".dtsi"))
