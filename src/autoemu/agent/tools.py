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
from autoemu.parsers.register_extractor import extract_register_blocks
from autoemu.parsers.driver_parser import (
    analyze_driver_file,
    analyze_driver_string,
    DriverAnalysis,
)
from autoemu.fetchers.generic import GenericDataFetcher
from autoemu.generators.bundle_generator import generate_model_bundle
from autoemu.inference.dependency_inference import infer_dependency_graph
from autoemu.inference.interrupt_inference import infer_interrupt_model
from autoemu.inference.state_machine_inference import infer_state_machine
from autoemu.pipeline import run_model_pipeline
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
)


# ------------------------------------------------------------------ helpers

def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"Error: {text}"}], "is_error": True}


def _load_json_arg(args: dict[str, Any], key: str) -> Any:
    """Load JSON from inline string or file path."""
    raw = args.get(key, "")
    if not raw or raw == "null":
        return None
    if raw.startswith("{"):
        return json.loads(raw)
    return json.loads(Path(raw).read_text(encoding="utf-8"))


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


async def _extract_register_structure(args: dict[str, Any]) -> dict[str, Any]:
    try:
        periph = args.get("peripheral_name") or None
        blocks, warnings = extract_register_blocks(
            svd_path=args.get("svd_path", ""),
            header_path=args.get("header_path", ""),
            peripheral_name=periph,
        )
        result: dict[str, Any] = {name: blk.model_dump() for name, blk in blocks.items()}
        if warnings:
            result["_warnings"] = warnings
        return _ok(json.dumps(result, indent=2))
    except Exception as e:
        return _err(f"Register extraction failed: {e}")


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


async def _infer_state_machine(args: dict[str, Any]) -> dict[str, Any]:
    try:
        analysis_data = json.loads(args["driver_analysis_json"])
        documentation_text = args.get("documentation_text", "")
        sm = infer_state_machine(
            analysis_data,
            documentation_text=documentation_text,
        )
        result = {
            "model": sm.model_dump(),
            "dot": sm.to_dot(),
            "states": [state.name for state in sm.states],
            "transitions": len(sm.transitions),
        }
        return _ok(json.dumps(result, indent=2))
    except Exception as e:
        return _err(f"State machine inference failed: {e}")


async def _infer_interrupt_model(args: dict[str, Any]) -> dict[str, Any]:
    try:
        analysis_data = json.loads(args["driver_analysis_json"])
        register_blocks_data = json.loads(args.get("register_blocks_json", "{}"))
        peripheral_name = args.get("peripheral_name", "")
        model = infer_interrupt_model(
            analysis_data,
            register_blocks_data,
            peripheral_name=peripheral_name,
        )
        result = {
            "model": model.model_dump(),
            "lines": len(model.lines),
            "event_sources": model.event_sources,
        }
        return _ok(json.dumps(result, indent=2))
    except Exception as e:
        return _err(f"Interrupt model inference failed: {e}")


async def _infer_dependency_graph(args: dict[str, Any]) -> dict[str, Any]:
    try:
        analysis_data = json.loads(args["driver_analysis_json"])
        documentation_text = args.get("documentation_text", "")
        interrupt_model_data = json.loads(args.get("interrupt_model_json", "null"))
        peripheral_name = args.get("peripheral_name", "")
        mcu_name = args.get("mcu_name", "")

        model = infer_dependency_graph(
            analysis_data,
            peripheral_name=peripheral_name,
            documentation_text=documentation_text,
            interrupt_model=interrupt_model_data,
            mcu_name=mcu_name,
        )
        result = {
            "model": model.model_dump(),
            "dot": model.to_dot(),
            "edges": len(model.edges),
            "peripherals": sorted(model.get_all_peripherals()),
        }
        return _ok(json.dumps(result, indent=2))
    except Exception as e:
        return _err(f"Dependency graph inference failed: {e}")


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

        lines = [InterruptLine.model_validate(line_data) for line_data in lines_data]

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


async def _generate_model_bundle(args: dict[str, Any]) -> dict[str, Any]:
    try:
        # Accept either inline JSON or a file path to avoid huge tool arguments
        rb_raw = args.get("register_blocks_json", "")
        if rb_raw.startswith("{"):
            register_blocks_data = json.loads(rb_raw)
        else:
            register_blocks_data = json.loads(Path(rb_raw).read_text(encoding="utf-8"))

        state_machine_data = _load_json_arg(args, "state_machine_json")
        interrupt_model_data = _load_json_arg(args, "interrupt_model_json")
        dependency_graph_data = _load_json_arg(args, "dependency_graph_json")
        driver_analysis_data = _load_json_arg(args, "driver_analysis_json")

        result = generate_model_bundle(
            args["peripheral_name"],
            register_blocks_data,
            output_dir=args.get("output_dir", "output"),
            peripheral_type=args.get("peripheral_type") or None,
            state_machine=state_machine_data,
            interrupt_model=interrupt_model_data,
            dependencies=dependency_graph_data,
            driver_analysis=driver_analysis_data,
            mcu_family=args.get("mcu_family", ""),
        )
        summary = {
            "peripheral_json": result["peripheral_json"],
            "generated_files": result["generated_files"],
            "validation_report": result["validation_report"],
        }
        return _ok(json.dumps(summary, indent=2))
    except Exception as e:
        return _err(f"Model bundle generation failed: {e}")


async def _run_model_pipeline(args: dict[str, Any]) -> dict[str, Any]:
    try:
        driver_paths = json.loads(args.get("driver_paths_json", "[]"))
        docs_paths = json.loads(args.get("docs_paths_json", "[]"))
        result = run_model_pipeline(
            args["peripheral_name"],
            output_dir=args.get("output_dir", "output"),
            svd_path=args.get("svd_path", ""),
            header_path=args.get("header_path", ""),
            driver_paths=driver_paths,
            documentation_paths=docs_paths,
            mcu_family=args.get("mcu_family", ""),
        )
        return _ok(json.dumps(result, indent=2))
    except Exception as e:
        return _err(f"Model pipeline failed: {e}")


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


_BINARY_EXTENSIONS = {
    ".pdf", ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp",
    ".mp3", ".mp4", ".avi", ".mov", ".mkv",
    ".exe", ".dll", ".so", ".dylib", ".bin",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
}


async def _read_file(args: dict[str, Any]) -> dict[str, Any]:
    try:
        path = Path(args["file_path"])
        if not path.exists():
            return _err(f"File not found: {path}")
        if path.stat().st_size > 5 * 1024 * 1024:
            return _err(f"File too large: {path.stat().st_size} bytes")
        if path.suffix.lower() in _BINARY_EXTENSIONS:
            return _err(
                f"Binary file ({path.suffix}) cannot be read as text: {path}"
            )
        raw = path.read_bytes()
        # Reject files with significant binary content (many nulls or control chars)
        if len(raw) > 0:
            null_count = raw.count(0)
            if null_count > max(16, len(raw) * 0.01):
                return _err(f"File appears to be binary: {path}")
        content = raw.decode("utf-8", errors="replace")
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


async def _fetch_data(args: dict[str, Any]) -> dict[str, Any]:
    try:
        target_mcu = args["target_mcu"]
        target_peripheral = args["target_peripheral"]
        output_dir = args.get("output_dir", "data")

        # Use output_dir directly as the data directory — callers already include
        # the MCU-specific path (e.g. "data/hikey960").  Adding the MCU slug here
        # again would produce a double-nested path like "data/hikey960/hikey960".
        data_dir = output_dir

        fetcher = GenericDataFetcher()
        candidates = fetcher.discover_candidates(target_mcu, target_peripheral)
        select_candidates = getattr(fetcher, "select_candidates", None)
        selected = (
            select_candidates(candidates, limit=10)
            if select_candidates
            else candidates[:10]
        )
        fetch_result = fetcher.fetch_selected(
            selected, data_dir,
            target_mcu=target_mcu, target_peripheral=target_peripheral,
        )

        lines = [f"Fetched input data for {target_mcu} / {target_peripheral}"]
        for d in fetch_result.downloaded:
            lines.append(f"  Downloaded: {d['local_path']} ({d['category']})")
        for e in fetch_result.errors:
            lines.append(f"  Error: {e}")
        return _ok("\n".join(lines))
    except Exception as e:
        return _err(f"STM32 data fetch failed: {e}")


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
        name="extract_register_structure",
        description=(
            "Build a peripheral register structure by combining SVD and header inputs. "
            "Prefers SVD field semantics but fills gaps from CMSIS header layout and access qualifiers."
        ),
        parameters={"svd_path": str, "header_path": str, "peripheral_name": str},
        handler=_extract_register_structure,
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
        name="infer_state_machine",
        description=(
            "Infer a peripheral state machine from driver analysis JSON and optional "
            "documentation text. Returns state-machine JSON and Graphviz DOT."
        ),
        parameters={"driver_analysis_json": str, "documentation_text": str},
        handler=_infer_state_machine,
    ),
    ToolSpec(
        name="infer_interrupt_model",
        description=(
            "Infer an interrupt model from driver analysis JSON and extracted register "
            "blocks JSON. Returns interrupt-model JSON with event mappings."
        ),
        parameters={"driver_analysis_json": str, "register_blocks_json": str, "peripheral_name": str},
        handler=_infer_interrupt_model,
    ),
    ToolSpec(
        name="infer_dependency_graph",
        description=(
            "Infer a cross-peripheral dependency graph from driver analysis JSON, "
            "optional documentation text, and an optional interrupt model JSON."
        ),
        parameters={
            "driver_analysis_json": str,
            "documentation_text": str,
            "interrupt_model_json": str,
            "peripheral_name": str,
            "mcu_name": str,
        },
        handler=_infer_dependency_graph,
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
        name="generate_model_bundle",
        description=(
            "Assemble a peripheral model from step outputs, generate QEMU "
            "artifacts, and emit a validation report."
        ),
        parameters={
            "peripheral_name": str,
            "register_blocks_json": str,
            "state_machine_json": str,
            "interrupt_model_json": str,
            "dependency_graph_json": str,
            "driver_analysis_json": str,
            "output_dir": str,
            "peripheral_type": str,
            "mcu_family": str,
        },
        handler=_generate_model_bundle,
    ),
    ToolSpec(
        name="run_model_pipeline",
        description=(
            "Run the full deterministic pipeline from register inputs and driver files: "
            "extract registers, infer behavior, infer interrupts, infer dependencies, "
            "generate QEMU artifacts, and emit validation results."
        ),
        parameters={
            "peripheral_name": str,
            "svd_path": str,
            "header_path": str,
            "driver_paths_json": str,
            "docs_paths_json": str,
            "output_dir": str,
            "mcu_family": str,
        },
        handler=_run_model_pipeline,
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
    ToolSpec(
        name="fetch_data",
        description=(
            "Fetch STM32 input data for one MCU/peripheral target. "
            "Uses web search plus trusted fallbacks to collect manuals, datasheets, "
            "SVDs, headers, HAL/LL drivers, and RTOS adaptation sources. "
            "Writes manifests under the output directory."
        ),
        parameters={"target_mcu": str, "target_peripheral": str, "output_dir": str, "refresh": bool},
        handler=_fetch_data,
    ),
]

TOOL_NAMES: list[str] = [t.name for t in ALL_TOOLS]
