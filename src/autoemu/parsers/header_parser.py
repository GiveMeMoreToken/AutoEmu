"""C header file parser for CMSIS/HAL register definitions.

Extracts peripheral base addresses, register struct offsets,
and bit-field macro definitions from STM32 CMSIS headers.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from autoemu.models.register import AccessType, BitField, Register, RegisterBlock

logger = logging.getLogger(__name__)


@dataclass
class MacroDef:
    name: str
    value: str
    raw_value: str = ""


@dataclass
class StructField:
    name: str
    c_type: str
    offset: int  # byte offset within struct
    array_size: int = 1
    access: AccessType = AccessType.RW
    is_reserved: bool = False
    description: str = ""


@dataclass
class TypedefStruct:
    name: str
    tag: str = ""
    fields: list[StructField] = field(default_factory=list)
    total_size: int = 0


_RE_DEFINE = re.compile(
    r"^\s*#define\s+(\w+)\s+(.+?)(?:\s*/\*.*?\*/)?\s*$"
)
_RE_STRUCT_START = re.compile(
    r"typedef\s+struct\s*(?:\w+)?\s*\{"
)
_RE_STRUCT_FIELD = re.compile(
    r"^\s*(?P<qualifiers>(?:(?:__IO|__I|__O|__IOM|__IM|__OM|volatile|const)\s+)*)"
    r"(?P<c_type>uint\d+_t|int\d+_t)\s+(?P<name>\w+)"
    r"(?:\[(?P<array_size>\d+)\])?\s*;\s*(?P<comment>/\*.*?\*/)?\s*$"
)
_RE_STRUCT_RESERVED = re.compile(
    r"^\s*(?:(?:__IO|__I|__O|__IOM|__IM|__OM|volatile|const)\s+)*uint32_t\s+(RESERVED\w*)"
    r"(?:\[(\d+)\])?\s*;\s*(/\*.*?\*/)?\s*$"
)
_RE_STRUCT_END = re.compile(
    r"^\s*\}\s*(\w+)_TypeDef\s*;"
)
_RE_BASE_ADDR = re.compile(
    r"#define\s+(\w+_BASE)\s+\((.+?)\)"
)
_RE_PERIPH_CAST = re.compile(
    r"#define\s+(\w+)\s+\(\((\w+_TypeDef)\s*\*\)\s*(\w+_BASE)\)"
)
_RE_BIT_DEF = re.compile(
    r"#define\s+(\w+)_(\w+)_(\w+)(?:_(\w+))?\s+"
    r"(?:\(\s*)?(0x[0-9A-Fa-f]+U?|[\d]+U?)(?:\s*\))?"
)
# Matches: #define PERIPH_REG  (PERIPH_BASE + 0xNN)
# Used as a fallback when no struct layout is found.
_RE_DEFINE_REG_OFFSET = re.compile(
    r"#define\s+(\w+?)_(\w+)\s+\(\s*(\w+_BASE)\s*\+\s*(0x[0-9A-Fa-f]+)U?\s*\)"
)
_RE_POS_DEF = re.compile(
    r"#define\s+(\w+)_(\w+)_(\w+)_Pos\s+\((\d+)U?\)"
)
_RE_MSK_DEF = re.compile(
    r"#define\s+(\w+)_(\w+)_(\w+)_Msk\s+\(0x([0-9A-Fa-f]+)U?\s*<<\s*(\w+)_\w+_\w+_Pos\)"
)

_TYPE_SIZES = {
    "uint8_t": 1,
    "uint16_t": 2,
    "uint32_t": 4,
    "int8_t": 1,
    "int16_t": 2,
    "int32_t": 4,
}


def parse_macros(content: str) -> list[MacroDef]:
    """Extract all #define macros."""
    macros = []
    for m in _RE_DEFINE.finditer(content):
        macros.append(MacroDef(name=m.group(1), value=m.group(2).strip(), raw_value=m.group(0)))
    return macros


def _access_from_qualifiers(qualifiers: str) -> AccessType:
    text = qualifiers or ""
    tokens = set(text.split())
    if {"__O", "__OM"} & tokens:
        return AccessType.WO
    if {"__I", "__IM"} & tokens or "const" in tokens:
        return AccessType.RO
    return AccessType.RW


def _strip_comment_markers(comment: str) -> str:
    cleaned = comment.strip()
    if cleaned.startswith("/*"):
        cleaned = cleaned[2:]
    if cleaned.endswith("*/"):
        cleaned = cleaned[:-2]
    return cleaned.replace("!<", "").replace("<!", "").strip(" *")


def _infer_access_from_description(description: str, default: AccessType) -> AccessType:
    text = description.lower()
    if "write 1 to clear" in text or "cleared by writing 1" in text:
        return AccessType.W1C
    if "write 1 to set" in text or "set by writing 1" in text:
        return AccessType.W1S
    if "write 0 to clear" in text or "cleared by writing 0" in text:
        return AccessType.W0C
    if "read only" in text:
        return AccessType.RO
    if "write only" in text:
        return AccessType.WO
    return default


def parse_typedef_structs(content: str) -> list[TypedefStruct]:
    """Extract typedef struct definitions (peripheral register layouts)."""
    structs: list[TypedefStruct] = []
    lines = content.split("\n")
    i = 0
    while i < len(lines):
        if _RE_STRUCT_START.search(lines[i]):
            offset = 0
            fields: list[StructField] = []
            i += 1
            while i < len(lines):
                line = lines[i]
                end_match = _RE_STRUCT_END.search(line)
                if end_match:
                    structs.append(TypedefStruct(
                        name=end_match.group(1) + "_TypeDef",
                        fields=fields,
                        total_size=offset,
                    ))
                    break

                # Reserved fields
                res_match = _RE_STRUCT_RESERVED.match(line)
                if res_match:
                    arr_size = int(res_match.group(2)) if res_match.group(2) else 1
                    fields.append(StructField(
                        name=res_match.group(1),
                        c_type="uint32_t",
                        offset=offset,
                        array_size=arr_size,
                        access=AccessType.RW,
                        is_reserved=True,
                        description=_strip_comment_markers(res_match.group(3) or ""),
                    ))
                    offset += 4 * arr_size
                    i += 1
                    continue

                # Regular fields
                field_match = _RE_STRUCT_FIELD.match(line)
                if field_match:
                    c_type = field_match.group("c_type")
                    name = field_match.group("name")
                    arr_size = int(field_match.group("array_size")) if field_match.group("array_size") else 1
                    type_size = _TYPE_SIZES.get(c_type, 4)
                    is_res = name.upper().startswith("RESERVED")
                    description = _strip_comment_markers(field_match.group("comment") or "")
                    access = _infer_access_from_description(
                        description,
                        _access_from_qualifiers(field_match.group("qualifiers") or ""),
                    )
                    fields.append(StructField(
                        name=name,
                        c_type=c_type,
                        offset=offset,
                        array_size=arr_size,
                        access=access,
                        is_reserved=is_res,
                        description=description,
                    ))
                    offset += type_size * arr_size
                i += 1
        i += 1
    return structs


def parse_base_addresses(content: str) -> dict[str, int]:
    """Extract peripheral base addresses from #define macros."""
    # First pass: collect all address macros
    addr_macros: dict[str, str] = {}
    for m in _RE_BASE_ADDR.finditer(content):
        addr_macros[m.group(1)] = m.group(2)

    # Resolve references
    resolved: dict[str, int] = {}

    def resolve(expr: str) -> int:
        expr = expr.strip()
        # Direct hex
        hex_match = re.match(r"(0x[0-9A-Fa-f]+)U?", expr)
        if hex_match:
            return int(hex_match.group(1), 16)
        # Reference + offset
        plus_match = re.match(r"(\w+)\s*\+\s*(0x[0-9A-Fa-f]+)U?", expr)
        if plus_match:
            base_name = plus_match.group(1)
            offset = int(plus_match.group(2), 16)
            if base_name in resolved:
                return resolved[base_name] + offset
            if base_name in addr_macros:
                resolved[base_name] = resolve(addr_macros[base_name])
                return resolved[base_name] + offset
        # Plain reference
        if expr in resolved:
            return resolved[expr]
        if expr in addr_macros:
            resolved[expr] = resolve(addr_macros[expr])
            return resolved[expr]
        return 0

    for name, expr in addr_macros.items():
        if name not in resolved:
            resolved[name] = resolve(expr)
    return resolved


def parse_bit_definitions(
    content: str, peripheral_prefix: str
) -> dict[str, list[BitField]]:
    """Extract bit field definitions for a peripheral's registers.

    Returns: dict of register_name -> list of BitField
    """
    # Collect Pos/Msk definitions
    positions: dict[str, dict[str, int]] = {}  # reg -> {field -> pos}
    masks: dict[str, dict[str, int]] = {}  # reg -> {field -> mask_val}

    for m in _RE_POS_DEF.finditer(content):
        periph, reg, field_name = m.group(1), m.group(2), m.group(3)
        if periph == peripheral_prefix:
            positions.setdefault(reg, {})[field_name] = int(m.group(4))

    for m in _RE_MSK_DEF.finditer(content):
        periph, reg, field_name = m.group(1), m.group(2), m.group(3)
        if periph == peripheral_prefix:
            masks.setdefault(reg, {})[field_name] = int(m.group(4), 16)

    result: dict[str, list[BitField]] = {}
    for reg_name in positions:
        fields = []
        for field_name, pos in positions[reg_name].items():
            mask_val = masks.get(reg_name, {}).get(field_name, 1)
            bit_width = mask_val.bit_length()
            fields.append(BitField(
                name=field_name,
                bit_offset=pos,
                bit_width=bit_width,
                access=AccessType.RW,
                reset_value=0,
            ))
        result[reg_name] = sorted(fields, key=lambda f: f.bit_offset)
    return result


def struct_to_register_block(
    struct: TypedefStruct,
    bit_defs: dict[str, list[BitField]] | None = None,
    base_address: int = 0,
) -> RegisterBlock:
    """Convert a parsed typedef struct into a RegisterBlock."""
    registers: list[Register] = []
    periph_prefix = struct.name.replace("_TypeDef", "")

    for sf in struct.fields:
        if sf.is_reserved:
            continue
        for idx in range(sf.array_size):
            reg_name = sf.name if sf.array_size == 1 else f"{sf.name}{idx}"
            offset = sf.offset + idx * _TYPE_SIZES.get(sf.c_type, 4)
            size = _TYPE_SIZES.get(sf.c_type, 4) * 8

            fields = []
            if bit_defs and reg_name in bit_defs:
                fields = bit_defs[reg_name]

            registers.append(Register(
                name=reg_name,
                offset=offset,
                size=size,
                description=sf.description,
                access=sf.access,
                reset_value=0,
                fields=fields,
            ))

    return RegisterBlock(
        name=periph_prefix,
        base_address=base_address,
        registers=registers,
    )


def _define_based_register_blocks(
    content: str,
    base_addrs: dict[str, int],
    peripheral_name: str | None = None,
) -> dict[str, RegisterBlock]:
    """Fallback: create register blocks from ``#define PERIPH_REG (BASE + offset)`` patterns."""
    # Collect: periph_prefix -> list of (reg_name, offset)
    periph_regs: dict[str, list[tuple[str, int]]] = {}
    for m in _RE_DEFINE_REG_OFFSET.finditer(content):
        prefix = m.group(1)
        reg_name = m.group(2)
        base_key = m.group(3)
        offset = int(m.group(4), 16)
        # Skip _BASE self-definitions and common non-register suffixes
        if reg_name == "BASE" or reg_name.endswith("_BASE"):
            continue
        if peripheral_name and prefix != peripheral_name:
            continue
        periph_regs.setdefault(prefix, []).append((reg_name, offset))

    results: dict[str, RegisterBlock] = {}
    for prefix, regs in periph_regs.items():
        base_key = f"{prefix}_BASE"
        base = base_addrs.get(base_key, 0)
        registers = [
            Register(name=rn, offset=off, size=32, access=AccessType.RW, reset_value=0, fields=[])
            for rn, off in sorted(regs, key=lambda x: x[1])
        ]
        results[prefix] = RegisterBlock(name=prefix, base_address=base, registers=registers)
    return results


def parse_header_file(
    path: str | Path, peripheral_name: str | None = None
) -> dict[str, RegisterBlock]:
    """Parse a C header file and extract register blocks.

    If peripheral_name is given, only extract that peripheral.
    Returns an empty dict on error rather than crashing.
    """
    try:
        content = Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        logger.warning("Failed to read header file '%s': %s", path, exc)
        return {}

    try:
        structs = parse_typedef_structs(content)
        base_addrs = parse_base_addresses(content)

        results: dict[str, RegisterBlock] = {}
        for s in structs:
            prefix = s.name.replace("_TypeDef", "")
            if peripheral_name and prefix != peripheral_name:
                continue
            bit_defs = parse_bit_definitions(content, prefix)
            base_key = f"{prefix}_BASE"
            base = base_addrs.get(base_key, 0)
            results[prefix] = struct_to_register_block(s, bit_defs, base)

        # Fallback: if no struct was found for the requested peripheral,
        # try #define-based register extraction.
        if not results:
            results = _define_based_register_blocks(content, base_addrs, peripheral_name)

        return results
    except Exception as exc:
        logger.warning("Header parse error for '%s': %s", path, exc)
        return {}
