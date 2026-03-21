"""USB OTG peripheral modeling.

STM32 USB OTG FS/HS controller. Covers the core, device, host,
and FIFO register blocks.
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


def build_usb_otg_register_block(
    variant: str = "FS",
    base_address: int = 0x5000_0000,
) -> RegisterBlock:
    """Build register block for STM32 USB OTG FS/HS controller."""
    registers: list[Register] = []

    # ---- Core Global Registers (offset 0x000) ----

    # OTG Control and Status Register (GOTGCTL)
    registers.append(Register(
        name="GOTGCTL",
        description="OTG control and status register",
        offset=0x000,
        fields=[
            BitField(name="SRQSCS", bit_offset=0, bit_width=1, access=AccessType.RO),
            BitField(name="SRQ", bit_offset=1, bit_width=1, access=AccessType.RW,
                     description="Session request"),
            BitField(name="VBVALOEN", bit_offset=2, bit_width=1, access=AccessType.RW),
            BitField(name="VBVALOVAL", bit_offset=3, bit_width=1, access=AccessType.RW),
            BitField(name="AVALOEN", bit_offset=4, bit_width=1, access=AccessType.RW),
            BitField(name="AVALOVAL", bit_offset=5, bit_width=1, access=AccessType.RW),
            BitField(name="BVALOEN", bit_offset=6, bit_width=1, access=AccessType.RW),
            BitField(name="BVALOVAL", bit_offset=7, bit_width=1, access=AccessType.RW),
            BitField(name="HNGSCS", bit_offset=8, bit_width=1, access=AccessType.RO),
            BitField(name="HNPRQ", bit_offset=9, bit_width=1, access=AccessType.RW),
            BitField(name="HSHNPEN", bit_offset=10, bit_width=1, access=AccessType.RW),
            BitField(name="DHNPEN", bit_offset=11, bit_width=1, access=AccessType.RW),
            BitField(name="EHEN", bit_offset=12, bit_width=1, access=AccessType.RW),
            BitField(name="CIDSTS", bit_offset=16, bit_width=1, access=AccessType.RO,
                     description="Connector ID status"),
            BitField(name="DBCT", bit_offset=17, bit_width=1, access=AccessType.RO),
            BitField(name="ASVLD", bit_offset=18, bit_width=1, access=AccessType.RO),
            BitField(name="BSVLD", bit_offset=19, bit_width=1, access=AccessType.RO),
            BitField(name="OTGVER", bit_offset=20, bit_width=1, access=AccessType.RW),
        ],
    ))

    # OTG Interrupt Register (GOTGINT)
    registers.append(Register(
        name="GOTGINT",
        description="OTG interrupt register",
        offset=0x004,
        fields=[
            BitField(name="SEDET", bit_offset=2, bit_width=1, access=AccessType.W1C,
                     description="Session end detected"),
            BitField(name="SRSSCHG", bit_offset=8, bit_width=1, access=AccessType.W1C),
            BitField(name="HNSSCHG", bit_offset=9, bit_width=1, access=AccessType.W1C),
            BitField(name="HNGDET", bit_offset=17, bit_width=1, access=AccessType.W1C),
            BitField(name="ADTOCHG", bit_offset=18, bit_width=1, access=AccessType.W1C),
            BitField(name="DBCDNE", bit_offset=19, bit_width=1, access=AccessType.W1C),
            BitField(name="IDCHNG", bit_offset=20, bit_width=1, access=AccessType.W1C),
        ],
    ))

    # AHB Configuration Register (GAHBCFG)
    registers.append(Register(
        name="GAHBCFG",
        description="AHB configuration register",
        offset=0x008,
        fields=[
            BitField(name="GINTMSK", bit_offset=0, bit_width=1, access=AccessType.RW,
                     description="Global interrupt mask"),
            BitField(name="HBSTLEN", bit_offset=1, bit_width=4, access=AccessType.RW),
            BitField(name="DMAEN", bit_offset=5, bit_width=1, access=AccessType.RW),
            BitField(name="TXFELVL", bit_offset=7, bit_width=1, access=AccessType.RW),
            BitField(name="PTXFELVL", bit_offset=8, bit_width=1, access=AccessType.RW),
        ],
    ))

    # USB Configuration Register (GUSBCFG)
    registers.append(Register(
        name="GUSBCFG",
        description="USB configuration register",
        offset=0x00C,
        reset_value=0x00000A00,
        fields=[
            BitField(name="TOCAL", bit_offset=0, bit_width=3, access=AccessType.RW),
            BitField(name="PHYSEL", bit_offset=6, bit_width=1, access=AccessType.RW),
            BitField(name="SRPCAP", bit_offset=8, bit_width=1, access=AccessType.RW),
            BitField(name="HNPCAP", bit_offset=9, bit_width=1, access=AccessType.RW),
            BitField(name="TRDT", bit_offset=10, bit_width=4, access=AccessType.RW,
                     description="USB turnaround time"),
            BitField(name="FHMOD", bit_offset=29, bit_width=1, access=AccessType.RW,
                     description="Force host mode"),
            BitField(name="FDMOD", bit_offset=30, bit_width=1, access=AccessType.RW,
                     description="Force device mode"),
        ],
    ))

    # Reset Register (GRSTCTL)
    registers.append(Register(
        name="GRSTCTL",
        description="Reset register",
        offset=0x010,
        reset_value=0x80000000,
        fields=[
            BitField(name="CSRST", bit_offset=0, bit_width=1, access=AccessType.RW,
                     description="Core soft reset"),
            BitField(name="HSRST", bit_offset=1, bit_width=1, access=AccessType.RW),
            BitField(name="FCRST", bit_offset=2, bit_width=1, access=AccessType.RW),
            BitField(name="RXFFLSH", bit_offset=4, bit_width=1, access=AccessType.RW,
                     description="RxFIFO flush"),
            BitField(name="TXFFLSH", bit_offset=5, bit_width=1, access=AccessType.RW,
                     description="TxFIFO flush"),
            BitField(name="TXFNUM", bit_offset=6, bit_width=5, access=AccessType.RW),
            BitField(name="AHBIDL", bit_offset=31, bit_width=1, access=AccessType.RO,
                     description="AHB master idle"),
        ],
    ))

    # Core Interrupt Register (GINTSTS)
    registers.append(Register(
        name="GINTSTS",
        description="Core interrupt register",
        offset=0x014,
        reset_value=0x04000020,
        fields=[
            BitField(name="CMOD", bit_offset=0, bit_width=1, access=AccessType.RO,
                     description="Current mode of operation"),
            BitField(name="MMIS", bit_offset=1, bit_width=1, access=AccessType.W1C,
                     description="Mode mismatch interrupt"),
            BitField(name="OTGINT", bit_offset=2, bit_width=1, access=AccessType.RO),
            BitField(name="SOF", bit_offset=3, bit_width=1, access=AccessType.W1C,
                     description="Start of frame"),
            BitField(name="RXFLVL", bit_offset=4, bit_width=1, access=AccessType.RO,
                     description="RxFIFO non-empty"),
            BitField(name="NPTXFE", bit_offset=5, bit_width=1, access=AccessType.RO),
            BitField(name="GINAKEFF", bit_offset=6, bit_width=1, access=AccessType.RO),
            BitField(name="GONAKEFF", bit_offset=7, bit_width=1, access=AccessType.RO),
            BitField(name="ESUSP", bit_offset=10, bit_width=1, access=AccessType.W1C),
            BitField(name="USBSUSP", bit_offset=11, bit_width=1, access=AccessType.W1C,
                     description="USB suspend"),
            BitField(name="USBRST", bit_offset=12, bit_width=1, access=AccessType.W1C,
                     description="USB reset"),
            BitField(name="ENUMDNE", bit_offset=13, bit_width=1, access=AccessType.W1C,
                     description="Enumeration done"),
            BitField(name="ISOODRP", bit_offset=14, bit_width=1, access=AccessType.W1C),
            BitField(name="EOPF", bit_offset=15, bit_width=1, access=AccessType.W1C),
            BitField(name="IEPINT", bit_offset=18, bit_width=1, access=AccessType.RO,
                     description="IN endpoint interrupt"),
            BitField(name="OEPINT", bit_offset=19, bit_width=1, access=AccessType.RO,
                     description="OUT endpoint interrupt"),
            BitField(name="IISOIXFR", bit_offset=20, bit_width=1, access=AccessType.W1C),
            BitField(name="IPXFR_INCOMPISOOUT", bit_offset=21, bit_width=1, access=AccessType.W1C),
            BitField(name="DATAFSUSP", bit_offset=22, bit_width=1, access=AccessType.W1C),
            BitField(name="HPRTINT", bit_offset=24, bit_width=1, access=AccessType.RO),
            BitField(name="HCINT", bit_offset=25, bit_width=1, access=AccessType.RO),
            BitField(name="PTXFE", bit_offset=26, bit_width=1, access=AccessType.RO),
            BitField(name="CIDSCHG", bit_offset=28, bit_width=1, access=AccessType.W1C),
            BitField(name="DISCINT", bit_offset=29, bit_width=1, access=AccessType.W1C),
            BitField(name="SRQINT", bit_offset=30, bit_width=1, access=AccessType.W1C),
            BitField(name="WKUPINT", bit_offset=31, bit_width=1, access=AccessType.W1C),
        ],
    ))

    # Core Interrupt Mask Register (GINTMSK)
    registers.append(Register(
        name="GINTMSK",
        description="Interrupt mask register",
        offset=0x018,
        fields=[
            BitField(name="MMISM", bit_offset=1, bit_width=1, access=AccessType.RW),
            BitField(name="OTGINT", bit_offset=2, bit_width=1, access=AccessType.RW),
            BitField(name="SOFM", bit_offset=3, bit_width=1, access=AccessType.RW),
            BitField(name="RXFLVLM", bit_offset=4, bit_width=1, access=AccessType.RW),
            BitField(name="NPTXFEM", bit_offset=5, bit_width=1, access=AccessType.RW),
            BitField(name="GINAKEFFM", bit_offset=6, bit_width=1, access=AccessType.RW),
            BitField(name="GONAKEFFM", bit_offset=7, bit_width=1, access=AccessType.RW),
            BitField(name="ESUSPM", bit_offset=10, bit_width=1, access=AccessType.RW),
            BitField(name="USBSUSPM", bit_offset=11, bit_width=1, access=AccessType.RW),
            BitField(name="USBRST", bit_offset=12, bit_width=1, access=AccessType.RW),
            BitField(name="ENUMDNEM", bit_offset=13, bit_width=1, access=AccessType.RW),
            BitField(name="ISOODRPM", bit_offset=14, bit_width=1, access=AccessType.RW),
            BitField(name="EOPFM", bit_offset=15, bit_width=1, access=AccessType.RW),
            BitField(name="IEPINT", bit_offset=18, bit_width=1, access=AccessType.RW),
            BitField(name="OEPINT", bit_offset=19, bit_width=1, access=AccessType.RW),
            BitField(name="IISOIXFRM", bit_offset=20, bit_width=1, access=AccessType.RW),
            BitField(name="IPXFRM_IISOOXFRM", bit_offset=21, bit_width=1, access=AccessType.RW),
            BitField(name="HPRTINTM", bit_offset=24, bit_width=1, access=AccessType.RW),
            BitField(name="HCINTM", bit_offset=25, bit_width=1, access=AccessType.RW),
            BitField(name="PTXFEM", bit_offset=26, bit_width=1, access=AccessType.RW),
            BitField(name="CIDSCHGM", bit_offset=28, bit_width=1, access=AccessType.RW),
            BitField(name="DISCINTM", bit_offset=29, bit_width=1, access=AccessType.RW),
            BitField(name="SRQINTM", bit_offset=30, bit_width=1, access=AccessType.RW),
            BitField(name="WKUINTM", bit_offset=31, bit_width=1, access=AccessType.RW),
        ],
    ))

    # Device Configuration Register (DCFG)
    registers.append(Register(
        name="DCFG",
        description="Device configuration register",
        offset=0x800,
        fields=[
            BitField(name="DSPD", bit_offset=0, bit_width=2, access=AccessType.RW,
                     description="Device speed"),
            BitField(name="NZLSOHSK", bit_offset=2, bit_width=1, access=AccessType.RW),
            BitField(name="DAD", bit_offset=4, bit_width=7, access=AccessType.RW,
                     description="Device address"),
            BitField(name="PFIVL", bit_offset=11, bit_width=2, access=AccessType.RW),
            BitField(name="ERRATIM", bit_offset=15, bit_width=1, access=AccessType.RW),
        ],
    ))

    # Device Control Register (DCTL)
    registers.append(Register(
        name="DCTL",
        description="Device control register",
        offset=0x804,
        fields=[
            BitField(name="RWUSIG", bit_offset=0, bit_width=1, access=AccessType.RW,
                     description="Remote wakeup signaling"),
            BitField(name="SDIS", bit_offset=1, bit_width=1, access=AccessType.RW,
                     description="Soft disconnect"),
            BitField(name="GINSTS", bit_offset=2, bit_width=1, access=AccessType.RO),
            BitField(name="GONSTS", bit_offset=3, bit_width=1, access=AccessType.RO),
            BitField(name="SGINAK", bit_offset=7, bit_width=1, access=AccessType.WO),
            BitField(name="CGINAK", bit_offset=8, bit_width=1, access=AccessType.WO),
            BitField(name="SGONAK", bit_offset=9, bit_width=1, access=AccessType.WO),
            BitField(name="CGONAK", bit_offset=10, bit_width=1, access=AccessType.WO),
            BitField(name="POPRGDNE", bit_offset=11, bit_width=1, access=AccessType.RW),
        ],
    ))

    # Device Status Register (DSTS)
    registers.append(Register(
        name="DSTS",
        description="Device status register",
        offset=0x808,
        access=AccessType.RO,
        fields=[
            BitField(name="SUSPSTS", bit_offset=0, bit_width=1, access=AccessType.RO,
                     description="Suspend status"),
            BitField(name="ENUMSPD", bit_offset=1, bit_width=2, access=AccessType.RO,
                     description="Enumerated speed"),
            BitField(name="EERR", bit_offset=3, bit_width=1, access=AccessType.RO),
            BitField(name="FNSOF", bit_offset=8, bit_width=14, access=AccessType.RO,
                     description="Frame number of received SOF"),
        ],
    ))

    return RegisterBlock(
        name=f"USB_OTG_{variant}",
        base_address=base_address,
        registers=registers,
    )


def build_usb_state_machine() -> list[StateMachine]:
    """Build state machines for USB OTG controller."""
    device_sm = StateMachine(
        name="USB_Device",
        description="USB device mode state machine",
        states=[
            State(name="powered_off", is_initial=True),
            State(name="attached", description="VBUS detected, pull-up enabled"),
            State(name="powered", description="VBUS stable"),
            State(name="default", description="Bus reset received"),
            State(name="addressed", description="Device address assigned"),
            State(name="configured", description="Configuration set"),
            State(name="suspended", description="No SOF for 3ms"),
        ],
        transitions=[
            Transition(source="powered_off", target="attached",
                       trigger="internal:vbus_detect"),
            Transition(source="attached", target="powered",
                       trigger="internal:vbus_stable"),
            Transition(source="powered", target="default",
                       trigger="internal:bus_reset"),
            Transition(source="default", target="addressed",
                       trigger="reg_write:DCFG", condition="DAD field set"),
            Transition(source="addressed", target="configured",
                       trigger="internal:set_config"),
            Transition(source="configured", target="suspended",
                       trigger="internal:suspend_detect"),
            Transition(source="suspended", target="configured",
                       trigger="internal:resume"),
            Transition(source="configured", target="default",
                       trigger="internal:bus_reset"),
            Transition(source="addressed", target="default",
                       trigger="internal:bus_reset"),
            Transition(source="suspended", target="default",
                       trigger="internal:bus_reset"),
        ],
    )

    ep_sm = StateMachine(
        name="USB_EP0",
        description="USB endpoint 0 control transfer state machine",
        states=[
            State(name="idle", is_initial=True),
            State(name="setup", description="SETUP packet received"),
            State(name="data_in", description="Data IN stage"),
            State(name="data_out", description="Data OUT stage"),
            State(name="status_in", description="Status IN stage"),
            State(name="status_out", description="Status OUT stage"),
            State(name="stall", description="Endpoint stalled"),
        ],
        transitions=[
            Transition(source="idle", target="setup",
                       trigger="internal:setup_received"),
            Transition(source="setup", target="data_in",
                       trigger="internal:data_in_request"),
            Transition(source="setup", target="data_out",
                       trigger="internal:data_out_request"),
            Transition(source="setup", target="status_in",
                       trigger="internal:no_data_request"),
            Transition(source="data_in", target="status_out",
                       trigger="internal:data_in_complete"),
            Transition(source="data_out", target="status_in",
                       trigger="internal:data_out_complete"),
            Transition(source="status_in", target="idle",
                       trigger="internal:status_complete"),
            Transition(source="status_out", target="idle",
                       trigger="internal:status_complete"),
            Transition(source="setup", target="stall",
                       trigger="internal:request_error"),
            Transition(source="stall", target="idle",
                       trigger="internal:clear_stall"),
        ],
    )

    return [device_sm, ep_sm]


def build_usb_interrupt_model(variant: str = "FS") -> InterruptModel:
    """Build interrupt model for USB OTG controller."""
    # STM32F4: OTG_FS_IRQn=67, OTG_HS_IRQn=77
    irq_num = 67 if variant == "FS" else 77

    return InterruptModel(
        peripheral_name=f"USB_OTG_{variant}",
        lines=[
            InterruptLine(
                irq_number=irq_num,
                name=f"OTG_{variant}_IRQn",
                description=f"USB OTG {variant} global interrupt",
                flags=[
                    InterruptFlag(
                        name="USBRST", register_name="GINTSTS", bit_offset=12,
                        clear_behavior=FlagBehavior.W1C,
                        enable_register="GINTMSK", enable_bit_offset=12,
                    ),
                    InterruptFlag(
                        name="ENUMDNE", register_name="GINTSTS", bit_offset=13,
                        clear_behavior=FlagBehavior.W1C,
                        enable_register="GINTMSK", enable_bit_offset=13,
                    ),
                    InterruptFlag(
                        name="SOF", register_name="GINTSTS", bit_offset=3,
                        clear_behavior=FlagBehavior.W1C,
                        enable_register="GINTMSK", enable_bit_offset=3,
                    ),
                    InterruptFlag(
                        name="USBSUSP", register_name="GINTSTS", bit_offset=11,
                        clear_behavior=FlagBehavior.W1C,
                        enable_register="GINTMSK", enable_bit_offset=11,
                    ),
                    InterruptFlag(
                        name="WKUPINT", register_name="GINTSTS", bit_offset=31,
                        clear_behavior=FlagBehavior.W1C,
                        enable_register="GINTMSK", enable_bit_offset=31,
                    ),
                ],
            ),
        ],
        event_sources=[
            "bus_reset", "enumeration_done", "sof", "suspend",
            "wakeup", "rx_fifo_notempty", "tx_complete",
        ],
        flag_to_event_map={
            "bus_reset": ["USBRST"],
            "enumeration_done": ["ENUMDNE"],
            "sof": ["SOF"],
            "suspend": ["USBSUSP"],
            "wakeup": ["WKUPINT"],
        },
    )


def build_usb_peripheral(
    variant: str = "FS",
    base_address: int = 0x5000_0000,
    mcu_family: str = "STM32F4",
) -> Peripheral:
    """Build a complete USB OTG peripheral model."""
    return Peripheral(
        name=f"USB_OTG_{variant}",
        description=f"{mcu_family} USB OTG {variant} Controller",
        peripheral_type=PeripheralType.USB,
        base_address=base_address,
        register_block=build_usb_otg_register_block(variant, base_address),
        state_machines=build_usb_state_machine(),
        interrupt_model=build_usb_interrupt_model(variant),
        mcu_family=mcu_family,
        clock=ClockConfig(
            bus="AHB2" if variant == "FS" else "AHB1",
            enable_register=f"RCC_AHB{2 if variant == 'FS' else 1}ENR",
            enable_bit=f"OTGFS{'EN' if variant == 'FS' else 'HSEN'}",
        ),
    )
