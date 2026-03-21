"""Tests for code generators."""

import tempfile
from pathlib import Path

import pytest
from autoemu.models.register import AccessType, BitField, Register, RegisterBlock
from autoemu.models.peripheral import Peripheral, PeripheralType
from autoemu.models.interrupt import (
    InterruptModel, InterruptLine, InterruptFlag, FlagBehavior,
)
from autoemu.generators.qemu_generator import generate_peripheral_code
from autoemu.generators.test_generator import generate_test_harness


def _make_test_peripheral() -> Peripheral:
    return Peripheral(
        name="TEST_PERIPH",
        peripheral_type=PeripheralType.GENERIC,
        base_address=0x40000000,
        register_block=RegisterBlock(
            name="TEST_PERIPH",
            registers=[
                Register(
                    name="CR",
                    offset=0x00,
                    reset_value=0x00,
                    fields=[
                        BitField(name="EN", bit_offset=0, bit_width=1, access=AccessType.RW),
                        BitField(name="MODE", bit_offset=1, bit_width=2, access=AccessType.RW),
                    ],
                ),
                Register(
                    name="SR",
                    offset=0x04,
                    reset_value=0x00,
                    access=AccessType.RO,
                    fields=[
                        BitField(name="BSY", bit_offset=0, bit_width=1, access=AccessType.RO),
                        BitField(name="TXE", bit_offset=1, bit_width=1, access=AccessType.RO),
                    ],
                ),
                Register(
                    name="ICR",
                    offset=0x08,
                    fields=[
                        BitField(name="TC", bit_offset=0, bit_width=1, access=AccessType.W1C),
                        BitField(name="IDLE", bit_offset=1, bit_width=1, access=AccessType.W1C),
                    ],
                ),
                Register(name="DR", offset=0x0C, reset_value=0x00),
            ],
        ),
        interrupt_model=InterruptModel(
            peripheral_name="TEST_PERIPH",
            lines=[
                InterruptLine(
                    irq_number=37,
                    name="TEST_IRQn",
                    flags=[
                        InterruptFlag(
                            name="TC",
                            register_name="ICR",
                            bit_offset=0,
                            clear_behavior=FlagBehavior.W1C,
                            enable_register="CR",
                            enable_bit_offset=0,
                        ),
                    ],
                ),
            ],
        ),
    )


class TestQEMUGenerator:
    def test_generates_files(self):
        periph = _make_test_peripheral()
        with tempfile.TemporaryDirectory() as tmpdir:
            files = generate_peripheral_code(periph, tmpdir)
            assert len(files) == 3

            paths = [Path(f) for f in files]
            extensions = {p.suffix for p in paths}
            assert ".h" in extensions
            assert ".c" in extensions
            assert ".json" in extensions

    def test_header_content(self):
        periph = _make_test_peripheral()
        with tempfile.TemporaryDirectory() as tmpdir:
            files = generate_peripheral_code(periph, tmpdir)
            header = [f for f in files if f.endswith(".h")][0]
            content = Path(header).read_text()

            assert "TYPE_STM32_TEST_PERIPH" in content
            assert "TEST_PERIPH_CR_OFFSET" in content
            assert "TEST_PERIPH_SR_OFFSET" in content
            assert "MemoryRegion mmio" in content
            assert "qemu_irq irq" in content

    def test_source_content(self):
        periph = _make_test_peripheral()
        with tempfile.TemporaryDirectory() as tmpdir:
            files = generate_peripheral_code(periph, tmpdir)
            source = [f for f in files if f.endswith(".c")][0]
            content = Path(source).read_text()

            assert "stm32_test_periph_read" in content
            assert "stm32_test_periph_write" in content
            assert "stm32_test_periph_reset" in content
            assert "MemoryRegionOps" in content
            assert "type_register_static" in content
            # Check W1C handling
            assert "Write-1-to-clear" in content


class TestTestGenerator:
    def test_generates_test_file(self):
        periph = _make_test_peripheral()
        with tempfile.TemporaryDirectory() as tmpdir:
            files = generate_test_harness(periph, tmpdir)
            assert len(files) == 1
            assert files[0].endswith(".c")

    def test_test_content(self):
        periph = _make_test_peripheral()
        with tempfile.TemporaryDirectory() as tmpdir:
            files = generate_test_harness(periph, tmpdir)
            content = Path(files[0]).read_text()
            assert "test_reset_values" in content
            assert "test_w1c_behavior" in content
            assert "int main" in content
