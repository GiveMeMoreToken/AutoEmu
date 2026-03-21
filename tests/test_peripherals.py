"""Tests for peripheral-specific modules."""

import pytest
from autoemu.peripherals.dma import build_dma_peripheral, build_dma_state_machine
from autoemu.peripherals.eth import build_eth_peripheral
from autoemu.peripherals.usb import build_usb_peripheral
from autoemu.peripherals.radio import build_radio_peripheral


class TestDMAPeripheral:
    def test_build(self):
        dma = build_dma_peripheral()
        assert dma.name == "DMA1"
        assert dma.peripheral_type.value == "dma"
        assert len(dma.register_block.registers) > 0
        assert len(dma.state_machines) == 8
        assert dma.interrupt_model is not None

    def test_registers(self):
        dma = build_dma_peripheral()
        lisr = dma.register_block.get_register("LISR")
        assert lisr is not None
        assert lisr.access.value == "RO"

        s0cr = dma.register_block.get_register("S0CR")
        assert s0cr is not None
        en = s0cr.get_field("EN")
        assert en is not None
        assert en.bit_offset == 0

    def test_state_machine(self):
        sm = build_dma_state_machine(0)
        assert sm.current_state == "disabled"
        sm.evaluate_transitions("reg_write:S0CR")
        assert sm.current_state == "idle"

    def test_interrupt_model(self):
        dma = build_dma_peripheral()
        model = dma.interrupt_model
        assert len(model.lines) == 8
        assert all(len(line.flags) == 5 for line in model.lines)


class TestETHPeripheral:
    def test_build(self):
        eth = build_eth_peripheral()
        assert eth.name == "ETH"
        assert eth.peripheral_type.value == "eth"
        assert len(eth.state_machines) == 2  # TX + RX

    def test_dma_status_register(self):
        eth = build_eth_peripheral()
        dmasr = eth.register_block.get_register("DMASR")
        assert dmasr is not None
        # Check W1C flags
        ts = dmasr.get_field("TS")
        assert ts is not None
        assert ts.access.value == "W1C"

    def test_w1c_behavior(self):
        eth = build_eth_peripheral()
        # Set DMASR.TS flag (bit 0)
        dmasr_offset = eth.register_block.get_register("DMASR").offset
        eth._register_values[dmasr_offset] = 0x01  # TS flag set
        # Write 1 to clear
        eth.write_register(dmasr_offset, 0x01)
        assert eth.get_register_value(dmasr_offset) & 0x01 == 0


class TestUSBPeripheral:
    def test_build_fs(self):
        usb = build_usb_peripheral("FS")
        assert usb.name == "USB_OTG_FS"
        assert usb.peripheral_type.value == "usb"
        assert len(usb.state_machines) == 2  # Device + EP0

    def test_build_hs(self):
        usb = build_usb_peripheral("HS", base_address=0x40040000)
        assert usb.name == "USB_OTG_HS"
        assert usb.base_address == 0x40040000

    def test_gintsts_w1c(self):
        usb = build_usb_peripheral("FS")
        gintsts = usb.register_block.get_register("GINTSTS")
        assert gintsts is not None
        usbrst = gintsts.get_field("USBRST")
        assert usbrst is not None
        assert usbrst.access.value == "W1C"


class TestRadioPeripheral:
    def test_build(self):
        radio = build_radio_peripheral()
        assert radio.name == "SUBGHZ"
        assert radio.peripheral_type.value == "radio"
        assert len(radio.state_machines) == 1

    def test_state_machine(self):
        radio = build_radio_peripheral()
        sm = radio.state_machines[0]
        assert sm.current_state == "sleep"
        sm.evaluate_transitions("cmd:SetStandby(RC)")
        assert sm.current_state == "stdby_rc"
        sm.evaluate_transitions("cmd:SetTx")
        assert sm.current_state == "tx"
        sm.evaluate_transitions("internal:tx_done")
        assert sm.current_state == "stdby_rc"

    def test_interrupt_model(self):
        radio = build_radio_peripheral()
        model = radio.interrupt_model
        assert len(model.lines) == 1
        assert model.lines[0].irq_number == 42
        assert model.trigger_event("tx_done") == ["TX_DONE"]
