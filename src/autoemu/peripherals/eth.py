"""Ethernet (ETH) peripheral modeling.

STM32 Ethernet MAC with DMA support. Covers the MAC, MMC, PTP,
and DMA sub-blocks of the ETH peripheral.
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


def build_eth_register_block(
    base_address: int = 0x4002_8000,
) -> RegisterBlock:
    """Build register block for STM32 Ethernet MAC."""
    registers: list[Register] = []

    # MAC Configuration Register (MACCR)
    registers.append(Register(
        name="MACCR",
        description="Ethernet MAC configuration register",
        offset=0x0000,
        fields=[
            BitField(name="RE", bit_offset=2, bit_width=1, access=AccessType.RW,
                     description="Receiver enable"),
            BitField(name="TE", bit_offset=3, bit_width=1, access=AccessType.RW,
                     description="Transmitter enable"),
            BitField(name="DC", bit_offset=4, bit_width=1, access=AccessType.RW,
                     description="Deferral check"),
            BitField(name="BL", bit_offset=5, bit_width=2, access=AccessType.RW,
                     description="Back-off limit"),
            BitField(name="APCS", bit_offset=7, bit_width=1, access=AccessType.RW,
                     description="Automatic pad/CRC stripping"),
            BitField(name="RD", bit_offset=9, bit_width=1, access=AccessType.RW,
                     description="Retry disable"),
            BitField(name="IPCO", bit_offset=10, bit_width=1, access=AccessType.RW,
                     description="IPv4 checksum offload"),
            BitField(name="DM", bit_offset=11, bit_width=1, access=AccessType.RW,
                     description="Duplex mode"),
            BitField(name="LM", bit_offset=12, bit_width=1, access=AccessType.RW,
                     description="Loopback mode"),
            BitField(name="ROD", bit_offset=13, bit_width=1, access=AccessType.RW,
                     description="Receive own disable"),
            BitField(name="FES", bit_offset=14, bit_width=1, access=AccessType.RW,
                     description="Fast Ethernet speed"),
            BitField(name="CSD", bit_offset=16, bit_width=1, access=AccessType.RW,
                     description="Carrier sense disable"),
            BitField(name="IFG", bit_offset=17, bit_width=3, access=AccessType.RW,
                     description="Interframe gap"),
            BitField(name="JD", bit_offset=22, bit_width=1, access=AccessType.RW,
                     description="Jabber disable"),
            BitField(name="WD", bit_offset=23, bit_width=1, access=AccessType.RW,
                     description="Watchdog disable"),
            BitField(name="CSTF", bit_offset=25, bit_width=1, access=AccessType.RW,
                     description="CRC stripping for type frames"),
        ],
    ))

    # MAC Frame Filter Register (MACFFR)
    registers.append(Register(
        name="MACFFR",
        description="Ethernet MAC frame filter register",
        offset=0x0004,
        fields=[
            BitField(name="PM", bit_offset=0, bit_width=1, access=AccessType.RW,
                     description="Promiscuous mode"),
            BitField(name="HU", bit_offset=1, bit_width=1, access=AccessType.RW),
            BitField(name="HM", bit_offset=2, bit_width=1, access=AccessType.RW),
            BitField(name="DAIF", bit_offset=3, bit_width=1, access=AccessType.RW),
            BitField(name="PAM", bit_offset=4, bit_width=1, access=AccessType.RW,
                     description="Pass all multicast"),
            BitField(name="BFD", bit_offset=5, bit_width=1, access=AccessType.RW,
                     description="Broadcast frames disable"),
            BitField(name="PCF", bit_offset=6, bit_width=2, access=AccessType.RW),
            BitField(name="SAIF", bit_offset=8, bit_width=1, access=AccessType.RW),
            BitField(name="SAF", bit_offset=9, bit_width=1, access=AccessType.RW),
            BitField(name="HPF", bit_offset=10, bit_width=1, access=AccessType.RW),
            BitField(name="RA", bit_offset=31, bit_width=1, access=AccessType.RW,
                     description="Receive all"),
        ],
    ))

    # MAC MII Address Register (MACMIIAR)
    registers.append(Register(
        name="MACMIIAR",
        description="Ethernet MAC MII address register",
        offset=0x0010,
        fields=[
            BitField(name="MB", bit_offset=0, bit_width=1, access=AccessType.RW,
                     description="MII busy"),
            BitField(name="MW", bit_offset=1, bit_width=1, access=AccessType.RW,
                     description="MII write"),
            BitField(name="CR", bit_offset=2, bit_width=3, access=AccessType.RW,
                     description="Clock range"),
            BitField(name="MR", bit_offset=6, bit_width=5, access=AccessType.RW,
                     description="MII register"),
            BitField(name="PA", bit_offset=11, bit_width=5, access=AccessType.RW,
                     description="PHY address"),
        ],
    ))

    # MAC MII Data Register (MACMIIDR)
    registers.append(Register(
        name="MACMIIDR",
        description="Ethernet MAC MII data register",
        offset=0x0014,
        fields=[
            BitField(name="MD", bit_offset=0, bit_width=16, access=AccessType.RW,
                     description="MII data"),
        ],
    ))

    # MAC Flow Control Register (MACFCR)
    registers.append(Register(
        name="MACFCR",
        description="Ethernet MAC flow control register",
        offset=0x0018,
        fields=[
            BitField(name="FCB_BPA", bit_offset=0, bit_width=1, access=AccessType.RW),
            BitField(name="TFCE", bit_offset=1, bit_width=1, access=AccessType.RW),
            BitField(name="RFCE", bit_offset=2, bit_width=1, access=AccessType.RW),
            BitField(name="UPFD", bit_offset=3, bit_width=1, access=AccessType.RW),
            BitField(name="PLT", bit_offset=4, bit_width=2, access=AccessType.RW),
            BitField(name="ZQPD", bit_offset=7, bit_width=1, access=AccessType.RW),
            BitField(name="PT", bit_offset=16, bit_width=16, access=AccessType.RW),
        ],
    ))

    # MAC Interrupt Status Register (MACSR)
    registers.append(Register(
        name="MACSR",
        description="Ethernet MAC interrupt status register",
        offset=0x0038,
        access=AccessType.RO,
        fields=[
            BitField(name="PMTS", bit_offset=3, bit_width=1, access=AccessType.RO,
                     description="PMT status"),
            BitField(name="MMCS", bit_offset=4, bit_width=1, access=AccessType.RO,
                     description="MMC status"),
            BitField(name="MMCRS", bit_offset=5, bit_width=1, access=AccessType.RO),
            BitField(name="MMCTS", bit_offset=6, bit_width=1, access=AccessType.RO),
            BitField(name="TSTS", bit_offset=9, bit_width=1, access=AccessType.RO,
                     description="Time stamp trigger status"),
        ],
    ))

    # MAC Interrupt Mask Register (MACIMR)
    registers.append(Register(
        name="MACIMR",
        description="Ethernet MAC interrupt mask register",
        offset=0x003C,
        fields=[
            BitField(name="PMTIM", bit_offset=3, bit_width=1, access=AccessType.RW),
            BitField(name="TSTIM", bit_offset=9, bit_width=1, access=AccessType.RW),
        ],
    ))

    # --- DMA registers (offset 0x1000) ---

    # DMA Bus Mode Register (DMABMR)
    registers.append(Register(
        name="DMABMR",
        description="Ethernet DMA bus mode register",
        offset=0x1000,
        reset_value=0x00020101,
        fields=[
            BitField(name="SR", bit_offset=0, bit_width=1, access=AccessType.RW,
                     description="Software reset"),
            BitField(name="DA", bit_offset=1, bit_width=1, access=AccessType.RW),
            BitField(name="DSL", bit_offset=2, bit_width=5, access=AccessType.RW),
            BitField(name="EDFE", bit_offset=7, bit_width=1, access=AccessType.RW),
            BitField(name="PBL", bit_offset=8, bit_width=6, access=AccessType.RW,
                     description="Programmable burst length"),
            BitField(name="PM", bit_offset=14, bit_width=2, access=AccessType.RW),
            BitField(name="FB", bit_offset=16, bit_width=1, access=AccessType.RW,
                     description="Fixed burst"),
            BitField(name="RDP", bit_offset=17, bit_width=6, access=AccessType.RW),
            BitField(name="USP", bit_offset=23, bit_width=1, access=AccessType.RW),
            BitField(name="FPM", bit_offset=24, bit_width=1, access=AccessType.RW),
            BitField(name="AAB", bit_offset=25, bit_width=1, access=AccessType.RW),
            BitField(name="MB", bit_offset=26, bit_width=1, access=AccessType.RW),
        ],
    ))

    # DMA Transmit Poll Demand (DMATPDR)
    registers.append(Register(
        name="DMATPDR",
        description="Ethernet DMA transmit poll demand register",
        offset=0x1004,
    ))

    # DMA Receive Poll Demand (DMARPDR)
    registers.append(Register(
        name="DMARPDR",
        description="Ethernet DMA receive poll demand register",
        offset=0x1008,
    ))

    # DMA Receive Descriptor List Address (DMARDLAR)
    registers.append(Register(
        name="DMARDLAR",
        offset=0x100C,
        description="Ethernet DMA receive descriptor list address register",
    ))

    # DMA Transmit Descriptor List Address (DMATDLAR)
    registers.append(Register(
        name="DMATDLAR",
        offset=0x1010,
        description="Ethernet DMA transmit descriptor list address register",
    ))

    # DMA Status Register (DMASR)
    registers.append(Register(
        name="DMASR",
        description="Ethernet DMA status register",
        offset=0x1014,
        fields=[
            BitField(name="TS", bit_offset=0, bit_width=1, access=AccessType.W1C,
                     description="Transmit status"),
            BitField(name="TPSS", bit_offset=1, bit_width=1, access=AccessType.W1C),
            BitField(name="TBUS", bit_offset=2, bit_width=1, access=AccessType.W1C,
                     description="Transmit buffer unavailable"),
            BitField(name="TJTS", bit_offset=3, bit_width=1, access=AccessType.W1C),
            BitField(name="ROS", bit_offset=4, bit_width=1, access=AccessType.W1C),
            BitField(name="TUS", bit_offset=5, bit_width=1, access=AccessType.W1C),
            BitField(name="RS", bit_offset=6, bit_width=1, access=AccessType.W1C,
                     description="Receive status"),
            BitField(name="RBUS", bit_offset=7, bit_width=1, access=AccessType.W1C,
                     description="Receive buffer unavailable"),
            BitField(name="RPSS", bit_offset=8, bit_width=1, access=AccessType.W1C),
            BitField(name="RWTS", bit_offset=9, bit_width=1, access=AccessType.W1C),
            BitField(name="ETS", bit_offset=10, bit_width=1, access=AccessType.W1C),
            BitField(name="FBES", bit_offset=13, bit_width=1, access=AccessType.W1C,
                     description="Fatal bus error"),
            BitField(name="ERS", bit_offset=14, bit_width=1, access=AccessType.W1C),
            BitField(name="AIS", bit_offset=15, bit_width=1, access=AccessType.W1C,
                     description="Abnormal interrupt summary"),
            BitField(name="NIS", bit_offset=16, bit_width=1, access=AccessType.W1C,
                     description="Normal interrupt summary"),
            BitField(name="RPS", bit_offset=17, bit_width=3, access=AccessType.RO,
                     description="Receive process state"),
            BitField(name="TPS", bit_offset=20, bit_width=3, access=AccessType.RO,
                     description="Transmit process state"),
            BitField(name="EBS", bit_offset=23, bit_width=3, access=AccessType.RO),
            BitField(name="MMCS", bit_offset=27, bit_width=1, access=AccessType.RO),
            BitField(name="PMTS", bit_offset=28, bit_width=1, access=AccessType.RO),
            BitField(name="TSTS", bit_offset=29, bit_width=1, access=AccessType.RO),
        ],
    ))

    # DMA Interrupt Enable Register (DMAIER)
    registers.append(Register(
        name="DMAIER",
        description="Ethernet DMA interrupt enable register",
        offset=0x101C,
        fields=[
            BitField(name="TIE", bit_offset=0, bit_width=1, access=AccessType.RW,
                     description="Transmit interrupt enable"),
            BitField(name="TPSIE", bit_offset=1, bit_width=1, access=AccessType.RW),
            BitField(name="TBUIE", bit_offset=2, bit_width=1, access=AccessType.RW),
            BitField(name="TJTIE", bit_offset=3, bit_width=1, access=AccessType.RW),
            BitField(name="ROIE", bit_offset=4, bit_width=1, access=AccessType.RW),
            BitField(name="TUIE", bit_offset=5, bit_width=1, access=AccessType.RW),
            BitField(name="RIE", bit_offset=6, bit_width=1, access=AccessType.RW,
                     description="Receive interrupt enable"),
            BitField(name="RBUIE", bit_offset=7, bit_width=1, access=AccessType.RW),
            BitField(name="RPSIE", bit_offset=8, bit_width=1, access=AccessType.RW),
            BitField(name="RWTIE", bit_offset=9, bit_width=1, access=AccessType.RW),
            BitField(name="ETIE", bit_offset=10, bit_width=1, access=AccessType.RW),
            BitField(name="FBEIE", bit_offset=13, bit_width=1, access=AccessType.RW),
            BitField(name="ERIE", bit_offset=14, bit_width=1, access=AccessType.RW),
            BitField(name="AISE", bit_offset=15, bit_width=1, access=AccessType.RW),
            BitField(name="NISE", bit_offset=16, bit_width=1, access=AccessType.RW),
        ],
    ))

    # DMA Operation Mode Register (DMAOMR)
    registers.append(Register(
        name="DMAOMR",
        description="Ethernet DMA operation mode register",
        offset=0x1018,
        fields=[
            BitField(name="SR", bit_offset=1, bit_width=1, access=AccessType.RW,
                     description="Start/stop receive"),
            BitField(name="OSF", bit_offset=2, bit_width=1, access=AccessType.RW),
            BitField(name="RTC", bit_offset=3, bit_width=2, access=AccessType.RW),
            BitField(name="FUGF", bit_offset=6, bit_width=1, access=AccessType.RW),
            BitField(name="FEF", bit_offset=7, bit_width=1, access=AccessType.RW),
            BitField(name="ST", bit_offset=13, bit_width=1, access=AccessType.RW,
                     description="Start/stop transmission"),
            BitField(name="TTC", bit_offset=14, bit_width=3, access=AccessType.RW),
            BitField(name="FTF", bit_offset=20, bit_width=1, access=AccessType.RW,
                     description="Flush transmit FIFO"),
            BitField(name="TSF", bit_offset=21, bit_width=1, access=AccessType.RW),
            BitField(name="DFRF", bit_offset=24, bit_width=1, access=AccessType.RW),
            BitField(name="RSF", bit_offset=25, bit_width=1, access=AccessType.RW),
            BitField(name="DTCEFD", bit_offset=26, bit_width=1, access=AccessType.RW),
        ],
    ))

    return RegisterBlock(
        name="ETH",
        base_address=base_address,
        registers=registers,
    )


def build_eth_state_machine() -> list[StateMachine]:
    """Build state machines for Ethernet MAC + DMA."""
    tx_sm = StateMachine(
        name="ETH_TX",
        description="Ethernet transmit state machine",
        states=[
            State(name="stopped", is_initial=True),
            State(name="running_fetching", description="Fetching TX descriptor"),
            State(name="running_waiting", description="Waiting for status"),
            State(name="running_reading", description="Reading data from memory"),
            State(name="running_writing", description="Writing TX status"),
            State(name="suspended", description="TX suspended, buffer unavailable"),
        ],
        transitions=[
            Transition(source="stopped", target="running_fetching",
                       trigger="reg_write:DMAOMR", condition="ST bit set"),
            Transition(source="running_fetching", target="running_reading",
                       trigger="internal:desc_valid"),
            Transition(source="running_reading", target="running_writing",
                       trigger="internal:tx_done"),
            Transition(source="running_writing", target="running_fetching",
                       trigger="internal:next_desc"),
            Transition(source="running_fetching", target="suspended",
                       trigger="internal:desc_unavailable"),
            Transition(source="suspended", target="running_fetching",
                       trigger="reg_write:DMATPDR"),
            Transition(source="running_fetching", target="stopped",
                       trigger="reg_write:DMAOMR", condition="ST bit cleared"),
        ],
    )

    rx_sm = StateMachine(
        name="ETH_RX",
        description="Ethernet receive state machine",
        states=[
            State(name="stopped", is_initial=True),
            State(name="running_fetching", description="Fetching RX descriptor"),
            State(name="running_waiting", description="Waiting for packet"),
            State(name="running_transferring", description="Transferring to memory"),
            State(name="running_closing", description="Closing RX descriptor"),
            State(name="suspended", description="RX suspended, buffer unavailable"),
        ],
        transitions=[
            Transition(source="stopped", target="running_fetching",
                       trigger="reg_write:DMAOMR", condition="SR bit set"),
            Transition(source="running_fetching", target="running_waiting",
                       trigger="internal:desc_valid"),
            Transition(source="running_waiting", target="running_transferring",
                       trigger="internal:packet_arrived"),
            Transition(source="running_transferring", target="running_closing",
                       trigger="internal:transfer_done"),
            Transition(source="running_closing", target="running_fetching",
                       trigger="internal:next_desc"),
            Transition(source="running_fetching", target="suspended",
                       trigger="internal:desc_unavailable"),
            Transition(source="suspended", target="running_fetching",
                       trigger="reg_write:DMARPDR"),
            Transition(source="running_fetching", target="stopped",
                       trigger="reg_write:DMAOMR", condition="SR bit cleared"),
        ],
    )

    return [tx_sm, rx_sm]


def build_eth_interrupt_model() -> InterruptModel:
    """Build interrupt model for STM32 Ethernet."""
    # STM32F4: ETH_IRQn = 61, ETH_WKUP_IRQn = 62
    return InterruptModel(
        peripheral_name="ETH",
        lines=[
            InterruptLine(
                irq_number=61,
                name="ETH_IRQn",
                description="Ethernet global interrupt",
                flags=[
                    InterruptFlag(
                        name="NIS", register_name="DMASR", bit_offset=16,
                        clear_behavior=FlagBehavior.W1C,
                        enable_register="DMAIER", enable_bit_offset=16,
                    ),
                    InterruptFlag(
                        name="AIS", register_name="DMASR", bit_offset=15,
                        clear_behavior=FlagBehavior.W1C,
                        enable_register="DMAIER", enable_bit_offset=15,
                    ),
                ],
            ),
        ],
        event_sources=["tx_complete", "rx_complete", "tx_buf_unavail", "rx_buf_unavail", "fatal_bus_error"],
        flag_to_event_map={
            "tx_complete": ["TS"],
            "rx_complete": ["RS"],
            "tx_buf_unavail": ["TBUS"],
            "rx_buf_unavail": ["RBUS"],
            "fatal_bus_error": ["FBES"],
        },
    )


def build_eth_peripheral(
    base_address: int = 0x4002_8000,
    mcu_family: str = "STM32F4",
) -> Peripheral:
    """Build a complete Ethernet peripheral model."""
    return Peripheral(
        name="ETH",
        description=f"{mcu_family} Ethernet MAC with DMA",
        peripheral_type=PeripheralType.ETH,
        base_address=base_address,
        register_block=build_eth_register_block(base_address),
        state_machines=build_eth_state_machine(),
        interrupt_model=build_eth_interrupt_model(),
        mcu_family=mcu_family,
        clock=ClockConfig(
            bus="AHB1",
            enable_register="RCC_AHB1ENR",
            enable_bit="ETHMACEN",
        ),
    )
