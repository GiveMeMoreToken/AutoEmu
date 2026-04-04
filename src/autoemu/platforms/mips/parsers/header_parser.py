"""MIPS vendor header parser (non-CMSIS conventions)."""
from __future__ import annotations

import logging
import re
from pathlib import Path

from autoemu.models.register import AccessType, BitField, Register, RegisterBlock

logger = logging.getLogger(__name__)

# MIPS vendors use different macro patterns than ARM CMSIS
_RE_BASE_ADDR = re.compile(
    r"#define\s+(\w+_BASE)\s+(?:\(?\s*)?(0x[0-9A-Fa-f]+)"
)
_RE_REG_OFFSET = re.compile(
    r"#define\s+(\w+)\s+\(\s*(\w+_BASE)\s*\+\s*(0x[0-9A-Fa-f]+)\s*\)"
)
_RE_BIT_FIELD = re.compile(
    r"#define\s+(\w+)\s+\(\s*(\d+)\s*<<\s*(\d+)\s*\)"
)
_RE_BIT_MASK = re.compile(r"#define\s+(\w+)\s+(0x[0-9A-Fa-f]+)")
_RE_STRUCT_FIELD = re.compile(
    r"^\s*(?:volatile\s+)?(?:unsigned\s+)?(u?int\d+_t|uint32_t|__u32|u32)\s+(\w+)\s*;"
)


def parse_mips_header(
    path: str | Path, peripheral_name: str = ""
) -> dict[str, RegisterBlock]:
    """Parse a MIPS vendor header file for register definitions."""
    try:
        content = Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        logger.warning("Failed to read MIPS header %s: %s", path, exc)
        return {}

    return parse_mips_header_string(content, peripheral_name)


def parse_mips_header_string(
    content: str, peripheral_name: str = ""
) -> dict[str, RegisterBlock]:
    """Parse MIPS vendor header content from a string."""
    periph_upper = peripheral_name.upper() if peripheral_name else ""
    base_addrs = _extract_base_addresses(content)
    reg_offsets = _extract_register_offsets(content, base_addrs)
    bit_fields = _extract_bit_fields(content)

    # Group registers by peripheral base
    periph_regs: dict[str, list[Register]] = {}
    periph_bases: dict[str, int] = {}

    for reg_name, (base_name, offset) in reg_offsets.items():
        periph = base_name.removesuffix("_BASE")
        if periph_upper and periph_upper != periph and periph_upper not in periph:
            continue
        if periph not in periph_regs:
            periph_regs[periph] = []
            periph_bases[periph] = base_addrs.get(base_name, 0)

        fields = _match_bit_fields(reg_name, bit_fields)
        periph_regs[periph].append(
            Register(
                name=reg_name.split("_", 1)[-1] if "_" in reg_name else reg_name,
                offset=offset,
                size=32,
                reset_value=0,
                fields=fields,
            )
        )

    results: dict[str, RegisterBlock] = {}
    for periph, regs in periph_regs.items():
        regs.sort(key=lambda r: r.offset)
        results[periph] = RegisterBlock(
            name=periph,
            base_address=periph_bases.get(periph, 0),
            registers=regs,
        )
    return results


def _extract_base_addresses(content: str) -> dict[str, int]:
    return {m.group(1): int(m.group(2), 16) for m in _RE_BASE_ADDR.finditer(content)}


def _extract_register_offsets(
    content: str, bases: dict[str, int]
) -> dict[str, tuple[str, int]]:
    result: dict[str, tuple[str, int]] = {}
    for m in _RE_REG_OFFSET.finditer(content):
        reg_name, base_name, offset_str = m.group(1), m.group(2), m.group(3)
        if base_name in bases:
            result[reg_name] = (base_name, int(offset_str, 16))
    return result


def _extract_bit_fields(
    content: str,
) -> dict[str, list[tuple[str, int, int]]]:
    """Extract bit field definitions grouped by prefix."""
    fields: dict[str, list[tuple[str, int, int]]] = {}
    for m in _RE_BIT_FIELD.finditer(content):
        name = m.group(1)
        value = int(m.group(2))
        shift = int(m.group(3))
        parts = name.rsplit("_", 1)
        if len(parts) == 2:
            prefix, field_name = parts
            fields.setdefault(prefix, []).append(
                (field_name, shift, value.bit_length())
            )
    return fields


def _match_bit_fields(
    reg_name: str, all_fields: dict[str, list[tuple[str, int, int]]]
) -> list[BitField]:
    matched = all_fields.get(reg_name, [])
    return [
        BitField(
            name=name,
            bit_offset=offset,
            bit_width=max(width, 1),
            access=AccessType.RW,
        )
        for name, offset, width in matched
    ]
