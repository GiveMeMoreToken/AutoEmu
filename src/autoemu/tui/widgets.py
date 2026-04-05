"""Custom widgets for the AutoEmu TUI."""

from __future__ import annotations

from textual.containers import Vertical
from textual.widgets import RichLog, Static

from autoemu.agent.runtime import PIPELINE_PHASES


class LogPanel(RichLog):
    """A scrolling log panel for operation output with Rich markup support."""

    DEFAULT_CSS = """
    LogPanel {
        height: 1fr;
        border: solid $accent;
        background: $surface;
        padding: 0 1;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(markup=True, **kwargs)

    def log_info(self, message: str) -> None:
        self.write(f"[bold cyan]INFO [/]  {message}")

    def log_success(self, message: str) -> None:
        self.write(f"[bold green]OK   [/]  {message}")

    def log_error(self, message: str) -> None:
        self.write(f"[bold red]ERROR[/]  {message}")


class PipelinePhaseList(Vertical):
    """Displays the pipeline phases with status indicators."""

    DEFAULT_CSS = """
    PipelinePhaseList {
        height: auto;
        padding: 1 0;
    }

    PipelinePhaseList .phase-item {
        height: auto;
        padding: 0 0;
    }
    """

    def compose(self):
        for i, name in enumerate(PIPELINE_PHASES, start=1):
            yield Static(
                f"  [dim]{i}. {name}[/]",
                id=f"phase-{i}",
                classes="phase-item",
            )

    def reset_phases(self) -> None:
        for i, name in enumerate(PIPELINE_PHASES, start=1):
            widget = self.query_one(f"#phase-{i}", Static)
            widget.update(f"  [dim]{i}. {name}[/]")

    def set_phase_running(self, phase: int) -> None:
        # Mark previous phases as done
        for i in range(1, phase):
            name = PIPELINE_PHASES[i - 1]
            widget = self.query_one(f"#phase-{i}", Static)
            widget.update(f"  [bold green]✓[/] {i}. {name}")
        # Mark current as running
        name = PIPELINE_PHASES[phase - 1]
        widget = self.query_one(f"#phase-{phase}", Static)
        widget.update(f"  [bold yellow]▶[/] {phase}. {name} ...")

    def set_phase_done(self, phase: int) -> None:
        name = PIPELINE_PHASES[phase - 1]
        widget = self.query_one(f"#phase-{phase}", Static)
        widget.update(f"  [bold green]✓[/] {phase}. {name}")

    def set_phase_error(self, phase: int) -> None:
        name = PIPELINE_PHASES[phase - 1]
        widget = self.query_one(f"#phase-{phase}", Static)
        widget.update(f"  [bold red]✗[/] {phase}. {name}")
