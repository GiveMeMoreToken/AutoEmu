"""High-level register extraction utilities.

Combines SVD and header sources into a single register abstraction step so the
modeling pipeline can consume a better peripheral register model directly.
"""

from __future__ import annotations

from pathlib import Path
import re

from autoemu.models.register import Register, RegisterBlock
from autoemu.parsers.header_parser import parse_header_file
from autoemu.parsers.svd_parser import parse_svd_file


def extract_register_blocks(
    *,
    svd_path: str | Path = "",
    header_path: str | Path = "",
    peripheral_name: str | None = None,
) -> dict[str, RegisterBlock]:
    """Extract and merge register blocks from SVD and header inputs."""
    svd_blocks = parse_svd_file(svd_path) if svd_path else {}
    header_blocks = parse_header_file(header_path) if header_path else {}

    if peripheral_name:
        names = _resolve_peripheral_names(
            peripheral_name,
            set(svd_blocks) | set(header_blocks),
        )
    else:
        names = sorted(set(svd_blocks) | set(header_blocks))

    results: dict[str, RegisterBlock] = {}
    for name in names:
        svd_block = svd_blocks.get(name)
        header_block = header_blocks.get(name)
        if svd_block and header_block:
            results[name] = merge_register_blocks(svd_block, header_block)
        elif svd_block:
            results[name] = svd_block
        elif header_block:
            results[name] = header_block
    return results


def merge_register_blocks(primary: RegisterBlock, secondary: RegisterBlock) -> RegisterBlock:
    """Merge two register blocks, preferring semantic richness from ``primary``."""
    merged = primary.model_copy(deep=True)

    if not merged.description and secondary.description:
        merged.description = secondary.description
    if not merged.base_address and secondary.base_address:
        merged.base_address = secondary.base_address

    secondary_by_name = {register.name: register for register in secondary.registers}
    merged_registers: list[Register] = []

    for register in merged.registers:
        fallback = secondary_by_name.pop(register.name, None)
        if fallback is not None:
            merged_registers.append(_merge_register(register, fallback))
        else:
            merged_registers.append(register)

    for fallback in sorted(secondary_by_name.values(), key=lambda item: item.offset):
        merged_registers.append(fallback)

    merged.registers = sorted(merged_registers, key=lambda item: item.offset)
    return merged


def _merge_register(primary: Register, secondary: Register) -> Register:
    merged = primary.model_copy(deep=True)

    if not merged.description and secondary.description:
        merged.description = secondary.description
    if merged.access.value == "RW" and secondary.access.value != "RW":
        merged.access = secondary.access
    if merged.reset_value == 0 and secondary.reset_value != 0:
        merged.reset_value = secondary.reset_value

    secondary_fields = {field.name: field for field in secondary.fields}
    merged_fields = []
    for field in merged.fields:
        fallback = secondary_fields.pop(field.name, None)
        if fallback is None:
            merged_fields.append(field)
            continue
        merged_field = field.model_copy(deep=True)
        if not merged_field.description and fallback.description:
            merged_field.description = fallback.description
        if merged_field.access.value == "RW" and fallback.access.value != "RW":
            merged_field.access = fallback.access
        if not merged_field.enum_values and fallback.enum_values:
            merged_field.enum_values = dict(fallback.enum_values)
        merged_fields.append(merged_field)

    for fallback in sorted(secondary_fields.values(), key=lambda item: item.bit_offset):
        merged_fields.append(fallback)

    merged.fields = sorted(merged_fields, key=lambda item: item.bit_offset)
    return merged


def _resolve_peripheral_names(
    requested_name: str,
    available_names: set[str],
) -> list[str]:
    if requested_name in available_names:
        return [requested_name]

    normalized_available = {
        name: _normalize_name(name)
        for name in available_names
    }
    requested_normalized = _normalize_name(requested_name)

    exact_matches = [
        name
        for name, normalized in normalized_available.items()
        if normalized == requested_normalized
    ]
    if exact_matches:
        return sorted(exact_matches)

    alias_patterns = _PERIPHERAL_ALIASES.get(requested_normalized, (requested_normalized,))
    fuzzy_matches = [
        name
        for name, normalized in normalized_available.items()
        if any(
            pattern in normalized or normalized in pattern
            for pattern in alias_patterns
        )
    ]
    return sorted(fuzzy_matches)


def _normalize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


_PERIPHERAL_ALIASES: dict[str, tuple[str, ...]] = {
    "eth": ("ethernet", "ethernetmac", "ethernetdma", "ethernetmmc", "ethernetptp"),
    "usbotgfs": ("otgfs", "otgfsglobal", "otgfsdevice", "otgfshost", "otgfspwrclk"),
    "usbotghs": ("otghs", "otghsglobal", "otghsdevice", "otghshost", "otghspwrclk"),
    "usb": ("usb", "otgfs", "otghs"),
}
