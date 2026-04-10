"""Custom widgets for the AutoEmu TUI."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from textual.containers import Vertical
from textual.widgets import RichLog, Static

from autoemu.agent.runtime import PIPELINE_PHASES

# Strip Rich markup tags for plain-text log files
_MARKUP_RE = re.compile(r"\[/?[^\[\]]*\]")


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
        self._plain_lines: list[str] = []

    def write(self, content, **kwargs):  # type: ignore[override]
        """Override to capture a plain-text copy of every line written."""
        if isinstance(content, str):
            plain = _MARKUP_RE.sub("", content).rstrip()
            if plain:
                self._plain_lines.append(plain)
        return super().write(content, **kwargs)

    def save_to_file(self, path: str | Path | None = None) -> Path:
        """Write the current log to a plain-text file.

        If *path* is not given, a timestamped file is created in the current
        working directory (``autoemu_<timestamp>.log``).

        Returns the path of the saved file.
        """
        if path is None:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = Path(f"autoemu_{stamp}.log")
        path = Path(path)
        path.write_text("\n".join(self._plain_lines), encoding="utf-8")
        return path

    def clear_log_buffer(self) -> None:
        """Clear the plain-text log buffer (call before a new run)."""
        self._plain_lines.clear()

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
