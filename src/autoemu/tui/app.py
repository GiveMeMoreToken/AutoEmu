"""Main TUI application for AutoEmu."""

from __future__ import annotations

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Button, Footer, Header, Input, Label, Static

from autoemu.tui.widgets import LogPanel, PipelinePhaseList


BANNER = (
    "     _         _        _____                 " "\n"
    r"   / \  _   _| |_ ___ | ____|_ __ ___  _   _ " "\n"
    r"  / _ \| | | | __/ _ \|  _| | '_ ` _ \| | | |" "\n"
    r" / ___ \ |_| | || (_) | |___| | | | | | |_| |" "\n"
    r"/_/   \_\__,_|\__\___/|_____|_| |_| |_|\__,_|"
)


class AutoEmuApp(App):
    """Interactive terminal UI for the AutoEmu pipeline."""

    TITLE = "AutoEmu"
    SUB_TITLE = "QEMU Peripheral Model Generator"

    CSS = """
    Screen {
        layout: vertical;
        background: $surface;
    }

    #banner {
        text-align: center;
        color: $accent;
        padding: 1 0 0 0;
        width: 1fr;
        text-style: bold;
    }

    #subtitle {
        text-align: center;
        color: $text-muted;
        padding: 0 0 1 0;
        width: 1fr;
    }

    #form-area {
        height: auto;
        padding: 0 4;
    }

    .form-row {
        height: auto;
        margin-bottom: 1;
        align: left middle;
    }

    .form-row Label {
        width: 22;
        padding: 1 1 0 0;
    }

    .form-row Input {
        width: 1fr;
    }

    #action-bar {
        height: auto;
        padding: 1 4;
        align: center middle;
    }

    #action-bar Button {
        margin: 0 2;
        min-width: 20;
    }

    #pipeline-area {
        height: auto;
        padding: 0 4;
    }

    #log-area {
        height: 1fr;
        padding: 0 4 1 4;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
        Binding("ctrl+r", "run_pipeline", "Run", show=True),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(BANNER, id="banner")
        yield Static("Automated QEMU peripheral model generation for any MCU", id="subtitle")

        with Vertical(id="form-area"):
            with Horizontal(classes="form-row"):
                yield Label("Target Board / MCU:")
                yield Input(
                    placeholder="e.g. STM32F407VG, HIKEY960, ESP32, NRF52840",
                    id="input-mcu",
                )
            with Horizontal(classes="form-row"):
                yield Label("Target Peripheral:")
                yield Input(
                    placeholder="e.g. ETH, GPU, USB, SPI, UART",
                    id="input-peripheral",
                )

        with Container(id="action-bar"):
            yield Button(
                "Run Pipeline  [dim](Ctrl+R)[/]",
                variant="primary",
                id="btn-run",
            )

        with Container(id="pipeline-area"):
            yield PipelinePhaseList(id="phase-list")

        with Container(id="log-area"):
            yield LogPanel(id="log")

        yield Footer()

    def on_mount(self) -> None:
        log = self.query_one("#log", LogPanel)
        log.log_info(
            "Enter a [bold]target board[/] and [bold]peripheral[/], "
            "then press [bold]Run Pipeline[/]."
        )
        log.log_info(
            "AutoEmu will automatically detect the platform, fetch data, "
            "build a QEMU model, and validate the output."
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-run":
            self.action_run_pipeline()

    def action_run_pipeline(self) -> None:
        mcu = self.query_one("#input-mcu", Input).value.strip()
        periph = self.query_one("#input-peripheral", Input).value.strip()
        log = self.query_one("#log", LogPanel)

        if not mcu or not periph:
            log.log_error("Please fill in both [bold]Target Board[/] and [bold]Target Peripheral[/].")
            return

        # Disable button to prevent double-run
        btn = self.query_one("#btn-run", Button)
        btn.disabled = True
        self._run_pipeline_worker(mcu, periph)

    @work(thread=True)
    def _run_pipeline_worker(self, mcu: str, periph: str) -> None:
        from autoemu.agent.runtime import AutoEmuAgentRuntime, PipelineProgress

        log = self.query_one("#log", LogPanel)
        phases = self.query_one("#phase-list", PipelinePhaseList)

        # Reset phase display
        self.app.call_from_thread(phases.reset_phases)

        log.log_info(f"Starting pipeline for [bold]{mcu}[/] / [bold]{periph}[/] ...")

        def on_progress(p: PipelineProgress) -> None:
            if p.error:
                log.log_error(f"Pipeline error: {p.error}")
                return
            if p.finished:
                return
            self.app.call_from_thread(phases.set_phase_running, p.phase)
            if p.detail:
                log.log_info(f"[Phase {p.phase}] {p.detail}")

        try:
            runtime = AutoEmuAgentRuntime()
            result = runtime.run_pipeline(
                target_mcu=mcu,
                target_peripheral=periph,
                on_progress=on_progress,
            )

            # Mark phases done/errored based on actual results
            if result.success:
                for i in range(1, 4):  # phases 1-3 always done if success
                    self.app.call_from_thread(phases.set_phase_done, i)
                # Phase 4 reflects validation outcome
                val_ok = result.validation_result.get("success", True)
                if val_ok:
                    self.app.call_from_thread(phases.set_phase_done, 4)
                else:
                    self.app.call_from_thread(phases.set_phase_error, 4)
                log.log_success("Pipeline completed!")
            else:
                log.log_error(f"Pipeline failed: {result.error}")

            # Show results
            if result.platform:
                log.log_info(f"Platform: [bold]{result.platform}[/]")

            if result.generated_files:
                log.log_info(f"Generated [bold]{len(result.generated_files)}[/] file(s):")
                for f in result.generated_files[:20]:
                    log.write(f"  [cyan]{f}[/]")
                if len(result.generated_files) > 20:
                    log.write(f"  ... and {len(result.generated_files) - 20} more")

            # Show validation results
            val = result.validation_result
            if val:
                checked = val.get("files_checked", 0)
                errors = val.get("errors", [])
                warnings = val.get("warnings", [])
                if val.get("success"):
                    log.log_success(f"Validation: {checked} file(s) checked, no errors")
                else:
                    log.log_error(f"Validation: {len(errors)} error(s) in {checked} file(s)")
                    for err in errors[:10]:
                        log.write(f"  [red]{err.get('file', '?')}: {err.get('stderr', '')[:200]}[/]")
                for w in warnings:
                    log.write(f"  [yellow]Warning: {w}[/]")

        except Exception as exc:
            import traceback
            log.log_error(f"Unexpected error: {exc}")
            log.write(f"[red]{traceback.format_exc()}[/]")

        finally:
            btn = self.query_one("#btn-run", Button)
            self.app.call_from_thread(setattr, btn, "disabled", False)
