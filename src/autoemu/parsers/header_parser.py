"""C header file parser for CMSIS/HAL register definitions.

Extracts peripheral base addresses, register struct offsets,
and bit-field macro definitions from STM32 CMSIS headers.
"""

from __future__ import annotations

import ast
import logging
import operator
import re
from dataclasses import dataclass, field
from pathlib import Path

from autoemu.models.register import AccessType, BitField, Register, RegisterBlock

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Minimal C preprocessor
# ---------------------------------------------------------------------------

_RE_INCLUDE_QUOTED = re.compile(r'^\s*#\s*include\s+"([^"]+)"')
_RE_IFDEF = re.compile(r"^\s*#\s*ifdef\s+(\w+)")
_RE_IFNDEF = re.compile(r"^\s*#\s*ifndef\s+(\w+)")
_RE_ELSE = re.compile(r"^\s*#\s*else\b")
_RE_ENDIF = re.compile(r"^\s*#\s*endif\b")
_RE_PP_DEFINE_SYM = re.compile(r"^\s*#\s*define\s+(\w+)(?:\s+(.*))?$")


def preprocess_header(
    content: str,
    *,
    include_dirs: list[str] | None = None,
    defines: dict[str, str] | None = None,
) -> str:
    """Minimal C preprocessor: resolve ``#include`` directives and evaluate
    ``#ifdef``/``#ifndef``/``#else``/``#endif`` blocks.

    * Tracks conditional nesting with a stack.
    * ``#ifdef SYMBOL`` where *SYMBOL* is **not** in *defines* → skip until
      matching ``#else`` or ``#endif``.
    * ``#ifndef SYMBOL`` where *SYMBOL* **is** in *defines* → skip.
    * ``#include "file.h"`` — search *include_dirs* and inline (with a
      recursion guard to prevent infinite loops).
    * ``#include <system_header.h>`` (angle-bracket) is ignored.
    """
    include_dirs = include_dirs or []
    defines = dict(defines) if defines else {}
    seen_includes: set[str] = set()

    def _resolve(
        text: str,
        inc_dirs: list[str],
        defs: dict[str, str],
        seen: set[str],
    ) -> list[str]:
        output_lines: list[str] = []
        # Stack of (active, seen_else) — *active* means we are emitting lines.
        cond_stack: list[tuple[bool, bool]] = []

        def _currently_active() -> bool:
            return all(active for active, _ in cond_stack)

        for line in text.splitlines():
            # --- #ifdef ---
            m = _RE_IFDEF.match(line)
            if m:
                symbol = m.group(1)
                active = symbol in defs
                cond_stack.append((active, False))
                continue

            # --- #ifndef ---
            m = _RE_IFNDEF.match(line)
            if m:
                symbol = m.group(1)
                active = symbol not in defs
                cond_stack.append((active, False))
                continue

            # --- #else ---
            if _RE_ELSE.match(line):
                if cond_stack:
                    prev_active, _ = cond_stack[-1]
                    cond_stack[-1] = (not prev_active, True)
                continue

            # --- #endif ---
            if _RE_ENDIF.match(line):
                if cond_stack:
                    cond_stack.pop()
                continue

            if not _currently_active():
                continue

            # --- #define (collect into defs so later #ifdef can see it) ---
            dm = _RE_PP_DEFINE_SYM.match(line)
            if dm:
                defs[dm.group(1)] = (dm.group(2) or "").strip()

            # --- #include "file.h" ---
            m = _RE_INCLUDE_QUOTED.match(line)
            if m:
                filename = m.group(1)
                resolved = _find_include(filename, inc_dirs)
                if resolved and resolved not in seen:
                    seen.add(resolved)
                    try:
                        inc_content = Path(resolved).read_text(
                            encoding="utf-8", errors="replace"
                        )
                        output_lines.extend(
                            _resolve(inc_content, inc_dirs, defs, seen)
                        )
                    except Exception as exc:  # pragma: no cover
                        logger.debug("Failed to read included file %s: %s", resolved, exc)
                continue

            output_lines.append(line)

        return output_lines

    return "\n".join(_resolve(content, include_dirs, defines, seen_includes))


def _find_include(filename: str, include_dirs: list[str]) -> str | None:
    """Search *include_dirs* for *filename* and return the first match."""
    for d in include_dirs:
        candidate = Path(d) / filename
        if candidate.is_file():
            return str(candidate.resolve())
    return None


@dataclass
class MacroDef:
    name: str
    value: str
    raw_value: str = ""
    params: tuple[str, ...] = ()
    comment: str = ""


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


_RE_DEFINE_LINE = re.compile(
    r"^\s*#define\s+(?P<name>\w+)(?:\((?P<params>[^)]*)\))?\s+(?P<value>.*?)(?:\s*(?P<comment>/\*.*?\*/|//.*))?\s*$"
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
    r"\s*(/\*.*?\*/|//.*)?"
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
    for line in content.splitlines():
        m = _RE_DEFINE_LINE.match(line)
        if not m:
            continue
        params = tuple(
            p.strip() for p in (m.group("params") or "").split(",") if p.strip()
        )
        macros.append(MacroDef(
            name=m.group("name"),
            value=m.group("value").strip(),
            raw_value=line,
            params=params,
            comment=_strip_comment_markers(m.group("comment") or ""),
        ))
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
    if "(ro)" in text or "read-only" in text or "read only" in text:
        return AccessType.RO
    if "(wo)" in text or "write-only" in text or "write only" in text:
        return AccessType.WO
    if "(rw)" in text or "read-write" in text or "read write" in text:
        return AccessType.RW
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
    *,
    family_expand_count: int = 4,
) -> dict[str, RegisterBlock]:
    """Fallback: create register blocks from ``#define PERIPH_REG (BASE + offset)`` patterns."""
    # Collect: periph_prefix -> list of (reg_name, offset)
    periph_regs: dict[str, list[Register]] = {}
    for m in _RE_DEFINE_REG_OFFSET.finditer(content):
        prefix = m.group(1)
        reg_name = m.group(2)
        offset = int(m.group(4), 16)
        description = _strip_comment_markers(m.group(5) or "")
        # Skip _BASE self-definitions and common non-register suffixes
        if reg_name == "BASE" or reg_name.endswith("_BASE"):
            continue
        if peripheral_name and prefix != peripheral_name:
            continue
        periph_regs.setdefault(prefix, []).append(Register(
            name=reg_name,
            offset=offset,
            size=32,
            description=description,
            access=_infer_access_from_description(description, AccessType.RW),
            reset_value=0,
            fields=[],
        ))

    results: dict[str, RegisterBlock] = {}
    for prefix, regs in periph_regs.items():
        base_key = f"{prefix}_BASE"
        base = base_addrs.get(base_key, 0)
        registers = sorted(regs, key=lambda reg: (reg.offset, reg.name))
        results[prefix] = RegisterBlock(name=prefix, base_address=base, registers=registers)
    macro_results = _macro_only_register_blocks(
        content,
        peripheral_name=peripheral_name,
        family_expand_count=family_expand_count,
    )
    for block_name, macro_block in macro_results.items():
        if block_name not in results:
            results[block_name] = macro_block
            continue
        existing = results[block_name]
        by_key = {(reg.name, reg.offset): reg for reg in existing.registers}
        by_key.update({(reg.name, reg.offset): reg for reg in macro_block.registers})
        existing.registers = sorted(by_key.values(), key=lambda reg: (reg.offset, reg.name))
    return results


_VALUE_SUFFIXES = (
    "_MASK", "_MSK", "_BIT", "_BITS", "_SHIFT", "_POS", "_POSITION",
    "_FLAGS", "_EN", "_ENABLE", "_DISABLE", "_DISABLED",
)
_VALUE_NAME_PARTS = ("_CMD_", "_COMMAND_")
_VALUE_TOKENS = {"MODE", "VALUE", "VAL", "OPTION", "OPT", "SEL"}


def _macro_only_register_blocks(
    content: str,
    *,
    peripheral_name: str | None = None,
    family_expand_count: int = 4,
) -> dict[str, RegisterBlock]:
    """Create register blocks from macro-only register maps.

    This handles Linux-style headers that define offsets directly, for example
    ``#define GPU_ID 0x00`` and indexed families such as
    ``#define JS_HEAD_LO(n) (JS_BASE + ((n) * JS_SLOT_STRIDE) + 0x00)``.
    """
    macros = parse_macros(content)
    constants = _resolve_numeric_macros(macros)

    requested = peripheral_name.upper() if peripheral_name else None
    has_requested_prefix = bool(
        requested
        and any(
            not macro.params
            and macro.name.startswith(f"{requested}_")
            and _is_direct_register_macro(macro)
            and _eval_simple_expr(macro.value, constants) is not None
            for macro in macros
        )
    )
    include_all = bool(requested and has_requested_prefix)

    related_prefixes = _related_macro_prefixes(macros, requested, constants) if requested else set()
    block_regs: dict[str, list[Register]] = {}
    target_block = requested
    for macro in macros:
        if not macro.params:
            offset = _eval_simple_expr(macro.value, constants)
            if offset is None:
                continue
            if requested:
                include = (
                    macro.name.startswith(f"{requested}_")
                    and _is_direct_register_macro(macro)
                )
                block_name = target_block if include else ""
            else:
                prefix = macro.name.split("_", 1)[0]
                include = _is_direct_register_macro(macro)
                block_name = prefix
            if not include or not block_name:
                continue
            block_regs.setdefault(block_name, []).append(_register_from_macro(macro, offset))
            continue

        expanded = _expand_family_macro(
            macro,
            constants,
            count=_infer_family_bound(macro, constants, family_expand_count),
        )
        if not expanded:
            continue
        if requested:
            macro_prefix = macro.name.split("_", 1)[0]
            include = macro.name.startswith(f"{requested}_") or (
                include_all and macro_prefix in related_prefixes
            )
            block_name = target_block if include else ""
        else:
            block_name = macro.name.split("_", 1)[0]
            include = True
        if not include or not block_name:
            continue
        block_regs.setdefault(block_name, []).extend(
            _register_from_macro(macro, offset, index=index)
            for index, offset in expanded
        )

    results: dict[str, RegisterBlock] = {}
    for block_name, regs in block_regs.items():
        deduped = {(reg.name, reg.offset): reg for reg in regs}
        registers = sorted(deduped.values(), key=lambda reg: (reg.offset, reg.name))
        results[block_name] = RegisterBlock(
            name=block_name,
            base_address=0,
            registers=registers,
        )
    return results


def _related_macro_prefixes(
    macros: list[MacroDef],
    requested: str | None,
    constants: dict[str, int],
) -> set[str]:
    if not requested:
        return set()
    related: set[str] = {requested}
    requested_offsets = [
        _eval_simple_expr(macro.value, constants)
        for macro in macros
        if not macro.params
        and macro.name.startswith(f"{requested}_")
        and _is_direct_register_macro(macro)
    ]
    requested_offsets = [offset for offset in requested_offsets if offset is not None]
    if not requested_offsets:
        return related
    requested_min = min(requested_offsets)
    requested_max = max(requested_offsets)
    direct_prefixes = {
        macro.name.split("_", 1)[0]
        for macro in macros
        if not macro.params
        and not macro.name.startswith(f"{requested}_")
        and _is_direct_register_macro(macro)
    }
    candidate_prefixes: set[str] = set()
    for macro in macros:
        if not macro.params:
            continue
        prefix = macro.name.split("_", 1)[0]
        if prefix in direct_prefixes:
            continue
        expanded = _expand_family_macro(macro, constants, count=1)
        if not expanded:
            continue
        first_offset = expanded[0][1]
        if first_offset >= requested_min and first_offset <= requested_max + 0x10000:
            candidate_prefixes.add(prefix)
    if len(candidate_prefixes) == 1:
        related.update(candidate_prefixes)
    return related


def _register_from_macro(macro: MacroDef, offset: int, *, index: int | None = None) -> Register:
    name = macro.name if index is None else f"{macro.name}{index}"
    description = macro.comment
    return Register(
        name=name,
        offset=offset,
        size=32,
        description=description,
        access=_infer_access_from_description(description, AccessType.RW),
        reset_value=0,
        fields=[],
    )


def _is_direct_register_macro(macro: MacroDef) -> bool:
    name = macro.name.upper()
    if name.endswith(("_BASE", "_OFFSET", "_STRIDE", "_SIZE", "_COUNT", "_NUM")):
        return False
    if name.endswith(_VALUE_SUFFIXES):
        return False
    if any(part in name for part in _VALUE_NAME_PARTS):
        return False
    if "BIT(" in macro.value.upper() or "GENMASK(" in macro.value.upper():
        return False
    if _expr_references_identifier(macro.value):
        return False
    tokens = name.split("_")
    if not macro.comment and len(tokens) >= 4 and _VALUE_TOKENS.intersection(tokens):
        return False
    value = macro.value.strip()
    numeric = re.fullmatch(r"\(?\s*(0x[0-9A-Fa-f]+|\d+)[UuLl]*\s*\)?", value)
    if numeric:
        raw = numeric.group(1)
        number = int(raw, 16) if raw.lower().startswith("0x") else int(raw)
        if number % 4 != 0:
            return False
    return True


def _expr_references_identifier(expr: str) -> bool:
    text = re.sub(r"/\*.*?\*/", "", expr)
    text = re.sub(r"//.*$", "", text)
    text = re.sub(r"(0[xX][0-9A-Fa-f]+|(?<![A-Za-z_])\d+)[UuLl]+", r"\1", text)
    text = re.sub(r"0[xX][0-9A-Fa-f]+", "", text)
    text = re.sub(r"\b\d+\b", "", text)
    return re.search(r"\b[A-Za-z_]\w*\b", text) is not None


def _resolve_numeric_macros(macros: list[MacroDef]) -> dict[str, int]:
    raw = {
        macro.name: macro.value
        for macro in macros
        if not macro.params and not macro.name.endswith(("_MASK", "_MSK"))
    }
    resolved: dict[str, int] = {}

    for _ in range(len(raw)):
        changed = False
        for name, expr in raw.items():
            if name in resolved:
                continue
            value = _eval_simple_expr(expr, resolved)
            if value is not None:
                resolved[name] = value
                changed = True
        if not changed:
            break
    return resolved


def _expand_family_macro(
    macro: MacroDef,
    constants: dict[str, int],
    *,
    count: int,
) -> list[tuple[int, int]]:
    if len(macro.params) != 1:
        return []
    if not _is_family_register_macro(macro):
        return []
    param = macro.params[0]
    expanded = []
    for index in range(max(count, 0)):
        value = _eval_simple_expr(macro.value, constants | {param: index})
        if value is None or value % 4 != 0:
            return []
        expanded.append((index, value))
    return expanded


def _is_family_register_macro(macro: MacroDef) -> bool:
    name = macro.name.upper()
    if name.endswith(_VALUE_SUFFIXES):
        return False
    if any(part in name for part in _VALUE_NAME_PARTS):
        return False
    expr = macro.value.upper()
    if "BIT(" in expr or "GENMASK(" in expr:
        return False
    if len(macro.params) != 1:
        return False
    return re.search(rf"\b{re.escape(macro.params[0])}\b", macro.value) is not None


def _infer_family_bound(
    macro: MacroDef,
    constants: dict[str, int],
    default_count: int,
) -> int:
    family_prefix = macro.name.split("_", 1)[0]
    candidates = [
        f"{family_prefix}_COUNT",
        f"{family_prefix}_NUM",
        f"NUM_{family_prefix}",
        f"{family_prefix}_SLOTS",
        f"{family_prefix}_SLOT_COUNT",
    ]
    for name in candidates:
        value = constants.get(name)
        if value and 0 < value <= 64:
            return value
    return default_count


def _eval_simple_expr(expr: str, constants: dict[str, int]) -> int | None:
    text = expr.strip()
    text = re.sub(r"/\*.*?\*/", "", text)
    text = re.sub(r"//.*$", "", text)
    text = re.sub(r"(0[xX][0-9A-Fa-f]+|(?<![A-Za-z_])\d+)[UuLl]+", r"\1", text)
    text = re.sub(
        r"0[xX][0-9A-Fa-f]+",
        lambda match: str(int(match.group(0), 16)),
        text,
    )
    for name in sorted(constants, key=len, reverse=True):
        text = re.sub(rf"\b{re.escape(name)}\b", str(constants[name]), text)
    if re.search(r"[A-Za-z_]", text):
        return None
    if not re.fullmatch(r"[0-9\s()+\-*/<>|&]+", text):
        return None
    return _safe_eval_int(text)


_SAFE_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.FloorDiv: operator.floordiv,
    ast.Div: operator.floordiv,
    ast.LShift: operator.lshift,
    ast.RShift: operator.rshift,
    ast.BitOr: operator.or_,
    ast.BitAnd: operator.and_,
}
_SAFE_UNARYOPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}
_MAX_EXPR_ABS_VALUE = 0xFFFFFFFF
_MAX_SHIFT = 63
_MAX_AST_NODES = 64
_MAX_AST_DEPTH = 16


def _safe_eval_int(expr: str) -> int | None:
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return None
    if not _ast_within_limits(tree):
        return None
    try:
        value = _eval_ast_int(tree.body)
    except (ArithmeticError, TypeError, ValueError):
        return None
    return value if 0 <= value <= _MAX_EXPR_ABS_VALUE else None


def _ast_within_limits(tree: ast.AST) -> bool:
    node_count = 0
    stack: list[tuple[ast.AST, int]] = [(tree, 0)]
    while stack:
        node, depth = stack.pop()
        node_count += 1
        if node_count > _MAX_AST_NODES or depth > _MAX_AST_DEPTH:
            return False
        stack.extend((child, depth + 1) for child in ast.iter_child_nodes(node))
    return True


def _eval_ast_int(node: ast.AST) -> int:
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        value = int(node.value)
    elif isinstance(node, ast.UnaryOp) and type(node.op) in _SAFE_UNARYOPS:
        value = _SAFE_UNARYOPS[type(node.op)](_eval_ast_int(node.operand))
    elif isinstance(node, ast.BinOp) and type(node.op) in _SAFE_BINOPS:
        left = _eval_ast_int(node.left)
        right = _eval_ast_int(node.right)
        if isinstance(node.op, (ast.LShift, ast.RShift)) and not (0 <= right <= _MAX_SHIFT):
            raise ValueError("shift too large")
        if isinstance(node.op, (ast.Div, ast.FloorDiv)) and right == 0:
            raise ArithmeticError("division by zero")
        value = _SAFE_BINOPS[type(node.op)](left, right)
    else:
        raise TypeError(f"unsupported expression node: {type(node).__name__}")
    if abs(value) > _MAX_EXPR_ABS_VALUE:
        raise ValueError("expression value out of range")
    return value


def parse_header_file(
    path: str | Path,
    peripheral_name: str | None = None,
    *,
    include_dirs: list[str] | None = None,
    defines: dict[str, str] | None = None,
    family_expand_count: int = 4,
) -> dict[str, RegisterBlock]:
    """Parse a C header file and extract register blocks.

    If peripheral_name is given, only extract that peripheral.
    Returns an empty dict on error rather than crashing.

    Parameters
    ----------
    include_dirs:
        Directories to search when resolving ``#include "file.h"`` directives.
    defines:
        Pre-defined symbols for ``#ifdef``/``#ifndef`` evaluation.
    family_expand_count:
        Default number of entries to emit for one-index macro register families
        when no ``*_COUNT``/``*_NUM`` bound is available.
    """
    try:
        content = Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        logger.warning("Failed to read header file '%s': %s", path, exc)
        return {}

    # Run minimal preprocessor before parsing
    content = preprocess_header(content, include_dirs=include_dirs, defines=defines)

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
            results = _define_based_register_blocks(
                content,
                base_addrs,
                peripheral_name,
                family_expand_count=family_expand_count,
            )

        return results
    except Exception as exc:
        logger.warning("Header parse error for '%s': %s", path, exc)
        return {}
