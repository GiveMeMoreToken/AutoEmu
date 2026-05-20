"""Data models for peripheral modeling."""

from autoemu.models.register import (
    AccessType,
    BitField,
    Register,
    RegisterBlock,
)
from autoemu.models.peripheral import Peripheral, PeripheralType
from autoemu.models.state_machine import State, Transition, StateMachine
from autoemu.models.interrupt import InterruptLine, InterruptModel, FlagBehavior
from autoemu.models.dependency import DependencyEdge, DependencyGraph, DependencyType
from autoemu.models.qemu import (
    QEMUDeviceIdentity,
    QEMUDeviceTreeNode,
    QEMUDeviceTreeRegRegion,
    QEMUFileLayout,
    QEMUHardwareModel,
    QEMUIRQResource,
    QEMUMMIORegion,
    build_qemu_hardware_model,
)

__all__ = [
    "AccessType",
    "BitField",
    "Register",
    "RegisterBlock",
    "Peripheral",
    "PeripheralType",
    "State",
    "Transition",
    "StateMachine",
    "InterruptLine",
    "InterruptModel",
    "FlagBehavior",
    "DependencyEdge",
    "DependencyGraph",
    "DependencyType",
    "QEMUDeviceIdentity",
    "QEMUDeviceTreeNode",
    "QEMUDeviceTreeRegRegion",
    "QEMUFileLayout",
    "QEMUHardwareModel",
    "QEMUIRQResource",
    "QEMUMMIORegion",
    "build_qemu_hardware_model",
]
