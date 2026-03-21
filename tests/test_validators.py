"""Tests for validators."""

import pytest
from autoemu.models.register import AccessType, BitField, Register, RegisterBlock
from autoemu.models.peripheral import Peripheral
from autoemu.validators.register_validator import validate_register_block
from autoemu.validators.behavior_validator import validate_behavior, replay_register_sequence


class TestRegisterValidator:
    def test_valid_block(self):
        block = RegisterBlock(
            name="TEST",
            registers=[
                Register(name="CR", offset=0x00),
                Register(name="SR", offset=0x04),
            ],
        )
        issues = validate_register_block(block)
        assert len(issues) == 0

    def test_duplicate_name(self):
        block = RegisterBlock(
            name="TEST",
            registers=[
                Register(name="CR", offset=0x00),
                Register(name="CR", offset=0x04),
            ],
        )
        issues = validate_register_block(block)
        errors = [i for i in issues if i["severity"] == "error"]
        assert any("Duplicate" in i["message"] for i in errors)

    def test_overlapping_fields(self):
        block = RegisterBlock(
            name="TEST",
            registers=[
                Register(
                    name="CR", offset=0x00,
                    fields=[
                        BitField(name="A", bit_offset=0, bit_width=4),
                        BitField(name="B", bit_offset=2, bit_width=4),  # overlaps A
                    ],
                ),
            ],
        )
        issues = validate_register_block(block)
        assert any("overlapping" in i["message"] for i in issues)

    def test_field_exceeds_width(self):
        block = RegisterBlock(
            name="TEST",
            registers=[
                Register(
                    name="CR", offset=0x00, size=8,
                    fields=[
                        BitField(name="BIG", bit_offset=4, bit_width=8),
                    ],
                ),
            ],
        )
        issues = validate_register_block(block)
        assert any("extends beyond" in i["message"] for i in issues)


class TestBehaviorValidator:
    def test_missing_register(self):
        peripheral = Peripheral(
            name="TEST",
            register_block=RegisterBlock(
                name="TEST",
                registers=[Register(name="CR", offset=0x00)],
            ),
        )
        driver_data = {
            "register_accesses": [{"register": "NONEXIST"}],
            "isr_patterns": [],
            "init_sequences": [],
        }
        issues = validate_behavior(peripheral, driver_data)
        assert any("not in the peripheral model" in i["message"] for i in issues)

    def test_replay_register_sequence(self):
        peripheral = Peripheral(
            name="TEST",
            register_block=RegisterBlock(
                name="TEST",
                registers=[
                    Register(name="CR", offset=0x00, reset_value=0),
                    Register(name="DR", offset=0x04, reset_value=0),
                ],
            ),
        )
        sequence = [
            {"type": "write", "offset": 0x00, "value": 0x1234},
            {"type": "read", "offset": 0x00, "expected": 0x1234},
            {"type": "read", "offset": 0x04, "expected": 0},
        ]
        mismatches = replay_register_sequence(peripheral, sequence)
        assert len(mismatches) == 0

    def test_replay_detects_mismatch(self):
        peripheral = Peripheral(
            name="TEST",
            register_block=RegisterBlock(
                name="TEST",
                registers=[Register(name="CR", offset=0x00, reset_value=0)],
            ),
        )
        sequence = [
            {"type": "read", "offset": 0x00, "expected": 0xFFFF},
        ]
        mismatches = replay_register_sequence(peripheral, sequence)
        assert len(mismatches) == 1
        assert mismatches[0]["expected"] == 0xFFFF
        assert mismatches[0]["actual"] == 0
