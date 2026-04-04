"""Linux kernel driver parser for MIPS peripherals."""
from __future__ import annotations

import logging
import re
from pathlib import Path

from autoemu.parsers.driver_parser import (
    DriverAnalysis,
    ISRPattern,
    InitSequence,
    RegisterAccess,
)

logger = logging.getLogger(__name__)

# Kernel driver patterns
_RE_READL = re.compile(
    r"(\w+)\s*=\s*(?:readl|__raw_readl|ioread32)\s*\(\s*(\w+)\s*(?:\+\s*(\w+))?\s*\)"
)
_RE_WRITEL = re.compile(
    r"(?:writel|__raw_writel|iowrite32)\s*\(\s*(.+?)\s*,\s*(\w+)\s*(?:\+\s*(\w+))?\s*\)"
)
_RE_IOREMAP = re.compile(r"(\w+)\s*=\s*(?:ioremap|devm_ioremap)\w*\s*\(")
_RE_REQUEST_IRQ = re.compile(
    r"(?:request_irq|devm_request_irq)\s*\(\s*(\w+)\s*,\s*(\w+)"
)
_RE_PROBE_FUNC = re.compile(
    r"static\s+int\s+(\w+_probe)\s*\(", re.MULTILINE
)
_RE_REMOVE_FUNC = re.compile(
    r"static\s+(?:int|void)\s+(\w+_remove)\s*\(", re.MULTILINE
)
_RE_IRQ_HANDLER = re.compile(
    r"static\s+irqreturn_t\s+(\w+)\s*\(", re.MULTILINE
)
_RE_PLATFORM_DRIVER = re.compile(
    r"module_platform_driver\s*\(\s*(\w+)\s*\)"
)
_RE_COMPATIBLE = re.compile(r'\.compatible\s*=\s*"([^"]+)"')
_RE_FUNC_DEF = re.compile(
    r"^static\s+(?:int|void|irqreturn_t)\s+(\w+)\s*\(", re.MULTILINE
)


def analyze_kernel_driver(
    path: str | Path, peripheral_name: str = ""
) -> DriverAnalysis:
    """Analyze a Linux kernel driver source file."""
    try:
        content = Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        logger.warning("Failed to read kernel driver %s: %s", path, exc)
        return DriverAnalysis(peripheral_name=peripheral_name, source_file="")

    return analyze_kernel_driver_string(content, peripheral_name, Path(path).name)


def analyze_kernel_driver_string(
    content: str, peripheral_name: str = "", source_file: str = "<string>"
) -> DriverAnalysis:
    """Analyze kernel driver source code from a string."""
    if not peripheral_name:
        peripheral_name = _infer_peripheral_from_filename(source_file)

    analysis = DriverAnalysis(
        peripheral_name=peripheral_name, source_file=source_file
    )
    functions = _extract_function_bodies(content)

    for func_name, body in functions.items():
        accesses = _extract_register_accesses(body, func_name, source_file)
        analysis.register_accesses.extend(accesses)

        # Detect IRQ handlers
        if any(
            tok in func_name.lower()
            for tok in ("irq", "isr", "interrupt")
        ):
            isr = _extract_isr_pattern(body, func_name, peripheral_name)
            isr.register_accesses = accesses
            analysis.isr_patterns.append(isr)

        if "probe" in func_name.lower() or "init" in func_name.lower():
            analysis.init_sequences.append(
                InitSequence(
                    function_name=func_name,
                    peripheral=peripheral_name,
                    accesses=accesses,
                )
            )

    return analysis


def _extract_register_accesses(
    body: str, func_name: str, source_file: str
) -> list[RegisterAccess]:
    accesses: list[RegisterAccess] = []
    context = _classify_function(func_name)

    for m in _RE_READL.finditer(body):
        reg = m.group(3) or "MMIO"
        accesses.append(
            RegisterAccess(
                register=reg,
                access_type="read",
                source_file=source_file,
                in_function=func_name,
                context=context,
            )
        )

    for m in _RE_WRITEL.finditer(body):
        value_expr = m.group(1)
        reg = m.group(3) or "MMIO"
        accesses.append(
            RegisterAccess(
                register=reg,
                access_type="write",
                value_expr=value_expr,
                source_file=source_file,
                in_function=func_name,
                context=context,
            )
        )

    return accesses


def _extract_isr_pattern(
    body: str, func_name: str, peripheral_name: str
) -> ISRPattern:
    pattern = ISRPattern(function_name=func_name, peripheral=peripheral_name)
    # Extract flag checks and clears from readl/writel patterns in ISR
    for m in _RE_READL.finditer(body):
        reg = m.group(3) or m.group(2)
        if any(
            tok in reg.upper()
            for tok in ("STATUS", "SR", "ISR", "STAT", "INT")
        ):
            pattern.checked_flags.append(reg)
    for m in _RE_WRITEL.finditer(body):
        reg = m.group(3) or m.group(2)
        if any(
            tok in reg.upper()
            for tok in ("STATUS", "SR", "ISR", "ICR", "CLEAR")
        ):
            pattern.cleared_flags.append(reg)
    return pattern


def _classify_function(name: str) -> str:
    lower = name.lower()
    if "probe" in lower:
        return "init"
    if "remove" in lower:
        return "deinit"
    if "irq" in lower or "isr" in lower or "interrupt" in lower:
        return "isr"
    if "init" in lower:
        return "init"
    if "start" in lower or "enable" in lower:
        return "enable"
    if "stop" in lower or "disable" in lower:
        return "disable"
    if "tx" in lower or "send" in lower or "write" in lower:
        return "transfer"
    if "rx" in lower or "recv" in lower or "read" in lower:
        return "transfer"
    return "other"


def _infer_peripheral_from_filename(filename: str) -> str:
    stem = Path(filename).stem
    # Remove common prefixes
    for prefix in ("jz47xx_", "mt76_", "pic32_", "ingenic_", "ralink_"):
        if stem.startswith(prefix):
            stem = stem[len(prefix):]
            break
    return stem.upper()


def _extract_function_bodies(content: str) -> dict[str, str]:
    functions: dict[str, str] = {}
    for m in _RE_FUNC_DEF.finditer(content):
        func_name = m.group(1)
        start = m.start()
        brace_pos = content.find("{", start)
        if brace_pos < 0:
            continue
        depth, pos = 1, brace_pos + 1
        while pos < len(content) and depth > 0:
            if content[pos] == "{":
                depth += 1
            elif content[pos] == "}":
                depth -= 1
            pos += 1
        functions[func_name] = content[brace_pos:pos]
    return functions
