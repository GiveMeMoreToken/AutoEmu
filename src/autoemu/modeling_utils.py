"""Shared helpers for loading and normalizing AutoEmu modeling data."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
import json
from pathlib import Path
import re
from typing import Any

from autoemu.models.register import RegisterBlock


def load_json_file(path: str | Path) -> Any:
    """Load JSON data from disk."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def unwrap_model_payload(data: Any) -> Any:
    """Return the inner ``model`` payload when the JSON uses a wrapper object."""
    if isinstance(data, dict):
        return data.get("model", data)
    return data


def load_wrapped_model_json(path: str | Path, model_type: Any) -> Any:
    """Load a Pydantic model from raw or ``{\"model\": ...}`` JSON."""
    return model_type.model_validate(unwrap_model_payload(load_json_file(path)))


def normalize_driver_analysis(driver_analysis: Any) -> dict[str, Any]:
    """Normalize a ``DriverAnalysis`` dataclass or dict to a plain dict."""
    if isinstance(driver_analysis, dict):
        return driver_analysis
    if is_dataclass(driver_analysis):
        return asdict(driver_analysis)
    raise TypeError(f"Unsupported driver analysis type: {type(driver_analysis)!r}")


def normalize_register_blocks(
    register_blocks: dict[str, RegisterBlock] | dict[str, Any],
) -> dict[str, RegisterBlock]:
    """Normalize register-block payloads to ``RegisterBlock`` models."""
    normalized: dict[str, RegisterBlock] = {}
    for name, block in register_blocks.items():
        if isinstance(block, RegisterBlock):
            normalized[name] = block
        else:
            normalized[name] = RegisterBlock.model_validate(block)
    return normalized


def load_register_blocks_json(path: str | Path) -> dict[str, RegisterBlock]:
    """Load step-1 register blocks from JSON."""
    return normalize_register_blocks(load_json_file(path))


def canonical_flag_name(symbol: str) -> str:
    """Normalize a driver flag symbol to a compact uppercase token."""
    return _normalize_token(symbol).upper()


def is_non_mmio_register(register_name: str) -> bool:
    """Identify driver-side pseudo-registers that represent descriptor memory."""
    normalized = register_name.strip().upper()
    if not normalized:
        return False
    return bool(re.fullmatch(r"DESC\d+", normalized))


def snake_case(name: str) -> str:
    """Convert a CamelCase or mixed name to snake_case."""
    result: list[str] = []
    for i, c in enumerate(name):
        if c.isupper() and i > 0 and not name[i - 1].isupper():
            result.append("_")
        result.append(c.lower())
    return "".join(result).replace("__", "_")


def upper_case(name: str) -> str:
    """Convert name to UPPER_SNAKE_CASE."""
    return snake_case(name).upper()


def normalize_name(name: str) -> str:
    """Normalize an arbitrary name to a safe lowercase identifier."""
    normalized = "".join(ch.lower() if ch.isalnum() else "_" for ch in name)
    while "__" in normalized:
        normalized = normalized.replace("__", "_")
    return normalized.strip("_")


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
