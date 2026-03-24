"""Backend-agnostic tool definitions for the AutoEmu modeling agent.

Tools are defined as :class:`ToolSpec` instances so they can be converted
to any backend's native tool format by the selected backend implementation.
"""

from __future__ import annotations

import json
import traceback
from pathlib import Path
from typing import Any

from autoemu.agent.backend import ToolSpec
from autoemu.parsers.svd_parser import parse_svd_file, parse_svd_string
from autoemu.parsers.header_parser import parse_header_file
from autoemu.parsers.driver_parser import (
    analyze_driver_file,
    analyze_driver_string,
    DriverAnalysis,
)
from autoemu.models import (
    RegisterBlock,
    Peripheral,
    PeripheralType,
    StateMachine,
    State,
    Transition,
    InterruptModel,
    InterruptLine,
    DependencyGraph,
    DependencyEdge,
    DependencyType,
)


# ------------------------------------------------------------------ helpers

def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"Error: {text}"}], "is_error": True}


def _format_driver_analysis(analysis: DriverAnalysis) -> str:
    sections = [f"# Driver Analysis: {analysis.peripheral_name}\n"]

    if analysis.register_accesses:
        sections.append(f"## Register Accesses ({len(analysis.register_accesses)} total)")
        by_context: dict[str, list] = {}
        for a in analysis.register_accesses:
            by_context.setdefault(a.context, []).append(a)
        for ctx, accesses in by_context.items():
            sections.append(f"\n### Context: {ctx}")
            for a in accesses[:50]:
                sections.append(
                    f"  {a.access_type:12s} {a.register:12s} "
                    f"{a.field:20s} in {a.in_function}"
                )

    if analysis.isr_patterns:
        sections.append(f"\n## ISR Patterns ({len(analysis.isr_patterns)})")
        for isr in analysis.isr_patterns:
            sections.append(f"\n### {isr.function_name}")
            sections.append(f"  Checked flags:  {', '.join(isr.checked_flags)}")
            sections.append(f"  Cleared flags:  {', '.join(isr.cleared_flags)}")
            sections.append(f"  Enable checks:  {', '.join(isr.enabled_checks)}")
            sections.append(f"  Callbacks:      {', '.join(isr.callbacks)}")

    if analysis.init_sequences:
        sections.append(f"\n## Init Sequences ({len(analysis.init_sequences)})")
        for init in analysis.init_sequences:
            sections.append(f"\n### {init.function_name}")
            for a in init.accesses[:30]:
                sections.append(f"  {a.access_type:12s} {a.register}")

    if analysis.dma_configs:
        sections.append(f"\n## DMA Configurations ({len(analysis.dma_configs)})")
        for dma in analysis.dma_configs:
            sections.append(
                f"  Direction: {dma.direction}, Channel: {dma.channel}, "
                f"Circular: {dma.circular}"
            )

    return "\n".join(sections)


# ------------------------------------------------------------------ handlers

async def _parse_svd(args: dict[str, Any]) -> dict[str, Any]:
    try:
        blocks = parse_svd_file(args["file_path"])
        result = {name: blk.model_dump() for name, blk in blocks.items()}
        return _ok(json.dumps(result, indent=2))
    except Exception as e:
        return _err(f"SVD parse failed: {e}")


async def _parse_svd_text(args: dict[str, Any]) -> dict[str, Any]:
    try:
        blocks = parse_svd_string(args["xml_content"])
        result = {name: blk.model_dump() for name, blk in blocks.items()}
        return _ok(json.dumps(result, indent=2))
    except Exception as e:
        return _err(f"SVD text parse failed: {e}")


async def _parse_header(args: dict[str, Any]) -> dict[str, Any]:
    try:
        periph = args.get("peripheral_name") or None
        blocks = parse_header_file(args["file_path"], periph)
        result = {name: blk.model_dump() for name, blk in blocks.items()}
        return _ok(json.dumps(result, indent=2))
    except Exception as e:
        return _err(f"Header parse failed: {e}")


async def _analyze_driver(args: dict[str, Any]) -> dict[str, Any]:
    try:
        analysis = analyze_driver_file(
            args["file_path"],
            args.get("peripheral_name", ""),
        )
        return _ok(_format_driver_analysis(analysis))
    except Exception as e:
        return _err(f"Driver analysis failed: {e}")


async def _analyze_driver_text(args: dict[str, Any]) -> dict[str, Any]:
    try:
        analysis = analyze_driver_string(
            args["source_code"],
            args.get("peripheral_name", ""),
        )
        return _ok(_format_driver_analysis(analysis))
    except Exception as e:
        return _err(f"Driver text analysis failed: {e}")


async def _build_peripheral_model(args: dict[str, Any]) -> dict[str, Any]:
    try:
        reg_block = RegisterBlock.model_validate_json(args["register_block_json"])
        ptype = PeripheralType(args.get("peripheral_type", "generic"))
        base_addr = int(args.get("base_address", "0"), 0)

        peripheral = Peripheral(
            name=args["name"],
            peripheral_type=ptype,
            base_address=base_addr,
            register_block=reg_block,
            mcu_family=args.get("mcu_family", ""),
        )
        return _ok(peripheral.model_dump_json(indent=2))
    except Exception as e:
        return _err(f"Model build failed: {e}\n{traceback.format_exc()}")


async def _build_state_machine(args: dict[str, Any]) -> dict[str, Any]:
    try:
        states_data = json.loads(args["states_json"])
        trans_data = json.loads(args["transitions_json"])

        states = [State.model_validate(s) for s in states_data]
        transitions = [Transition.model_validate(t) for t in trans_data]

        sm = StateMachine(
            name=args["name"],
            states=states,
            transitions=transitions,
        )
        result = {
            "model": sm.model_dump(),
            "dot": sm.to_dot(),
            "reachable_states": list(sm.get_reachable_states()),
        }
        return _ok(json.dumps(result, indent=2))
    except Exception as e:
        return _err(f"State machine build failed: {e}")


async def _build_interrupt_model(args: dict[str, Any]) -> dict[str, Any]:
    try:
        lines_data = json.loads(args["lines_json"])
        event_map = json.loads(args.get("event_map_json", "{}"))

        lines = [InterruptLine.model_validate(l) for l in lines_data]

        model = InterruptModel(
            peripheral_name=args["peripheral_name"],
            lines=lines,
            event_sources=list(event_map.keys()),
            flag_to_event_map=event_map,
        )
        return _ok(model.model_dump_json(indent=2))
    except Exception as e:
        return _err(f"Interrupt model build failed: {e}")


async def _build_dependency_graph(args: dict[str, Any]) -> dict[str, Any]:
    try:
        edges_data = json.loads(args["edges_json"])
        edges = [DependencyEdge.model_validate(e) for e in edges_data]

        graph = DependencyGraph(mcu_name=args["mcu_name"], edges=edges)
        result = {
            "model": graph.model_dump(),
            "dot": graph.to_dot(),
            "peripherals": list(graph.get_all_peripherals()),
            "topological_order": graph.topological_order(),
        }
        return _ok(json.dumps(result, indent=2))
    except Exception as e:
        return _err(f"Dependency graph build failed: {e}")


async def _generate_qemu_peripheral(args: dict[str, Any]) -> dict[str, Any]:
    try:
        from autoemu.generators.qemu_generator import generate_peripheral_code

        peripheral = Peripheral.model_validate_json(args["peripheral_json"])
        output_dir = Path(args.get("output_dir", "output"))
        output_dir.mkdir(parents=True, exist_ok=True)

        files = generate_peripheral_code(peripheral, output_dir)
        return _ok(
            f"Generated {len(files)} files:\n" +
            "\n".join(f"  - {f}" for f in files)
        )
    except Exception as e:
        return _err(f"QEMU generation failed: {e}\n{traceback.format_exc()}")


async def _generate_test_harness(args: dict[str, Any]) -> dict[str, Any]:
    try:
        from autoemu.generators.test_generator import generate_test_harness

        peripheral = Peripheral.model_validate_json(args["peripheral_json"])
        output_dir = Path(args.get("output_dir", "output"))
        output_dir.mkdir(parents=True, exist_ok=True)

        files = generate_test_harness(peripheral, output_dir)
        return _ok(
            f"Generated test harness ({len(files)} files):\n" +
            "\n".join(f"  - {f}" for f in files)
        )
    except Exception as e:
        return _err(f"Test generation failed: {e}")


async def _validate_register_model(args: dict[str, Any]) -> dict[str, Any]:
    try:
        from autoemu.validators.register_validator import validate_register_block

        block = RegisterBlock.model_validate_json(args["register_block_json"])
        issues = validate_register_block(block)

        if not issues:
            return _ok("Register model validation passed. No issues found.")
        return _ok(
            f"Found {len(issues)} issues:\n" +
            "\n".join(f"  [{i['severity']}] {i['message']}" for i in issues)
        )
    except Exception as e:
        return _err(f"Validation failed: {e}")


async def _validate_behavior(args: dict[str, Any]) -> dict[str, Any]:
    try:
        from autoemu.validators.behavior_validator import validate_behavior

        peripheral = Peripheral.model_validate_json(args["peripheral_json"])
        analysis_data = json.loads(args["driver_analysis_json"])
        issues = validate_behavior(peripheral, analysis_data)

        if not issues:
            return _ok("Behavior validation passed.")
        return _ok(
            f"Found {len(issues)} behavioral issues:\n" +
            "\n".join(f"  [{i['severity']}] {i['message']}" for i in issues)
        )
    except Exception as e:
        return _err(f"Behavior validation failed: {e}")


async def _read_file(args: dict[str, Any]) -> dict[str, Any]:
    try:
        path = Path(args["file_path"])
        if not path.exists():
            return _err(f"File not found: {path}")
        if path.stat().st_size > 5 * 1024 * 1024:
            return _err(f"File too large: {path.stat().st_size} bytes")
        content = path.read_text(encoding="utf-8", errors="replace")
        return _ok(content)
    except Exception as e:
        return _err(f"File read failed: {e}")


async def _list_files(args: dict[str, Any]) -> dict[str, Any]:
    try:
        d = Path(args["directory"])
        if not d.exists():
            return _err(f"Directory not found: {d}")
        pattern = args.get("pattern", "*")
        files = sorted(d.glob(pattern))
        return _ok("\n".join(str(f) for f in files[:200]))
    except Exception as e:
        return _err(f"List files failed: {e}")


async def _write_file(args: dict[str, Any]) -> dict[str, Any]:
    try:
        path = Path(args["file_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(args["content"], encoding="utf-8")
        return _ok(f"Written {len(args['content'])} bytes to {path}")
    except Exception as e:
        return _err(f"File write failed: {e}")


# ------------------------------------------------------------------ registry

ALL_TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="parse_svd",
        description=(
            "Parse an SVD file to extract register maps for all peripherals. "
            "Returns JSON with peripheral names as keys and register block data as values."
        ),
        parameters={"file_path": str},
        handler=_parse_svd,
    ),
    ToolSpec(
        name="parse_svd_text",
        description=(
            "Parse SVD XML content from a text string. "
            "Returns JSON with peripheral register blocks."
        ),
        parameters={"xml_content": str},
        handler=_parse_svd_text,
    ),
    ToolSpec(
        name="parse_header",
        description=(
            "Parse a C header file (CMSIS/HAL) to extract register structures "
            "and bit definitions. Optionally filter by peripheral_name."
        ),
        parameters={"file_path": str, "peripheral_name": str},
        handler=_parse_header,
    ),
    ToolSpec(
        name="analyze_driver",
        description=(
            "Analyze a HAL/LL driver C source file for register access patterns, "
            "ISR logic, init sequences, and DMA configurations."
        ),
        parameters={"file_path": str, "peripheral_name": str},
        handler=_analyze_driver,
    ),
    ToolSpec(
        name="analyze_driver_text",
        description="Analyze HAL/LL driver source code from a text string.",
        parameters={"source_code": str, "peripheral_name": str},
        handler=_analyze_driver_text,
    ),
    ToolSpec(
        name="build_peripheral_model",
        description=(
            "Build a complete peripheral model from register block JSON data. "
            "Returns the peripheral model as JSON."
        ),
        parameters={
            "name": str, "peripheral_type": str, "register_block_json": str,
            "base_address": str, "mcu_family": str,
        },
        handler=_build_peripheral_model,
    ),
    ToolSpec(
        name="build_state_machine",
        description=(
            "Build a state machine model from a JSON description of states "
            "and transitions."
        ),
        parameters={"name": str, "states_json": str, "transitions_json": str},
        handler=_build_state_machine,
    ),
    ToolSpec(
        name="build_interrupt_model",
        description=(
            "Build an interrupt model from JSON description of IRQ lines, "
            "flags, and events."
        ),
        parameters={"peripheral_name": str, "lines_json": str, "event_map_json": str},
        handler=_build_interrupt_model,
    ),
    ToolSpec(
        name="build_dependency_graph",
        description=(
            "Build a cross-peripheral dependency graph from JSON edge descriptions."
        ),
        parameters={"mcu_name": str, "edges_json": str},
        handler=_build_dependency_graph,
    ),
    ToolSpec(
        name="generate_qemu_peripheral",
        description=(
            "Generate QEMU-compatible C source code for a peripheral model. "
            "Input is the peripheral model JSON."
        ),
        parameters={"peripheral_json": str, "output_dir": str},
        handler=_generate_qemu_peripheral,
    ),
    ToolSpec(
        name="generate_test_harness",
        description=(
            "Generate a test harness for validating a peripheral model "
            "against driver behavior."
        ),
        parameters={"peripheral_json": str, "driver_analysis_json": str, "output_dir": str},
        handler=_generate_test_harness,
    ),
    ToolSpec(
        name="validate_register_model",
        description=(
            "Validate a register model for consistency: overlapping fields, "
            "missing reset values, access type conflicts, etc."
        ),
        parameters={"register_block_json": str},
        handler=_validate_register_model,
    ),
    ToolSpec(
        name="validate_behavior",
        description=(
            "Validate peripheral model behavior against driver access patterns. "
            "Checks that register writes produce expected state changes."
        ),
        parameters={"peripheral_json": str, "driver_analysis_json": str},
        handler=_validate_behavior,
    ),
    ToolSpec(
        name="read_file",
        description="Read a file's contents. Useful for reading SVD files, headers, or driver source.",
        parameters={"file_path": str},
        handler=_read_file,
    ),
    ToolSpec(
        name="list_files",
        description="List files matching a glob pattern in a directory.",
        parameters={"directory": str, "pattern": str},
        handler=_list_files,
    ),
    ToolSpec(
        name="write_file",
        description="Write content to a file, creating parent directories if needed.",
        parameters={"file_path": str, "content": str},
        handler=_write_file,
    ),
]

TOOL_NAMES: list[str] = [t.name for t in ALL_TOOLS]
