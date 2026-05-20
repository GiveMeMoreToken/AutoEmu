"""QEMU hardware integration schema models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from autoemu.modeling_utils import (
    normalize_driver_analysis,
    normalize_name,
    snake_case,
)
from autoemu.models.peripheral import Peripheral


class QEMUDeviceIdentity(BaseModel):
    """Names used to identify a generated QEMU device."""

    peripheral_name: str
    qom_type: str
    c_identifier_prefix: str
    type_macro: str
    state_struct_name: str
    kconfig_symbol: str


class QEMUFileLayout(BaseModel):
    """QEMU tree-relative file locations for generated artifacts."""

    source_path: str
    header_path: str
    meson_path: str
    meson_snippet_path: str
    qtest_path: str


class QEMUMMIORegion(BaseModel):
    """A memory-mapped IO region exported by the QEMU device."""

    name: str
    base_address: int = Field(ge=0)
    size: int = Field(ge=0)
    register_count: int = Field(ge=0)


class QEMUIRQResource(BaseModel):
    """A named interrupt resource exposed by the device."""

    name: str
    index: int = Field(ge=0)
    irq_number: int | None = Field(default=None, ge=0)
    source: str | None = None


class QEMUDeviceTreeRegRegion(BaseModel):
    """Device Tree reg entry corresponding to an MMIO region."""

    name: str
    base_address: int = Field(ge=0)
    size: int = Field(ge=0)


class QEMUDeviceTreeNode(BaseModel):
    """Device Tree node data for integrating the QEMU device."""

    node_name: str
    unit_address: str
    address_cells: int = Field(default=1, ge=0)
    size_cells: int = Field(default=1, ge=0)
    compatible: list[str] = Field(default_factory=list)
    reg: list[QEMUDeviceTreeRegRegion] = Field(default_factory=list)
    interrupt_names: list[str] = Field(default_factory=list)
    properties: dict[str, Any] = Field(default_factory=dict)


class QEMUHardwareModel(BaseModel):
    """Schema describing how an AutoEmu peripheral maps into QEMU hardware."""

    identity: QEMUDeviceIdentity
    file_layout: QEMUFileLayout
    mmio_regions: list[QEMUMMIORegion] = Field(default_factory=list)
    irq_resources: list[QEMUIRQResource] = Field(default_factory=list)
    device_tree: QEMUDeviceTreeNode


def build_qemu_hardware_model(
    peripheral: Peripheral,
    driver_analysis: Any = None,
    *,
    source_subdir: str = "hw/misc",
    header_subdir: str = "include/hw/misc",
) -> QEMUHardwareModel:
    """Derive a generic QEMU hardware integration schema from a peripheral."""
    prefix = _device_prefix(peripheral)
    peripheral_snake = normalize_name(snake_case(peripheral.name))
    peripheral_upper = peripheral_snake.upper()
    c_prefix = f"{prefix}_{peripheral_snake}"
    c_upper = c_prefix.upper()

    base_address = peripheral.base_address
    size = peripheral.address_size or _infer_address_size(peripheral)
    mmio_region = QEMUMMIORegion(
        name="mmio",
        base_address=base_address,
        size=size,
        register_count=len(peripheral.register_block.registers),
    )

    state_hints = _state_hints(driver_analysis)
    irq_resources = _irq_resources_from_hints(state_hints)
    if not irq_resources:
        irq_resources = _irq_resources_from_interrupt_model(peripheral)

    compatible = _dedupe(
        [
            *(
                _clean_hint_string(hint.get("value"))
                for hint in state_hints
                if hint.get("kind") == "compatible"
            ),
            _fallback_compatible(prefix, peripheral_snake),
        ]
    )
    address_cells = _device_tree_address_cells([mmio_region])
    size_cells = _device_tree_size_cells([mmio_region])

    return QEMUHardwareModel(
        identity=QEMUDeviceIdentity(
            peripheral_name=peripheral.name,
            qom_type=f"{prefix}-{peripheral_snake}",
            c_identifier_prefix=c_prefix,
            type_macro=f"TYPE_{c_upper}",
            state_struct_name=f"{prefix.upper()}{peripheral_upper}State",
            kconfig_symbol=c_upper,
        ),
        file_layout=QEMUFileLayout(
            source_path=f"{source_subdir.rstrip('/')}/{c_prefix}.c",
            header_path=f"{header_subdir.rstrip('/')}/{c_prefix}.h",
            meson_path=f"{source_subdir.rstrip('/')}/meson.build",
            meson_snippet_path=f"{source_subdir.rstrip('/')}/{c_prefix}.meson.inc",
            qtest_path=f"tests/qtest/{c_prefix}-test.c",
        ),
        mmio_regions=[mmio_region],
        irq_resources=irq_resources,
        device_tree=QEMUDeviceTreeNode(
            node_name=peripheral_snake,
            unit_address=f"{base_address:x}",
            address_cells=address_cells,
            size_cells=size_cells,
            compatible=compatible,
            reg=[
                QEMUDeviceTreeRegRegion(
                    name=mmio_region.name,
                    base_address=mmio_region.base_address,
                    size=mmio_region.size,
                )
            ],
            interrupt_names=[irq.name for irq in irq_resources],
            properties={},
        ),
    )


def _device_prefix(peripheral: Peripheral) -> str:
    family = (peripheral.mcu_family or "").strip()
    if not family:
        return "autoemu"
    return normalize_name(family)


def _state_hints(driver_analysis: Any) -> list[dict[str, Any]]:
    if driver_analysis is None:
        return []
    data = normalize_driver_analysis(driver_analysis)
    hints = data.get("state_hints", [])
    if not isinstance(hints, list):
        return []
    return [hint for hint in hints if isinstance(hint, dict)]


def _irq_resources_from_hints(hints: list[dict[str, Any]]) -> list[QEMUIRQResource]:
    resources: list[QEMUIRQResource] = []
    seen: set[str] = set()
    for hint in hints:
        if hint.get("kind") != "irq_resource":
            continue
        name = _clean_hint_string(hint.get("name"))
        if not name or name in seen:
            continue
        seen.add(name)
        source = (
            _clean_hint_string(hint.get("source"))
            or _clean_hint_string(hint.get("function"))
            or None
        )
        resources.append(
            QEMUIRQResource(
                name=name,
                index=len(resources),
                source=source,
            )
        )
    return resources


def _clean_hint_string(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _device_tree_address_cells(mmio_regions: list[QEMUMMIORegion]) -> int:
    max_address = 0
    for region in mmio_regions:
        end_address = region.base_address + max(region.size - 1, 0)
        max_address = max(max_address, region.base_address, end_address)
    return 2 if max_address > 0xFFFFFFFF else 1


def _device_tree_size_cells(mmio_regions: list[QEMUMMIORegion]) -> int:
    max_size = max((region.size for region in mmio_regions), default=0)
    return 2 if max_size > 0xFFFFFFFF else 1


def _irq_resources_from_interrupt_model(peripheral: Peripheral) -> list[QEMUIRQResource]:
    if peripheral.interrupt_model is None:
        return []
    resources: list[QEMUIRQResource] = []
    seen: set[str] = set()
    for line in peripheral.interrupt_model.lines:
        name = line.name.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        resources.append(
            QEMUIRQResource(
                name=name,
                index=len(resources),
                irq_number=line.irq_number if line.irq_number >= 0 else None,
            )
        )
    return resources


def _infer_address_size(peripheral: Peripheral) -> int:
    registers = peripheral.register_block.registers
    if not registers:
        return 0
    last_register = max(registers, key=lambda reg: reg.offset + (reg.size // 8))
    return last_register.offset + (last_register.size // 8)


def _fallback_compatible(prefix: str, peripheral_snake: str) -> str:
    return f"{prefix},{peripheral_snake}"


def _dedupe(values: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped
