"""CLI entry point for AutoEmu — launches the interactive TUI or runs headless."""

from __future__ import annotations

import click

from autoemu import __version__


@click.command()
@click.version_option(version=__version__)
@click.option(
    "--mcu",
    help="Target MCU or board (e.g. STM32F407VG). Runs headless when combined with --peripheral.",
)
@click.option(
    "--peripheral",
    help="Target peripheral (e.g. ETH). Runs headless when combined with --mcu.",
)
@click.option(
    "--cve-id",
    help="Optional target CVE (e.g. CVE-2021-12345).",
)
def cli(mcu: str | None, peripheral: str | None, cve_id: str | None) -> None:
    """AutoEmu — automated QEMU peripheral model generation for any MCU."""
    if mcu and peripheral:
        _run_headless(mcu, peripheral, cve_id=cve_id or "")
        return
    if mcu or peripheral:
        raise click.UsageError("Both --mcu and --peripheral are required for headless mode.")

    from autoemu.tui import AutoEmuApp
    app = AutoEmuApp()
    app.run()


def _run_headless(mcu: str, peripheral: str, *, cve_id: str = "") -> None:
    from autoemu.agent.runtime import AutoEmuAgentRuntime, PipelineProgress

    runtime = AutoEmuAgentRuntime()

    def on_progress(p: PipelineProgress) -> None:
        if p.error:
            click.echo(f"ERROR: {p.error}", err=True)
            return
        if p.detail:
            prefix = f"[{p.phase_name}]" if p.phase_name else "[autoemu]"
            click.echo(f"{prefix} {p.detail}")

    result = runtime.run_pipeline(
        target_mcu=mcu,
        target_peripheral=peripheral,
        cve_id=cve_id,
        on_progress=on_progress,
    )

    click.echo()
    if result.success:
        click.echo(click.style("Pipeline completed successfully!", fg="green", bold=True))
    else:
        click.echo(click.style(f"Pipeline failed: {result.error}", fg="red", bold=True))

    if result.platform:
        click.echo(f"Platform: {result.platform}")
    if result.generated_files:
        click.echo(f"Generated {len(result.generated_files)} file(s):")
        for f in result.generated_files:
            click.echo(f"  {f}")

    if result.cve_findings:
        click.echo()
        click.echo(click.style("CVE findings:", bold=True))
        details = result.cve_findings.get("details", {})
        if details.get("severity"):
            click.echo(f"  Severity: {details['severity']}")
        poc = result.cve_findings.get("poc_findings", [])
        if poc:
            click.echo(f"  PoC / exploit references ({len(poc)} found):")
            for item in poc[:10]:
                click.echo(f"    [{item.get('category', '?')}] {item.get('title', '')}")
                click.echo(f"      {item.get('url', '')}")
        warnings = result.cve_findings.get("warnings", [])
        for w in warnings:
            click.echo(click.style(f"  Warning: {w}", fg="yellow"))

    if result.test_commands:
        click.echo()
        click.echo(click.style("Test and validation commands:", bold=True))
        for cmd in result.test_commands:
            if cmd.startswith("# "):
                click.echo(f"  {cmd}")
            else:
                click.echo(click.style(f"  {cmd}", fg="green"))


if __name__ == "__main__":
    cli()
