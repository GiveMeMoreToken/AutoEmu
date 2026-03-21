"""DMA Controller peripheral modeling.

Provides domain-specific knowledge and templates for STM32 DMA controllers.
Supports both DMA1/DMA2 (basic) and DMA2D (graphic) controllers.
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
from autoemu.models.dependency import DependencyEdge, DependencyType


def build_dma_register_block(
    streams: int = 8,
    name: str = "DMA1",
    base_address: int = 0x4002_6000,
) -> RegisterBlock:
    """Build register block for STM32 DMA controller (F4/F7/H7 style).

    Args:
        streams: Number of DMA streams (typically 8).
        name: Peripheral name.
        base_address: Base address of the DMA controller.
    """
    registers: list[Register] = []

    # Low Interrupt Status Register (LISR) - streams 0-3
    lisr_fields = []
    for i in range(4):
        base_bit = i * 6 + (i // 2) * 4
        for flag_name, offset in [
            ("FEIF", 0), ("DMEIF", 2), ("TEIF", 3), ("HTIF", 4), ("TCIF", 5),
        ]:
            lisr_fields.append(BitField(
                name=f"{flag_name}{i}",
                description=f"Stream {i} {flag_name}",
                bit_offset=base_bit + offset,
                bit_width=1,
                access=AccessType.RO,
            ))

    registers.append(Register(
        name="LISR", description="Low interrupt status register",
        offset=0x00, access=AccessType.RO, fields=lisr_fields,
    ))

    # High Interrupt Status Register (HISR) - streams 4-7
    hisr_fields = []
    for i in range(4, min(streams, 8)):
        base_bit = (i - 4) * 6 + ((i - 4) // 2) * 4
        for flag_name, offset in [
            ("FEIF", 0), ("DMEIF", 2), ("TEIF", 3), ("HTIF", 4), ("TCIF", 5),
        ]:
            hisr_fields.append(BitField(
                name=f"{flag_name}{i}",
                description=f"Stream {i} {flag_name}",
                bit_offset=base_bit + offset,
                bit_width=1,
                access=AccessType.RO,
            ))

    registers.append(Register(
        name="HISR", description="High interrupt status register",
        offset=0x04, access=AccessType.RO, fields=hisr_fields,
    ))

    # Low Interrupt Flag Clear Register (LIFCR)
    lifcr_fields = []
    for i in range(4):
        base_bit = i * 6 + (i // 2) * 4
        for flag_name, offset in [
            ("CFEIF", 0), ("CDMEIF", 2), ("CTEIF", 3), ("CHTIF", 4), ("CTCIF", 5),
        ]:
            lifcr_fields.append(BitField(
                name=f"{flag_name}{i}",
                bit_offset=base_bit + offset,
                bit_width=1,
                access=AccessType.WO,
            ))

    registers.append(Register(
        name="LIFCR", description="Low interrupt flag clear register",
        offset=0x08, access=AccessType.WO, fields=lifcr_fields,
    ))

    # High Interrupt Flag Clear Register (HIFCR)
    hifcr_fields = []
    for i in range(4, min(streams, 8)):
        base_bit = (i - 4) * 6 + ((i - 4) // 2) * 4
        for flag_name, offset in [
            ("CFEIF", 0), ("CDMEIF", 2), ("CTEIF", 3), ("CHTIF", 4), ("CTCIF", 5),
        ]:
            hifcr_fields.append(BitField(
                name=f"{flag_name}{i}",
                bit_offset=base_bit + offset,
                bit_width=1,
                access=AccessType.WO,
            ))

    registers.append(Register(
        name="HIFCR", description="High interrupt flag clear register",
        offset=0x0C, access=AccessType.WO, fields=hifcr_fields,
    ))

    # Per-stream registers (SxCR, SxNDTR, SxPAR, SxM0AR, SxM1AR, SxFCR)
    for i in range(streams):
        stream_base = 0x10 + i * 0x18

        # Stream Configuration Register (SxCR)
        cr_fields = [
            BitField(name="EN", bit_offset=0, bit_width=1, access=AccessType.RW,
                     description="Stream enable"),
            BitField(name="DMEIE", bit_offset=1, bit_width=1, access=AccessType.RW,
                     description="Direct mode error interrupt enable"),
            BitField(name="TEIE", bit_offset=2, bit_width=1, access=AccessType.RW,
                     description="Transfer error interrupt enable"),
            BitField(name="HTIE", bit_offset=3, bit_width=1, access=AccessType.RW,
                     description="Half transfer interrupt enable"),
            BitField(name="TCIE", bit_offset=4, bit_width=1, access=AccessType.RW,
                     description="Transfer complete interrupt enable"),
            BitField(name="PFCTRL", bit_offset=5, bit_width=1, access=AccessType.RW,
                     description="Peripheral flow controller"),
            BitField(name="DIR", bit_offset=6, bit_width=2, access=AccessType.RW,
                     description="Data transfer direction",
                     enum_values={"P2M": 0, "M2P": 1, "M2M": 2}),
            BitField(name="CIRC", bit_offset=8, bit_width=1, access=AccessType.RW,
                     description="Circular mode"),
            BitField(name="PINC", bit_offset=9, bit_width=1, access=AccessType.RW,
                     description="Peripheral increment mode"),
            BitField(name="MINC", bit_offset=10, bit_width=1, access=AccessType.RW,
                     description="Memory increment mode"),
            BitField(name="PSIZE", bit_offset=11, bit_width=2, access=AccessType.RW,
                     description="Peripheral data size"),
            BitField(name="MSIZE", bit_offset=13, bit_width=2, access=AccessType.RW,
                     description="Memory data size"),
            BitField(name="PINCOS", bit_offset=15, bit_width=1, access=AccessType.RW),
            BitField(name="PL", bit_offset=16, bit_width=2, access=AccessType.RW,
                     description="Priority level"),
            BitField(name="DBM", bit_offset=18, bit_width=1, access=AccessType.RW,
                     description="Double buffer mode"),
            BitField(name="CT", bit_offset=19, bit_width=1, access=AccessType.RW,
                     description="Current target"),
            BitField(name="PBURST", bit_offset=21, bit_width=2, access=AccessType.RW),
            BitField(name="MBURST", bit_offset=23, bit_width=2, access=AccessType.RW),
            BitField(name="CHSEL", bit_offset=25, bit_width=3, access=AccessType.RW,
                     description="Channel selection"),
        ]
        registers.append(Register(
            name=f"S{i}CR", description=f"Stream {i} configuration register",
            offset=stream_base, fields=cr_fields,
        ))

        # Number of Data Register (SxNDTR)
        registers.append(Register(
            name=f"S{i}NDTR", description=f"Stream {i} number of data register",
            offset=stream_base + 0x04,
            fields=[BitField(name="NDT", bit_offset=0, bit_width=16, access=AccessType.RW)],
        ))

        # Peripheral Address Register (SxPAR)
        registers.append(Register(
            name=f"S{i}PAR", description=f"Stream {i} peripheral address register",
            offset=stream_base + 0x08,
        ))

        # Memory 0 Address Register (SxM0AR)
        registers.append(Register(
            name=f"S{i}M0AR", description=f"Stream {i} memory 0 address register",
            offset=stream_base + 0x0C,
        ))

        # Memory 1 Address Register (SxM1AR)
        registers.append(Register(
            name=f"S{i}M1AR", description=f"Stream {i} memory 1 address register",
            offset=stream_base + 0x10,
        ))

        # FIFO Control Register (SxFCR)
        fcr_fields = [
            BitField(name="FTH", bit_offset=0, bit_width=2, access=AccessType.RW,
                     description="FIFO threshold selection"),
            BitField(name="DMDIS", bit_offset=2, bit_width=1, access=AccessType.RW,
                     description="Direct mode disable"),
            BitField(name="FS", bit_offset=3, bit_width=3, access=AccessType.RO,
                     description="FIFO status"),
            BitField(name="FEIE", bit_offset=7, bit_width=1, access=AccessType.RW,
                     description="FIFO error interrupt enable"),
        ]
        registers.append(Register(
            name=f"S{i}FCR", description=f"Stream {i} FIFO control register",
            offset=stream_base + 0x14,
            reset_value=0x21,
            fields=fcr_fields,
        ))

    return RegisterBlock(
        name=name,
        base_address=base_address,
        registers=registers,
    )


def build_dma_state_machine(stream_index: int = 0) -> StateMachine:
    """Build state machine for a single DMA stream."""
    return StateMachine(
        name=f"DMA_Stream{stream_index}",
        description=f"DMA stream {stream_index} transfer state machine",
        states=[
            State(name="disabled", is_initial=True,
                  description="Stream disabled, EN=0"),
            State(name="idle", description="Stream enabled but no transfer active"),
            State(name="transferring",
                  description="Transfer in progress, NDTR counting down"),
            State(name="half_transfer",
                  description="Half transfer reached, HTIF set"),
            State(name="complete",
                  description="Transfer complete, TCIF set"),
            State(name="error", description="Transfer error, TEIF set"),
        ],
        transitions=[
            Transition(source="disabled", target="idle",
                       trigger=f"reg_write:S{stream_index}CR",
                       condition="EN bit set to 1"),
            Transition(source="idle", target="transferring",
                       trigger="dma_request",
                       condition="Peripheral DMA request"),
            Transition(source="transferring", target="half_transfer",
                       trigger="internal:half_count",
                       condition="NDTR reaches half of initial value"),
            Transition(source="half_transfer", target="transferring",
                       trigger="internal:continue",
                       condition="Continue after HTIF"),
            Transition(source="transferring", target="complete",
                       trigger="internal:count_zero",
                       condition="NDTR reaches 0"),
            Transition(source="complete", target="idle",
                       trigger="internal:restart",
                       condition="Circular mode, reload NDTR"),
            Transition(source="complete", target="disabled",
                       trigger="internal:done",
                       condition="Normal mode, disable stream"),
            Transition(source="transferring", target="error",
                       trigger="internal:error",
                       condition="Bus error or FIFO error"),
            Transition(source="error", target="disabled",
                       trigger="internal:error_disable"),
            Transition(source="idle", target="disabled",
                       trigger=f"reg_write:S{stream_index}CR",
                       condition="EN bit cleared"),
        ],
    )


def build_dma_interrupt_model(
    name: str = "DMA1",
    streams: int = 8,
    base_irq: int = 11,
) -> InterruptModel:
    """Build interrupt model for DMA controller.

    STM32F4 DMA1 IRQs: Stream0=11, Stream1=12, ..., Stream7=47 (non-contiguous)
    """
    lines: list[InterruptLine] = []

    for i in range(streams):
        flags = []
        sr_name = "LISR" if i < 4 else "HISR"
        base_bit = (i % 4) * 6 + ((i % 4) // 2) * 4

        for flag_name, bit_offset_in_group, enable_field in [
            ("TCIF", 5, "TCIE"),
            ("HTIF", 4, "HTIE"),
            ("TEIF", 3, "TEIE"),
            ("DMEIF", 2, "DMEIE"),
            ("FEIF", 0, "FEIE"),
        ]:
            cr_name = f"S{i}CR" if flag_name != "FEIF" else f"S{i}FCR"
            enable_bit = {
                "TCIE": 4, "HTIE": 3, "TEIE": 2, "DMEIE": 1, "FEIE": 7,
            }[enable_field]

            flags.append(InterruptFlag(
                name=f"{flag_name}{i}",
                register_name=sr_name,
                bit_offset=base_bit + bit_offset_in_group,
                clear_behavior=FlagBehavior.SOFTWARE_CLEAR,
                clear_register="LIFCR" if i < 4 else "HIFCR",
                clear_bit_offset=base_bit + bit_offset_in_group,
                enable_register=cr_name,
                enable_bit_offset=enable_bit,
            ))

        lines.append(InterruptLine(
            irq_number=base_irq + i,
            name=f"{name}_Stream{i}_IRQn",
            description=f"{name} Stream {i} global interrupt",
            flags=flags,
        ))

    event_map = {}
    for i in range(streams):
        event_map[f"stream{i}_tc"] = [f"TCIF{i}"]
        event_map[f"stream{i}_ht"] = [f"HTIF{i}"]
        event_map[f"stream{i}_te"] = [f"TEIF{i}"]
        event_map[f"stream{i}_dme"] = [f"DMEIF{i}"]
        event_map[f"stream{i}_fe"] = [f"FEIF{i}"]

    return InterruptModel(
        peripheral_name=name,
        lines=lines,
        event_sources=list(event_map.keys()),
        flag_to_event_map=event_map,
    )


def build_dma_peripheral(
    name: str = "DMA1",
    base_address: int = 0x4002_6000,
    streams: int = 8,
    mcu_family: str = "STM32F4",
) -> Peripheral:
    """Build a complete DMA controller peripheral model."""
    reg_block = build_dma_register_block(streams, name, base_address)
    state_machines = [build_dma_state_machine(i) for i in range(streams)]
    interrupt_model = build_dma_interrupt_model(name, streams)

    return Peripheral(
        name=name,
        description=f"{mcu_family} DMA Controller",
        peripheral_type=PeripheralType.DMA,
        base_address=base_address,
        register_block=reg_block,
        state_machines=state_machines,
        interrupt_model=interrupt_model,
        mcu_family=mcu_family,
        clock=ClockConfig(
            bus="AHB1",
            enable_register="RCC_AHB1ENR",
            enable_bit=f"{name}EN",
        ),
    )


def get_dma_dependency_edges(
    dma_name: str,
    peripheral_name: str,
    stream: int,
    channel: int,
    direction: str = "periph_to_mem",
) -> DependencyEdge:
    """Create a dependency edge for DMA-peripheral connection."""
    return DependencyEdge(
        source=dma_name,
        target=peripheral_name,
        dep_type=DependencyType.DMA_CHANNEL,
        description=f"{dma_name} Stream {stream} Ch {channel} -> {peripheral_name}",
        channel=f"Stream{stream}_Ch{channel}",
    )
