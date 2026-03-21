"""Radio/Wireless communication module modeling.

Covers STM32WB/WL Sub-GHz radio and BLE radio peripherals.
Also provides a generic SPI-connected radio template (e.g., SX1276, nRF24L01).
"""

from __future__ import annotations

from autoemu.models.register import AccessType, BitField, Register, RegisterBlock
from autoemu.models.peripheral import Peripheral, PeripheralType, ClockConfig
from autoemu.models.state_machine import State, Transition, StateMachine
from autoemu.models.interrupt import (
    InterruptModel,
    InterruptLine,
    InterruptFlag,
    FlagBehavior,
)


def build_subghz_register_block(
    base_address: int = 0x5801_0000,
) -> RegisterBlock:
    """Build register block for STM32WL Sub-GHz radio (based on SX1262)."""
    registers: list[Register] = []

    # Sub-GHz Radio SPI Control Register
    registers.append(Register(
        name="SUBGHZSPICR",
        description="Sub-GHz radio SPI control register",
        offset=0x00,
        fields=[
            BitField(name="NSS", bit_offset=2, bit_width=1, access=AccessType.RW,
                     description="Sub-GHz radio SPI NSS"),
        ],
    ))

    # Sub-GHz Radio SPI Status Register
    registers.append(Register(
        name="SUBGHZSPISR",
        description="Sub-GHz radio SPI status register",
        offset=0x04,
        access=AccessType.RO,
        fields=[
            BitField(name="RFBUSYS", bit_offset=1, bit_width=1, access=AccessType.RO,
                     description="Radio busy after command"),
            BitField(name="RFBUSYMS", bit_offset=2, bit_width=1, access=AccessType.RO,
                     description="Radio busy masked signal status"),
        ],
    ))

    # Sub-GHz Radio SPI Data Register
    registers.append(Register(
        name="SUBGHZSPIDR",
        description="Sub-GHz radio SPI data register",
        offset=0x08,
        fields=[
            BitField(name="DATA", bit_offset=0, bit_width=8, access=AccessType.RW,
                     description="SPI data byte"),
        ],
    ))

    # -- Internal radio registers (accessed via SPI commands) --
    # These model the radio's internal register space

    # Status register (radio internal)
    registers.append(Register(
        name="RADIO_STATUS",
        description="Radio status (internal, read via GetStatus command)",
        offset=0x100,
        access=AccessType.RO,
        fields=[
            BitField(name="CMD_STATUS", bit_offset=1, bit_width=3, access=AccessType.RO,
                     description="Command status",
                     enum_values={
                         "DATA_AVAILABLE": 2, "CMD_TIMEOUT": 3,
                         "CMD_PROCESSING_ERROR": 4, "CMD_EXEC_FAILURE": 5,
                         "CMD_TX_DONE": 6,
                     }),
            BitField(name="CHIP_MODE", bit_offset=4, bit_width=3, access=AccessType.RO,
                     description="Current chip mode",
                     enum_values={
                         "STDBY_RC": 2, "STDBY_XOSC": 3,
                         "FS": 4, "RX": 5, "TX": 6,
                     }),
        ],
    ))

    # IRQ Status register (radio internal)
    registers.append(Register(
        name="RADIO_IRQSTATUS",
        description="Radio IRQ status register",
        offset=0x104,
        fields=[
            BitField(name="TX_DONE", bit_offset=0, bit_width=1, access=AccessType.W1C),
            BitField(name="RX_DONE", bit_offset=1, bit_width=1, access=AccessType.W1C),
            BitField(name="PREAMBLE_DETECTED", bit_offset=2, bit_width=1, access=AccessType.W1C),
            BitField(name="SYNC_WORD_VALID", bit_offset=3, bit_width=1, access=AccessType.W1C),
            BitField(name="HEADER_VALID", bit_offset=4, bit_width=1, access=AccessType.W1C),
            BitField(name="HEADER_ERR", bit_offset=5, bit_width=1, access=AccessType.W1C),
            BitField(name="CRC_ERR", bit_offset=6, bit_width=1, access=AccessType.W1C),
            BitField(name="CAD_DONE", bit_offset=7, bit_width=1, access=AccessType.W1C),
            BitField(name="CAD_DETECTED", bit_offset=8, bit_width=1, access=AccessType.W1C),
            BitField(name="TIMEOUT", bit_offset=9, bit_width=1, access=AccessType.W1C),
        ],
    ))

    # DIO IRQ Mask
    registers.append(Register(
        name="RADIO_IRQMASK",
        description="Radio DIO/IRQ mask register",
        offset=0x108,
        fields=[
            BitField(name="TX_DONE_MASK", bit_offset=0, bit_width=1, access=AccessType.RW),
            BitField(name="RX_DONE_MASK", bit_offset=1, bit_width=1, access=AccessType.RW),
            BitField(name="PREAMBLE_MASK", bit_offset=2, bit_width=1, access=AccessType.RW),
            BitField(name="SYNC_MASK", bit_offset=3, bit_width=1, access=AccessType.RW),
            BitField(name="HEADER_VALID_MASK", bit_offset=4, bit_width=1, access=AccessType.RW),
            BitField(name="HEADER_ERR_MASK", bit_offset=5, bit_width=1, access=AccessType.RW),
            BitField(name="CRC_ERR_MASK", bit_offset=6, bit_width=1, access=AccessType.RW),
            BitField(name="CAD_DONE_MASK", bit_offset=7, bit_width=1, access=AccessType.RW),
            BitField(name="CAD_DET_MASK", bit_offset=8, bit_width=1, access=AccessType.RW),
            BitField(name="TIMEOUT_MASK", bit_offset=9, bit_width=1, access=AccessType.RW),
        ],
    ))

    # Packet parameters
    registers.append(Register(
        name="RADIO_PKTPARAMS",
        description="Packet parameters register",
        offset=0x110,
        fields=[
            BitField(name="PREAMBLE_LEN", bit_offset=0, bit_width=16, access=AccessType.RW),
            BitField(name="HEADER_TYPE", bit_offset=16, bit_width=1, access=AccessType.RW,
                     enum_values={"VARIABLE": 0, "FIXED": 1}),
            BitField(name="PAYLOAD_LEN", bit_offset=17, bit_width=8, access=AccessType.RW),
            BitField(name="CRC_TYPE", bit_offset=25, bit_width=3, access=AccessType.RW),
        ],
    ))

    # Modulation parameters
    registers.append(Register(
        name="RADIO_MODPARAMS",
        description="Modulation parameters register",
        offset=0x114,
        fields=[
            BitField(name="SF", bit_offset=0, bit_width=4, access=AccessType.RW,
                     description="Spreading factor (LoRa)"),
            BitField(name="BW", bit_offset=4, bit_width=4, access=AccessType.RW,
                     description="Bandwidth"),
            BitField(name="CR", bit_offset=8, bit_width=3, access=AccessType.RW,
                     description="Coding rate"),
            BitField(name="LDRO", bit_offset=11, bit_width=1, access=AccessType.RW,
                     description="Low data rate optimize"),
        ],
    ))

    # Frequency register
    registers.append(Register(
        name="RADIO_RFFREQ",
        description="RF frequency register",
        offset=0x118,
        fields=[
            BitField(name="RFFREQ", bit_offset=0, bit_width=32, access=AccessType.RW,
                     description="RF frequency = RFFREQ * F_XTAL / 2^25"),
        ],
    ))

    # TX power
    registers.append(Register(
        name="RADIO_TXPARAMS",
        description="TX power and ramp time",
        offset=0x11C,
        fields=[
            BitField(name="POWER", bit_offset=0, bit_width=8, access=AccessType.RW,
                     description="TX output power"),
            BitField(name="RAMP_TIME", bit_offset=8, bit_width=8, access=AccessType.RW),
        ],
    ))

    return RegisterBlock(
        name="SUBGHZ",
        base_address=base_address,
        registers=registers,
    )


def build_radio_state_machine() -> StateMachine:
    """Build state machine for Sub-GHz radio (SX1262-style)."""
    return StateMachine(
        name="Radio",
        description="Sub-GHz radio operating mode state machine",
        states=[
            State(name="sleep", is_initial=True,
                  description="Ultra-low power sleep"),
            State(name="stdby_rc",
                  description="Standby with RC oscillator"),
            State(name="stdby_xosc",
                  description="Standby with XOSC"),
            State(name="fs",
                  description="Frequency synthesis mode"),
            State(name="tx",
                  description="Transmitting packet"),
            State(name="rx",
                  description="Receiving / listening"),
            State(name="rx_dc",
                  description="Duty-cycled receive"),
            State(name="cad",
                  description="Channel activity detection"),
        ],
        transitions=[
            Transition(source="sleep", target="stdby_rc",
                       trigger="cmd:SetStandby(RC)"),
            Transition(source="sleep", target="stdby_xosc",
                       trigger="cmd:SetStandby(XOSC)"),
            Transition(source="stdby_rc", target="stdby_xosc",
                       trigger="cmd:SetStandby(XOSC)"),
            Transition(source="stdby_xosc", target="stdby_rc",
                       trigger="cmd:SetStandby(RC)"),
            Transition(source="stdby_rc", target="fs",
                       trigger="cmd:SetFs"),
            Transition(source="stdby_xosc", target="fs",
                       trigger="cmd:SetFs"),
            Transition(source="fs", target="tx",
                       trigger="cmd:SetTx"),
            Transition(source="stdby_rc", target="tx",
                       trigger="cmd:SetTx"),
            Transition(source="stdby_xosc", target="tx",
                       trigger="cmd:SetTx"),
            Transition(source="tx", target="stdby_rc",
                       trigger="internal:tx_done",
                       condition="TX complete, TxDone IRQ"),
            Transition(source="tx", target="stdby_rc",
                       trigger="internal:tx_timeout"),
            Transition(source="fs", target="rx",
                       trigger="cmd:SetRx"),
            Transition(source="stdby_rc", target="rx",
                       trigger="cmd:SetRx"),
            Transition(source="stdby_xosc", target="rx",
                       trigger="cmd:SetRx"),
            Transition(source="rx", target="stdby_rc",
                       trigger="internal:rx_done",
                       condition="Packet received, RxDone IRQ"),
            Transition(source="rx", target="stdby_rc",
                       trigger="internal:rx_timeout"),
            Transition(source="stdby_rc", target="rx_dc",
                       trigger="cmd:SetRxDutyCycle"),
            Transition(source="rx_dc", target="stdby_rc",
                       trigger="internal:rx_done"),
            Transition(source="stdby_rc", target="cad",
                       trigger="cmd:SetCAD"),
            Transition(source="cad", target="stdby_rc",
                       trigger="internal:cad_done"),
            Transition(source="cad", target="rx",
                       trigger="internal:cad_detected",
                       condition="Activity detected and RxOnCAD enabled"),
            # Any state -> sleep via SetSleep command
            Transition(source="stdby_rc", target="sleep",
                       trigger="cmd:SetSleep"),
            Transition(source="stdby_xosc", target="sleep",
                       trigger="cmd:SetSleep"),
            Transition(source="fs", target="sleep",
                       trigger="cmd:SetSleep"),
        ],
    )


def build_radio_interrupt_model() -> InterruptModel:
    """Build interrupt model for Sub-GHz radio."""
    # STM32WL: SUBGHZ_Radio_IRQn = 42
    return InterruptModel(
        peripheral_name="SUBGHZ",
        lines=[
            InterruptLine(
                irq_number=42,
                name="SUBGHZ_Radio_IRQn",
                description="Sub-GHz radio interrupt",
                flags=[
                    InterruptFlag(
                        name="TX_DONE",
                        register_name="RADIO_IRQSTATUS",
                        bit_offset=0,
                        clear_behavior=FlagBehavior.W1C,
                        enable_register="RADIO_IRQMASK",
                        enable_bit_offset=0,
                    ),
                    InterruptFlag(
                        name="RX_DONE",
                        register_name="RADIO_IRQSTATUS",
                        bit_offset=1,
                        clear_behavior=FlagBehavior.W1C,
                        enable_register="RADIO_IRQMASK",
                        enable_bit_offset=1,
                    ),
                    InterruptFlag(
                        name="TIMEOUT",
                        register_name="RADIO_IRQSTATUS",
                        bit_offset=9,
                        clear_behavior=FlagBehavior.W1C,
                        enable_register="RADIO_IRQMASK",
                        enable_bit_offset=9,
                    ),
                    InterruptFlag(
                        name="CRC_ERR",
                        register_name="RADIO_IRQSTATUS",
                        bit_offset=6,
                        clear_behavior=FlagBehavior.W1C,
                        enable_register="RADIO_IRQMASK",
                        enable_bit_offset=6,
                    ),
                ],
            ),
        ],
        event_sources=[
            "tx_done", "rx_done", "preamble_detected",
            "sync_word_valid", "header_valid", "header_error",
            "crc_error", "cad_done", "cad_detected", "timeout",
        ],
        flag_to_event_map={
            "tx_done": ["TX_DONE"],
            "rx_done": ["RX_DONE"],
            "preamble_detected": ["PREAMBLE_DETECTED"],
            "sync_word_valid": ["SYNC_WORD_VALID"],
            "crc_error": ["CRC_ERR"],
            "cad_done": ["CAD_DONE"],
            "timeout": ["TIMEOUT"],
        },
    )


def build_radio_peripheral(
    base_address: int = 0x5801_0000,
    mcu_family: str = "STM32WL",
) -> Peripheral:
    """Build a complete Sub-GHz radio peripheral model."""
    return Peripheral(
        name="SUBGHZ",
        description=f"{mcu_family} Sub-GHz Radio (SX1262-compatible)",
        peripheral_type=PeripheralType.RADIO,
        base_address=base_address,
        register_block=build_subghz_register_block(base_address),
        state_machines=[build_radio_state_machine()],
        interrupt_model=build_radio_interrupt_model(),
        mcu_family=mcu_family,
        clock=ClockConfig(
            bus="APB3",
            enable_register="RCC_APB3ENR",
            enable_bit="SUBGHZSPIEN",
        ),
    )
