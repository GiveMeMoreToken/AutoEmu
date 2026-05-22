"""Cross-peripheral dependency graph model."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class DependencyType(str, Enum):
    """Type of cross-peripheral dependency."""

    DMA_CHANNEL = "dma_channel"  # Peripheral uses DMA for data transfer
    CLOCK_GATE = "clock_gate"  # Peripheral depends on clock enable
    TRIGGER = "trigger"  # Timer/event triggers peripheral operation
    IRQ_CHAIN = "irq_chain"  # Interrupt chaining between peripherals
    DATA_FLOW = "data_flow"  # Data flows from one peripheral to another
    GPIO_AF = "gpio_af"  # GPIO alternate function mapping
    SHARED_BUS = "shared_bus"  # Shared memory bus arbitration


class DependencyEdge(BaseModel):
    """A single dependency edge between two peripherals."""

    source: str  # Source peripheral name
    target: str  # Target peripheral name
    dep_type: DependencyType
    description: str = ""
    channel: str = ""  # e.g., DMA channel/stream number
    trigger_source: str = ""  # e.g., timer trigger output
    config_registers: list[str] = Field(
        default_factory=list,
        description="Registers that configure this dependency",
    )
    bidirectional: bool = False


class DependencyGraph(BaseModel):
    """Graph of dependencies between peripherals in an MCU."""

    mcu_name: str = ""
    edges: list[DependencyEdge] = Field(default_factory=list)

    def get_dependencies_of(self, peripheral: str) -> list[DependencyEdge]:
        """Get all dependencies where peripheral is the target (depends on)."""
        return [e for e in self.edges if e.target == peripheral]

    def get_dependents_of(self, peripheral: str) -> list[DependencyEdge]:
        """Get all peripherals that depend on this one."""
        return [e for e in self.edges if e.source == peripheral]

    def get_edges_by_type(self, dep_type: DependencyType) -> list[DependencyEdge]:
        return [e for e in self.edges if e.dep_type == dep_type]

    def get_all_peripherals(self) -> set[str]:
        peripherals: set[str] = set()
        for e in self.edges:
            peripherals.add(e.source)
            peripherals.add(e.target)
        return peripherals

    def topological_order(self) -> list[str]:
        """Return peripherals in dependency order (dependencies first)."""
        in_degree: dict[str, int] = {}
        adj: dict[str, list[str]] = {}
        for p in self.get_all_peripherals():
            in_degree[p] = 0
            adj[p] = []

        for e in self.edges:
            adj[e.source].append(e.target)
            in_degree[e.target] = in_degree.get(e.target, 0) + 1

        queue = [p for p, d in in_degree.items() if d == 0]
        result: list[str] = []
        while queue:
            node = queue.pop(0)
            result.append(node)
            for neighbor in adj[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)
        if len(result) < len(in_degree):
            raise ValueError(f"Cycle detected in dependency graph for {self.mcu_name}")
        return result

    def to_dot(self) -> str:
        lines = [f'digraph "{self.mcu_name}" {{', "  rankdir=LR;"]
        for e in self.edges:
            style = "dashed" if e.dep_type == DependencyType.TRIGGER else "solid"
            label = e.dep_type.value
            if e.channel:
                label += f"\\n{e.channel}"
            lines.append(
                f'  "{e.source}" -> "{e.target}" [label="{label}", style={style}];'
            )
        lines.append("}")
        return "\n".join(lines)
