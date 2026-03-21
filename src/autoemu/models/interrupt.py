"""Interrupt and event model for peripheral modeling."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class FlagBehavior(str, Enum):
    """How an interrupt/status flag is cleared."""

    SOFTWARE_CLEAR = "software_clear"  # Cleared by writing to register
    READ_CLEAR = "read_clear"  # Cleared on read
    HARDWARE_CLEAR = "hardware_clear"  # Cleared by hardware event
    W1C = "w1c"  # Write-1-to-clear


class InterruptFlag(BaseModel):
    """A single interrupt/status flag in a register."""

    name: str
    description: str = ""
    register_name: str = ""  # Which status register
    bit_offset: int = 0
    clear_behavior: FlagBehavior = FlagBehavior.W1C
    clear_register: str = ""  # Register used to clear (if different)
    clear_bit_offset: int = 0
    enable_register: str = ""  # Interrupt enable register
    enable_bit_offset: int = 0


class InterruptLine(BaseModel):
    """An NVIC interrupt line connection."""

    irq_number: int
    name: str  # e.g., "ETH_IRQn", "OTG_FS_IRQn"
    description: str = ""
    flags: list[InterruptFlag] = Field(default_factory=list)
    priority: int = 0
    subpriority: int = 0

    def get_active_flags(self, register_values: dict[str, int]) -> list[InterruptFlag]:
        """Determine which flags would trigger this interrupt."""
        active = []
        for flag in self.flags:
            sr_val = register_values.get(flag.register_name, 0)
            en_val = register_values.get(flag.enable_register, 0)
            flag_set = (sr_val >> flag.bit_offset) & 1
            enable_set = (en_val >> flag.enable_bit_offset) & 1
            if flag_set and enable_set:
                active.append(flag)
        return active


class InterruptModel(BaseModel):
    """Complete interrupt model for a peripheral."""

    peripheral_name: str = ""
    lines: list[InterruptLine] = Field(default_factory=list)
    event_sources: list[str] = Field(
        default_factory=list,
        description="Internal events that can set flags (e.g., 'tx_complete', 'rx_fifo_not_empty')",
    )
    flag_to_event_map: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Mapping: event_name -> list of flag names it sets",
    )

    def get_line(self, irq_number: int) -> InterruptLine | None:
        for line in self.lines:
            if line.irq_number == irq_number:
                return line
        return None

    def get_line_by_name(self, name: str) -> InterruptLine | None:
        for line in self.lines:
            if line.name == name:
                return line
        return None

    def trigger_event(self, event_name: str) -> list[str]:
        """Returns list of flag names that should be set by this event."""
        return self.flag_to_event_map.get(event_name, [])

    def should_assert_irq(
        self, irq_number: int, register_values: dict[str, int]
    ) -> bool:
        """Check if an IRQ line should be asserted given current register state."""
        line = self.get_line(irq_number)
        if line is None:
            return False
        return len(line.get_active_flags(register_values)) > 0
