"""Tests for code generators (targeting QEMU v9.2.4)."""

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
from autoemu.generators.qemu_tree_generator import generate_qemu_tree_artifacts
from autoemu.generators.test_generator import generate_test_harness
from autoemu.models.qemu import build_qemu_hardware_model


def _make_custom_hardware_model(periph: Peripheral):
    hardware_model = build_qemu_hardware_model(periph)
    hardware_model.identity = hardware_model.identity.model_copy(
        update={
            "peripheral_name": "ACCELERATOR",
            "qom_type": "vendor-accelerator",
            "c_identifier_prefix": "vendor_accel",
            "type_macro": "TYPE_VENDOR_ACCEL",
            "state_struct_name": "VendorAccelState",
            "kconfig_symbol": "VENDOR_ACCEL",
        }
    )
    hardware_model.file_layout = hardware_model.file_layout.model_copy(
        update={
            "source_path": "hw/dma/vendor_accel.c",
            "header_path": "include/hw/dma/vendor_accel.h",
            "meson_path": "hw/dma/meson.build",
            "meson_snippet_path": "hw/dma/vendor_accel.meson.inc",
            "qtest_path": "tests/qtest/vendor_accel-test.c",
        }
    )
    hardware_model.device_tree.node_name = "accelerator"
    hardware_model.device_tree.compatible = ["vendor,accelerator"]
    return hardware_model


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
    def test_target_version(self):
        assert QEMU_TARGET_VERSION == "v9.2.4"

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

            assert "QEMU v9.2.4" in content
            assert "TYPE_STM32F4_TEST_PERIPH" in content
            assert "TEST_PERIPH_CR_OFFSET" in content
            assert "TEST_PERIPH_SR_OFFSET" in content
            assert "MemoryRegion mmio" in content
            assert "qemu_irq irq" in content

    def test_source_v924_apis(self):
        """Verify generated C code uses QEMU v9.2.4 APIs."""
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

            # QEMU v9.2.4 specific: device_class_set_legacy_reset
            assert "device_class_set_legacy_reset" in content
            assert "dc->reset" not in content

            # QEMU v9.2.4 specific: hw/qdev-properties.h
            assert 'hw/qdev-properties.h' in content

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


class TestQEMUTreeGenerator:
    def test_writes_source_and_header_under_schema_tree_paths(self):
        periph = _make_test_peripheral()
        hardware_model = build_qemu_hardware_model(periph)

        with tempfile.TemporaryDirectory() as tmpdir:
            files = generate_qemu_tree_artifacts(periph, tmpdir, hardware_model=hardware_model)

            expected_source = Path(tmpdir) / hardware_model.file_layout.source_path
            expected_header = Path(tmpdir) / hardware_model.file_layout.header_path
            assert str(expected_source) in files
            assert str(expected_header) in files
            assert expected_source.exists()
            assert expected_header.exists()

    def test_builds_hardware_model_when_omitted(self):
        periph = _make_test_peripheral()

        with tempfile.TemporaryDirectory() as tmpdir:
            files = generate_qemu_tree_artifacts(
                periph,
                tmpdir,
                driver_analysis={
                    "state_hints": [
                        {"kind": "compatible", "value": "vendor,auto-built"},
                    ],
                },
            )

            dts_path = next(Path(path) for path in files if Path(path).suffix == ".dtsi")
            dts = dts_path.read_text(encoding="utf-8")
            assert 'compatible = "vendor,auto-built", "stm32f4,test_periph";' in dts

    def test_meson_and_kconfig_use_schema_identity_and_source_basename(self):
        periph = _make_test_peripheral()
        hardware_model = build_qemu_hardware_model(periph)

        with tempfile.TemporaryDirectory() as tmpdir:
            files = generate_qemu_tree_artifacts(periph, tmpdir, hardware_model=hardware_model)

            source_basename = Path(hardware_model.file_layout.source_path).name
            meson_path = Path(tmpdir) / hardware_model.file_layout.meson_snippet_path
            kconfig_path = next(Path(path) for path in files if Path(path).suffix == ".kconfig")
            meson = meson_path.read_text(encoding="utf-8")
            kconfig = kconfig_path.read_text(encoding="utf-8")

            assert f"CONFIG_{hardware_model.identity.kconfig_symbol}" in meson
            assert f"files('{source_basename}')" in meson
            assert f"config {hardware_model.identity.kconfig_symbol}" in kconfig
            assert source_basename in kconfig

    def test_device_tree_snippet_uses_32_bit_cells_interrupt_names_and_properties(self):
        periph = _make_test_peripheral()
        hardware_model = build_qemu_hardware_model(
            periph,
            {
                "state_hints": [
                    {"kind": "compatible", "value": "vendor,test-periph"},
                    {"kind": "irq_resource", "name": "done"},
                    {"kind": "irq_resource", "name": "error"},
                ],
            },
        )
        hardware_model.device_tree.properties = {
            "dma-coherent": True,
            "clock-frequency": 12_000_000,
            "status": "okay",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            files = generate_qemu_tree_artifacts(periph, tmpdir, hardware_model=hardware_model)

            dts_path = next(Path(path) for path in files if Path(path).suffix == ".dtsi")
            dts = dts_path.read_text(encoding="utf-8")

            assert 'compatible = "vendor,test-periph", "stm32f4,test_periph";' in dts
            assert "reg = <0x40000000 0x00000010>;" in dts
            assert 'interrupt-names = "done", "error";' in dts
            assert "dma-coherent;" in dts
            assert "clock-frequency = <12000000>;" in dts
            assert 'status = "okay";' in dts

    def test_device_tree_cell_metadata_is_emitted_on_parent_node(self):
        periph = _make_test_peripheral()
        hardware_model = build_qemu_hardware_model(periph)

        with tempfile.TemporaryDirectory() as tmpdir:
            files = generate_qemu_tree_artifacts(periph, tmpdir, hardware_model=hardware_model)

            dts_path = next(Path(path) for path in files if Path(path).suffix == ".dtsi")
            dts = dts_path.read_text(encoding="utf-8")
            child_node = dts[dts.index("test_periph@40000000"):]

            assert "\n    #address-cells = <1>;" in dts
            assert "\n    #size-cells = <1>;" in dts
            assert "\n        #address-cells" not in child_node
            assert "\n        #size-cells" not in child_node

    def test_device_tree_snippet_uses_64_bit_reg_cells_for_high_addresses(self):
        periph = _make_test_peripheral()
        periph.base_address = 0x1_0000_1000
        periph.address_size = 0x200
        hardware_model = build_qemu_hardware_model(periph)

        with tempfile.TemporaryDirectory() as tmpdir:
            files = generate_qemu_tree_artifacts(periph, tmpdir, hardware_model=hardware_model)

            dts_path = next(Path(path) for path in files if Path(path).suffix == ".dtsi")
            dts = dts_path.read_text(encoding="utf-8")

            assert "#address-cells = <2>;" in dts
            assert "#size-cells = <1>;" in dts
            assert "reg = <0x00000001 0x00001000 0x00000200>;" in dts

    def test_tree_source_and_header_use_schema_identity_when_it_differs_from_peripheral(self):
        periph = _make_test_peripheral()
        hardware_model = _make_custom_hardware_model(periph)

        with tempfile.TemporaryDirectory() as tmpdir:
            generate_qemu_tree_artifacts(periph, tmpdir, hardware_model=hardware_model)

            source = (Path(tmpdir) / hardware_model.file_layout.source_path).read_text(encoding="utf-8")
            header = (Path(tmpdir) / hardware_model.file_layout.header_path).read_text(encoding="utf-8")

            assert '#include "hw/dma/vendor_accel.h"' in source
            assert "vendor_accel_read" in source
            assert "VendorAccelState" in source
            assert "TYPE_VENDOR_ACCEL" in source
            assert '"vendor-accelerator"' in header
            assert "OBJECT_DECLARE_SIMPLE_TYPE(VendorAccelState, VENDOR_ACCEL)" in header
            assert "TYPE_STM32F4_TEST_PERIPH" not in source
            assert "STM32F4TEST_PERIPHState" not in header

    def test_tree_qtest_uses_schema_identity_when_it_differs_from_peripheral(self):
        periph = _make_test_peripheral()
        hardware_model = _make_custom_hardware_model(periph)

        with tempfile.TemporaryDirectory() as tmpdir:
            generate_qemu_tree_artifacts(periph, tmpdir, hardware_model=hardware_model)

            qtest = (Path(tmpdir) / hardware_model.file_layout.qtest_path).read_text(encoding="utf-8")

            assert "QTest for VENDOR_ACCEL ACCELERATOR peripheral model" in qtest
            assert "#define ACCELERATOR_BASE  0x40000000ULL" in qtest
            assert "test_accelerator_reset" in qtest
            assert '"/vendor/accelerator/reset"' in qtest
            assert "TEST_PERIPH_BASE" not in qtest
            assert '"/stm32f4/test_periph/reset"' not in qtest


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
