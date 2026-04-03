"""CLI entry point for AutoEmu."""

from __future__ import annotations

import json

import click

from autoemu import __version__
from autoemu.agent.runtime import AutoEmuAgentRuntime


@click.group()
@click.version_option(version=__version__)
def cli():
    """AutoEmu agent runtime for STM32 data fetching and QEMU generation."""


@cli.command(name="fetch-data")
@click.option("--target-mcu", required=True, help="Target MCU or board, e.g. STM32F407VG")
@click.option("--target-peripheral", required=True, help="Target peripheral, e.g. ETH, USB, DMA")
@click.option("--output", "-o", default="data/stm32", help="Output directory")
@click.option("--refresh", is_flag=True, help="Redownload files even if they already exist")
def fetch_data_cmd(target_mcu, target_peripheral, output, refresh):
    """Fetch STM32 input data for one MCU/peripheral target."""
    result = _run_cli_action(
        lambda: AutoEmuAgentRuntime().fetch_data(
            target_mcu=target_mcu,
            target_peripheral=target_peripheral,
            output_dir=output,
            refresh=refresh,
        )
    )
    click.echo(json.dumps(result, indent=2))


@cli.command(name="build-qemu-peripheral")
@click.option("--target-mcu", required=True, help="Target MCU or board, e.g. STM32F407VG")
@click.option("--target-peripheral", required=True, help="Target peripheral, e.g. ETH, USB, DMA")
@click.option("--data-dir", default="data/stm32", help="Directory containing fetched data")
@click.option("--output-dir", "-o", default="output", help="Directory for generated artifacts")
def build_qemu_peripheral_cmd(target_mcu, target_peripheral, data_dir, output_dir):
    """Build a QEMU-ready peripheral model from fetched target data."""
    result = _run_cli_action(
        lambda: AutoEmuAgentRuntime().build_qemu_peripheral(
            target_mcu=target_mcu,
            target_peripheral=target_peripheral,
            data_dir=data_dir,
            output_dir=output_dir,
        )
    )
    click.echo(json.dumps(result, indent=2))


def _run_cli_action(action):
    try:
        return action()
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


if __name__ == "__main__":
    cli()
