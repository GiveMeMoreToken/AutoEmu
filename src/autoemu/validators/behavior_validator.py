"""Behavioral validation of peripheral models.

Validates that a peripheral model's behavior matches expectations
derived from driver code analysis.
"""

from __future__ import annotations

from typing import Any

from autoemu.modeling_utils import canonical_flag_name, is_non_mmio_register
from autoemu.models.peripheral import Peripheral
from autoemu.models.register import AccessType


def validate_behavior(
    peripheral: Peripheral,
    driver_analysis: dict[str, Any],
) -> list[dict[str, Any]]:
    """Validate peripheral model behavior against driver patterns.

    Args:
        peripheral: The peripheral model to validate.
        driver_analysis: Dict from DriverAnalysis (register_accesses, isr_patterns, etc.)

    Returns:
        List of issue dicts with 'severity' and 'message'.
    """
    issues: list[dict[str, Any]] = []

    # Check that all registers accessed by driver exist in model
    accessed_registers = set()
    for access in driver_analysis.get("register_accesses", []):
        if isinstance(access, dict):
            reg_name = access.get("register", "")
        else:
            reg_name = getattr(access, "register", "")
        if is_non_mmio_register(reg_name):
            continue
        accessed_registers.add(reg_name)

    model_registers = {r.name for r in peripheral.register_block.registers}
    for reg_name in accessed_registers:
        if reg_name and reg_name not in model_registers:
            issues.append({
                "severity": "warning",
                "message": (
                    f"Driver accesses register '{reg_name}' "
                    f"which is not in the peripheral model"
                ),
            })

    # Check ISR patterns match interrupt model
    if peripheral.interrupt_model:
        model_flags = set()
        for line in peripheral.interrupt_model.lines:
            for flag in line.flags:
                model_flags.add(flag.name)

        for isr in driver_analysis.get("isr_patterns", []):
            checked = isr.get("checked_flags", []) if isinstance(isr, dict) else getattr(isr, "checked_flags", [])
            cleared = isr.get("cleared_flags", []) if isinstance(isr, dict) else getattr(isr, "cleared_flags", [])

            for flag in checked:
                canonical = canonical_flag_name(flag)
                if canonical not in model_flags:
                    issues.append({
                        "severity": "warning",
                        "message": (
                            f"ISR checks flag '{flag}' not in interrupt model"
                        ),
                    })
            for flag in cleared:
                canonical = canonical_flag_name(flag)
                if canonical not in model_flags:
                    issues.append({
                        "severity": "warning",
                        "message": (
                            f"ISR clears flag '{flag}' not in interrupt model"
                        ),
                    })

    # Check init sequence covers essential registers
    init_seqs = driver_analysis.get("init_sequences", [])
    if init_seqs:
        init_regs = set()
        for seq in init_seqs:
            accesses = seq.get("accesses", []) if isinstance(seq, dict) else getattr(seq, "accesses", [])
            for a in accesses:
                reg = a.get("register", "") if isinstance(a, dict) else getattr(a, "register", "")
                init_regs.add(reg)

        # Check that control registers are initialized
        for reg in peripheral.register_block.registers:
            if reg.name.startswith("CR") and reg.name not in init_regs:
                issues.append({
                    "severity": "info",
                    "message": (
                        f"Control register '{reg.name}' is not touched "
                        f"in any init sequence"
                    ),
                })

    # Validate state machine transitions against driver operations
    for sm in peripheral.state_machines:
        unreachable = set(s.name for s in sm.states) - sm.get_reachable_states()
        for state_name in unreachable:
            issues.append({
                "severity": "warning",
                "message": (
                    f"State '{state_name}' in machine '{sm.name}' "
                    f"is unreachable from initial state"
                ),
            })

    return issues


def replay_register_sequence(
    peripheral: Peripheral,
    sequence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Replay a sequence of register operations and check results.

    Each entry in sequence: {"type": "read"|"write", "offset": int, "value": int, "expected": int}
    Returns list of mismatches.
    """
    peripheral.reset()
    mismatches: list[dict[str, Any]] = []

    for i, op in enumerate(sequence):
        offset = op["offset"]
        op_type = op["type"]

        if op_type == "write":
            peripheral.write_register(offset, op["value"])
        elif op_type == "read":
            actual = peripheral.read_register(offset)
            expected = op.get("expected")
            if expected is not None and actual != expected:
                mismatches.append({
                    "step": i,
                    "offset": offset,
                    "type": op_type,
                    "expected": expected,
                    "actual": actual,
                })

    return mismatches
