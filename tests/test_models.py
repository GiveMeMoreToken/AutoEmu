"""Tests for data models."""

import pytest
from autoemu.models.register import AccessType, BitField, Register, RegisterBlock
from autoemu.models.state_machine import State, Transition, StateMachine
from autoemu.models.interrupt import InterruptModel, InterruptLine, InterruptFlag, FlagBehavior
from autoemu.models.dependency import DependencyEdge, DependencyGraph, DependencyType
from autoemu.models.peripheral import Peripheral, PeripheralType


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
