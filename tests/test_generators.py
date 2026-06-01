"""Tests for code generators targeting latest upstream QEMU."""

import tempfile
from pathlib import Path

from autoemu.models.register import AccessType, BitField, Register, RegisterBlock
from autoemu.models.peripheral import Peripheral, PeripheralType
from autoemu.models.interrupt import (
    InterruptModel, InterruptLine, InterruptFlag, FlagBehavior,
)
from autoemu.generators.qemu_generator import (
    generate_peripheral_code,
    QEMU_TARGET_VERSION,
)
def _make_test_peripheral() -> Peripheral:
    return Peripheral(
        name="TEST_PERIPH",
        peripheral_type=PeripheralType.GENERIC,
        base_address=0x40000000,
        mcu_family="STM32F4",
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
            assert len(files) == 5  # .h, .c, meson.build, qtest .c, .json

            names = [Path(f).name for f in files]
            assert "stm32f4_test_periph.h" in names
            assert "stm32f4_test_periph.c" in names
            assert "meson.build" in names
            assert "qtest_stm32f4_test_periph.c" in names
            assert "stm32f4_test_periph_model.json" in names

    def test_header_content(self):
        periph = _make_test_peripheral()
        with tempfile.TemporaryDirectory() as tmpdir:
            files = generate_peripheral_code(periph, tmpdir)
            header = [f for f in files if f.endswith(".h")][0]
            content = Path(header).read_text()

            assert "latest upstream QEMU" in content
            assert "TYPE_STM32F4_TEST_PERIPH" in content
            assert 'hw/sysbus.h' in content
            assert 'hw/core/sysbus.h' not in content
            assert "TEST_PERIPH_CR_OFFSET" in content
            assert "TEST_PERIPH_SR_OFFSET" in content
            assert "MemoryRegion mmio" in content
            assert "qemu_irq irq" in content

    def test_source_latest_qemu_apis(self):
        """Verify generated C code uses current QEMU APIs."""
        periph = _make_test_peripheral()
        with tempfile.TemporaryDirectory() as tmpdir:
            files = generate_peripheral_code(periph, tmpdir)
            source = [f for f in files if Path(f).name == "stm32f4_test_periph.c"][0]
            content = Path(source).read_text()

            # Core functions present
            assert "stm32f4_test_periph_read" in content
            assert "stm32f4_test_periph_write" in content
            assert "stm32f4_test_periph_reset" in content
            assert "MemoryRegionOps" in content
            assert "type_register_static" in content
            assert "Write-1-to-clear" in content

            # Current QEMU reset API: device_class_set_legacy_reset
            assert "device_class_set_legacy_reset" in content
            assert "dc->reset" not in content

            # DeviceClass access in latest QEMU requires hw/core/qdev-properties.h
            assert 'hw/qdev-properties.h' in content
            assert 'hw/core/qdev-properties.h' not in content
            assert 'hw/irq.h' in content

            # VMSTATE uses bare field names (not s->field)
            assert "VMSTATE_UINT32(cr," in content
            assert "VMSTATE_UINT32(s->" not in content

    def test_meson_build(self):
        periph = _make_test_peripheral()
        with tempfile.TemporaryDirectory() as tmpdir:
            files = generate_peripheral_code(periph, tmpdir)
            meson = [f for f in files if Path(f).name == "meson.build"][0]
            content = Path(meson).read_text()

            assert "system_ss.add" in content
            assert "CONFIG_STM32F4_TEST_PERIPH" in content
            assert "stm32f4_test_periph.c" in content
            assert QEMU_TARGET_VERSION in content

    def test_qtest_content(self):
        periph = _make_test_peripheral()
        with tempfile.TemporaryDirectory() as tmpdir:
            files = generate_peripheral_code(periph, tmpdir)
            qtest = [f for f in files if "qtest_" in Path(f).name][0]
            content = Path(qtest).read_text()

            assert "libqtest.h" in content
            assert "qtest_init" in content
            assert "qtest_readl" in content
            assert "g_test_run" in content
            assert "test_test_periph_reset" in content
            assert QEMU_TARGET_VERSION in content

    def test_source_handles_interrupt_flags_without_enable_register(self):
        periph = _make_test_peripheral()
        periph.interrupt_model.lines[0].flags.append(
            InterruptFlag(
                name="WAKEUP",
                register_name="SR",
                bit_offset=1,
                clear_behavior=FlagBehavior.SOFTWARE_CLEAR,
                enable_register="",
                enable_bit_offset=0,
            )
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            files = generate_peripheral_code(periph, tmpdir)
            source = [f for f in files if Path(f).name == "stm32f4_test_periph.c"][0]
            content = Path(source).read_text()

            assert "(s->sr & (1U << 1))" in content
            assert "s-> &" not in content

    def test_source_derives_interrupt_status_and_clears_raw_status(self):
        periph = Peripheral(
            name="GPU",
            peripheral_type=PeripheralType.GENERIC,
            base_address=0xE82C0000,
            address_size=0x4000,
            mcu_family="HISI",
            register_block=RegisterBlock(
                name="GPU",
                registers=[
                    Register(name="MMU_INT_RAWSTAT", offset=0x2000, access=AccessType.RO),
                    Register(name="MMU_INT_CLEAR", offset=0x2004, access=AccessType.W1C),
                    Register(name="MMU_INT_MASK", offset=0x2008),
                    Register(name="MMU_INT_STAT", offset=0x200C, access=AccessType.RO),
                ],
            ),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            files = generate_peripheral_code(periph, tmpdir)
            source = [f for f in files if Path(f).name == "hisi_gpu.c"][0]
            content = Path(source).read_text()

        assert "return s->mmu_int_rawstat & ~s->mmu_int_mask;" in content
        assert "s->mmu_int_rawstat &= ~value;" in content
        assert "s->mmu_int_clear = value;" not in content
