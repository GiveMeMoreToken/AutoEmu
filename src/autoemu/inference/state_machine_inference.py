"""Automatic behavior and state-machine inference."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from autoemu.modeling_utils import normalize_driver_analysis
from autoemu.models.state_machine import State, StateMachine, Transition
from autoemu.parsers.driver_parser import DriverAnalysis, analyze_driver_file


def infer_state_machine(
    driver_analysis: DriverAnalysis | dict[str, Any],
    *,
    documentation_text: str = "",
) -> StateMachine:
    """Infer a state machine from driver analysis and optional documentation text."""
    analysis = normalize_driver_analysis(driver_analysis)
    peripheral_name = analysis.get("peripheral_name", "PERIPHERAL") or "PERIPHERAL"
    register_accesses = analysis.get("register_accesses", [])
    init_sequences = analysis.get("init_sequences", [])
    isr_patterns = analysis.get("isr_patterns", [])

    states: dict[str, State] = {}
    transitions: list[Transition] = []

    def add_state(name: str, *, description: str = "", is_initial: bool = False) -> None:
        if name in states:
            if description and not states[name].description:
                states[name].description = description
            if is_initial:
                states[name].is_initial = True
            return
        states[name] = State(name=name, description=description, is_initial=is_initial)

    def add_transition(source: str, target: str, trigger: str, *, condition: str = "", actions: list[str] | None = None) -> None:
        candidate = Transition(
            source=source,
            target=target,
            trigger=trigger,
            condition=condition,
            actions=actions or [],
        )
        if candidate not in transitions:
            transitions.append(candidate)

    add_state(
        "reset",
        is_initial=True,
        description=_doc_or_default(
            documentation_text,
            ("reset", "after reset"),
            f"{peripheral_name} hardware reset state",
        ),
    )

    init_functions = _collect_function_names_from_sequences(init_sequences)
    enable_functions = _functions_by_context(register_accesses, {"enable"})
    disable_functions = _functions_by_context(register_accesses, {"disable", "deinit", "stop"})
    transfer_functions = _functions_by_context(register_accesses, {"transfer", "start"})

    if init_functions:
        add_state(
            "ready",
            description=_doc_or_default(
                documentation_text,
                ("ready", "configured", "initialized", "idle"),
                "Peripheral configured and ready for operation",
            ),
        )
        for func in init_functions:
            add_transition("reset", "ready", f"call:{func}", actions=_register_actions(register_accesses, func))

    if enable_functions:
        add_state(
            "enabled",
            description=_doc_or_default(
                documentation_text,
                ("enabled", "active"),
                "Peripheral clocked and enabled",
            ),
        )
        for func in enable_functions:
            add_transition(
                "ready" if "ready" in states else "reset",
                "enabled",
                f"call:{func}",
                actions=_register_actions(register_accesses, func),
            )

    transfer_state_map = _transfer_states(transfer_functions, documentation_text)
    for state_name, funcs in transfer_state_map.items():
        add_state(
            state_name,
            description=_doc_or_default(
                documentation_text,
                _doc_keywords_for_state(state_name),
                _default_state_description(state_name),
            ),
        )
        source_state = "enabled" if "enabled" in states else "ready" if "ready" in states else "reset"
        for func in funcs:
            add_transition(
                source_state,
                state_name,
                f"call:{func}",
                actions=_register_actions(register_accesses, func),
            )

    complete_events = _extract_events(isr_patterns, _is_complete_token)
    error_events = _extract_events(isr_patterns, _is_error_token)

    if complete_events:
        add_state(
            "complete",
            description=_doc_or_default(
                documentation_text,
                ("complete", "done"),
                "Operation completed and completion flags are visible",
            ),
        )
        active_states = list(transfer_state_map) or (["enabled"] if "enabled" in states else ["ready"])
        for source_state in active_states:
            for event in complete_events:
                add_transition(source_state, "complete", event["trigger"], actions=event["actions"])

        resume_target = "enabled" if "enabled" in states else "ready" if "ready" in states else "reset"
        add_transition("complete", resume_target, "event:ack_complete")

    if error_events:
        add_state(
            "error",
            description=_doc_or_default(
                documentation_text,
                ("error", "fault", "timeout", "overrun"),
                "Peripheral detected an error condition",
            ),
        )
        active_states = list(transfer_state_map) or (["enabled"] if "enabled" in states else ["ready"])
        for source_state in active_states:
            for event in error_events:
                add_transition(source_state, "error", event["trigger"], actions=event["actions"])

    if disable_functions:
        add_state(
            "disabled",
            description=_doc_or_default(
                documentation_text,
                ("disabled", "off", "stop"),
                "Peripheral disabled or deinitialized",
            ),
        )
        source_candidates = [
            state_name
            for state_name in ("enabled", "ready", "complete", "error")
            if state_name in states
        ] or ["reset"]
        for func in disable_functions:
            for source_state in source_candidates:
                add_transition(
                    source_state,
                    "disabled",
                    f"call:{func}",
                    actions=_register_actions(register_accesses, func),
                )

    if len(states) == 1:
        add_state(
            "active",
            description=_doc_or_default(
                documentation_text,
                ("active", "run"),
                "Generic active operating state",
            ),
        )
        add_transition("reset", "active", "call:activate")

    return StateMachine(
        name=f"{peripheral_name}_behavior",
        description=f"Inferred state machine for {peripheral_name}",
        states=list(states.values()),
        transitions=transitions,
    )


def infer_state_machine_from_driver(
    driver_path: str | Path,
    *,
    peripheral_name: str = "",
    documentation_text: str = "",
) -> StateMachine:
    """Analyze a driver file and infer its state machine."""
    analysis = analyze_driver_file(driver_path, peripheral_name)
    return infer_state_machine(analysis, documentation_text=documentation_text)

def _collect_function_names_from_sequences(sequences: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for seq in sequences:
        name = seq.get("function_name", "")
        if name and name not in names:
            names.append(name)
    return names


def _functions_by_context(
    register_accesses: list[dict[str, Any]],
    contexts: set[str],
) -> list[str]:
    names: list[str] = []
    for access in register_accesses:
        if access.get("context") not in contexts:
            continue
        name = access.get("in_function", "")
        if name and name not in names:
            names.append(name)
    return names


def _register_actions(register_accesses: list[dict[str, Any]], function_name: str) -> list[str]:
    actions: list[str] = []
    for access in register_accesses:
        if access.get("in_function") != function_name:
            continue
        reg = access.get("register", "")
        access_type = access.get("access_type", "")
        if reg:
            actions.append(f"{access_type}:{reg}")
    return actions[:8]


def _transfer_states(
    functions: list[str],
    documentation_text: str,
) -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = {}
    for func in functions:
        lower = func.lower()
        if any(token in lower for token in ("transmit", "_tx", "send")):
            mapping.setdefault("transmitting", []).append(func)
        elif any(token in lower for token in ("receive", "_rx", "recv")):
            mapping.setdefault("receiving", []).append(func)
        else:
            mapping.setdefault("transferring", []).append(func)

    if not mapping and documentation_text:
        lower_doc = documentation_text.lower()
        if "transmit" in lower_doc:
            mapping["transmitting"] = []
        if "receive" in lower_doc:
            mapping["receiving"] = []
    return mapping


def _extract_events(
    isr_patterns: list[dict[str, Any]],
    matcher: Any,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    seen: set[str] = set()
    for isr in isr_patterns:
        callbacks = isr.get("callbacks", [])
        checked_flags = isr.get("checked_flags", [])
        cleared_flags = isr.get("cleared_flags", [])
        for callback in callbacks:
            if matcher(callback):
                trigger = f"callback:{callback}"
                if trigger not in seen:
                    seen.add(trigger)
                    events.append({"trigger": trigger, "actions": [f"callback:{callback}"]})
        for flag in checked_flags + cleared_flags:
            if matcher(flag):
                trigger = f"event:{flag}"
                if trigger not in seen:
                    seen.add(trigger)
                    actions = [f"check_flag:{flag}"] if flag in checked_flags else []
                    if flag in cleared_flags:
                        actions.append(f"clear_flag:{flag}")
                    events.append({"trigger": trigger, "actions": actions})
    return events


def _is_complete_token(value: str) -> bool:
    text = value.lower()
    tokens = ("cplt", "complete", "done", "tc", "txcplt", "rxcplt", "xfercplt")
    return any(token in text for token in tokens) and "error" not in text


def _is_error_token(value: str) -> bool:
    text = value.lower()
    tokens = ("error", "err", "fault", "timeout", "ore", "teif", "dmeif", "crc")
    return any(token in text for token in tokens)


def _doc_or_default(documentation_text: str, keywords: tuple[str, ...], default: str) -> str:
    sentence = _find_doc_sentence(documentation_text, keywords)
    return sentence or default


def _find_doc_sentence(documentation_text: str, keywords: tuple[str, ...]) -> str:
    if not documentation_text:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", documentation_text.replace("\n", " "))
    for sentence in sentences:
        lower = sentence.lower()
        if any(keyword in lower for keyword in keywords):
            return sentence.strip()
    return ""


def _doc_keywords_for_state(state_name: str) -> tuple[str, ...]:
    return {
        "transmitting": ("transmit", "sending", "tx"),
        "receiving": ("receive", "receiving", "rx"),
        "transferring": ("transfer", "busy"),
        "ready": ("ready", "idle"),
        "enabled": ("enabled", "active"),
        "disabled": ("disabled", "off"),
        "complete": ("complete", "done"),
        "error": ("error", "fault", "timeout"),
    }.get(state_name, (state_name,))


def _default_state_description(state_name: str) -> str:
    return {
        "transmitting": "Peripheral is actively transmitting data",
        "receiving": "Peripheral is actively receiving data",
        "transferring": "Peripheral transfer is in progress",
        "ready": "Peripheral has been configured and is idle",
        "enabled": "Peripheral has been enabled and can accept work",
        "disabled": "Peripheral is disabled",
        "complete": "A transfer or operation completed successfully",
        "error": "Peripheral encountered an error condition",
    }.get(state_name, state_name)
