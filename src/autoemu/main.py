"""CLI entry point for AutoEmu — launches the interactive TUI."""

from __future__ import annotations

import click

from autoemu import __version__


@click.command()
@click.version_option(version=__version__)
def cli():
    """AutoEmu — automated QEMU peripheral model generation for any MCU."""
    from autoemu.tui import AutoEmuApp

    app = AutoEmuApp()
    app.run()


if __name__ == "__main__":
    cli()
