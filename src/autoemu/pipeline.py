"""End-to-end peripheral modeling pipeline."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from autoemu.fetchers.generic import infer_stm32_mcu_family, resolve_fetched_input_bundle
from autoemu.generators.bundle_generator import generate_model_bundle
from autoemu.inference.dependency_inference import infer_dependency_graph
from autoemu.inference.interrupt_inference import infer_interrupt_model
from autoemu.inference.state_machine_inference import infer_state_machine
from autoemu.parsers.driver_parser import (
    DriverAnalysis,
    ISRPattern,
    InitSequence,
    analyze_driver_file,
)
from autoemu.parsers.register_extractor import extract_register_blocks


def run_model_pipeline(
    peripheral_name: str,
    *,
    output_dir: str | Path,
    svd_path: str | Path = "",
    header_path: str | Path = "",
    driver_paths: Iterable[str | Path] = (),
    documentation_paths: Iterable[str | Path] = (),
    mcu_family: str = "",
) -> dict[str, Any]:
    """Run steps 1-5 and emit an end-to-end QEMU-ready peripheral bundle."""
    driver_path_list = [str(path) for path in driver_paths if str(path)]
    doc_path_list = [str(path) for path in documentation_paths if str(path)]

    if not svd_path and not header_path and not driver_path_list:
        raise ValueError(
            "Provide at least one input source: SVD, header, or driver file."
        )

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    # Register extraction — may yield empty blocks when no SVD/header available
    register_blocks, extraction_warnings = extract_register_blocks(
        svd_path=str(svd_path or ""),
        header_path=str(header_path or ""),
        peripheral_name=peripheral_name,
    )
    register_path = _write_json(
        output_path / f"{_snake(peripheral_name)}_registers.json",
        {name: block.model_dump() for name, block in register_blocks.items()},
    )

    analyses = [analyze_driver_file(path, peripheral_name) for path in driver_path_list]
    driver_analysis = merge_driver_analyses(analyses, peripheral_name=peripheral_name)
    source_texts = [
        Path(path).read_text(encoding="utf-8", errors="replace")
        for path in driver_path_list
    ]
    docs_text = _load_documentation_text(doc_path_list)

    state_machine = infer_state_machine(driver_analysis, documentation_text=docs_text)
    state_machine_path = _write_json(
        output_path / f"{_snake(peripheral_name)}_state_machine.json",
        {
            "model": state_machine.model_dump(),
            "dot": state_machine.to_dot(),
        },
    )

    interrupt_model = infer_interrupt_model(
        driver_analysis,
        register_blocks,
        peripheral_name=peripheral_name,
    )
    interrupt_path = _write_json(
        output_path / f"{_snake(peripheral_name)}_interrupt_model.json",
        {
            "model": interrupt_model.model_dump(),
        },
    )

    dependency_graph = infer_dependency_graph(
        driver_analysis,
        peripheral_name=peripheral_name,
        documentation_text=docs_text,
        interrupt_model=interrupt_model,
        source_texts=source_texts,
        mcu_name=mcu_family or peripheral_name,
    )
    dependency_path = _write_json(
        output_path / f"{_snake(peripheral_name)}_dependencies.json",
        {
            "model": dependency_graph.model_dump(),
            "dot": dependency_graph.to_dot(),
        },
    )

    bundle = generate_model_bundle(
        peripheral_name,
        register_blocks,
        output_dir=output_path,
        state_machine=state_machine,
        interrupt_model=interrupt_model,
        dependencies=dependency_graph,
        driver_analysis=driver_analysis,
        mcu_family=mcu_family,
    )

    return {
        "peripheral_name": peripheral_name,
        "mcu_family": mcu_family,
        "driver_files": driver_path_list,
        "documentation_files": doc_path_list,
        "registers_json": str(register_path),
        "state_machine_json": str(state_machine_path),
        "interrupt_model_json": str(interrupt_path),
        "dependency_graph_json": str(dependency_path),
        "peripheral_json": bundle["peripheral_json"],
        "generated_files": bundle["generated_files"],
        "validation_report": bundle["validation_report"],
        "validation_json": bundle["validation_json"],
    }


def run_target_model_pipeline(
    *,
    target_mcu: str,
    target_peripheral: str,
    data_dir: str | Path = "data/stm32",
    output_dir: str | Path = "output",
) -> dict[str, Any]:
    """Run the end-to-end pipeline from previously fetched target data."""
    inputs = resolve_fetched_input_bundle(
        target_mcu=target_mcu,
        target_peripheral=target_peripheral,
        data_dir=data_dir,
    )

    if not inputs.svd_path and not inputs.header_path and not inputs.driver_paths:
        raise ValueError(
            f"No inputs found for {target_mcu} / {target_peripheral}. "
            f"No SVD, headers, or drivers under {data_dir}."
        )

    result = run_model_pipeline(
        target_peripheral,
        output_dir=output_dir,
        svd_path=inputs.svd_path,
        header_path=inputs.header_path,
        driver_paths=inputs.driver_paths,
        documentation_paths=inputs.documentation_paths,
        mcu_family=infer_stm32_mcu_family(target_mcu),
    )
    result["target_mcu"] = target_mcu
    result["target_peripheral"] = target_peripheral
    result["data_manifest"] = inputs.manifest_path
    return result


def merge_driver_analyses(
    analyses: Iterable[DriverAnalysis],
    *,
    peripheral_name: str = "",
) -> DriverAnalysis:
    """Merge one or more driver analyses into a single aggregate view."""
    analysis_list = list(analyses)
    if not analysis_list:
        return DriverAnalysis(
            peripheral_name=peripheral_name,
            source_file="",
        )

    merged = DriverAnalysis(
        peripheral_name=peripheral_name or analysis_list[0].peripheral_name,
        source_file=";".join(_unique_in_order([analysis.source_file for analysis in analysis_list if analysis.source_file])),
        register_accesses=[],
        isr_patterns=[],
        init_sequences=[],
        dma_configs=[],
        state_hints=[],
    )

    merged.register_accesses = _dedupe_by_key(
        (
            access
            for analysis in analysis_list
            for access in analysis.register_accesses
        ),
        key=lambda access: (
            access.source_file,
            access.source_line,
            access.in_function,
            access.context,
            access.access_type,
            access.register,
            access.field,
            access.value_expr,
        ),
    )

    isr_index: dict[tuple[str, str], ISRPattern] = {}
    for analysis in analysis_list:
        for pattern in analysis.isr_patterns:
            key = (pattern.function_name, pattern.peripheral)
            if key not in isr_index:
                isr_index[key] = ISRPattern(
                    function_name=pattern.function_name,
                    peripheral=pattern.peripheral,
                    checked_flags=list(pattern.checked_flags),
                    cleared_flags=list(pattern.cleared_flags),
                    enabled_checks=list(pattern.enabled_checks),
                    callbacks=list(pattern.callbacks),
                    register_accesses=list(pattern.register_accesses),
                )
                continue
            existing_pattern = isr_index[key]
            existing_pattern.checked_flags = _unique_in_order(existing_pattern.checked_flags + pattern.checked_flags)
            existing_pattern.cleared_flags = _unique_in_order(existing_pattern.cleared_flags + pattern.cleared_flags)
            existing_pattern.enabled_checks = _unique_in_order(existing_pattern.enabled_checks + pattern.enabled_checks)
            existing_pattern.callbacks = _unique_in_order(existing_pattern.callbacks + pattern.callbacks)
            existing_pattern.register_accesses = _dedupe_by_key(
                [*existing_pattern.register_accesses, *pattern.register_accesses],
                key=lambda access: (
                    access.source_file,
                    access.source_line,
                    access.in_function,
                    access.context,
                    access.access_type,
                    access.register,
                    access.field,
                    access.value_expr,
                ),
            )
    merged.isr_patterns = list(isr_index.values())

    init_index: dict[tuple[str, str], InitSequence] = {}
    for analysis in analysis_list:
        for sequence in analysis.init_sequences:
            key = (sequence.function_name, sequence.peripheral)
            if key not in init_index:
                init_index[key] = InitSequence(
                    function_name=sequence.function_name,
                    peripheral=sequence.peripheral,
                    accesses=list(sequence.accesses),
                    dependencies=list(sequence.dependencies),
                )
                continue
            existing_sequence = init_index[key]
            existing_sequence.accesses = _dedupe_by_key(
                [*existing_sequence.accesses, *sequence.accesses],
                key=lambda access: (
                    access.source_file,
                    access.source_line,
                    access.in_function,
                    access.context,
                    access.access_type,
                    access.register,
                    access.field,
                    access.value_expr,
                ),
            )
            existing_sequence.dependencies = _unique_in_order(existing_sequence.dependencies + sequence.dependencies)
    merged.init_sequences = list(init_index.values())

    merged.dma_configs = _dedupe_by_key(
        (
            config
            for analysis in analysis_list
            for config in analysis.dma_configs
        ),
        key=lambda config: (
            config.peripheral,
            config.direction,
            config.channel,
            config.stream,
            config.source_register,
            config.trigger,
            config.circular,
            config.fifo_mode,
        ),
    )

    merged.state_hints = _dedupe_by_key(
        (
            hint
            for analysis in analysis_list
            for hint in analysis.state_hints
        ),
        key=lambda hint: json.dumps(hint, sort_keys=True),
    )

    return merged


def _write_json(path: Path, payload: Any) -> Path:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _load_documentation_text(paths: list[str]) -> str:
    texts = [
        Path(path).read_text(encoding="utf-8", errors="replace")
        for path in paths
    ]
    if not texts:
        return ""
    return "\n\n".join(texts)


from autoemu.modeling_utils import normalize_name as _snake  # noqa: E402


def _unique_in_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def _dedupe_by_key(items: Iterable[Any], *, key: Any) -> list[Any]:
    seen: set[Any] = set()
    ordered: list[Any] = []
    for item in items:
        item_key = key(item)
        if item_key in seen:
            continue
        seen.add(item_key)
        ordered.append(item)
    return ordered
