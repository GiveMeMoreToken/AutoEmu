"""C header file parser for CMSIS/HAL register definitions.

Extracts peripheral base addresses, register struct offsets,
and bit-field macro definitions from STM32 CMSIS headers.
"""

from __future__ import annotations

import ast
import logging
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
_RE_DEFINE_ANY = re.compile(
    r"^\s*#define\s+(?P<name>[A-Z][A-Z0-9_]*)"
    r"(?:\((?P<params>[A-Za-z0-9_,\s]*)\))?\s+"
    r"(?P<expr>.+?)\s*$"
)
_RE_INLINE_COMMENT = re.compile(r"/\*(?P<comment>.*?)\*/")
_RE_C_INT_SUFFIX = re.compile(r"(?<=\d)[uUlL]+\b")
_RE_IDENTIFIER = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
_RE_FUNC_CALL = re.compile(r"\b([A-Z][A-Z0-9_]*)\s*\(([^()]*)\)")

_TYPE_SIZES = {
    "uint8_t": 1,
    "uint16_t": 2,
    "uint32_t": 4,
    "int8_t": 1,
    "int16_t": 2,
    "int32_t": 4,
}
_MACRO_INDEX_EXPANSION_COUNT = 16
_MACRO_MAX_REGISTER_OFFSET = 0x10000


@dataclass(frozen=True)
class _DefineMacro:
    name: str
    params: tuple[str, ...]
    expr: str
    description: str = ""
    order: int = 0


@dataclass(frozen=True)
class _MacroRegisterCandidate:
    name: str
    offset: int
    description: str
    access: AccessType
    order: int


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
    if "read only" in text or "(ro)" in text:
        return AccessType.RO
    if "write only" in text or "(wo)" in text:
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


def _macro_only_register_blocks(
    content: str,
    peripheral_name: str | None = None,
) -> dict[str, RegisterBlock]:
    """Fallback: create a block from Linux-style ``#define REG 0xOFF`` maps."""
    macros = _parse_define_macros(content)
    if not macros:
        return {}

    function_macros = {macro.name: macro for macro in macros if macro.params}
    helper_function_names = _find_helper_function_macros(function_macros)
    symbols = _resolve_numeric_symbols(macros, function_macros)
    direct_candidates = _direct_macro_register_candidates(macros, symbols)
    function_candidates = _indexed_macro_register_candidates(
        function_macros,
        helper_function_names,
        symbols,
        direct_candidates,
    )
    candidates = _filter_value_macros(direct_candidates + function_candidates)
    candidates = _dedupe_register_candidates(candidates)

    if not candidates:
        return {}

    if peripheral_name:
        requested = _normalize_macro_token(peripheral_name)
        has_requested_prefix = any(
            _normalize_macro_token(candidate.name).startswith(f"{requested}_")
            or _normalize_macro_token(candidate.name) == requested
            for candidate in candidates
        )
        if not has_requested_prefix:
            candidates = [
                candidate
                for candidate in candidates
                if requested in _normalize_macro_token(candidate.name)
            ]
        if not candidates:
            return {}
        return {
            peripheral_name: RegisterBlock(
                name=peripheral_name,
                registers=_macro_candidates_to_registers(candidates),
            )
        }

    grouped: dict[str, list[_MacroRegisterCandidate]] = {}
    for candidate in candidates:
        prefix = candidate.name.split("_", 1)[0]
        grouped.setdefault(prefix, []).append(candidate)
    return {
        prefix: RegisterBlock(
            name=prefix,
            registers=_macro_candidates_to_registers(items),
        )
        for prefix, items in grouped.items()
    }


def _parse_define_macros(content: str) -> list[_DefineMacro]:
    macros: list[_DefineMacro] = []
    for order, line in enumerate(content.splitlines()):
        if line.rstrip().endswith("\\"):
            continue
        comment_match = _RE_INLINE_COMMENT.search(line)
        description = _strip_comment_markers(comment_match.group(0)) if comment_match else ""
        line_without_comment = _RE_INLINE_COMMENT.sub("", line)
        match = _RE_DEFINE_ANY.match(line_without_comment)
        if not match:
            continue
        params = tuple(
            param.strip()
            for param in (match.group("params") or "").split(",")
            if param.strip()
        )
        macros.append(_DefineMacro(
            name=match.group("name"),
            params=params,
            expr=match.group("expr").strip(),
            description=description,
            order=order,
        ))
    return macros


def _resolve_numeric_symbols(
    macros: list[_DefineMacro],
    function_macros: dict[str, _DefineMacro],
) -> dict[str, int]:
    symbols: dict[str, int] = {}
    plain_macros = [macro for macro in macros if not macro.params]
    for _ in range(len(plain_macros) + 1):
        changed = False
        for macro in plain_macros:
            if macro.name in symbols:
                continue
            value = _evaluate_offset_expr(macro.expr, symbols, function_macros)
            if value is None:
                continue
            symbols[macro.name] = value
            changed = True
        if not changed:
            break
    return symbols


def _direct_macro_register_candidates(
    macros: list[_DefineMacro],
    symbols: dict[str, int],
) -> list[_MacroRegisterCandidate]:
    candidates: list[_MacroRegisterCandidate] = []
    for macro in macros:
        if macro.params or _is_obvious_non_register_macro(macro.name):
            continue
        if not _direct_expr_looks_like_register_offset(macro.expr):
            continue
        offset = symbols.get(macro.name)
        if offset is None or not _looks_like_register_offset(offset):
            continue
        candidates.append(_MacroRegisterCandidate(
            name=macro.name,
            offset=offset,
            description=macro.description,
            access=_infer_access_from_description(macro.description, AccessType.RW),
            order=macro.order,
        ))
    return candidates


def _indexed_macro_register_candidates(
    function_macros: dict[str, _DefineMacro],
    helper_function_names: set[str],
    symbols: dict[str, int],
    direct_candidates: list[_MacroRegisterCandidate],
) -> list[_MacroRegisterCandidate]:
    candidates: list[_MacroRegisterCandidate] = []
    direct_names = {candidate.name for candidate in direct_candidates}
    for macro in function_macros.values():
        if len(macro.params) != 1:
            continue
        if macro.name in helper_function_names:
            continue
        if _is_obvious_non_register_macro(macro.name):
            continue
        if not _function_expr_looks_like_register_offset(macro.expr):
            continue
        if _has_shorter_register_prefix(macro.name, direct_names):
            continue
        param = macro.params[0]
        for index in range(_MACRO_INDEX_EXPANSION_COUNT):
            offset = _evaluate_offset_expr(
                macro.expr,
                symbols,
                function_macros,
                args={param: index},
            )
            if offset is None or not _looks_like_register_offset(offset):
                continue
            candidates.append(_MacroRegisterCandidate(
                name=f"{macro.name}_{index}",
                offset=offset,
                description=macro.description,
                access=_infer_access_from_description(macro.description, AccessType.RW),
                order=macro.order * _MACRO_INDEX_EXPANSION_COUNT + index,
            ))
    return candidates


def _find_helper_function_macros(function_macros: dict[str, _DefineMacro]) -> set[str]:
    helpers: set[str] = set()
    for macro in function_macros.values():
        for match in _RE_FUNC_CALL.finditer(macro.expr):
            name = match.group(1)
            if name in function_macros and name != macro.name:
                helpers.add(name)
    return helpers


def _filter_value_macros(
    candidates: list[_MacroRegisterCandidate],
) -> list[_MacroRegisterCandidate]:
    names = {candidate.name for candidate in candidates}
    names.update(
        re.sub(r"_\d+$", "", candidate.name)
        for candidate in candidates
        if re.search(r"_\d+$", candidate.name)
    )
    filtered: list[_MacroRegisterCandidate] = []
    for candidate in candidates:
        prefix_names = set(names)
        indexed_base = re.sub(r"_\d+$", "", candidate.name)
        if indexed_base != candidate.name:
            prefix_names.discard(indexed_base)
        if _has_shorter_register_prefix(candidate.name, prefix_names):
            continue
        filtered.append(candidate)
    return filtered


def _dedupe_register_candidates(
    candidates: list[_MacroRegisterCandidate],
) -> list[_MacroRegisterCandidate]:
    ordered = sorted(candidates, key=lambda item: (item.offset, item.order, item.name))
    seen_offsets: set[int] = set()
    seen_names: set[str] = set()
    deduped: list[_MacroRegisterCandidate] = []
    for candidate in ordered:
        if candidate.offset in seen_offsets or candidate.name in seen_names:
            continue
        seen_offsets.add(candidate.offset)
        seen_names.add(candidate.name)
        deduped.append(candidate)
    return deduped


def _macro_candidates_to_registers(
    candidates: list[_MacroRegisterCandidate],
) -> list[Register]:
    return [
        Register(
            name=candidate.name,
            offset=candidate.offset,
            size=32,
            description=candidate.description,
            access=candidate.access,
            reset_value=0,
            fields=[],
        )
        for candidate in sorted(candidates, key=lambda item: (item.offset, item.name))
    ]


def _evaluate_offset_expr(
    expr: str,
    symbols: dict[str, int],
    function_macros: dict[str, _DefineMacro],
    *,
    args: dict[str, int] | None = None,
    depth: int = 0,
) -> int | None:
    if depth > 8:
        return None
    normalized = _normalize_c_integer_expr(expr)
    args = args or {}
    for name, value in args.items():
        normalized = re.sub(rf"\b{re.escape(name)}\b", str(value), normalized)

    for _ in range(12):
        previous = normalized
        normalized = _replace_function_calls(
            normalized,
            symbols,
            function_macros,
            depth=depth,
        )
        normalized = _replace_numeric_symbols(normalized, symbols)
        if normalized == previous:
            break

    without_hex = re.sub(r"0[xX][0-9a-fA-F]+", "0", normalized)
    if _RE_IDENTIFIER.search(without_hex):
        return None
    try:
        value = _safe_eval_int(normalized)
    except Exception:
        return None
    return value if value >= 0 else None


def _replace_function_calls(
    expr: str,
    symbols: dict[str, int],
    function_macros: dict[str, _DefineMacro],
    *,
    depth: int,
) -> str:
    def repl(match: re.Match[str]) -> str:
        macro = function_macros.get(match.group(1))
        if macro is None:
            return match.group(0)
        arg_exprs = _split_macro_args(match.group(2))
        if len(arg_exprs) != len(macro.params):
            return match.group(0)
        arg_values = [
            _evaluate_offset_expr(arg_expr, symbols, function_macros, depth=depth + 1)
            for arg_expr in arg_exprs
        ]
        if any(value is None for value in arg_values):
            return match.group(0)
        local_args = dict(zip(macro.params, [int(value) for value in arg_values if value is not None]))
        value = _evaluate_offset_expr(
            macro.expr,
            symbols,
            function_macros,
            args=local_args,
            depth=depth + 1,
        )
        return str(value) if value is not None else match.group(0)

    return _RE_FUNC_CALL.sub(repl, expr)


def _replace_numeric_symbols(expr: str, symbols: dict[str, int]) -> str:
    def repl(match: re.Match[str]) -> str:
        name = match.group(0)
        if name in symbols:
            return str(symbols[name])
        return name

    return _RE_IDENTIFIER.sub(repl, expr)


def _split_macro_args(text: str) -> list[str]:
    return [item.strip() for item in text.split(",") if item.strip()]


def _normalize_c_integer_expr(expr: str) -> str:
    normalized = expr.strip()
    normalized = _RE_C_INT_SUFFIX.sub("", normalized)
    normalized = normalized.replace("ULL", "").replace("UL", "").replace("LL", "")
    return normalized


def _safe_eval_int(expr: str) -> int:
    tree = ast.parse(expr, mode="eval")
    _validate_int_expr_ast(tree)
    value = eval(compile(tree, "<macro-expr>", "eval"), {"__builtins__": {}}, {})
    if not isinstance(value, int):
        raise ValueError("expression did not evaluate to int")
    return value


def _validate_int_expr_ast(node: ast.AST) -> None:
    allowed = (
        ast.Expression,
        ast.Constant,
        ast.BinOp,
        ast.UnaryOp,
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.FloorDiv,
        ast.Mod,
        ast.LShift,
        ast.RShift,
        ast.BitOr,
        ast.BitAnd,
        ast.BitXor,
        ast.Invert,
        ast.UAdd,
        ast.USub,
    )
    for child in ast.walk(node):
        if not isinstance(child, allowed):
            raise ValueError(f"unsupported expression node: {type(child).__name__}")
        if isinstance(child, ast.Constant) and not isinstance(child.value, int):
            raise ValueError("non-integer constant")


def _is_obvious_non_register_macro(name: str) -> bool:
    return name.endswith(("_BASE", "_SHIFT", "_STRIDE", "_SIZE"))


def _function_expr_looks_like_register_offset(expr: str) -> bool:
    return "+" in expr


def _direct_expr_looks_like_register_offset(expr: str) -> bool:
    return "<<" not in expr and "BIT(" not in expr and "GENMASK" not in expr


def _looks_like_register_offset(value: int) -> bool:
    return value % 4 == 0 and 0 <= value <= _MACRO_MAX_REGISTER_OFFSET


def _has_shorter_register_prefix(name: str, register_names: set[str]) -> bool:
    parts = name.split("_")
    for end in range(len(parts) - 1, 0, -1):
        prefix = "_".join(parts[:end])
        if prefix in register_names and prefix != name:
            return True
    return False


def _normalize_macro_token(value: str) -> str:
    return re.sub(r"[^A-Z0-9_]+", "", value.upper())


def parse_header_file(
    path: str | Path,
    peripheral_name: str | None = None,
    *,
    include_dirs: list[str] | None = None,
    defines: dict[str, str] | None = None,
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
            results = _define_based_register_blocks(content, base_addrs, peripheral_name)
        if not results:
            results = _macro_only_register_blocks(content, peripheral_name)

        return results
    except Exception as exc:
        logger.warning("Header parse error for '%s': %s", path, exc)
        return {}
