"""Tests for security audit validators."""

import pytest

from autoemu.models.peripheral import Peripheral
from autoemu.models.register import AccessType, BitField, Register, RegisterBlock
from autoemu.models.interrupt import InterruptFlag, InterruptLine, InterruptModel
from autoemu.models.state_machine import State, StateMachine, Transition
from autoemu.validators.security_validator import (
    check_config_lock_bypass,
    check_dma_boundaries,
    check_interrupt_safety,
    check_privilege_escalation,
    check_reserved_field_writes,
    validate_security,
)


def _simple_peripheral(name: str = "TEST", registers: list[Register] | None = None) -> Peripheral:
    """Build a minimal peripheral for testing."""
    return Peripheral(
        name=name,
        register_block=RegisterBlock(
            name=name,
            registers=registers or [],
        ),
    )


# -- DMA boundaries --------------------------------------------------------


class TestCheckDmaBoundaries:
    def test_dma_addr_register_flagged(self):
        periph = _simple_peripheral(registers=[
            Register(name="DMA_SADDR", offset=0x00, access=AccessType.RW),
            Register(name="CR", offset=0x04, access=AccessType.RW),
        ])
        issues = check_dma_boundaries(periph)
        assert len(issues) >= 1
        assert issues[0]["category"] == "dma_boundary"
        assert "DMA_SADDR" in issues[0]["message"]

    def test_dma_addr_with_length_register_ok(self):
        periph = _simple_peripheral(registers=[
            Register(name="DMA_SADDR", offset=0x00, access=AccessType.RW),
            Register(name="DMA_NDTR", offset=0x04, access=AccessType.RW),
        ])
        issues = check_dma_boundaries(periph)
        assert len(issues) == 0

    def test_dma_base_field_flagged(self):
        periph = _simple_peripheral(registers=[
            Register(
                name="DMA_CFG", offset=0x00, access=AccessType.RW,
                fields=[
                    BitField(name="BASE_ADDR", bit_offset=0, bit_width=16, access=AccessType.RW),
                ],
            ),
        ])
        issues = check_dma_boundaries(periph)
        assert any("DMA_CFG" in i["message"] for i in issues)


# -- Privilege escalation ---------------------------------------------------


class TestCheckPrivilegeEscalation:
    def test_lock_register_flagged(self):
        periph = _simple_peripheral(registers=[
            Register(name="FLASH_LOCK", offset=0x00, access=AccessType.RW),
        ])
        issues = check_privilege_escalation(periph)
        assert len(issues) >= 1
        assert issues[0]["category"] == "privilege_escalation"
        assert "FLASH_LOCK" in issues[0]["message"]

    def test_prot_field_flagged(self):
        periph = _simple_peripheral(registers=[
            Register(
                name="CTRL", offset=0x00, access=AccessType.RW,
                fields=[
                    BitField(name="PROT_LEVEL", bit_offset=0, bit_width=2, access=AccessType.RW),
                    BitField(name="DATA", bit_offset=2, bit_width=8, access=AccessType.RW),
                ],
            ),
        ])
        issues = check_privilege_escalation(periph)
        assert any("PROT_LEVEL" in i["message"] for i in issues)

    def test_ro_security_register_not_flagged(self):
        periph = _simple_peripheral(registers=[
            Register(name="SEC_STATUS", offset=0x00, access=AccessType.RO),
        ])
        issues = check_privilege_escalation(periph)
        assert len(issues) == 0


# -- Interrupt safety -------------------------------------------------------


class TestCheckInterruptSafety:
    def test_flag_and_enable_in_same_register_flagged(self):
        periph = _simple_peripheral(registers=[
            Register(
                name="ISR", offset=0x00, access=AccessType.RW,
                fields=[
                    BitField(name="TX_FLAG", bit_offset=0, bit_width=1, access=AccessType.W1C),
                    BitField(name="TX_IE", bit_offset=8, bit_width=1, access=AccessType.RW),
                ],
            ),
        ])
        issues = check_interrupt_safety(periph)
        assert any(i["category"] == "interrupt_safety" for i in issues)
        assert any("TX_FLAG" in i["message"] for i in issues)

    def test_w1c_and_rw_in_same_register_info(self):
        """W1C + RW without flag/enable naming gets info severity."""
        periph = _simple_peripheral(registers=[
            Register(
                name="SR", offset=0x00, access=AccessType.RW,
                fields=[
                    BitField(name="DONE", bit_offset=0, bit_width=1, access=AccessType.W1C),
                    BitField(name="VALUE", bit_offset=1, bit_width=7, access=AccessType.RW),
                ],
            ),
        ])
        issues = check_interrupt_safety(periph)
        assert any(i["severity"] == "info" for i in issues)

    def test_separate_registers_no_issue(self):
        periph = _simple_peripheral(registers=[
            Register(
                name="SR", offset=0x00, access=AccessType.RW,
                fields=[
                    BitField(name="FLAG", bit_offset=0, bit_width=1, access=AccessType.W1C),
                ],
            ),
            Register(
                name="IER", offset=0x04, access=AccessType.RW,
                fields=[
                    BitField(name="EN", bit_offset=0, bit_width=1, access=AccessType.RW),
                ],
            ),
        ])
        issues = check_interrupt_safety(periph)
        # No issue because flag and enable are in separate registers
        flag_issues = [i for i in issues if i["severity"] == "warning"]
        assert len(flag_issues) == 0


# -- Reserved field writes --------------------------------------------------


class TestCheckReservedFieldWrites:
    def test_rsvd_field_with_rw_access_flagged(self):
        periph = _simple_peripheral(registers=[
            Register(
                name="CR", offset=0x00, access=AccessType.RW,
                fields=[
                    BitField(name="EN", bit_offset=0, bit_width=1, access=AccessType.RW),
                    BitField(name="RESERVED", bit_offset=1, bit_width=31, access=AccessType.RW),
                ],
            ),
        ])
        issues = check_reserved_field_writes(periph)
        rw_issues = [i for i in issues if "RW access" in i.get("message", "")]
        assert len(rw_issues) >= 1
        assert rw_issues[0]["category"] == "reserved_field_writes"

    def test_proper_rsvd_access_not_flagged_for_rw(self):
        periph = _simple_peripheral(registers=[
            Register(
                name="CR", offset=0x00, access=AccessType.RW,
                fields=[
                    BitField(name="EN", bit_offset=0, bit_width=1, access=AccessType.RW),
                    BitField(name="RESERVED", bit_offset=1, bit_width=31, access=AccessType.RSVD),
                ],
            ),
        ])
        issues = check_reserved_field_writes(periph)
        rw_issues = [i for i in issues if "RW access" in i.get("message", "")]
        assert len(rw_issues) == 0

    def test_majority_reserved_bits_flagged(self):
        periph = _simple_peripheral(registers=[
            Register(
                name="SR", offset=0x00, access=AccessType.RW,
                fields=[
                    BitField(name="FLAG", bit_offset=0, bit_width=1, access=AccessType.RW),
                    BitField(name="RSVD", bit_offset=1, bit_width=31, access=AccessType.RSVD),
                ],
            ),
        ])
        issues = check_reserved_field_writes(periph)
        info_issues = [i for i in issues if i["severity"] == "info"]
        assert any("96%" in i["message"] or "reserved" in i["message"].lower() for i in info_issues)


# -- Config lock bypass -----------------------------------------------------


class TestCheckConfigLockBypass:
    def test_config_register_without_lock_flagged(self):
        periph = _simple_peripheral(registers=[
            Register(name="CFG", offset=0x00, access=AccessType.RW),
        ])
        issues = check_config_lock_bypass(periph)
        assert len(issues) >= 1
        assert issues[0]["category"] == "config_lock_bypass"
        assert "CFG" in issues[0]["message"]

    def test_config_register_with_lock_not_flagged(self):
        periph = _simple_peripheral(registers=[
            Register(name="CFG", offset=0x00, access=AccessType.RW),
            Register(name="LOCK", offset=0x04, access=AccessType.RW),
        ])
        issues = check_config_lock_bypass(periph)
        assert len(issues) == 0

    def test_config_with_state_machine_init_only(self):
        periph = Peripheral(
            name="TEST",
            register_block=RegisterBlock(
                name="TEST",
                registers=[
                    Register(name="MODE_CFG", offset=0x00, access=AccessType.RW),
                ],
            ),
            state_machines=[
                StateMachine(
                    name="main",
                    states=[
                        State(name="init", is_initial=True),
                        State(name="running"),
                    ],
                    transitions=[
                        Transition(
                            source="init",
                            target="running",
                            trigger="reg_write:MODE_CFG",
                        ),
                    ],
                ),
            ],
        )
        issues = check_config_lock_bypass(periph)
        assert len(issues) >= 1
        assert issues[0]["severity"] == "error"


# -- Combined ---------------------------------------------------------------


class TestValidateSecurity:
    def test_all_checks_run(self):
        """validate_security runs every category and combines results."""
        periph = Peripheral(
            name="COMBO",
            register_block=RegisterBlock(
                name="COMBO",
                registers=[
                    Register(name="DMA_SADDR", offset=0x00, access=AccessType.RW),
                    Register(name="FLASH_KEY", offset=0x04, access=AccessType.RW),
                    Register(name="CFG", offset=0x08, access=AccessType.RW),
                    Register(
                        name="ISR", offset=0x0C, access=AccessType.RW,
                        fields=[
                            BitField(name="TX_FLAG", bit_offset=0, bit_width=1, access=AccessType.W1C),
                            BitField(name="TX_IE", bit_offset=8, bit_width=1, access=AccessType.RW),
                        ],
                    ),
                    Register(
                        name="MISC", offset=0x10, access=AccessType.RW,
                        fields=[
                            BitField(name="RESERVED", bit_offset=0, bit_width=32, access=AccessType.RW),
                        ],
                    ),
                ],
            ),
        )
        issues = validate_security(periph)
        categories = {i["category"] for i in issues}
        assert "dma_boundary" in categories
        assert "privilege_escalation" in categories
        assert "interrupt_safety" in categories
        assert "reserved_field_writes" in categories
        assert "config_lock_bypass" in categories

    def test_clean_peripheral_no_issues(self):
        """A simple peripheral with no risky patterns produces no issues."""
        periph = _simple_peripheral(registers=[
            Register(
                name="CR", offset=0x00, access=AccessType.RW,
                fields=[
                    BitField(name="EN", bit_offset=0, bit_width=1, access=AccessType.RW),
                ],
            ),
            Register(name="DR", offset=0x04, access=AccessType.RO),
        ])
        issues = validate_security(periph)
        assert len(issues) == 0
