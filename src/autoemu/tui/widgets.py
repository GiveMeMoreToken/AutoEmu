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

    # -- plain log levels ----------------------------------------------------

    def log_info(self, message: str) -> None:
        self.write(f"[bold cyan]INFO [/]  {message}")

    def log_success(self, message: str) -> None:
        self.write(f"[bold green]OK   [/]  {message}")

    def log_error(self, message: str) -> None:
        self.write(f"[bold red]ERROR[/]  {message}")

    # -- styled kinds (Claude Code-like) -------------------------------------

    def log_kind(self, message: str, kind: str = "info") -> None:
        """Write a message with a style determined by *kind*."""
        formatter = _KIND_FORMATTERS.get(kind, _fmt_info)
        self.write(formatter(message))


# -- formatters for each kind -----------------------------------------------

def _fmt_info(msg: str) -> str:
    return f"[bold cyan]INFO [/]  {msg}"

def _fmt_agent_thinking(msg: str) -> str:
    return f"[bold magenta]  ◐  [/] [dim italic]{msg}[/]"

def _fmt_agent_tool(msg: str) -> str:
    return f"[bold yellow]  ⚙  [/] [bold]{msg}[/]"

def _fmt_agent_text(msg: str) -> str:
    return f"[bold blue]  ▸  [/] {msg}"

def _fmt_search(msg: str) -> str:
    return f"[bold cyan]  🔍 [/] {msg}"

def _fmt_download(msg: str) -> str:
    return f"[bold green]  ⬇  [/] {msg}"

def _fmt_compile(msg: str) -> str:
    return f"[bold white]  ⏻  [/] [dim]{msg}[/]"

def _fmt_warn(msg: str) -> str:
    return f"[bold yellow]WARN [/]  {msg}"

def _fmt_fail(msg: str) -> str:
    return f"[bold red]FAIL [/]  {msg}"

_KIND_FORMATTERS: dict[str, object] = {
    "info":           _fmt_info,
    "agent_thinking": _fmt_agent_thinking,
    "agent_tool":     _fmt_agent_tool,
    "agent_text":     _fmt_agent_text,
    "search":         _fmt_search,
    "download":       _fmt_download,
    "compile":        _fmt_compile,
    "warn":           _fmt_warn,
    "fail":           _fmt_fail,
}


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
        for i in range(1, phase):
            name = PIPELINE_PHASES[i - 1]
            widget = self.query_one(f"#phase-{i}", Static)
            widget.update(f"  [bold green]✓[/] {i}. {name}")
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
