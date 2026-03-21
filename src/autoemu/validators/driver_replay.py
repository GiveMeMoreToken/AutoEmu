"""Driver replay engine for peripheral model validation.

Replays register access traces captured from driver execution
against the peripheral model to verify behavioral consistency.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from autoemu.models.peripheral import Peripheral


@dataclass
class TraceEntry:
    """A single register access trace entry."""

    timestamp: int = 0  # Relative timestamp in cycles
    operation: str = ""  # "read" or "write"
    offset: int = 0
    value: int = 0
    expected: int | None = None  # Expected read value (for verification)
    source: str = ""  # e.g., function name


@dataclass
class ReplayResult:
    """Result of replaying a trace against a model."""

    total_operations: int = 0
    successful: int = 0
    mismatches: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        if self.total_operations == 0:
            return 1.0
        return self.successful / self.total_operations

    def summary(self) -> str:
        return (
            f"Replay: {self.successful}/{self.total_operations} operations matched "
            f"({self.accuracy:.1%} accuracy), "
            f"{len(self.mismatches)} mismatches, {len(self.errors)} errors"
        )


def parse_trace_file(path: str | Path) -> list[TraceEntry]:
    """Parse a register access trace file (JSON format).

    Expected format:
    [
        {"timestamp": 0, "operation": "write", "offset": "0x00", "value": "0x01"},
        {"timestamp": 1, "operation": "read", "offset": "0x04", "expected": "0x00"},
        ...
    ]
    """
    content = Path(path).read_text(encoding="utf-8")
    data = json.loads(content)

    entries: list[TraceEntry] = []
    for item in data:
        entry = TraceEntry(
            timestamp=item.get("timestamp", 0),
            operation=item.get("operation", "read"),
            offset=int(str(item.get("offset", 0)), 0),
            value=int(str(item.get("value", 0)), 0),
            expected=int(str(item["expected"]), 0) if "expected" in item else None,
            source=item.get("source", ""),
        )
        entries.append(entry)

    return sorted(entries, key=lambda e: e.timestamp)


def replay_trace(
    peripheral: Peripheral,
    trace: list[TraceEntry],
    stop_on_first_mismatch: bool = False,
) -> ReplayResult:
    """Replay a register access trace against a peripheral model.

    Args:
        peripheral: The peripheral model to test.
        trace: List of trace entries to replay.
        stop_on_first_mismatch: If True, stop at the first mismatch.

    Returns:
        ReplayResult with accuracy metrics and mismatch details.
    """
    peripheral.reset()
    result = ReplayResult(total_operations=len(trace))

    for i, entry in enumerate(trace):
        try:
            if entry.operation == "write":
                success = peripheral.write_register(entry.offset, entry.value)
                if not success:
                    result.errors.append(
                        f"Step {i}: write to unknown offset 0x{entry.offset:X}"
                    )
                else:
                    result.successful += 1

            elif entry.operation == "read":
                actual = peripheral.read_register(entry.offset)
                if actual is None:
                    result.errors.append(
                        f"Step {i}: read from unknown offset 0x{entry.offset:X}"
                    )
                elif entry.expected is not None:
                    if actual == entry.expected:
                        result.successful += 1
                    else:
                        result.mismatches.append({
                            "step": i,
                            "timestamp": entry.timestamp,
                            "offset": entry.offset,
                            "expected": entry.expected,
                            "actual": actual,
                            "source": entry.source,
                        })
                        if stop_on_first_mismatch:
                            break
                else:
                    result.successful += 1

        except Exception as e:
            result.errors.append(f"Step {i}: exception: {e}")

    return result


def generate_trace_from_init(
    peripheral: Peripheral,
    init_accesses: list[dict[str, Any]],
) -> list[TraceEntry]:
    """Generate a trace from driver init sequence analysis.

    Converts driver_parser.RegisterAccess-like dicts into trace entries
    for replay validation.
    """
    trace: list[TraceEntry] = []
    reg_lookup = {r.name: r for r in peripheral.register_block.registers}

    for i, access in enumerate(init_accesses):
        reg_name = access.get("register", "")
        reg = reg_lookup.get(reg_name)
        if not reg:
            continue

        access_type = access.get("access_type", "read")

        if access_type in ("write", "set_bit", "modify"):
            trace.append(TraceEntry(
                timestamp=i,
                operation="write",
                offset=reg.offset,
                value=0,  # Actual value would need expression evaluation
                source=access.get("in_function", ""),
            ))
        elif access_type == "read":
            trace.append(TraceEntry(
                timestamp=i,
                operation="read",
                offset=reg.offset,
                source=access.get("in_function", ""),
            ))

    return trace
