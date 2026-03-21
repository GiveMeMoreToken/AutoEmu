"""HAL/LL driver code parser for behavioral analysis.

Analyzes STM32 HAL and LL driver source code to extract:
- Register access patterns (read/write sequences)
- Interrupt handler logic (ISR flag checking and clearing)
- State transitions triggered by driver operations
- DMA configuration patterns
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class RegisterAccess:
    """A single register access observed in driver code."""

    register: str  # e.g., "CR1", "SR"
    field: str = ""  # e.g., "UE", "RXNE"
    access_type: str = "read"  # "read", "write", "set_bit", "clear_bit", "modify"
    value_expr: str = ""  # Expression for the value
    source_file: str = ""
    source_line: int = 0
    in_function: str = ""
    context: str = ""  # "init", "enable", "disable", "isr", "transfer", etc.


@dataclass
class ISRPattern:
    """An interrupt service routine pattern extracted from driver code."""

    function_name: str
    peripheral: str
    checked_flags: list[str] = field(default_factory=list)
    cleared_flags: list[str] = field(default_factory=list)
    enabled_checks: list[str] = field(default_factory=list)  # IE bits checked
    callbacks: list[str] = field(default_factory=list)
    register_accesses: list[RegisterAccess] = field(default_factory=list)


@dataclass
class InitSequence:
    """An initialization sequence extracted from HAL_xxx_Init or LL init functions."""

    function_name: str
    peripheral: str
    accesses: list[RegisterAccess] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)  # Other peripherals used


@dataclass
class DMAConfig:
    """DMA configuration pattern."""

    peripheral: str
    direction: str = ""  # "periph_to_mem", "mem_to_periph", "mem_to_mem"
    channel: str = ""
    stream: str = ""
    source_register: str = ""  # Peripheral register used as DMA source/dest
    trigger: str = ""
    circular: bool = False
    fifo_mode: bool = False


@dataclass
class DriverAnalysis:
    """Complete analysis result for a driver file."""

    peripheral_name: str
    source_file: str
    register_accesses: list[RegisterAccess] = field(default_factory=list)
    isr_patterns: list[ISRPattern] = field(default_factory=list)
    init_sequences: list[InitSequence] = field(default_factory=list)
    dma_configs: list[DMAConfig] = field(default_factory=list)
    state_hints: list[dict[str, str]] = field(default_factory=list)


# Patterns for STM32 HAL/LL driver analysis
_RE_HAL_REG_WRITE = re.compile(
    r"(\w+)->(\w+)\s*=\s*(.+?);"
)
_RE_HAL_REG_READ = re.compile(
    r"(?:(\w+)\s*=\s*)?(\w+)->(\w+)\s*;"
)
_RE_SET_BIT = re.compile(
    r"SET_BIT\s*\(\s*(?:\w+->)*(\w+)->(\w+)\s*,\s*(.+?)\s*\)"
)
_RE_CLEAR_BIT = re.compile(
    r"CLEAR_BIT\s*\(\s*(?:\w+->)*(\w+)->(\w+)\s*,\s*(.+?)\s*\)"
)
_RE_MODIFY_REG = re.compile(
    r"MODIFY_REG\s*\(\s*(?:\w+->)*(\w+)->(\w+)\s*,\s*(.+?)\s*,\s*(.+?)\s*\)"
)
_RE_READ_BIT = re.compile(
    r"READ_BIT\s*\(\s*(?:\w+->)*(\w+)->(\w+)\s*,\s*(.+?)\s*\)"
)
_RE_FLAG_CHECK = re.compile(
    r"__HAL_(\w+)_GET_FLAG\s*\(\s*\w+\s*,\s*(\w+)\s*\)"
)
_RE_FLAG_CLEAR = re.compile(
    r"__HAL_(\w+)_CLEAR_FLAG\s*\(\s*\w+\s*,\s*(\w+)\s*\)"
)
_RE_IT_ENABLE = re.compile(
    r"__HAL_(\w+)_ENABLE_IT\s*\(\s*\w+\s*,\s*(\w+)\s*\)"
)
_RE_IT_CHECK = re.compile(
    r"__HAL_(\w+)_GET_IT_SOURCE\s*\(\s*\w+\s*,\s*(\w+)\s*\)"
)
_RE_FUNC_DEF = re.compile(
    r"^(?:HAL_StatusTypeDef|void|static\s+void|static\s+HAL_StatusTypeDef)"
    r"\s+(HAL_\w+|LL_\w+)\s*\(",
    re.MULTILINE,
)
_RE_ISR_FUNC = re.compile(
    r"void\s+(HAL_\w+_IRQHandler|(\w+)_IRQHandler)\s*\(",
    re.MULTILINE,
)
_RE_DMA_INIT = re.compile(
    r"hdma\w*->Init\.(\w+)\s*=\s*(\w+)\s*;"
)
_RE_HAL_CALLBACK = re.compile(
    r"HAL_(\w+)_(\w+)Callback\s*\("
)


def _extract_function_bodies(content: str) -> dict[str, str]:
    """Extract function name -> function body mappings."""
    functions: dict[str, str] = {}
    for m in _RE_FUNC_DEF.finditer(content):
        func_name = m.group(1)
        start = m.start()
        # Find the opening brace
        brace_pos = content.find("{", start)
        if brace_pos < 0:
            continue
        # Match braces
        depth = 1
        pos = brace_pos + 1
        while pos < len(content) and depth > 0:
            if content[pos] == "{":
                depth += 1
            elif content[pos] == "}":
                depth -= 1
            pos += 1
        functions[func_name] = content[brace_pos:pos]
    return functions


def _classify_function(name: str) -> str:
    """Classify a function by its role."""
    lower = name.lower()
    if "init" in lower:
        return "init"
    if "deinit" in lower:
        return "deinit"
    if "irqhandler" in lower or "_isr" in lower:
        return "isr"
    if "enable" in lower:
        return "enable"
    if "disable" in lower:
        return "disable"
    if "start" in lower:
        return "start"
    if "stop" in lower:
        return "stop"
    if "transmit" in lower or "_send" in lower or "_tx" in lower:
        return "transfer"
    if "receive" in lower or "_recv" in lower or "_rx" in lower:
        return "transfer"
    if "config" in lower or "set" in lower:
        return "config"
    return "other"


def _extract_register_accesses(
    body: str, func_name: str, source_file: str, peripheral: str
) -> list[RegisterAccess]:
    """Extract register accesses from a function body."""
    accesses: list[RegisterAccess] = []
    context = _classify_function(func_name)

    for m in _RE_SET_BIT.finditer(body):
        accesses.append(RegisterAccess(
            register=m.group(2), field=m.group(3),
            access_type="set_bit", value_expr=m.group(3),
            source_file=source_file, in_function=func_name, context=context,
        ))

    for m in _RE_CLEAR_BIT.finditer(body):
        accesses.append(RegisterAccess(
            register=m.group(2), field=m.group(3),
            access_type="clear_bit", value_expr=m.group(3),
            source_file=source_file, in_function=func_name, context=context,
        ))

    for m in _RE_MODIFY_REG.finditer(body):
        accesses.append(RegisterAccess(
            register=m.group(2), field=f"mask={m.group(3)}",
            access_type="modify", value_expr=m.group(4),
            source_file=source_file, in_function=func_name, context=context,
        ))

    for m in _RE_READ_BIT.finditer(body):
        accesses.append(RegisterAccess(
            register=m.group(2), field=m.group(3),
            access_type="read", value_expr="",
            source_file=source_file, in_function=func_name, context=context,
        ))

    for m in _RE_HAL_REG_WRITE.finditer(body):
        if m.group(2) not in ("Instance", "hdma", "State", "ErrorCode"):
            accesses.append(RegisterAccess(
                register=m.group(2), access_type="write",
                value_expr=m.group(3),
                source_file=source_file, in_function=func_name, context=context,
            ))

    return accesses


def _extract_isr_pattern(
    body: str, func_name: str, peripheral: str
) -> ISRPattern:
    """Extract ISR pattern from an interrupt handler function."""
    pattern = ISRPattern(function_name=func_name, peripheral=peripheral)

    for m in _RE_FLAG_CHECK.finditer(body):
        pattern.checked_flags.append(m.group(2))
    for m in _RE_FLAG_CLEAR.finditer(body):
        pattern.cleared_flags.append(m.group(2))
    for m in _RE_IT_CHECK.finditer(body):
        pattern.enabled_checks.append(m.group(2))
    for m in _RE_HAL_CALLBACK.finditer(body):
        pattern.callbacks.append(f"HAL_{m.group(1)}_{m.group(2)}Callback")

    return pattern


def _extract_dma_configs(body: str, peripheral: str) -> list[DMAConfig]:
    """Extract DMA configuration from init functions."""
    configs: list[DMAConfig] = []
    dma_inits: dict[str, str] = {}
    for m in _RE_DMA_INIT.finditer(body):
        dma_inits[m.group(1)] = m.group(2)

    if dma_inits:
        config = DMAConfig(peripheral=peripheral)
        if "Direction" in dma_inits:
            val = dma_inits["Direction"]
            if "PERIPH_TO_MEMORY" in val:
                config.direction = "periph_to_mem"
            elif "MEMORY_TO_PERIPH" in val:
                config.direction = "mem_to_periph"
            elif "MEMORY_TO_MEMORY" in val:
                config.direction = "mem_to_mem"
        if "Channel" in dma_inits:
            config.channel = dma_inits["Channel"]
        if "Mode" in dma_inits:
            config.circular = "CIRCULAR" in dma_inits["Mode"]
        if "FIFOMode" in dma_inits:
            config.fifo_mode = "ENABLE" in dma_inits["FIFOMode"]
        configs.append(config)

    return configs


def analyze_driver_file(path: str | Path, peripheral_name: str = "") -> DriverAnalysis:
    """Analyze a HAL/LL driver source file."""
    content = Path(path).read_text(encoding="utf-8", errors="replace")
    source_file = str(Path(path).name)

    if not peripheral_name:
        # Infer from filename: stm32f4xx_hal_eth.c -> ETH
        stem = Path(path).stem
        parts = stem.split("_")
        peripheral_name = parts[-1].upper() if parts else "UNKNOWN"

    analysis = DriverAnalysis(
        peripheral_name=peripheral_name,
        source_file=source_file,
    )

    functions = _extract_function_bodies(content)

    for func_name, body in functions.items():
        accesses = _extract_register_accesses(body, func_name, source_file, peripheral_name)
        analysis.register_accesses.extend(accesses)

        if "IRQHandler" in func_name or "isr" in func_name.lower():
            isr = _extract_isr_pattern(body, func_name, peripheral_name)
            isr.register_accesses = accesses
            analysis.isr_patterns.append(isr)

        if "init" in func_name.lower() or "Init" in func_name:
            init_seq = InitSequence(
                function_name=func_name,
                peripheral=peripheral_name,
                accesses=accesses,
            )
            analysis.init_sequences.append(init_seq)

        dma_cfgs = _extract_dma_configs(body, peripheral_name)
        analysis.dma_configs.extend(dma_cfgs)

    return analysis


def analyze_driver_string(content: str, peripheral_name: str = "") -> DriverAnalysis:
    """Analyze driver source code from a string."""
    analysis = DriverAnalysis(
        peripheral_name=peripheral_name,
        source_file="<string>",
    )

    functions = _extract_function_bodies(content)

    for func_name, body in functions.items():
        accesses = _extract_register_accesses(body, func_name, "<string>", peripheral_name)
        analysis.register_accesses.extend(accesses)

        if "IRQHandler" in func_name or "isr" in func_name.lower():
            isr = _extract_isr_pattern(body, func_name, peripheral_name)
            isr.register_accesses = accesses
            analysis.isr_patterns.append(isr)

        if "init" in func_name.lower():
            init_seq = InitSequence(
                function_name=func_name,
                peripheral=peripheral_name,
                accesses=accesses,
            )
            analysis.init_sequences.append(init_seq)

        dma_cfgs = _extract_dma_configs(body, peripheral_name)
        analysis.dma_configs.extend(dma_cfgs)

    return analysis
