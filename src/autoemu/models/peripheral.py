"""Peripheral model: top-level representation of an MCU peripheral."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from autoemu.models.register import RegisterBlock
from autoemu.models.state_machine import StateMachine
from autoemu.models.interrupt import InterruptModel
from autoemu.models.dependency import DependencyGraph


class PeripheralType(str, Enum):
    """Enumeration of supported peripheral types."""

    RADIO = "radio"
    ETH = "eth"
    USB = "usb"
    DMA = "dma"
    TIMER = "timer"
    UART = "uart"
    SPI = "spi"
    I2C = "i2c"
    GPIO = "gpio"
    ADC = "adc"
    RCC = "rcc"
    NVIC = "nvic"
    GENERIC = "generic"


class ClockConfig(BaseModel):
    """Clock configuration for a peripheral."""

    source: str = ""
    bus: str = ""  # AHB1, APB1, APB2, etc.
    enable_register: str = ""
    enable_bit: str = ""
    max_frequency_hz: int = 0


class Peripheral(BaseModel):
    """Complete model of an MCU peripheral."""

    name: str
    description: str = ""
    peripheral_type: PeripheralType = PeripheralType.GENERIC
    base_address: int = 0
    address_size: int = 0  # Total address space in bytes
    version: str = ""
    mcu_family: str = ""  # e.g., "STM32F4", "STM32H7"

    register_block: RegisterBlock = Field(default_factory=lambda: RegisterBlock(name=""))
    state_machines: list[StateMachine] = Field(default_factory=list)
    interrupt_model: InterruptModel | None = None
    dependencies: DependencyGraph | None = None
    clock: ClockConfig = Field(default_factory=ClockConfig)

    # Runtime state
    _register_values: dict[int, int] = {}

    def model_post_init(self, __context: Any) -> None:
        self._register_values = {}
        for reg in self.register_block.registers:
            self._register_values[reg.offset] = reg.reset_value

    def reset(self) -> None:
        """Reset all registers to their reset values."""
        for reg in self.register_block.registers:
            self._register_values[reg.offset] = reg.reset_value
        for sm in self.state_machines:
            sm.reset()

    def read_register(self, offset: int) -> int | None:
        reg = self.register_block.get_register_at(offset)
        if reg is None:
            return None
        current = self._register_values.get(offset, 0)
        read_val, new_val = reg.apply_read(current)
        self._register_values[offset] = new_val
        return read_val

    def write_register(self, offset: int, value: int) -> bool:
        reg = self.register_block.get_register_at(offset)
        if reg is None:
            return False
        current = self._register_values.get(offset, 0)
        new_val = reg.apply_write(current, value)
        self._register_values[offset] = new_val
        self._on_register_write(reg, current, new_val)
        return True

    def _on_register_write(self, reg: Any, old_value: int, new_value: int) -> None:
        """Hook for peripheral-specific write side effects."""
        for sm in self.state_machines:
            sm.evaluate_transitions(
                trigger_source=f"reg_write:{reg.name}",
                context={"old_value": old_value, "new_value": new_value},
            )

    def get_register_value(self, offset: int) -> int:
        return self._register_values.get(offset, 0)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Peripheral:
        return cls.model_validate(data)
