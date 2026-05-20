"""Tests for data models."""

import pytest
from pydantic import ValidationError

from autoemu.models.register import AccessType, BitField, Register, RegisterBlock
from autoemu.models.state_machine import State, Transition, StateMachine
from autoemu.models.interrupt import InterruptLine, InterruptModel
from autoemu.models.dependency import DependencyEdge, DependencyGraph, DependencyType
from autoemu.models.peripheral import Peripheral
from autoemu.models.qemu import (
    QEMUDeviceTreeRegRegion,
    QEMUIRQResource,
    QEMUMMIORegion,
    build_qemu_hardware_model,
)


class TestBitField:
    def test_mask(self):
        f = BitField(name="EN", bit_offset=0, bit_width=1)
        assert f.mask == 0x1

        f = BitField(name="DIR", bit_offset=6, bit_width=2)
        assert f.mask == 0xC0

    def test_extract(self):
        f = BitField(name="PL", bit_offset=16, bit_width=2)
        assert f.extract(0x00030000) == 3
        assert f.extract(0x00010000) == 1
        assert f.extract(0x0000FFFF) == 0

    def test_insert(self):
        f = BitField(name="PL", bit_offset=16, bit_width=2)
        assert f.insert(0, 3) == 0x00030000
        assert f.insert(0xFFFFFFFF, 0) == 0xFFFCFFFF


class TestRegister:
    def test_apply_write_rw(self):
        reg = Register(name="CR1", offset=0x00)
        assert reg.apply_write(0, 0x1234) == 0x1234

    def test_apply_write_ro(self):
        reg = Register(name="SR", offset=0x00, access=AccessType.RO)
        assert reg.apply_write(0x1234, 0) == 0x1234

    def test_apply_write_w1c(self):
        reg = Register(
            name="SR", offset=0x00,
            fields=[
                BitField(name="FLAG", bit_offset=0, bit_width=1, access=AccessType.W1C),
                BitField(name="DATA", bit_offset=1, bit_width=7, access=AccessType.RW),
            ],
        )
        # FLAG is set, writing 1 to it should clear it
        result = reg.apply_write(0x01, 0x01)
        assert result & 0x01 == 0

        # Writing 0 to W1C should not clear
        result = reg.apply_write(0x01, 0x00)
        assert result & 0x01 == 1

    def test_apply_read_normal(self):
        reg = Register(name="DR", offset=0x00)
        read_val, new_val = reg.apply_read(0x1234)
        assert read_val == 0x1234
        assert new_val == 0x1234

    def test_get_field(self):
        reg = Register(
            name="CR1", offset=0x00,
            fields=[
                BitField(name="EN", bit_offset=0, bit_width=1),
                BitField(name="DIR", bit_offset=6, bit_width=2),
            ],
        )
        assert reg.get_field("EN") is not None
        assert reg.get_field("EN").bit_offset == 0
        assert reg.get_field("NONEXIST") is None


class TestRegisterBlock:
    def test_get_register(self):
        block = RegisterBlock(
            name="USART",
            registers=[
                Register(name="CR1", offset=0x00),
                Register(name="SR", offset=0x04),
            ],
        )
        assert block.get_register("CR1") is not None
        assert block.get_register_at(0x04).name == "SR"
        assert block.get_register("NONE") is None


class TestStateMachine:
    def test_basic_transitions(self):
        sm = StateMachine(
            name="TX",
            states=[
                State(name="idle", is_initial=True),
                State(name="busy"),
                State(name="done", is_final=True),
            ],
            transitions=[
                Transition(source="idle", target="busy", trigger="start"),
                Transition(source="busy", target="done", trigger="complete"),
            ],
        )
        assert sm.current_state == "idle"
        sm.evaluate_transitions("start")
        assert sm.current_state == "busy"
        sm.evaluate_transitions("complete")
        assert sm.current_state == "done"

    def test_reachable_states(self):
        sm = StateMachine(
            name="test",
            states=[
                State(name="a", is_initial=True),
                State(name="b"),
                State(name="c"),
                State(name="unreachable"),
            ],
            transitions=[
                Transition(source="a", target="b", trigger="go"),
                Transition(source="b", target="c", trigger="go"),
            ],
        )
        reachable = sm.get_reachable_states()
        assert "a" in reachable
        assert "b" in reachable
        assert "c" in reachable
        assert "unreachable" not in reachable

    def test_reset(self):
        sm = StateMachine(
            name="test",
            states=[
                State(name="idle", is_initial=True),
                State(name="busy"),
            ],
            transitions=[
                Transition(source="idle", target="busy", trigger="go"),
            ],
        )
        sm.evaluate_transitions("go")
        assert sm.current_state == "busy"
        sm.reset()
        assert sm.current_state == "idle"

    def test_dot_export(self):
        sm = StateMachine(
            name="test",
            states=[State(name="a", is_initial=True), State(name="b")],
            transitions=[Transition(source="a", target="b", trigger="go")],
        )
        dot = sm.to_dot()
        assert "digraph" in dot
        assert '"a"' in dot
        assert '"b"' in dot


class TestInterruptModel:
    def test_trigger_event(self):
        model = InterruptModel(
            peripheral_name="USART",
            lines=[],
            flag_to_event_map={"rx_done": ["RXNE"], "tx_done": ["TXE", "TC"]},
        )
        assert model.trigger_event("rx_done") == ["RXNE"]
        assert model.trigger_event("tx_done") == ["TXE", "TC"]
        assert model.trigger_event("unknown") == []


class TestDependencyGraph:
    def test_topological_order(self):
        graph = DependencyGraph(
            mcu_name="STM32F4",
            edges=[
                DependencyEdge(source="RCC", target="DMA1", dep_type=DependencyType.CLOCK_GATE),
                DependencyEdge(source="DMA1", target="USART1", dep_type=DependencyType.DMA_CHANNEL),
            ],
        )
        order = graph.topological_order()
        assert order.index("RCC") < order.index("DMA1")
        assert order.index("DMA1") < order.index("USART1")

    def test_get_all_peripherals(self):
        graph = DependencyGraph(edges=[
            DependencyEdge(source="A", target="B", dep_type=DependencyType.TRIGGER),
        ])
        assert graph.get_all_peripherals() == {"A", "B"}


class TestPeripheral:
    def test_read_write_register(self):
        periph = Peripheral(
            name="TEST",
            register_block=RegisterBlock(
                name="TEST",
                registers=[
                    Register(name="CR", offset=0x00, reset_value=0),
                    Register(name="SR", offset=0x04, reset_value=0xFF),
                ],
            ),
        )
        assert periph.read_register(0x00) == 0
        assert periph.read_register(0x04) == 0xFF

        periph.write_register(0x00, 0x1234)
        assert periph.read_register(0x00) == 0x1234

    def test_reset(self):
        periph = Peripheral(
            name="TEST",
            register_block=RegisterBlock(
                name="TEST",
                registers=[Register(name="CR", offset=0x00, reset_value=0x42)],
            ),
        )
        periph.write_register(0x00, 0xFF)
        periph.reset()
        assert periph.read_register(0x00) == 0x42


class TestQEMUHardwareModel:
    def test_builder_derives_generic_identity_paths_and_mmio_region(self):
        peripheral = Peripheral(
            name="ACCEL",
            base_address=0x40010000,
            address_size=0,
            mcu_family="DemoSoC",
            register_block=RegisterBlock(
                name="ACCEL",
                base_address=0x40010000,
                registers=[
                    Register(name="CTRL", offset=0x00),
                    Register(name="STATUS", offset=0x0C),
                ],
            ),
        )

        model = build_qemu_hardware_model(peripheral)

        assert model.identity.peripheral_name == "ACCEL"
        assert model.identity.qom_type == "demosoc-accel"
        assert model.identity.c_identifier_prefix == "demosoc_accel"
        assert model.identity.type_macro == "TYPE_DEMOSOC_ACCEL"
        assert model.identity.state_struct_name == "DEMOSOCACCELState"
        assert model.identity.kconfig_symbol == "DEMOSOC_ACCEL"
        assert model.file_layout.source_path == "hw/misc/demosoc_accel.c"
        assert model.file_layout.header_path == "include/hw/misc/demosoc_accel.h"
        assert model.file_layout.meson_path == "hw/misc/meson.build"
        assert model.file_layout.qtest_path == "tests/qtest/demosoc_accel-test.c"
        assert model.mmio_regions[0].name == "mmio"
        assert model.mmio_regions[0].base_address == 0x40010000
        assert model.mmio_regions[0].size == 0x10
        assert model.mmio_regions[0].register_count == 2
        assert model.device_tree.node_name == "accel"
        assert model.device_tree.unit_address == "40010000"
        assert model.device_tree.address_cells == 1
        assert model.device_tree.size_cells == 1
        assert model.device_tree.reg[0].base_address == 0x40010000
        assert model.device_tree.reg[0].size == 0x10
        assert model.device_tree.compatible == ["demosoc,accel"]

    def test_builder_preserves_zero_peripheral_base_address(self):
        peripheral = Peripheral(
            name="ZEROBASE",
            base_address=0,
            address_size=0x20,
            mcu_family="DemoSoC",
            register_block=RegisterBlock(
                name="ZEROBASE",
                base_address=0x40010000,
                registers=[Register(name="CTRL", offset=0x00)],
            ),
        )

        model = build_qemu_hardware_model(peripheral)

        assert model.mmio_regions[0].base_address == 0
        assert model.device_tree.unit_address == "0"
        assert model.device_tree.reg[0].base_address == 0

    def test_builder_uses_64_bit_address_cells_for_high_mmio_range(self):
        peripheral = Peripheral(
            name="HIGHADDR",
            base_address=0x1_0000_0000,
            address_size=0x100,
            mcu_family="DemoSoC",
            register_block=RegisterBlock(
                name="HIGHADDR",
                base_address=0x1_0000_0000,
                registers=[Register(name="CTRL", offset=0x00)],
            ),
        )

        model = build_qemu_hardware_model(peripheral)

        assert model.device_tree.address_cells == 2
        assert model.device_tree.size_cells == 1
        assert model.device_tree.reg[0].base_address == 0x1_0000_0000
        assert model.device_tree.reg[0].size == 0x100

    def test_builder_uses_64_bit_size_cells_for_large_mmio_size(self):
        peripheral = Peripheral(
            name="LARGESIZE",
            base_address=0x40000000,
            address_size=0x1_0000_0000,
            mcu_family="DemoSoC",
            register_block=RegisterBlock(
                name="LARGESIZE",
                base_address=0x40000000,
                registers=[Register(name="CTRL", offset=0x00)],
            ),
        )

        model = build_qemu_hardware_model(peripheral)

        assert model.device_tree.address_cells == 2
        assert model.device_tree.size_cells == 2
        assert model.device_tree.reg[0].size == 0x1_0000_0000

    def test_builder_uses_deduped_driver_compatible_and_irq_hints(self):
        peripheral = Peripheral(
            name="SENSOR",
            base_address=0x50000000,
            address_size=0x100,
            mcu_family="DemoSoC",
            register_block=RegisterBlock(name="SENSOR", base_address=0x50000000),
        )

        model = build_qemu_hardware_model(
            peripheral,
            {
                "state_hints": [
                    {"kind": "compatible", "value": "vendor,demo-sensor"},
                    {"kind": "compatible", "value": "vendor,demo-sensor"},
                    {"kind": "compatible", "value": "vendor,demo-sensor-v2"},
                    {"kind": "irq_resource", "name": "done", "function": "probe"},
                    {"kind": "irq_resource", "name": "done"},
                    {"kind": "irq_resource", "name": "error", "source": "platform"},
                ],
            },
        )

        assert model.device_tree.compatible == [
            "vendor,demo-sensor",
            "vendor,demo-sensor-v2",
            "demosoc,sensor",
        ]
        assert [irq.name for irq in model.irq_resources] == ["done", "error"]
        assert [irq.index for irq in model.irq_resources] == [0, 1]
        assert model.irq_resources[0].source == "probe"
        assert model.irq_resources[1].source == "platform"
        assert model.device_tree.interrupt_names == ["done", "error"]

    def test_builder_ignores_empty_state_hints(self):
        peripheral = Peripheral(
            name="SENSOR",
            base_address=0x50000000,
            address_size=0x100,
            mcu_family="DemoSoC",
            register_block=RegisterBlock(name="SENSOR", base_address=0x50000000),
            interrupt_model=InterruptModel(
                peripheral_name="SENSOR",
                lines=[InterruptLine(irq_number=9, name="fallback")],
            ),
        )

        model = build_qemu_hardware_model(
            peripheral,
            {
                "state_hints": [
                    {"kind": "compatible", "value": None},
                    {"kind": "compatible", "value": "  "},
                    {"kind": "irq_resource", "name": None},
                    {"kind": "irq_resource", "name": "  "},
                ],
            },
        )

        assert model.device_tree.compatible == ["demosoc,sensor"]
        assert [irq.name for irq in model.irq_resources] == ["fallback"]
        assert model.device_tree.interrupt_names == ["fallback"]

    def test_builder_falls_back_to_interrupt_model_lines_for_irq_resources(self):
        peripheral = Peripheral(
            name="TIMER",
            base_address=0x40000000,
            address_size=0x40,
            mcu_family="DemoSoC",
            register_block=RegisterBlock(name="TIMER", base_address=0x40000000),
            interrupt_model=InterruptModel(
                peripheral_name="TIMER",
                lines=[
                    InterruptLine(irq_number=7, name="timer_update"),
                    InterruptLine(irq_number=8, name="timer_capture"),
                ],
            ),
        )

        model = build_qemu_hardware_model(peripheral)

        assert [irq.name for irq in model.irq_resources] == ["timer_update", "timer_capture"]
        assert [irq.index for irq in model.irq_resources] == [0, 1]
        assert [irq.irq_number for irq in model.irq_resources] == [7, 8]
        assert model.device_tree.interrupt_names == ["timer_update", "timer_capture"]

    def test_builder_converts_unknown_interrupt_model_irq_numbers_to_none(self):
        peripheral = Peripheral(
            name="UNKNOWNIRQ",
            base_address=0x40000000,
            address_size=0x40,
            mcu_family="DemoSoC",
            register_block=RegisterBlock(name="UNKNOWNIRQ", base_address=0x40000000),
            interrupt_model=InterruptModel(
                peripheral_name="UNKNOWNIRQ",
                lines=[InterruptLine(irq_number=-1, name="unknown_irq")],
            ),
        )

        model = build_qemu_hardware_model(peripheral)

        assert [irq.name for irq in model.irq_resources] == ["unknown_irq"]
        assert model.irq_resources[0].irq_number is None
        assert model.device_tree.interrupt_names == ["unknown_irq"]

    @pytest.mark.parametrize(
        ("model_type", "kwargs"),
        [
            (QEMUMMIORegion, {"name": "mmio", "base_address": -1, "size": 1, "register_count": 1}),
            (QEMUMMIORegion, {"name": "mmio", "base_address": 0, "size": -1, "register_count": 1}),
            (QEMUMMIORegion, {"name": "mmio", "base_address": 0, "size": 1, "register_count": -1}),
            (QEMUDeviceTreeRegRegion, {"name": "mmio", "base_address": -1, "size": 1}),
            (QEMUDeviceTreeRegRegion, {"name": "mmio", "base_address": 0, "size": -1}),
            (QEMUIRQResource, {"name": "irq", "index": -1, "irq_number": 1}),
            (QEMUIRQResource, {"name": "irq", "index": 0, "irq_number": -1}),
        ],
    )
    def test_qemu_resources_reject_negative_numeric_fields(self, model_type, kwargs):
        with pytest.raises(ValidationError):
            model_type(**kwargs)
