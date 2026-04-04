"""Device tree (.dts/.dtsi) parser for MIPS peripherals."""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_RE_NODE = re.compile(r"(\w[\w,.-]*)\s*(?:@([0-9a-fA-F]+))?\s*\{")
_RE_COMPATIBLE = re.compile(r'compatible\s*=\s*"([^"]+)"')
_RE_REG = re.compile(r"reg\s*=\s*<\s*((?:0x[0-9a-fA-F]+\s*)+)>")
_RE_INTERRUPTS = re.compile(r"interrupts\s*=\s*<\s*((?:\d+\s*)+)>")
_RE_CLOCKS = re.compile(r"clocks\s*=\s*<([^>]+)>")
_RE_STATUS = re.compile(r'status\s*=\s*"(\w+)"')


def parse_device_tree(
    path: str | Path, peripheral_name: str = ""
) -> dict[str, dict[str, Any]]:
    """Parse a device tree source file and return peripheral info.

    Returns dict of node_name -> {base_address, size, interrupts, compatible, clocks}.
    """
    try:
        content = Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        logger.warning("Failed to read DT file %s: %s", path, exc)
        return {}
    return _parse_dt_content(content, peripheral_name)


def parse_device_tree_string(
    content: str, peripheral_name: str = ""
) -> dict[str, dict[str, Any]]:
    """Parse device tree content from a string."""
    return _parse_dt_content(content, peripheral_name)


def _parse_dt_content(
    content: str, peripheral_name: str
) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    periph_lower = peripheral_name.lower() if peripheral_name else ""

    # Simple brace-level parser for DT nodes
    nodes = _extract_nodes(content)
    for node_name, node_addr, node_body in nodes:
        compatible = ""
        m = _RE_COMPATIBLE.search(node_body)
        if m:
            compatible = m.group(1)

        # Filter by peripheral name if given
        if periph_lower:
            if (
                periph_lower not in node_name.lower()
                and periph_lower not in compatible.lower()
            ):
                continue

        info: dict[str, Any] = {"compatible": compatible}

        # Parse reg property
        reg_match = _RE_REG.search(node_body)
        if reg_match:
            values = [
                int(v, 16)
                for v in re.findall(r"0x([0-9a-fA-F]+)", reg_match.group(1))
            ]
            if len(values) >= 1:
                info["base_address"] = values[0]
            if len(values) >= 2:
                info["size"] = values[1]
        elif node_addr:
            info["base_address"] = int(node_addr, 16)

        # Parse interrupts
        irq_match = _RE_INTERRUPTS.search(node_body)
        if irq_match:
            info["interrupts"] = [int(v) for v in irq_match.group(1).split()]

        # Parse clocks
        clk_match = _RE_CLOCKS.search(node_body)
        if clk_match:
            info["clocks"] = clk_match.group(1).strip()

        # Parse status
        status_match = _RE_STATUS.search(node_body)
        if status_match:
            info["status"] = status_match.group(1)

        results[node_name] = info

    return results


def _extract_nodes(content: str) -> list[tuple[str, str, str]]:
    """Extract DT nodes as (name, address, body) tuples."""
    nodes: list[tuple[str, str, str]] = []
    for m in _RE_NODE.finditer(content):
        name = m.group(1)
        addr = m.group(2) or ""
        start = m.end()
        depth = 1
        pos = start
        while pos < len(content) and depth > 0:
            if content[pos] == "{":
                depth += 1
            elif content[pos] == "}":
                depth -= 1
            pos += 1
        body = content[start : pos - 1] if pos <= len(content) else ""
        nodes.append((name, addr, body))
    return nodes
