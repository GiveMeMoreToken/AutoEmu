"""Register model: bit fields, registers, and register blocks."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class AccessType(str, Enum):
    """Register or field access type."""

    RW = "RW"  # Read-Write
    RO = "RO"  # Read-Only
    WO = "WO"  # Write-Only
    W1C = "W1C"  # Write-1-to-Clear
    W1S = "W1S"  # Write-1-to-Set
    W0C = "W0C"  # Write-0-to-Clear
    RC_W1 = "RC_W1"  # Read-Clear, Write-1
    RC_W0 = "RC_W0"  # Read-Clear, Write-0
    RS = "RS"  # Read-to-Set
    RSVD = "RSVD"  # Reserved


class BitField(BaseModel):
    """A single bit field within a register."""

    name: str
    description: str = ""
    bit_offset: int = Field(ge=0, le=31)
    bit_width: int = Field(ge=1, le=32)
    access: AccessType = AccessType.RW
    reset_value: int = 0
    enum_values: dict[str, int] = Field(default_factory=dict)

    @property
    def mask(self) -> int:
        return ((1 << self.bit_width) - 1) << self.bit_offset

    def extract(self, reg_value: int) -> int:
        return (reg_value & self.mask) >> self.bit_offset

    def insert(self, reg_value: int, field_value: int) -> int:
        cleared = reg_value & ~self.mask
        return cleared | ((field_value & ((1 << self.bit_width) - 1)) << self.bit_offset)


class Register(BaseModel):
    """A single memory-mapped register."""

    name: str
    description: str = ""
    offset: int = Field(ge=0, description="Offset from peripheral base address")
    size: int = Field(default=32, description="Register width in bits")
    access: AccessType = AccessType.RW
    reset_value: int = 0
    fields: list[BitField] = Field(default_factory=list)

    def get_field(self, name: str) -> BitField | None:
        for f in self.fields:
            if f.name == name:
                return f
        return None

    def apply_write(self, current: int, write_value: int) -> int:
        """Apply a write operation respecting field access types."""
        result = current
        if not self.fields:
            if self.access == AccessType.RO:
                return current
            return write_value

        for field in self.fields:
            written_bits = field.extract(write_value)
            current_bits = field.extract(current)

            match field.access:
                case AccessType.RO | AccessType.RSVD:
                    pass  # No change
                case AccessType.W1C:
                    new_bits = current_bits & ~written_bits
                    result = field.insert(result, new_bits)
                case AccessType.W1S:
                    new_bits = current_bits | written_bits
                    result = field.insert(result, new_bits)
                case AccessType.W0C:
                    new_bits = current_bits & written_bits
                    result = field.insert(result, new_bits)
                case _:
                    result = field.insert(result, written_bits)
        return result

    def apply_read(self, current: int) -> tuple[int, int]:
        """Apply a read operation. Returns (read_value, new_register_value)."""
        read_val = current
        new_val = current
        for field in self.fields:
            match field.access:
                case AccessType.RC_W1 | AccessType.RC_W0:
                    new_val = field.insert(new_val, 0)
                case AccessType.RS:
                    new_val = field.insert(new_val, (1 << field.bit_width) - 1)
                case AccessType.WO:
                    read_val = field.insert(read_val, 0)
        return read_val, new_val


class RegisterBlock(BaseModel):
    """A block of registers belonging to a peripheral."""

    name: str
    description: str = ""
    base_address: int = 0
    address_size: int = 0
    registers: list[Register] = Field(default_factory=list)

    def get_register(self, name: str) -> Register | None:
        for r in self.registers:
            if r.name == name:
                return r
        return None

    def get_register_at(self, offset: int) -> Register | None:
        for r in self.registers:
            if r.offset == offset:
                return r
        return None

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RegisterBlock:
        return cls.model_validate(data)
