"""Security audit validators for peripheral models.

Checks for potential security issues in peripheral register models:
- DMA boundary validation
- Privilege escalation risks
- Interrupt safety issues
- Reserved field write exposure
- Configuration lock bypass
"""

from __future__ import annotations

import re
from typing import Any

from autoemu.models.peripheral import Peripheral
from autoemu.models.register import AccessType


def validate_security(peripheral: Peripheral) -> list[dict[str, Any]]:
    """Run all security checks. Returns list of issues."""
    issues: list[dict[str, Any]] = []
    issues.extend(check_dma_boundaries(peripheral))
    issues.extend(check_privilege_escalation(peripheral))
    issues.extend(check_interrupt_safety(peripheral))
    issues.extend(check_reserved_field_writes(peripheral))
    issues.extend(check_config_lock_bypass(peripheral))
    return issues


_DMA_KEYWORDS = re.compile(r"(DMA|ADDR|PTR|BASE)", re.IGNORECASE)
_DMA_LENGTH_KEYWORDS = re.compile(r"(LEN|CNT|COUNT|SIZE|NDTR|NUM)", re.IGNORECASE)


def check_dma_boundaries(peripheral: Peripheral) -> list[dict[str, Any]]:
    """Flag DMA-like registers that lack bounds checking.

    Looks for registers whose names suggest DMA address descriptors
    (containing DMA, ADDR, PTR, BASE) that have RW access.  Flags when
    no corresponding length/count register exists nearby.
    """
    issues: list[dict[str, Any]] = []
    all_names = {r.name for r in peripheral.register_block.registers}

    for reg in peripheral.register_block.registers:
        if not _DMA_KEYWORDS.search(reg.name):
            continue

        # Only flag RW address-like registers
        if reg.access not in (AccessType.RW,):
            continue

        # Check if any field or the register name looks like an address
        is_addr_like = bool(re.search(r"(ADDR|PTR|BASE)", reg.name, re.IGNORECASE))
        if not is_addr_like:
            # Also check fields
            is_addr_like = any(
                re.search(r"(ADDR|PTR|BASE)", f.name, re.IGNORECASE)
                for f in reg.fields
            )

        if not is_addr_like:
            continue

        # Look for a matching length/count register nearby
        has_length_reg = any(
            _DMA_LENGTH_KEYWORDS.search(name)
            for name in all_names
        )

        if not has_length_reg:
            issues.append({
                "severity": "warning",
                "category": "dma_boundary",
                "message": (
                    f"DMA address register {reg.name} (offset 0x{reg.offset:X}) "
                    f"is RW with no corresponding length/count register — "
                    f"unbounded DMA risk"
                ),
                "register": reg.name,
            })

    return issues


_PRIV_KEYWORDS = re.compile(
    r"(LOCK|KEY|SEC|PROT|DEBUG|JTAG|RDP)", re.IGNORECASE
)


def check_privilege_escalation(peripheral: Peripheral) -> list[dict[str, Any]]:
    """Flag registers writable from unprivileged mode that control security features.

    Checks for registers with names containing security-related keywords
    (LOCK, KEY, SEC, PROT, DEBUG, JTAG, RDP) that have RW access and
    should likely be restricted.
    """
    issues: list[dict[str, Any]] = []

    for reg in peripheral.register_block.registers:
        # Check register-level name
        if not _PRIV_KEYWORDS.search(reg.name):
            # Also check field names
            sensitive_fields = [
                f for f in reg.fields if _PRIV_KEYWORDS.search(f.name)
            ]
            if not sensitive_fields:
                continue
            # Flag individual sensitive fields that are RW
            for f in sensitive_fields:
                if f.access == AccessType.RW:
                    issues.append({
                        "severity": "warning",
                        "category": "privilege_escalation",
                        "message": (
                            f"Security-sensitive field {f.name} in register "
                            f"{reg.name} (offset 0x{reg.offset:X}) has unrestricted "
                            f"RW access — potential privilege escalation"
                        ),
                        "register": reg.name,
                    })
            continue

        if reg.access == AccessType.RW:
            issues.append({
                "severity": "warning",
                "category": "privilege_escalation",
                "message": (
                    f"Security-sensitive register {reg.name} "
                    f"(offset 0x{reg.offset:X}) has unrestricted RW access — "
                    f"should likely be restricted or write-protected"
                ),
                "register": reg.name,
            })

    return issues


def check_interrupt_safety(peripheral: Peripheral) -> list[dict[str, Any]]:
    """Check for patterns that could cause infinite IRQ loops.

    Flags:
    - Flag bits with W1C access where the corresponding enable bit is in
      the same register (read-modify-write hazard).
    - ISR patterns in the interrupt model that check a flag but don't
      specify a clear mechanism.
    """
    issues: list[dict[str, Any]] = []

    # Check for flag + enable in the same register
    for reg in peripheral.register_block.registers:
        w1c_fields = [f for f in reg.fields if f.access == AccessType.W1C]
        rw_fields = [f for f in reg.fields if f.access == AccessType.RW]

        if w1c_fields and rw_fields:
            # If any RW field looks like an enable and a W1C looks like a flag,
            # that's a read-modify-write hazard
            enable_fields = [
                f for f in rw_fields
                if re.search(r"(EN|IE|ENABLE)", f.name, re.IGNORECASE)
            ]
            flag_fields = [
                f for f in w1c_fields
                if re.search(r"(FLAG|IF|SR|STATUS|PEND)", f.name, re.IGNORECASE)
            ]
            if enable_fields and flag_fields:
                issues.append({
                    "severity": "warning",
                    "category": "interrupt_safety",
                    "message": (
                        f"Register {reg.name} (offset 0x{reg.offset:X}) contains "
                        f"both interrupt flags ({', '.join(f.name for f in flag_fields)}) "
                        f"and enable bits ({', '.join(f.name for f in enable_fields)}) — "
                        f"read-modify-write hazard may cause infinite IRQ loop"
                    ),
                    "register": reg.name,
                })
            elif w1c_fields and rw_fields:
                # General case: W1C + RW in same register is still risky
                issues.append({
                    "severity": "info",
                    "category": "interrupt_safety",
                    "message": (
                        f"Register {reg.name} (offset 0x{reg.offset:X}) mixes "
                        f"W1C fields ({', '.join(f.name for f in w1c_fields)}) with "
                        f"RW fields — read-modify-write may inadvertently clear flags"
                    ),
                    "register": reg.name,
                })

    # Check interrupt model for ISR patterns missing clear
    if peripheral.interrupt_model:
        for line in peripheral.interrupt_model.lines:
            for flag in line.flags:
                if not flag.clear_register and not flag.clear_behavior:
                    issues.append({
                        "severity": "warning",
                        "category": "interrupt_safety",
                        "message": (
                            f"Interrupt flag {flag.name} on IRQ line "
                            f"{line.name} has no clear mechanism specified — "
                            f"may cause infinite IRQ assertion"
                        ),
                        "register": flag.register_name or "unknown",
                    })

    return issues


def check_reserved_field_writes(peripheral: Peripheral) -> list[dict[str, Any]]:
    """Warn about reserved fields that aren't properly protected.

    Flags:
    - RSVD-named fields that have RW access instead of RSVD access type.
    - Registers where more than 50% of bits are reserved.
    """
    issues: list[dict[str, Any]] = []

    for reg in peripheral.register_block.registers:
        if not reg.fields:
            continue

        reserved_bits = 0
        rsvd_rw_fields: list[str] = []

        for f in reg.fields:
            is_reserved_name = bool(
                re.search(r"(RSVD|RESERVED|RES)", f.name, re.IGNORECASE)
            )
            if is_reserved_name and f.access == AccessType.RW:
                rsvd_rw_fields.append(f.name)

            if f.access == AccessType.RSVD or is_reserved_name:
                reserved_bits += f.bit_width

        if rsvd_rw_fields:
            issues.append({
                "severity": "warning",
                "category": "reserved_field_writes",
                "message": (
                    f"Register {reg.name} (offset 0x{reg.offset:X}): reserved "
                    f"fields [{', '.join(rsvd_rw_fields)}] have RW access "
                    f"instead of RSVD — writes to reserved bits may cause "
                    f"undefined behavior"
                ),
                "register": reg.name,
            })

        if reserved_bits > reg.size / 2:
            pct = (reserved_bits / reg.size) * 100
            issues.append({
                "severity": "info",
                "category": "reserved_field_writes",
                "message": (
                    f"Register {reg.name} (offset 0x{reg.offset:X}): "
                    f"{reserved_bits}/{reg.size} bits ({pct:.0f}%) are reserved — "
                    f"register may be partially implemented or deprecated"
                ),
                "register": reg.name,
            })

    return issues


_CONFIG_KEYWORDS = re.compile(r"(CFG|CONFIG|MODE|INIT)", re.IGNORECASE)


def check_config_lock_bypass(peripheral: Peripheral) -> list[dict[str, Any]]:
    """Detect config registers that should be write-once but lack lock mechanism.

    Looks for registers with names containing CFG, CONFIG, MODE, INIT that:
    - Have state machine transitions suggesting they should only be written
      during init, but are RW at all times.
    - Have no corresponding LOCK register or field in the peripheral.
    """
    issues: list[dict[str, Any]] = []

    # Determine if the peripheral has any lock mechanism
    all_names = {r.name for r in peripheral.register_block.registers}
    all_field_names = {
        f.name
        for r in peripheral.register_block.registers
        for f in r.fields
    }
    has_lock = any(
        re.search(r"LOCK", n, re.IGNORECASE)
        for n in (all_names | all_field_names)
    )

    # Check state machines for init-only transitions
    init_only_regs: set[str] = set()
    for sm in peripheral.state_machines:
        for t in sm.transitions:
            # Transitions from an init-like state writing to config registers
            if re.search(r"(init|reset|idle)", t.source, re.IGNORECASE):
                if t.trigger.startswith("reg_write:"):
                    reg_name = t.trigger.split(":", 1)[1]
                    if _CONFIG_KEYWORDS.search(reg_name):
                        init_only_regs.add(reg_name)

    for reg in peripheral.register_block.registers:
        if not _CONFIG_KEYWORDS.search(reg.name):
            continue
        if reg.access != AccessType.RW:
            continue

        # Flag if no lock mechanism exists
        if not has_lock:
            severity = "warning"
            detail = ""
            if reg.name in init_only_regs:
                severity = "error"
                detail = (
                    " State machine suggests this register should only "
                    "be written during init."
                )

            issues.append({
                "severity": severity,
                "category": "config_lock_bypass",
                "message": (
                    f"Configuration register {reg.name} "
                    f"(offset 0x{reg.offset:X}) has unrestricted RW access "
                    f"with no lock mechanism in the peripheral.{detail}"
                ),
                "register": reg.name,
            })

    return issues
