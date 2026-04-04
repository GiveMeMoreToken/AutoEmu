"""Platform abstraction base classes."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from autoemu.models.register import RegisterBlock
from autoemu.parsers.driver_parser import DriverAnalysis


@dataclass(frozen=True)
class QEMUTargetInfo:
    arch: str  # "arm", "mipsel", etc.
    machine: str  # QEMU -machine value
    cpu: str  # QEMU -cpu value
    include_paths: list[str] = field(default_factory=list)
    extra_cflags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class NamingInfo:
    file_prefix: str  # e.g. "stm32"
    type_prefix: str  # e.g. "STM32"
    qemu_type_fmt: str  # e.g. "{prefix}-{snake}" -> "stm32-eth"


@dataclass(frozen=True)
class AssetDescriptor:
    key: str
    category: str
    description: str
    queries: tuple[str, ...] = ()
    preferred_domains: tuple[str, ...] = ()
    required: bool = False
    max_matches: int = 1
    file_extensions: tuple[str, ...] = ()


@dataclass
class InputBundle:
    target: str
    peripheral: str
    svd_path: str = ""
    header_path: str = ""
    driver_paths: list[str] = field(default_factory=list)
    documentation_paths: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


class Platform(ABC):
    name: str

    @abstractmethod
    def discover_inputs(self, mcu: str, peripheral: str) -> list[AssetDescriptor]:
        """Return fetchable asset descriptors for this target."""

    @abstractmethod
    def parse_registers(self, bundle: InputBundle) -> dict[str, RegisterBlock]:
        """Parse register descriptions from platform-specific input formats."""

    @abstractmethod
    def parse_drivers(self, bundle: InputBundle) -> DriverAnalysis:
        """Analyze driver code in platform-specific style."""

    @abstractmethod
    def qemu_target_info(self, mcu: str) -> QEMUTargetInfo:
        """Return QEMU arch, machine type, CPU model, include paths."""

    @abstractmethod
    def naming_convention(self, peripheral: str) -> NamingInfo:
        """Return file prefixes, QEMU type names, object names."""
