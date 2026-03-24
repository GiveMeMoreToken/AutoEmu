"""State machine model for peripheral behavior."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class Transition(BaseModel):
    """A state machine transition."""

    source: str
    target: str
    trigger: str = ""  # e.g., "reg_write:CR1", "event:tx_complete"
    condition: str = ""  # Human-readable condition expression
    actions: list[str] = Field(default_factory=list)  # Actions on transition
    priority: int = 0

    def matches(
        self, trigger_source: str, context: dict[str, Any] | None = None
    ) -> bool:
        """Check if this transition matches the given trigger."""
        if not self.trigger:
            return False
        return self.trigger == trigger_source


class State(BaseModel):
    """A state in the state machine."""

    name: str
    description: str = ""
    is_initial: bool = False
    is_final: bool = False
    entry_actions: list[str] = Field(default_factory=list)
    exit_actions: list[str] = Field(default_factory=list)
    register_effects: dict[str, int] = Field(
        default_factory=dict,
        description="Register field effects when entering this state: field_name -> value",
    )


class StateMachine(BaseModel):
    """A finite state machine modeling peripheral behavior."""

    name: str
    description: str = ""
    states: list[State] = Field(default_factory=list)
    transitions: list[Transition] = Field(default_factory=list)
    current_state: str = ""

    def model_post_init(self, __context: Any) -> None:
        if not self.current_state:
            for s in self.states:
                if s.is_initial:
                    self.current_state = s.name
                    break

    def reset(self) -> None:
        for s in self.states:
            if s.is_initial:
                self.current_state = s.name
                return

    def get_state(self, name: str) -> State | None:
        for s in self.states:
            if s.name == name:
                return s
        return None

    def get_transitions_from(self, state_name: str) -> list[Transition]:
        return sorted(
            [t for t in self.transitions if t.source == state_name],
            key=lambda t: t.priority,
            reverse=True,
        )

    def evaluate_transitions(
        self,
        trigger_source: str,
        context: dict[str, Any] | None = None,
    ) -> str | None:
        """Evaluate transitions from current state. Returns new state name or None."""
        candidates = self.get_transitions_from(self.current_state)
        for t in candidates:
            if t.matches(trigger_source, context):
                self.current_state = t.target
                return t.target
        return None

    def get_reachable_states(self) -> set[str]:
        """BFS to find all reachable states from initial state."""
        initial = None
        for s in self.states:
            if s.is_initial:
                initial = s.name
                break
        if initial is None:
            return set()

        visited: set[str] = set()
        queue = [initial]
        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            for t in self.transitions:
                if t.source == current and t.target not in visited:
                    queue.append(t.target)
        return visited

    def to_dot(self) -> str:
        """Export state machine to Graphviz DOT format."""
        lines = [f'digraph "{self.name}" {{', "  rankdir=LR;"]
        for s in self.states:
            shape = "doublecircle" if s.is_final else "circle"
            if s.is_initial:
                lines.append(f"  __start [shape=point];")
                lines.append(f'  __start -> "{s.name}";')
            lines.append(f'  "{s.name}" [shape={shape}];')
        for t in self.transitions:
            label = t.trigger
            if t.condition:
                label += f" [{t.condition}]"
            lines.append(f'  "{t.source}" -> "{t.target}" [label="{label}"];')
        lines.append("}")
        return "\n".join(lines)
