"""Main TUI application for AutoEmu."""

from __future__ import annotations

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Button, Footer, Header, Input, Label, Select, Static

from autoemu.tui.widgets import LogPanel, PipelinePhaseList


BANNER = (
    "     _         _        _____                 " "\n"
    r"   / \  _   _| |_ ___ | ____|_ __ ___  _   _ " "\n"
    r"  / _ \| | | | __/ _ \|  _| | '_ ` _ \| | | |" "\n"
    r" / ___ \ |_| | || (_) | |___| | | | | | |_| |" "\n"
    r"/_/   \_\__,_|\__\___/|_____|_| |_| |_|\__,_|"
)


def _final_phase_status_actions(result) -> list[tuple[str, int]]:
    """Map a completed pipeline result to phase-list status method calls."""
    actions: list[tuple[str, int]] = []
    if getattr(result, "fetch_result", None):
        actions.extend([("set_phase_done", 1), ("set_phase_done", 2)])
    if getattr(result, "build_result", None):
        actions.append(("set_phase_done", 3))

    validation = getattr(result, "validation_result", None) or {}
    if validation:
        val_warnings = validation.get("warnings", [])
        val_errors = validation.get("errors", [])
        val_skipped_qemu = any("QEMU source tree not found" in w for w in val_warnings)
        if validation.get("success") and not val_skipped_qemu and not val_warnings:
            actions.append(("set_phase_done", 4))
        elif validation.get("success") and not val_errors:
            actions.append(("set_phase_warn", 4))
        else:
            actions.append(("set_phase_error", 4))

    probe = getattr(result, "probe_result", None) or {}
    if probe:
        if probe.get("success"):
            actions.append(("set_phase_done", 5))
        else:
            actions.append(("set_phase_warn", 5))
    return actions


def _schedule_progress_update(app, phases, log, progress) -> None:
    """Schedule a pipeline progress update on Textual's UI thread."""
    if progress.error:
        app.call_from_thread(log.log_error, f"Pipeline error: {progress.error}")
        return
    if progress.finished:
        return
    app.call_from_thread(phases.set_phase_running, progress.phase)
    if progress.detail:
        app.call_from_thread(log.log_kind, progress.detail, progress.kind)


class SettingsScreen(Container):
    """Inline settings panel for backend configuration."""

    DEFAULT_CSS = """
    SettingsScreen {
        display: none;
        layout: vertical;
        height: auto;
        padding: 1 4;
        border: solid $accent;
        margin: 0 4;
        background: $surface-darken-1;
    }

    SettingsScreen.visible {
        display: block;
    }

    SettingsScreen .settings-row {
        height: auto;
        margin-bottom: 1;
        align: left middle;
    }

    SettingsScreen .settings-row Label {
        width: 22;
        padding: 1 1 0 0;
    }

    SettingsScreen .settings-row Input {
        width: 1fr;
    }

    SettingsScreen .settings-row Select {
        width: 1fr;
    }

    SettingsScreen #settings-buttons {
        height: auto;
        align: left middle;
        padding-top: 1;
    }

    SettingsScreen #settings-buttons Button {
        margin-right: 2;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("[bold]Backend Settings[/]")
        with Horizontal(classes="settings-row"):
            yield Label("Backend:")
            yield Select(
                [
                    ("Claude SDK", "claude-sdk"),
                    ("Codex SDK", "codex-sdk"),
                    ("Anthropic API", "anthropic-api"),
                    ("OpenAI API", "openai-api"),
                ],
                value="codex-sdk",
                id="cfg-backend",
            )
        with Horizontal(classes="settings-row"):
            yield Label("Model:")
            yield Input(placeholder="(auto)", id="cfg-model")
        with Horizontal(classes="settings-row"):
            yield Label("OpenAI API Key:")
            yield Input(placeholder="sk-...", password=True, id="cfg-openai-key")
        with Horizontal(classes="settings-row"):
            yield Label("OpenAI Base URL:")
            yield Input(placeholder="https://api.openai.com/v1", id="cfg-openai-base")
        with Horizontal(classes="settings-row"):
            yield Label("Anthropic API Key:")
            yield Input(placeholder="sk-ant-...", password=True, id="cfg-anthropic-key")
        with Horizontal(classes="settings-row"):
            yield Label("Anthropic Base URL:")
            yield Input(placeholder="https://api.anthropic.com", id="cfg-anthropic-base")
        with Horizontal(classes="settings-row"):
            yield Label("Max Budget (USD):")
            yield Input(placeholder="5.0", id="cfg-budget")
        with Horizontal(id="settings-buttons"):
            yield Button("Save", variant="primary", id="btn-save-cfg")
            yield Button("Close", variant="default", id="btn-close-cfg")

    def load_from_config(self) -> None:
        """Populate fields from the current runtime config."""
        from autoemu.agent.runtime import AgentRuntimeConfig
        cfg = AgentRuntimeConfig.load()

        self.query_one("#cfg-backend", Select).value = cfg.backend
        self.query_one("#cfg-model", Input).value = cfg.model or ""
        self.query_one("#cfg-openai-key", Input).value = cfg.openai_api_key
        self.query_one("#cfg-openai-base", Input).value = cfg.openai_base_url
        self.query_one("#cfg-anthropic-key", Input).value = cfg.anthropic_api_key
        self.query_one("#cfg-anthropic-base", Input).value = cfg.anthropic_base_url
        self.query_one("#cfg-budget", Input).value = str(cfg.max_budget_usd)

    def save_to_file(self) -> None:
        """Write current field values to .autoemu.toml."""
        from pathlib import Path

        backend = self.query_one("#cfg-backend", Select).value
        model = self.query_one("#cfg-model", Input).value.strip()
        openai_key = self.query_one("#cfg-openai-key", Input).value.strip()
        openai_base = self.query_one("#cfg-openai-base", Input).value.strip()
        anthropic_key = self.query_one("#cfg-anthropic-key", Input).value.strip()
        anthropic_base = self.query_one("#cfg-anthropic-base", Input).value.strip()
        budget = self.query_one("#cfg-budget", Input).value.strip()

        lines = ["# AutoEmu configuration (managed by TUI)\n", "\n", "[agent]\n"]
        lines.append(f'backend = "{backend}"\n')
        if model:
            lines.append(f'model = "{model}"\n')
        if budget:
            lines.append(f"max_budget_usd = {budget}\n")
        if openai_key:
            lines.append(f'openai_api_key = "{openai_key}"\n')
        if openai_base:
            lines.append(f'openai_base_url = "{openai_base}"\n')
        if anthropic_key:
            lines.append(f'anthropic_api_key = "{anthropic_key}"\n')
        if anthropic_base:
            lines.append(f'anthropic_base_url = "{anthropic_base}"\n')

        Path(".autoemu.toml").write_text("".join(lines), encoding="utf-8")


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
        padding: 0 0 0 0;
        width: 1fr;
    }

    #backend-bar {
        height: auto;
        padding: 0 4 1 4;
        align: center middle;
    }

    #backend-label {
        text-align: center;
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

    #settings-bar {
        height: auto;
        padding: 0 4;
        align: center middle;
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
        Binding("ctrl+s", "toggle_settings", "Settings", show=True),
        Binding("ctrl+l", "save_log", "Save log", show=True),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(BANNER, id="banner")
        yield Static("Automated QEMU peripheral model generation for any MCU", id="subtitle")

        with Container(id="backend-bar"):
            yield Static("", id="backend-label")

        yield SettingsScreen(id="settings-panel")

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
            with Horizontal(classes="form-row"):
                yield Label("Target CVE (optional):")
                yield Input(
                    placeholder="e.g. CVE-2021-12345",
                    id="input-cve",
                )

        with Container(id="action-bar"):
            yield Button("Run Pipeline  [dim](Ctrl+R)[/]", variant="primary", id="btn-run")
        with Container(id="settings-bar"):
            yield Button("Settings  [dim](Ctrl+S)[/]", variant="success", id="btn-settings")

        with Container(id="pipeline-area"):
            yield PipelinePhaseList(id="phase-list")

        with Container(id="log-area"):
            yield LogPanel(id="log")

        yield Footer()

    def on_mount(self) -> None:
        self._refresh_backend_label()
        log = self.query_one("#log", LogPanel)
        log.log_info(
            "Enter a [bold]target board[/] and [bold]peripheral[/], "
            "then press [bold]Run Pipeline[/]."
        )

    def _refresh_backend_label(self) -> None:
        from autoemu.agent.runtime import AgentRuntimeConfig
        cfg = AgentRuntimeConfig.load()
        backend = cfg.backend
        model = cfg.model or "default"
        if backend == "claude-sdk":
            base = f" @ {cfg.anthropic_base_url}" if cfg.anthropic_base_url else ""
            text = f"[dim]Backend:[/] [bold cyan]Claude SDK[/] model={model}{base}"
        elif backend == "codex-sdk":
            text = f"[dim]Backend:[/] [bold blue]Codex SDK[/] model={model}"
        elif backend == "anthropic-api":
            base = f" @ {cfg.anthropic_base_url}" if cfg.anthropic_base_url else ""
            text = f"[dim]Backend:[/] [bold magenta]Anthropic API[/] model={model}{base}"
        elif backend == "openai-api":
            base = f" @ {cfg.openai_base_url}" if cfg.openai_base_url else ""
            text = f"[dim]Backend:[/] [bold green]OpenAI API[/] model={model}{base}"
        else:
            text = f"[dim]Backend:[/] {backend}"
        self.query_one("#backend-label", Static).update(text)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-run":
            self.action_run_pipeline()
        elif event.button.id == "btn-settings":
            self.action_toggle_settings()
        elif event.button.id == "btn-save-cfg":
            self._save_settings()
        elif event.button.id == "btn-close-cfg":
            self.action_toggle_settings()

    def action_save_log(self) -> None:
        log = self.query_one("#log", LogPanel)
        try:
            path = log.save_to_file()
            log.log_success(f"Log saved to [bold]{path}[/]")
        except Exception as exc:
            log.log_error(f"Failed to save log: {exc}")

    def action_toggle_settings(self) -> None:
        panel = self.query_one("#settings-panel", SettingsScreen)
        if panel.has_class("visible"):
            panel.remove_class("visible")
        else:
            panel.load_from_config()
            panel.add_class("visible")

    def _save_settings(self) -> None:
        panel = self.query_one("#settings-panel", SettingsScreen)
        log = self.query_one("#log", LogPanel)
        try:
            panel.save_to_file()
            log.log_success("Settings saved to .autoemu.toml")
            panel.remove_class("visible")
            self._refresh_backend_label()
        except Exception as exc:
            log.log_error(f"Failed to save settings: {exc}")

    def action_run_pipeline(self) -> None:
        mcu = self.query_one("#input-mcu", Input).value.strip()
        periph = self.query_one("#input-peripheral", Input).value.strip()
        cve = self.query_one("#input-cve", Input).value.strip()
        log = self.query_one("#log", LogPanel)

        if not mcu or not periph:
            log.log_error("Please fill in both [bold]Target Board[/] and [bold]Target Peripheral[/].")
            return

        btn = self.query_one("#btn-run", Button)
        btn.disabled = True
        self._run_pipeline_worker(mcu, periph, cve)

    @work(thread=True)
    def _run_pipeline_worker(self, mcu: str, periph: str, cve_id: str = "") -> None:
        from autoemu.agent.runtime import AutoEmuAgentRuntime, PipelineProgress

        log = self.query_one("#log", LogPanel)
        phases = self.query_one("#phase-list", PipelinePhaseList)

        self.app.call_from_thread(phases.reset_phases)
        self.app.call_from_thread(log.clear)
        self.app.call_from_thread(log.clear_log_buffer)

        self.app.call_from_thread(log.log_info, f"Starting pipeline for [bold]{mcu}[/] / [bold]{periph}[/] ...")
        if cve_id:
            self.app.call_from_thread(log.log_info, f"CVE target: [bold]{cve_id}[/]")

        def on_progress(p: PipelineProgress) -> None:
            _schedule_progress_update(self.app, phases, log, p)

        try:
            runtime = AutoEmuAgentRuntime()
            result = runtime.run_pipeline(
                target_mcu=mcu,
                target_peripheral=periph,
                cve_id=cve_id,
                on_progress=on_progress,
            )

            for method_name, phase in _final_phase_status_actions(result):
                self.app.call_from_thread(getattr(phases, method_name), phase)

            if result.success:
                self.app.call_from_thread(log.log_success, "Pipeline completed!")
            else:
                self.app.call_from_thread(log.log_error, f"Pipeline failed: {result.error}")

            if result.platform:
                self.app.call_from_thread(log.log_info, f"Platform: [bold]{result.platform}[/]")

            if result.generated_files:
                self.app.call_from_thread(log.log_info, f"Generated [bold]{len(result.generated_files)}[/] file(s):")
                for f in result.generated_files[:20]:
                    self.app.call_from_thread(log.write, f"  [cyan]{f}[/]")
                if len(result.generated_files) > 20:
                    self.app.call_from_thread(log.write, f"  ... and {len(result.generated_files) - 20} more")

            if result.test_commands:
                self.app.call_from_thread(log.log_info, "Test and validation commands:")
                for cmd in result.test_commands:
                    if cmd.startswith("# "):
                        self.app.call_from_thread(log.write, f"  [dim]{cmd}[/]")
                    else:
                        self.app.call_from_thread(log.write, f"  [bold green]{cmd}[/]")

            val = result.validation_result
            if val:
                checked = val.get("files_checked", 0)
                errors = val.get("errors", [])
                warnings = val.get("warnings", [])
                skipped_qemu = any("QEMU source tree not found" in w for w in warnings)
                if val.get("success") and not warnings:
                    self.app.call_from_thread(log.log_success, f"Validation: {checked} file(s) checked, no errors")
                elif val.get("success") and skipped_qemu:
                    self.app.call_from_thread(log.log_kind, "Validation: compilation skipped (no QEMU source tree)", "warn")
                elif val.get("success") and warnings:
                    self.app.call_from_thread(
                        log.log_kind,
                        f"Validation: {checked} file(s) checked, {len(warnings)} warning(s)",
                        "warn",
                    )
                elif errors:
                    self.app.call_from_thread(log.log_error, f"Validation: {len(errors)} error(s) in {checked} file(s)")
                    for err in errors[:10]:
                        self.app.call_from_thread(
                            log.write,
                            f"  [red]{err.get('file', '?')}: {err.get('stderr', '')[:200]}[/]",
                        )
                else:
                    self.app.call_from_thread(log.log_error, f"Validation: {checked} file(s) checked, blocking warning(s)")

        except Exception as exc:
            import traceback
            self.app.call_from_thread(log.log_error, f"Unexpected error: {exc}")
            self.app.call_from_thread(log.write, f"[red]{traceback.format_exc()}[/]")

        finally:
            # Auto-save log after every run
            try:
                path = log.save_to_file()
                self.app.call_from_thread(log.write, f"[dim]Log saved -> {path}[/]")
            except Exception:
                pass

            btn = self.query_one("#btn-run", Button)
            self.app.call_from_thread(setattr, btn, "disabled", False)
