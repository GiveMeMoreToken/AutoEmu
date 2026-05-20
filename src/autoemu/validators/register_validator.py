"""Register model validation.

Checks for structural issues in register models:
- Overlapping bit fields
- Missing or inconsistent reset values
- Access type conflicts
- Register offset gaps or overlaps
"""

from __future__ import annotations

from typing import Any

from autoemu.models.register import AccessType, Register, RegisterBlock


def validate_register_block(block: RegisterBlock) -> list[dict[str, Any]]:
    """Validate a register block for consistency issues.

    Returns a list of issue dicts with 'severity' and 'message' keys.
    severity: 'error', 'warning', 'info'
    """
    issues: list[dict[str, Any]] = []

    if not block.registers:
        issues.append(
            {
                "severity": "error",
                "message": f"Register block {block.name} must contain at least one register",
            }
        )

    # Check for duplicate register names
    names = [r.name for r in block.registers]
    seen: set[str] = set()
    for n in names:
        if n in seen:
            issues.append(
                {
                    "severity": "error",
                    "message": f"Duplicate register name: {n}",
                }
            )
        seen.add(n)

    # Check for overlapping register offsets
    offsets = sorted(
        [(r.offset, r.size // 8, r.name) for r in block.registers],
        key=lambda x: x[0],
    )
    for i in range(len(offsets) - 1):
        off1, size1, name1 = offsets[i]
        off2, _, name2 = offsets[i + 1]
        if off1 + size1 > off2:
            issues.append(
                {
                    "severity": "error",
                    "message": (
                        f"Register overlap: {name1} (0x{off1:X}+{size1}) "
                        f"overlaps with {name2} (0x{off2:X})"
                    ),
                }
            )

    # Check each register
    for reg in block.registers:
        issues.extend(_validate_register(reg))

    return issues


def _validate_register(reg: Register) -> list[dict[str, Any]]:
    """Validate a single register."""
    issues: list[dict[str, Any]] = []

    if not reg.fields:
        return issues

    # Check for overlapping bit fields
    occupied = [False] * reg.size
    for f in reg.fields:
        overlap_found = False
        for bit in range(f.bit_offset, min(f.bit_offset + f.bit_width, reg.size)):
            if occupied[bit] and not overlap_found:
                issues.append(
                    {
                        "severity": "error",
                        "message": (
                            f"Register {reg.name}: overlapping field {f.name} "
                            f"at bit {bit}"
                        ),
                    }
                )
                overlap_found = True
            occupied[bit] = True

    # Check field extends beyond register width
    for f in reg.fields:
        if f.bit_offset + f.bit_width > reg.size:
            issues.append(
                {
                    "severity": "error",
                    "message": (
                        f"Register {reg.name}: field {f.name} extends beyond "
                        f"register width ({f.bit_offset}+{f.bit_width} > {reg.size})"
                    ),
                }
            )

    # Check reset value consistency
    computed_reset = 0
    for f in reg.fields:
        computed_reset |= (f.reset_value & ((1 << f.bit_width) - 1)) << f.bit_offset

    if computed_reset != reg.reset_value:
        # Only warn if fields exist and don't cover all bits
        uncovered = sum(1 for b in occupied if not b)
        if uncovered == 0:
            issues.append(
                {
                    "severity": "warning",
                    "message": (
                        f"Register {reg.name}: field reset values (0x{computed_reset:08X}) "
                        f"don't match register reset value (0x{reg.reset_value:08X})"
                    ),
                }
            )

    # Check for suspicious access patterns
    for f in reg.fields:
        if f.access == AccessType.W1C and f.reset_value != 0:
            issues.append(
                {
                    "severity": "warning",
                    "message": (
                        f"Register {reg.name}: W1C field {f.name} has "
                        f"non-zero reset value ({f.reset_value})"
                    ),
                }
            )

    # Check for duplicate field names
    field_names: set[str] = set()
    for f in reg.fields:
        if f.name in field_names:
            issues.append(
                {
                    "severity": "error",
                    "message": f"Register {reg.name}: duplicate field name {f.name}",
                }
            )
        field_names.add(f.name)

    return issues
