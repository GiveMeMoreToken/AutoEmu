"""Automatic cross-peripheral dependency inference."""

from __future__ import annotations

from pathlib import Path
import re
import sys
from typing import Any, Iterable

from autoemu.modeling_utils import (
    load_wrapped_model_json,
    normalize_driver_analysis,
)
from autoemu.models.dependency import DependencyEdge, DependencyGraph, DependencyType
from autoemu.models.interrupt import InterruptModel
from autoemu.parsers.driver_parser import DriverAnalysis, analyze_driver_file, analyze_driver_string


_DMA_CONTROLLER_RE = re.compile(r"\b(DMA[12])(?:_Stream(\d+)|_Channel(\d+))?\b")
_EXTI_RE = re.compile(r"\bEXTI(?:->\w+|_[A-Z0-9_]+|_LINE)?\b")
_GPIO_RE = re.compile(r"\bGPIO[A-K]\b")
_TIMER_TRIGGER_RE = re.compile(r"\b(TIM\d+)(?:_[A-Z0-9]+)?_TRGO\b")
_TIMER_RE = re.compile(r"\b(TIM\d+)\b")
_CLOCK_BUS_RE = re.compile(r"\b(AHB[1-4]?|APB[1-4]?)\b")


def infer_dependency_graph(
    driver_analyses: DriverAnalysis | dict[str, Any] | list[DriverAnalysis | dict[str, Any]],
    *,
    peripheral_name: str = "",
    documentation_text: str = "",
    interrupt_model: InterruptModel | dict[str, Any] | None = None,
    source_texts: list[str] | None = None,
    mcu_name: str = "",
) -> DependencyGraph:
    """Infer a dependency graph from driver analysis, source code, and docs."""
    try:
        if driver_analyses is None:
            return DependencyGraph(mcu_name=mcu_name or peripheral_name, edges=[])
        if isinstance(driver_analyses, list) and len(driver_analyses) == 0:
            return DependencyGraph(mcu_name=mcu_name or peripheral_name, edges=[])
        return _infer_dependency_graph_impl(
            driver_analyses,
            peripheral_name=peripheral_name,
            documentation_text=documentation_text,
            interrupt_model=interrupt_model,
            source_texts=source_texts,
            mcu_name=mcu_name,
        )
    except Exception as exc:
        print(f"[autoemu] warning: dependency inference failed: {exc}", file=sys.stderr)
        return DependencyGraph(mcu_name=mcu_name or peripheral_name, edges=[])


def _infer_dependency_graph_impl(
    driver_analyses: DriverAnalysis | dict[str, Any] | list[DriverAnalysis | dict[str, Any]],
    *,
    peripheral_name: str = "",
    documentation_text: str = "",
    interrupt_model: InterruptModel | dict[str, Any] | None = None,
    source_texts: list[str] | None = None,
    mcu_name: str = "",
) -> DependencyGraph:
    """Core implementation of dependency graph inference."""
    analyses = _normalize_driver_analyses(driver_analyses)
    periph = peripheral_name or next(
        (analysis.get("peripheral_name", "") for analysis in analyses if analysis.get("peripheral_name")),
        "",
    )
    interrupt = _normalize_interrupt_model(interrupt_model)
    relevant_docs = _filter_relevant_documentation(documentation_text, periph)
    docs_corpus = relevant_docs if len(relevant_docs.splitlines()) <= 20 and not (source_texts or []) else ""
    corpus = "\n".join(source_texts or [])
    if docs_corpus:
        corpus = f"{corpus}\n{docs_corpus}" if corpus else docs_corpus

    edges: list[DependencyEdge] = []
    seen: set[tuple[str, str, DependencyType, str, str]] = set()

    def add_edge(
        source: str,
        target: str,
        dep_type: DependencyType,
        *,
        description: str,
        channel: str = "",
        trigger_source: str = "",
        config_registers: Iterable[str] = (),
        bidirectional: bool = False,
    ) -> None:
        key = (source, target, dep_type, channel, trigger_source)
        if not source or not target or key in seen:
            return
        seen.add(key)
        edges.append(
            DependencyEdge(
                source=source,
                target=target,
                dep_type=dep_type,
                description=description,
                channel=channel,
                trigger_source=trigger_source,
                config_registers=_unique_in_order([item for item in config_registers if item]),
                bidirectional=bidirectional,
            )
        )

    if periph and periph.upper() != "RCC":
        buses = _CLOCK_BUS_RE.findall(corpus)
        clock_regs = _find_clock_registers(corpus, periph)
        desc = f"{periph} requires RCC clock enable before use"
        if buses:
            desc += f" on {_unique_in_order(buses)[0]}"
        add_edge(
            "RCC",
            periph,
            DependencyType.CLOCK_GATE,
            description=desc,
            config_registers=clock_regs,
        )

    if periph:
        dma_controllers, dma_channels = _find_dma_dependencies(corpus)
        for controller in dma_controllers:
            if controller == periph.upper():
                continue
            add_edge(
                controller,
                periph,
                DependencyType.DMA_CHANNEL,
                description=f"{periph} uses {controller} for data transfer or descriptor movement",
                channel=", ".join(dma_channels.get(controller, [])),
                config_registers=_find_dma_registers(corpus, controller),
            )

    if periph and _has_exti_dependency(corpus, interrupt):
        add_edge(
            "EXTI",
            periph,
            DependencyType.IRQ_CHAIN,
            description=f"EXTI feeds wakeup or external interrupt events into {periph}",
            config_registers=["PR", "IMR"] if "EXTI" in corpus else [],
        )

    if periph and _has_gpio_dependency(corpus, docs_corpus):
        add_edge(
            "GPIO",
            periph,
            DependencyType.GPIO_AF,
            description=f"{periph} depends on GPIO alternate-function or pinmux configuration",
            config_registers=_unique_in_order(_GPIO_RE.findall(corpus)),
        )

    if periph:
        for timer in _find_trigger_timers(corpus, docs_corpus):
            add_edge(
                timer,
                periph,
                DependencyType.TRIGGER,
                description=f"{timer} provides an external trigger path for {periph}",
                trigger_source="TRGO",
            )

    return DependencyGraph(mcu_name=mcu_name or periph, edges=edges)


def infer_dependency_graph_from_driver_files(
    driver_paths: list[str | Path],
    *,
    peripheral_name: str = "",
    documentation_text: str = "",
    interrupt_model: InterruptModel | dict[str, Any] | None = None,
    mcu_name: str = "",
) -> DependencyGraph:
    """Infer dependencies from one or more driver files."""
    analyses: list[DriverAnalysis] = []
    source_texts: list[str] = []
    for driver_path in driver_paths:
        analyses.append(analyze_driver_file(driver_path, peripheral_name))
        source_texts.append(Path(driver_path).read_text(encoding="utf-8", errors="replace"))
    return infer_dependency_graph(
        analyses,
        peripheral_name=peripheral_name,
        documentation_text=documentation_text,
        interrupt_model=interrupt_model,
        source_texts=source_texts,
        mcu_name=mcu_name,
    )


def infer_dependency_graph_from_driver_text(
    source_code: str,
    *,
    peripheral_name: str = "",
    documentation_text: str = "",
    interrupt_model: InterruptModel | dict[str, Any] | None = None,
    mcu_name: str = "",
) -> DependencyGraph:
    """Infer dependencies from source text, useful for tests and agent flows."""
    analysis = analyze_driver_string(source_code, peripheral_name)
    return infer_dependency_graph(
        analysis,
        peripheral_name=peripheral_name,
        documentation_text=documentation_text,
        interrupt_model=interrupt_model,
        source_texts=[source_code],
        mcu_name=mcu_name,
    )


def load_dependency_graph_json(path: str | Path) -> DependencyGraph:
    """Load a dependency graph JSON file from step 4 output."""
    return load_wrapped_model_json(path, DependencyGraph)


def _normalize_driver_analyses(
    driver_analyses: DriverAnalysis | dict[str, Any] | list[DriverAnalysis | dict[str, Any]],
) -> list[dict[str, Any]]:
    if isinstance(driver_analyses, list):
        return [_normalize_driver_analysis(item) for item in driver_analyses]
    return [_normalize_driver_analysis(driver_analyses)]


def _normalize_driver_analysis(driver_analysis: DriverAnalysis | dict[str, Any]) -> dict[str, Any]:
    return normalize_driver_analysis(driver_analysis)


def _normalize_interrupt_model(
    interrupt_model: InterruptModel | dict[str, Any] | None,
) -> InterruptModel | None:
    if interrupt_model is None:
        return None
    if isinstance(interrupt_model, InterruptModel):
        return interrupt_model
    model = interrupt_model.get("model", interrupt_model)
    return InterruptModel.model_validate(model)


def _find_clock_registers(corpus: str, peripheral_name: str) -> list[str]:
    aliases = _peripheral_aliases(peripheral_name)
    matches: list[str] = []
    for alias in aliases:
        matches.extend(
            re.findall(rf"\b(RCC_[A-Z0-9_]*{re.escape(alias)}[A-Z0-9_]*ENR?)\b", corpus)
        )
    return _unique_in_order(matches)


def _find_dma_dependencies(corpus: str) -> tuple[list[str], dict[str, list[str]]]:
    controllers: list[str] = []
    channels_by_controller: dict[str, list[str]] = {}
    for controller, stream, channel in _DMA_CONTROLLER_RE.findall(corpus):
        if controller not in controllers:
            controllers.append(controller)
        channel_tokens: list[str] = []
        if stream:
            channel_tokens.append(f"Stream{stream}")
        if channel:
            channel_tokens.append(f"Channel{channel}")
        if channel_tokens:
            channels_by_controller.setdefault(controller, [])
            channels_by_controller[controller].extend(channel_tokens)

    for controller, values in channels_by_controller.items():
        channels_by_controller[controller] = _unique_in_order(values)
    return controllers, channels_by_controller


def _find_dma_registers(corpus: str, controller: str) -> list[str]:
    matches = re.findall(rf"\b({re.escape(controller)}_[A-Z0-9_]+)\b", corpus)
    return _unique_in_order(matches[:8])


def _has_exti_dependency(corpus: str, interrupt_model: InterruptModel | None) -> bool:
    if _EXTI_RE.search(corpus):
        return True
    if interrupt_model is None:
        return False
    if any("wake" in event.lower() for event in interrupt_model.event_sources):
        return True
    return any("wake" in flag.name.lower() for line in interrupt_model.lines for flag in line.flags)


def _has_gpio_dependency(corpus: str, documentation_text: str) -> bool:
    if _GPIO_RE.search(corpus):
        return True
    doc_lower = documentation_text.lower()
    return "alternate function" in doc_lower or "pinmux" in doc_lower


def _find_trigger_timers(corpus: str, documentation_text: str) -> list[str]:
    matches = _TIMER_TRIGGER_RE.findall(corpus)
    if matches:
        return _unique_in_order(matches)

    combined = f"{corpus}\n{documentation_text}".lower()
    if "trigger" not in combined and "trgo" not in combined:
        return []
    return _unique_in_order(_TIMER_RE.findall(corpus + "\n" + documentation_text))


def _peripheral_aliases(peripheral_name: str) -> list[str]:
    upper = peripheral_name.upper()
    aliases = [upper]
    if "_" in upper:
        aliases.append(upper.split("_", 1)[-1])
        aliases.extend(part for part in upper.split("_") if len(part) > 2)
    if upper == "ETH":
        aliases.extend(["ETHMAC", "ETHDMA"])
    elif "USB" in upper or "OTG" in upper:
        aliases.extend(["USB", "OTG", "OTGFS", "OTGHS"])
    elif "SUBGHZ" in upper or "RADIO" in upper:
        aliases.extend(["SUBGHZ", "RADIO"])
    return _unique_in_order(aliases)


def _filter_relevant_documentation(documentation_text: str, peripheral_name: str) -> str:
    if not documentation_text or not peripheral_name:
        return documentation_text

    aliases = [alias.lower() for alias in _peripheral_aliases(peripheral_name)]
    lines = documentation_text.splitlines()
    keep: set[int] = set()

    for index, line in enumerate(lines):
        lower = line.lower()
        if any(alias in lower for alias in aliases):
            keep.update({index - 1, index, index + 1})

    if not keep:
        return ""
    return "\n".join(lines[index] for index in sorted(keep) if 0 <= index < len(lines))


def _unique_in_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered
