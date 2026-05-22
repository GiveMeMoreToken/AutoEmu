"""Generic device-tree source parser for MMIO metadata."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Iterable

from autoemu.models.register import RegisterBlock

logger = logging.getLogger(__name__)


_RE_NODE = re.compile(
    r"(?:(?P<label>[A-Za-z_][\w.-]*)\s*:\s*)?"
    r"(?P<name>[A-Za-z_][\w,.-]*)"
    r"(?:@(?P<addr>[0-9a-fA-F]+))?\s*\{"
)
_RE_COMPATIBLE_PROP = re.compile(r"\bcompatible\s*=\s*((?:\"[^\"]+\"\s*,?\s*)+);")
_RE_STRING = re.compile(r'"([^"]+)"')
_RE_REG = re.compile(r"\breg\s*=\s*<([^>]+)>")
_RE_INTERRUPTS = re.compile(r"\binterrupts\s*=\s*<([^>]+)>")
_RE_INTERRUPT_NAMES_PROP = re.compile(r"\binterrupt-names\s*=\s*((?:\"[^\"]+\"\s*,?\s*)+);")
_RE_CLOCKS = re.compile(r"\bclocks\s*=\s*<([^>]+)>")
_RE_STATUS = re.compile(r'\bstatus\s*=\s*"(\w+)"')
_RE_ADDRESS_CELLS = re.compile(r"#address-cells\s*=\s*<\s*(\d+)\s*>")
_RE_SIZE_CELLS = re.compile(r"#size-cells\s*=\s*<\s*(\d+)\s*>")
_RE_CELL = re.compile(r"0[xX][0-9a-fA-F]+|\b\d+\b")

_DT_SUFFIXES = {".dts", ".dtsi"}


def parse_device_tree(
    path: str | Path,
    peripheral_name: str = "",
) -> dict[str, dict[str, Any]]:
    """Parse a DTS/DTSI file and return MMIO-related node metadata."""
    try:
        content = Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        logger.warning("Failed to read DT file %s: %s", path, exc)
        return {}
    return parse_device_tree_string(content, peripheral_name)


def parse_device_tree_string(
    content: str,
    peripheral_name: str = "",
) -> dict[str, dict[str, Any]]:
    """Parse DTS/DTSI text and return matching nodes.

    The parser is intentionally lightweight. It extracts node labels/names,
    ``compatible``, ``reg``, interrupts, clocks, and status fields without
    depending on a C preprocessor or a full device-tree compiler.
    """
    results: dict[str, dict[str, Any]] = {}
    address_cells = _first_int(_RE_ADDRESS_CELLS.search(content), default=0)
    size_cells = _first_int(_RE_SIZE_CELLS.search(content), default=0)

    for node in _extract_nodes(content):
        label = node["label"]
        node_name = node["name"]
        node_addr = node["addr"]
        node_body = node["body"]

        compatible_values = _parse_string_list(_RE_COMPATIBLE_PROP, node_body)
        interrupt_names = _parse_string_list(_RE_INTERRUPT_NAMES_PROP, node_body)

        if peripheral_name and not _node_matches(
            peripheral_name,
            label=label,
            node_name=node_name,
            compatible_values=compatible_values,
            interrupt_names=interrupt_names,
        ):
            continue

        info: dict[str, Any] = {
            "compatible": compatible_values[0] if compatible_values else "",
        }
        if compatible_values:
            info["compatible_values"] = compatible_values
        if interrupt_names:
            info["interrupt_names"] = interrupt_names

        reg_match = _RE_REG.search(node_body)
        if reg_match:
            reg_info = _decode_reg_property(
                _parse_cells(reg_match.group(1)),
                address_cells=address_cells,
                size_cells=size_cells,
            )
            info.update(reg_info)
        elif node_addr:
            info["base_address"] = int(node_addr, 16)

        irq_match = _RE_INTERRUPTS.search(node_body)
        if irq_match:
            info["interrupts"] = _parse_cells(irq_match.group(1))

        clk_match = _RE_CLOCKS.search(node_body)
        if clk_match:
            info["clocks"] = clk_match.group(1).strip()

        status_match = _RE_STATUS.search(node_body)
        if status_match:
            info["status"] = status_match.group(1)

        results[label or node_name] = info

    return results


def infer_mmio_region_from_device_trees(
    documentation_paths: Iterable[str | Path],
    peripheral_name: str,
) -> dict[str, Any]:
    """Return the first DTS/DTSI MMIO region matching *peripheral_name*."""
    for path in documentation_paths:
        candidate = Path(path)
        if candidate.suffix.lower() not in _DT_SUFFIXES:
            continue
        parsed = parse_device_tree(candidate, peripheral_name)
        for node_name, info in parsed.items():
            base = info.get("base_address")
            if base is None:
                continue
            return {
                "base_address": base,
                "address_size": info.get("size", 0),
                "node": node_name,
                "source": str(candidate),
            }
    return {}


def apply_mmio_region_to_register_blocks(
    register_blocks: dict[str, RegisterBlock],
    *,
    peripheral_name: str,
    mmio_region: dict[str, Any],
) -> dict[str, RegisterBlock]:
    """Apply DTS/DTSI base/size evidence to extracted register blocks."""
    if not mmio_region:
        return register_blocks

    base_address = int(mmio_region.get("base_address", 0) or 0)
    address_size = int(mmio_region.get("address_size", 0) or 0)
    if not base_address and not address_size:
        return register_blocks

    if not register_blocks:
        return {
            peripheral_name: RegisterBlock(
                name=peripheral_name,
                base_address=base_address,
                address_size=address_size,
            )
        }

    updated = {
        name: block.model_copy(deep=True)
        for name, block in register_blocks.items()
    }
    target_names = _matching_block_names(updated, peripheral_name)
    if not target_names and len(updated) == 1:
        target_names = list(updated)

    for name in target_names:
        block = updated[name]
        if base_address and not block.base_address:
            block.base_address = base_address
        if address_size and not block.address_size:
            block.address_size = address_size

    return updated


def _extract_nodes(content: str) -> list[dict[str, str]]:
    nodes: list[dict[str, str]] = []
    for match in _RE_NODE.finditer(content):
        name = match.group("name")
        if name in {"if", "for", "while", "switch"}:
            continue
        start = match.end()
        depth = 1
        pos = start
        while pos < len(content) and depth > 0:
            if content[pos] == "{":
                depth += 1
            elif content[pos] == "}":
                depth -= 1
            pos += 1
        body = content[start : pos - 1] if pos <= len(content) else ""
        nodes.append({
            "label": match.group("label") or "",
            "name": name,
            "addr": match.group("addr") or "",
            "body": body,
        })
    return nodes


def _parse_string_list(pattern: re.Pattern[str], text: str) -> list[str]:
    match = pattern.search(text)
    if not match:
        return []
    return _RE_STRING.findall(match.group(1))


def _node_matches(
    peripheral_name: str,
    *,
    label: str,
    node_name: str,
    compatible_values: list[str],
    interrupt_names: list[str],
) -> bool:
    needle = _normalize(peripheral_name)
    candidates = [label, node_name, *compatible_values, *interrupt_names]
    return any(needle and needle in _normalize(candidate) for candidate in candidates)


def _parse_cells(text: str) -> list[int]:
    return [
        int(token, 16 if token.lower().startswith("0x") else 10)
        for token in _RE_CELL.findall(text)
    ]


def _decode_reg_property(
    cells: list[int],
    *,
    address_cells: int,
    size_cells: int,
) -> dict[str, int]:
    if not cells:
        return {}

    if address_cells > 0:
        size_cells = max(size_cells, 0)
        needed = address_cells + size_cells
        if len(cells) >= needed:
            result = {"base_address": _combine_cells(cells[:address_cells])}
            if size_cells:
                result["size"] = _combine_cells(cells[address_cells:needed])
            return result

    # Common 64-bit ARM form with #address-cells = <2>, #size-cells = <2>.
    if len(cells) >= 4 and (cells[0] == 0 or cells[2] == 0):
        return {
            "base_address": _combine_cells(cells[:2]),
            "size": _combine_cells(cells[2:4]),
        }

    # Common 64-bit address with one size cell.
    if len(cells) >= 3 and cells[0] == 0:
        return {
            "base_address": _combine_cells(cells[:2]),
            "size": cells[2],
        }

    if len(cells) >= 2:
        return {"base_address": cells[0], "size": cells[1]}

    return {"base_address": cells[0]}


def _combine_cells(cells: list[int]) -> int:
    value = 0
    for cell in cells:
        value = (value << 32) | cell
    return value


def _first_int(match: re.Match[str] | None, *, default: int) -> int:
    if not match:
        return default
    return int(match.group(1), 10)


def _matching_block_names(
    blocks: dict[str, RegisterBlock],
    peripheral_name: str,
) -> list[str]:
    requested = _normalize(peripheral_name)
    return [
        name
        for name in blocks
        if requested == _normalize(name) or requested in _normalize(name)
    ]


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())
