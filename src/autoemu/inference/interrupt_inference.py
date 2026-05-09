"""Automatic interrupt and event dependency inference."""

from __future__ import annotations

from pathlib import Path
import re
import sys
from typing import Any

from autoemu.modeling_utils import (
    canonical_flag_name,
    load_register_blocks_json as _load_register_blocks_json,
    normalize_driver_analysis,
    normalize_register_blocks,
)
from autoemu.models.interrupt import (
    FlagBehavior,
    InterruptFlag,
    InterruptLine,
    InterruptModel,
)
from autoemu.models.register import AccessType, RegisterBlock
from autoemu.parsers.driver_parser import DriverAnalysis, analyze_driver_file


def infer_interrupt_model(
    driver_analysis: DriverAnalysis | dict[str, Any],
    register_blocks: dict[str, RegisterBlock] | dict[str, Any] | None = None,
    *,
    peripheral_name: str = "",
) -> InterruptModel:
    """Infer an interrupt model from driver ISR patterns and register metadata."""
    try:
        return _infer_interrupt_model_impl(
            driver_analysis, register_blocks, peripheral_name=peripheral_name
        )
    except Exception as exc:
        periph = peripheral_name or "PERIPHERAL"
        print(f"[autoemu] warning: interrupt inference failed: {exc}", file=sys.stderr)
        return InterruptModel(
            peripheral_name=periph,
            lines=[],
            event_sources=[],
            flag_to_event_map={},
        )


def _infer_interrupt_model_impl(
    driver_analysis: DriverAnalysis | dict[str, Any],
    register_blocks: dict[str, RegisterBlock] | dict[str, Any] | None = None,
    *,
    peripheral_name: str = "",
) -> InterruptModel:
    """Core implementation of interrupt model inference."""
    analysis = normalize_driver_analysis(driver_analysis)
    blocks = normalize_register_blocks(register_blocks or {})

    periph = peripheral_name or analysis.get("peripheral_name", "") or next(iter(blocks), "PERIPHERAL")
    lines: list[InterruptLine] = []
    event_map: dict[str, list[str]] = {}
    event_sources: list[str] = []

    isr_patterns = analysis.get("isr_patterns", [])
    for isr in isr_patterns:
        function_name = isr.get("function_name", "")
        checked_flags = _unique_in_order(isr.get("checked_flags", []))
        cleared_flags = set(isr.get("cleared_flags", []))
        enabled_checks = _unique_in_order(isr.get("enabled_checks", []))
        callbacks = _unique_in_order(isr.get("callbacks", []))
        raw_flags = _unique_in_order(checked_flags + list(cleared_flags))
        checked_flag_names = {canonical_flag_name(flag) for flag in checked_flags}

        line_name = _infer_irq_line_name(function_name, periph)
        irq_number = _lookup_irq_number(line_name)

        inferred_flags: list[InterruptFlag] = []
        for index, raw_flag in enumerate(raw_flags):
            flag_match = _find_flag_field(raw_flag, blocks)
            enable_symbol = enabled_checks[index] if index < len(enabled_checks) else _best_enable_symbol(raw_flag, enabled_checks)
            enable_match = _find_enable_field(enable_symbol, blocks) if enable_symbol else None
            clear_match = _find_clear_field(raw_flag, blocks)

            clear_behavior = _infer_clear_behavior(raw_flag, flag_match, raw_flag in cleared_flags, clear_match)
            clear_register = ""
            clear_bit_offset = 0
            if clear_match is not None:
                clear_register = clear_match["register"].name
                clear_bit_offset = clear_match["field"].bit_offset

            inferred_flags.append(
                InterruptFlag(
                    name=canonical_flag_name(raw_flag),
                    description=raw_flag,
                    register_name=flag_match["register"].name if flag_match else "",
                    bit_offset=flag_match["field"].bit_offset if flag_match else 0,
                    clear_behavior=clear_behavior,
                    clear_register=clear_register,
                    clear_bit_offset=clear_bit_offset,
                    enable_register=enable_match["register"].name if enable_match else "",
                    enable_bit_offset=enable_match["field"].bit_offset if enable_match else 0,
                )
            )

        lines.append(
            InterruptLine(
                irq_number=irq_number,
                name=line_name,
                description=f"Inferred interrupt line from {function_name}" if function_name else "",
                flags=inferred_flags,
            )
        )

        for callback in callbacks:
            event_name = _event_name_from_callback(callback)
            callback_flags = [
                flag for flag in inferred_flags
                if not checked_flag_names or flag.name in checked_flag_names
            ] or inferred_flags
            related = _match_callback_flags(callback, callback_flags)
            if not related and len(callback_flags) == 1:
                related = [callback_flags[0].name]
            if related:
                event_map[event_name] = related
                if event_name not in event_sources:
                    event_sources.append(event_name)

        for raw_flag in checked_flags:
            event_name = _event_name_from_flag(raw_flag)
            canonical = canonical_flag_name(raw_flag)
            event_map.setdefault(event_name, [canonical])
            if event_name not in event_sources:
                event_sources.append(event_name)

    return InterruptModel(
        peripheral_name=periph,
        lines=lines,
        event_sources=event_sources,
        flag_to_event_map=event_map,
    )


def infer_interrupt_model_from_driver(
    driver_path: str | Path,
    register_blocks: dict[str, RegisterBlock] | dict[str, Any] | None = None,
    *,
    peripheral_name: str = "",
) -> InterruptModel:
    """Analyze a driver file and infer its interrupt model."""
    analysis = analyze_driver_file(driver_path, peripheral_name)
    return infer_interrupt_model(
        analysis,
        register_blocks,
        peripheral_name=peripheral_name or analysis.peripheral_name,
    )


def load_register_blocks_json(path: str | Path) -> dict[str, RegisterBlock]:
    """Load step-1 extracted register blocks from a JSON file."""
    return _load_register_blocks_json(path)


def _infer_irq_line_name(function_name: str, peripheral_name: str) -> str:
    if function_name.endswith("IRQHandler"):
        core = function_name.removesuffix("IRQHandler").rstrip("_")
        if core.startswith("HAL_"):
            core = core[4:].rstrip("_")
        return f"{core}_IRQn"
    return f"{peripheral_name}_IRQn"


def _lookup_irq_number(line_name: str) -> int:
    return _KNOWN_IRQ_NUMBERS.get(line_name, -1)


def _find_flag_field(symbol: str, blocks: dict[str, RegisterBlock]) -> dict[str, Any] | None:
    return _find_field(symbol, blocks, kind="flag")


def _find_enable_field(symbol: str, blocks: dict[str, RegisterBlock]) -> dict[str, Any] | None:
    return _find_field(symbol, blocks, kind="enable")


def _find_clear_field(symbol: str, blocks: dict[str, RegisterBlock]) -> dict[str, Any] | None:
    canonical = canonical_flag_name(symbol)
    clear_candidates = [f"C{canonical}", f"CLR{canonical}"]
    best: tuple[int, dict[str, Any]] | None = None
    for clear_symbol in clear_candidates:
        match = _find_field(clear_symbol, blocks, kind="clear")
        if match is None:
            continue
        score = match["score"]
        if best is None or score > best[0]:
            best = (score, match)
    return best[1] if best else None


def _find_field(symbol: str, blocks: dict[str, RegisterBlock], *, kind: str) -> dict[str, Any] | None:
    target = _normalize_token(symbol)
    candidates: list[tuple[int, dict[str, Any]]] = []

    for block in blocks.values():
        for register in block.registers:
            register_bonus = _register_kind_bonus(register.name, kind)
            for field in register.fields:
                field_score = _field_match_score(target, field.name, kind)
                if field_score <= 0:
                    continue
                score = field_score + register_bonus + _symbol_register_hint_bonus(symbol, register.name, block.name)
                candidates.append(
                    (
                        score,
                        {
                            "register": register,
                            "field": field,
                            "score": score,
                        },
                    )
                )

    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _field_match_score(target: str, field_name: str, kind: str) -> int:
    candidate = _normalize_token(field_name)
    if not target or not candidate:
        return 0

    if candidate == target:
        return 120

    candidate_core = candidate
    if kind == "enable":
        if re.search(r"(ie|im|msk|en|e)$", candidate):
            candidate_core = re.sub(r"(ie|im|msk|en|e)$", "", candidate)
        if candidate_core == target:
            return 115

    if kind == "clear":
        candidate_core = re.sub(r"^(c|clr)", "", candidate)
        if candidate_core == target:
            return 110

    if candidate.startswith(target) or target.startswith(candidate):
        return 75

    if target in candidate or candidate in target:
        return 55

    return 0


def _register_kind_bonus(register_name: str, kind: str) -> int:
    normalized = _normalize_token(register_name)
    if kind == "flag":
        if any(token in normalized for token in ("sr", "isr", "status", "mis", "gisr")):
            return 20
    elif kind == "enable":
        if any(token in normalized for token in ("ier", "imr", "mask", "msk", "cr", "cfg")):
            return 20
    elif kind == "clear":
        if any(token in normalized for token in ("icr", "ifcr", "clear", "clr")):
            return 25
    return 0


def _symbol_register_hint_bonus(symbol: str, register_name: str, block_name: str) -> int:
    normalized_symbol = re.sub(r"[^A-Z0-9]+", "", symbol.upper())
    normalized_register = re.sub(r"[^A-Z0-9]+", "", register_name.upper())
    normalized_block = re.sub(r"[^A-Z0-9]+", "", block_name.upper())

    if normalized_register and normalized_register in normalized_symbol:
        return 35
    if normalized_block and normalized_block in normalized_symbol:
        return 20

    prefix = normalized_register
    for suffix in ("DMAIER", "DMASR", "MACPMTCSR", "IER", "IMR", "ISR", "ICR", "IFCR", "CSR", "SR", "CR", "PR"):
        if prefix.endswith(suffix) and len(prefix) > len(suffix):
            prefix = prefix[: -len(suffix)]
            break
    if prefix and prefix in normalized_symbol:
        return 12
    return 0


def _infer_clear_behavior(
    raw_flag: str,
    flag_match: dict[str, Any] | None,
    cleared_in_driver: bool,
    clear_match: dict[str, Any] | None,
) -> FlagBehavior:
    if flag_match is not None:
        access = flag_match["field"].access
        if access == AccessType.W1C:
            return FlagBehavior.W1C
        if access in {AccessType.RC_W1, AccessType.RC_W0, AccessType.RS}:
            return FlagBehavior.READ_CLEAR

    if clear_match is not None or cleared_in_driver:
        return FlagBehavior.SOFTWARE_CLEAR
    return FlagBehavior.HARDWARE_CLEAR


def _best_enable_symbol(raw_flag: str, enabled_checks: list[str]) -> str:
    if not enabled_checks:
        return ""
    flag_token = _normalize_token(raw_flag)
    best_symbol = ""
    best_score = 0
    for symbol in enabled_checks:
        score = _symbol_similarity(flag_token, _normalize_token(symbol))
        if score > best_score:
            best_symbol = symbol
            best_score = score
    return best_symbol if best_score > 0 else ""


def _symbol_similarity(left: str, right: str) -> int:
    if left == right:
        return 100
    left_core = re.sub(r"(ie|im|msk)$", "", left)
    right_core = re.sub(r"(ie|im|msk)$", "", right)
    if left_core == right_core:
        return 95
    if left_core in right_core or right_core in left_core:
        return 70
    return 0

def _event_name_from_callback(callback: str) -> str:
    name = callback
    if name.startswith("HAL_"):
        name = name[4:]
    name = name.removesuffix("Callback")
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _event_name_from_flag(symbol: str) -> str:
    return canonical_flag_name(symbol).lower()


def _match_callback_flags(callback: str, flags: list[InterruptFlag]) -> list[str]:
    callback_text = callback.lower()
    matched: list[str] = []
    for flag in flags:
        flag_text = flag.name.lower()
        if "tx" in callback_text and flag_text in {"tc", "tx", "tx_done", "nis", "ts"}:
            matched.append(flag.name)
        elif "rx" in callback_text and flag_text.startswith(("rx", "rs")):
            matched.append(flag.name)
        elif "error" in callback_text and _is_error_flag(flag.name):
            matched.append(flag.name)
        elif "pmt" in callback_text and "pmt" in flag_text:
            matched.append(flag.name)
        elif "wake" in callback_text and "wk" in flag_text:
            matched.append(flag.name)
        elif "wake" in callback_text and ("wake" in flag_text or "exti" in flag_text):
            matched.append(flag.name)
        elif "suspend" in callback_text and "susp" in flag_text:
            matched.append(flag.name)
        elif flag_text and flag_text in callback_text:
            matched.append(flag.name)
    return _unique_in_order(matched)


def _is_error_flag(flag_name: str) -> bool:
    text = flag_name.lower()
    return any(token in text for token in ("err", "fault", "timeout", "ore", "teif", "dmeif", "crc", "ais"))


def _normalize_token(symbol: str) -> str:
    pieces = re.split(r"[^A-Za-z0-9]+", symbol)
    filtered = [
        piece.lower()
        for piece in pieces
        if piece and piece.lower() not in {"hal", "flag", "it", "irq", "eth", "uart", "usart", "usb", "otg"}
    ]
    if not filtered:
        return ""
    for piece in reversed(filtered):
        if piece not in {"line", "exti", "bit", "mask", "msk"}:
            return piece
    return filtered[-1]


def _unique_in_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


_KNOWN_IRQ_NUMBERS: dict[str, int] = {
    "ETH_IRQn": 61,
    "ETH_WKUP_IRQn": 62,
    "OTG_FS_IRQn": 67,
    "OTG_HS_IRQn": 77,
    "SUBGHZ_Radio_IRQn": 42,
    "DMA1_Stream0_IRQn": 11,
    "DMA1_Stream1_IRQn": 12,
    "DMA1_Stream2_IRQn": 13,
    "DMA1_Stream3_IRQn": 14,
    "DMA1_Stream4_IRQn": 15,
    "DMA1_Stream5_IRQn": 16,
    "DMA1_Stream6_IRQn": 17,
    "DMA1_Stream7_IRQn": 47,
    "DMA2_Stream0_IRQn": 56,
    "DMA2_Stream1_IRQn": 57,
    "DMA2_Stream2_IRQn": 58,
    "DMA2_Stream3_IRQn": 59,
    "DMA2_Stream4_IRQn": 60,
    "DMA2_Stream5_IRQn": 68,
    "DMA2_Stream6_IRQn": 69,
    "DMA2_Stream7_IRQn": 70,
}
