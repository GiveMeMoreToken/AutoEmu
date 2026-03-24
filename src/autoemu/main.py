"""CLI entry point for AutoEmu."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

from autoemu.agent.orchestrator import AutoEmuOrchestrator, ModelingTask, ModelingResult


console = Console()


def _run_async(coro):
    """Run an async coroutine from sync context."""
    return asyncio.run(coro)


@click.group()
@click.version_option(version="0.1.0")
def cli():
    """AutoEmu - LLM Agent-driven automated embedded peripheral modeling."""
    pass


@cli.command()
@click.argument("peripheral", type=str)
@click.option("--mcu", default="STM32F4", help="MCU family (e.g., STM32F4, STM32H7, STM32WL)")
@click.option("--svd", type=click.Path(exists=True), help="Path to SVD file")
@click.option("--header", type=click.Path(exists=True), help="Path to CMSIS header file")
@click.option("--driver", type=click.Path(exists=True), multiple=True, help="Path to driver source file(s)")
@click.option("--output", "-o", default="output", help="Output directory")
@click.option("--model", default=None, help="Claude model to use")
@click.option("--budget", default=5.0, help="Max budget in USD")
@click.option("--phases", default="extract,analyze,infer,connect,generate,validate",
              help="Comma-separated phases to run")
@click.option("--verbose", "-v", is_flag=True, help="Verbose output")
@click.option("--backend", "-b", default="claude", type=click.Choice(["claude", "openai"]),
              help="Agent backend to use")
def model(peripheral, mcu, svd, header, driver, output, model, budget, phases, verbose, backend):
    """Model a peripheral using the LLM agent pipeline.

    PERIPHERAL is the name of the peripheral to model (e.g., ETH, USB_OTG_FS, DMA1).
    """
    task = ModelingTask(
        peripheral_name=peripheral,
        mcu_family=mcu,
        svd_path=svd or "",
        header_path=header or "",
        driver_paths=list(driver),
        output_dir=output,
        phases=phases.split(","),
    )

    console.print(Panel(
        f"[bold]AutoEmu Peripheral Modeling[/bold]\n"
        f"Peripheral: {peripheral}\n"
        f"MCU Family: {mcu}\n"
        f"Backend: {backend}\n"
        f"Phases: {', '.join(task.phases)}\n"
        f"Output: {output}",
        title="Configuration",
    ))

    orchestrator = AutoEmuOrchestrator(
        backend=backend,
        model=model,
        max_budget_usd=budget,
        verbose=verbose,
    )

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        progress_task = progress.add_task("Modeling...", total=None)

        def on_message(phase: str, text: str):
            progress.update(progress_task, description=f"[{phase}] {text[:80]}")
            if verbose:
                console.print(f"  [{phase}] {text}")

        result = _run_async(orchestrator.model_peripheral(task, on_message=on_message))

    _print_result(result)


@cli.command()
@click.argument("peripheral", type=str)
@click.option("--mcu", default="STM32F4", help="MCU family")
@click.option("--output", "-o", default="output", help="Output directory")
def builtin(peripheral, mcu, output):
    """Generate a peripheral model from built-in templates.

    Uses pre-defined domain knowledge (no LLM needed).
    Supported: DMA1, DMA2, ETH, USB_OTG_FS, USB_OTG_HS, SUBGHZ
    """
    from autoemu.peripherals.dma import build_dma_peripheral
    from autoemu.peripherals.eth import build_eth_peripheral
    from autoemu.peripherals.usb import build_usb_peripheral
    from autoemu.peripherals.radio import build_radio_peripheral
    from autoemu.generators.qemu_generator import generate_peripheral_code

    builders = {
        "DMA1": lambda: build_dma_peripheral("DMA1", 0x40026000, 8, mcu),
        "DMA2": lambda: build_dma_peripheral("DMA2", 0x40026400, 8, mcu),
        "ETH": lambda: build_eth_peripheral(0x40028000, mcu),
        "USB_OTG_FS": lambda: build_usb_peripheral("FS", 0x50000000, mcu),
        "USB_OTG_HS": lambda: build_usb_peripheral("HS", 0x40040000, mcu),
        "SUBGHZ": lambda: build_radio_peripheral(0x58010000, "STM32WL"),
    }

    if peripheral not in builders:
        console.print(f"[red]Unknown built-in peripheral: {peripheral}[/red]")
        console.print(f"Available: {', '.join(builders.keys())}")
        sys.exit(1)

    console.print(f"Building {peripheral} from built-in template...")
    periph = builders[peripheral]()

    output_dir = Path(output)
    files = generate_peripheral_code(periph, output_dir)

    console.print(f"[green]Generated {len(files)} files:[/green]")
    for f in files:
        console.print(f"  {f}")


@cli.command()
@click.argument("svd_file", type=click.Path(exists=True))
@click.option("--peripheral", "-p", default=None, help="Filter by peripheral name")
@click.option("--json-output", "-j", is_flag=True, help="Output as JSON")
def parse_svd(svd_file, peripheral, json_output):
    """Parse an SVD file and display register maps."""
    from autoemu.parsers.svd_parser import parse_svd_file

    blocks = parse_svd_file(svd_file)

    if peripheral:
        blocks = {k: v for k, v in blocks.items() if k == peripheral}

    if json_output:
        data = {name: blk.model_dump() for name, blk in blocks.items()}
        click.echo(json.dumps(data, indent=2))
    else:
        for name, block in blocks.items():
            console.print(f"\n[bold]{name}[/bold] @ 0x{block.base_address:08X}")
            for reg in block.registers:
                console.print(f"  0x{reg.offset:03X}  {reg.name:20s}  "
                             f"reset=0x{reg.reset_value:08X}  [{reg.access.value}]")
                for f in reg.fields:
                    console.print(f"         [{f.bit_offset}:{f.bit_offset+f.bit_width-1}] "
                                 f"{f.name:16s} [{f.access.value}]")


@cli.command()
@click.argument("driver_file", type=click.Path(exists=True))
@click.option("--peripheral", "-p", default="", help="Peripheral name hint")
def analyze(driver_file, peripheral):
    """Analyze a HAL/LL driver source file."""
    from autoemu.parsers.driver_parser import analyze_driver_file

    analysis = analyze_driver_file(driver_file, peripheral)

    console.print(f"\n[bold]Driver Analysis: {analysis.peripheral_name}[/bold]")
    console.print(f"Source: {analysis.source_file}")
    console.print(f"Register accesses: {len(analysis.register_accesses)}")
    console.print(f"ISR patterns: {len(analysis.isr_patterns)}")
    console.print(f"Init sequences: {len(analysis.init_sequences)}")
    console.print(f"DMA configurations: {len(analysis.dma_configs)}")

    for isr in analysis.isr_patterns:
        console.print(f"\n  [bold]ISR: {isr.function_name}[/bold]")
        console.print(f"    Checked: {', '.join(isr.checked_flags)}")
        console.print(f"    Cleared: {', '.join(isr.cleared_flags)}")
        console.print(f"    Callbacks: {', '.join(isr.callbacks)}")


@cli.command()
@click.argument("peripheral_json", type=click.Path(exists=True))
def validate(peripheral_json):
    """Validate a peripheral model JSON file."""
    from autoemu.models.peripheral import Peripheral
    from autoemu.validators.register_validator import validate_register_block

    data = json.loads(Path(peripheral_json).read_text())
    peripheral = Peripheral.model_validate(data)

    issues = validate_register_block(peripheral.register_block)

    if not issues:
        console.print("[green]Validation passed. No issues found.[/green]")
    else:
        console.print(f"[yellow]Found {len(issues)} issues:[/yellow]")
        for issue in issues:
            color = {"error": "red", "warning": "yellow", "info": "blue"}[issue["severity"]]
            console.print(f"  [{color}][{issue['severity']}][/{color}] {issue['message']}")


@cli.command()
@click.argument("prompt", type=str)
@click.option("--model", default=None, help="Model to use")
@click.option("--budget", default=2.0, help="Max budget in USD")
@click.option("--backend", "-b", default="claude", type=click.Choice(["claude", "openai"]),
              help="Agent backend to use")
def query(prompt, model, budget, backend):
    """Send a free-form query to the AutoEmu agent."""
    orchestrator = AutoEmuOrchestrator(backend=backend, model=model, max_budget_usd=budget)
    result = _run_async(orchestrator.single_query(prompt))
    console.print(result)


@cli.command()
@click.option("--mcu", default="STM32F4", help="MCU family")
@click.option("--output", "-o", default="output", help="Output directory")
@click.option("--budget", default=10.0, help="Max budget in USD")
@click.option("--backend", "-b", default="claude", type=click.Choice(["claude", "openai"]),
              help="Agent backend to use")
def batch(mcu, output, budget, backend):
    """Model all supported peripherals in batch mode."""
    tasks = [
        ModelingTask(
            peripheral_name=name,
            mcu_family=mcu,
            output_dir=f"{output}/{name.lower()}",
        )
        for name in ["DMA1", "ETH", "USB_OTG_FS", "SUBGHZ"]
    ]

    orchestrator = AutoEmuOrchestrator(backend=backend, max_budget_usd=budget)

    console.print(f"Batch modeling {len(tasks)} peripherals...")
    results = _run_async(orchestrator.batch_model(tasks))

    for result in results:
        _print_result(result)


def _print_result(result: ModelingResult):
    """Print a modeling result."""
    status = "[green]SUCCESS[/green]" if result.success else "[red]FAILED[/red]"
    console.print(f"\n{status} {result.peripheral_name}")
    console.print(f"  Phases: {', '.join(result.phases_completed)}")
    console.print(f"  Cost: ${result.total_cost_usd:.4f}")

    if result.generated_files:
        console.print(f"  Generated files:")
        for f in result.generated_files:
            console.print(f"    {f}")

    if result.error:
        console.print(f"  [red]Error: {result.error}[/red]")

    if result.validation_issues:
        console.print(f"  Validation issues: {len(result.validation_issues)}")


if __name__ == "__main__":
    cli()
