"""MCP tools for the AutoEmu modeling agent.

These tools are registered as in-process MCP server tools via claude-agent-sdk,
giving the LLM agent direct access to parsing, analysis, generation, and
validation capabilities.
"""

from __future__ import annotations

import json
import traceback
from pathlib import Path
from typing import Any

from claude_agent_sdk import tool, SdkMcpTool

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


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"Error: {text}"}], "is_error": True}


# ---------- Parsing tools ----------

@tool(
    "parse_svd",
    "Parse an SVD file to extract register maps for all peripherals. "
    "Returns JSON with peripheral names as keys and register block data as values.",
    {"file_path": str},
)
async def parse_svd_tool(args: dict[str, Any]) -> dict[str, Any]:
    try:
        path = args["file_path"]
        blocks = parse_svd_file(path)
        result = {name: blk.model_dump() for name, blk in blocks.items()}
        return _ok(json.dumps(result, indent=2))
    except Exception as e:
        return _err(f"SVD parse failed: {e}")


@tool(
    "parse_svd_text",
    "Parse SVD XML content from a text string. "
    "Returns JSON with peripheral register blocks.",
    {"xml_content": str},
)
async def parse_svd_text_tool(args: dict[str, Any]) -> dict[str, Any]:
    try:
        blocks = parse_svd_string(args["xml_content"])
        result = {name: blk.model_dump() for name, blk in blocks.items()}
        return _ok(json.dumps(result, indent=2))
    except Exception as e:
        return _err(f"SVD text parse failed: {e}")


@tool(
    "parse_header",
    "Parse a C header file (CMSIS/HAL) to extract register structures and bit definitions. "
    "Optionally filter by peripheral_name.",
    {"file_path": str, "peripheral_name": str},
)
async def parse_header_tool(args: dict[str, Any]) -> dict[str, Any]:
    try:
        periph = args.get("peripheral_name") or None
        blocks = parse_header_file(args["file_path"], periph)
        result = {name: blk.model_dump() for name, blk in blocks.items()}
        return _ok(json.dumps(result, indent=2))
    except Exception as e:
        return _err(f"Header parse failed: {e}")


@tool(
    "analyze_driver",
    "Analyze a HAL/LL driver C source file for register access patterns, "
    "ISR logic, init sequences, and DMA configurations.",
    {"file_path": str, "peripheral_name": str},
)
async def analyze_driver_tool(args: dict[str, Any]) -> dict[str, Any]:
    try:
        analysis = analyze_driver_file(
            args["file_path"],
            args.get("peripheral_name", ""),
        )
        return _ok(_format_driver_analysis(analysis))
    except Exception as e:
        return _err(f"Driver analysis failed: {e}")


@tool(
    "analyze_driver_text",
    "Analyze HAL/LL driver source code from a text string.",
    {"source_code": str, "peripheral_name": str},
)
async def analyze_driver_text_tool(args: dict[str, Any]) -> dict[str, Any]:
    try:
        analysis = analyze_driver_string(
            args["source_code"],
            args.get("peripheral_name", ""),
        )
        return _ok(_format_driver_analysis(analysis))
    except Exception as e:
        return _err(f"Driver text analysis failed: {e}")


def _format_driver_analysis(analysis: DriverAnalysis) -> str:
    sections = [f"# Driver Analysis: {analysis.peripheral_name}\n"]

    if analysis.register_accesses:
        sections.append(f"## Register Accesses ({len(analysis.register_accesses)} total)")
        by_context: dict[str, list] = {}
        for a in analysis.register_accesses:
            by_context.setdefault(a.context, []).append(a)
        for ctx, accesses in by_context.items():
            sections.append(f"\n### Context: {ctx}")
            for a in accesses[:50]:  # Limit output
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


# ---------- Model building tools ----------

@tool(
    "build_peripheral_model",
    "Build a complete peripheral model from register block JSON data. "
    "Returns the peripheral model as JSON.",
    {"name": str, "peripheral_type": str, "register_block_json": str,
     "base_address": str, "mcu_family": str},
)
async def build_peripheral_model_tool(args: dict[str, Any]) -> dict[str, Any]:
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


@tool(
    "build_state_machine",
    "Build a state machine model from a JSON description of states and transitions.",
    {"name": str, "states_json": str, "transitions_json": str},
)
async def build_state_machine_tool(args: dict[str, Any]) -> dict[str, Any]:
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


@tool(
    "build_interrupt_model",
    "Build an interrupt model from JSON description of IRQ lines, flags, and events.",
    {"peripheral_name": str, "lines_json": str, "event_map_json": str},
)
async def build_interrupt_model_tool(args: dict[str, Any]) -> dict[str, Any]:
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


@tool(
    "build_dependency_graph",
    "Build a cross-peripheral dependency graph from JSON edge descriptions.",
    {"mcu_name": str, "edges_json": str},
)
async def build_dependency_graph_tool(args: dict[str, Any]) -> dict[str, Any]:
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


# ---------- Code generation tools ----------

@tool(
    "generate_qemu_peripheral",
    "Generate QEMU-compatible C source code for a peripheral model. "
    "Input is the peripheral model JSON.",
    {"peripheral_json": str, "output_dir": str},
)
async def generate_qemu_peripheral_tool(args: dict[str, Any]) -> dict[str, Any]:
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


@tool(
    "generate_test_harness",
    "Generate a test harness for validating a peripheral model against driver behavior.",
    {"peripheral_json": str, "driver_analysis_json": str, "output_dir": str},
)
async def generate_test_harness_tool(args: dict[str, Any]) -> dict[str, Any]:
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


# ---------- Validation tools ----------

@tool(
    "validate_register_model",
    "Validate a register model for consistency: overlapping fields, "
    "missing reset values, access type conflicts, etc.",
    {"register_block_json": str},
)
async def validate_register_model_tool(args: dict[str, Any]) -> dict[str, Any]:
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


@tool(
    "validate_behavior",
    "Validate peripheral model behavior against driver access patterns. "
    "Checks that register writes produce expected state changes.",
    {"peripheral_json": str, "driver_analysis_json": str},
)
async def validate_behavior_tool(args: dict[str, Any]) -> dict[str, Any]:
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


# ---------- Utility tools ----------

@tool(
    "read_file",
    "Read a file's contents. Useful for reading SVD files, headers, or driver source.",
    {"file_path": str},
)
async def read_file_tool(args: dict[str, Any]) -> dict[str, Any]:
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


@tool(
    "list_files",
    "List files matching a glob pattern in a directory.",
    {"directory": str, "pattern": str},
)
async def list_files_tool(args: dict[str, Any]) -> dict[str, Any]:
    try:
        d = Path(args["directory"])
        if not d.exists():
            return _err(f"Directory not found: {d}")
        pattern = args.get("pattern", "*")
        files = sorted(d.glob(pattern))
        return _ok("\n".join(str(f) for f in files[:200]))
    except Exception as e:
        return _err(f"List files failed: {e}")


@tool(
    "write_file",
    "Write content to a file, creating parent directories if needed.",
    {"file_path": str, "content": str},
)
async def write_file_tool(args: dict[str, Any]) -> dict[str, Any]:
    try:
        path = Path(args["file_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(args["content"], encoding="utf-8")
        return _ok(f"Written {len(args['content'])} bytes to {path}")
    except Exception as e:
        return _err(f"File write failed: {e}")


# ---------- Tool collection ----------

ALL_TOOLS: list[SdkMcpTool] = [
    parse_svd_tool,
    parse_svd_text_tool,
    parse_header_tool,
    analyze_driver_tool,
    analyze_driver_text_tool,
    build_peripheral_model_tool,
    build_state_machine_tool,
    build_interrupt_model_tool,
    build_dependency_graph_tool,
    generate_qemu_peripheral_tool,
    generate_test_harness_tool,
    validate_register_model_tool,
    validate_behavior_tool,
    read_file_tool,
    list_files_tool,
    write_file_tool,
]

TOOL_NAMES: list[str] = [t.name for t in ALL_TOOLS]
