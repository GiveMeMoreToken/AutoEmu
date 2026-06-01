"""Tests for TUI result-to-phase status mapping."""

from __future__ import annotations

from types import SimpleNamespace

from autoemu.agent.runtime import PipelineProgress
import autoemu.tui.app as tui_app
from autoemu.tui.app import _final_phase_status_actions


def test_final_phase_status_marks_probe_warn_when_validation_fails():
    result = SimpleNamespace(
        fetch_result={"success": True},
        build_result={"generated_files": ["output/device.c"]},
        validation_result={"success": False, "errors": [], "warnings": ["blocking warning"]},
        probe_result={"success": False, "skipped": True, "reason": "missing QEMU env"},
    )

    actions = _final_phase_status_actions(result)

    assert ("set_phase_error", 4) in actions
    assert ("set_phase_warn", 5) in actions


def test_final_phase_status_marks_probe_done_on_success():
    result = SimpleNamespace(
        fetch_result={"success": True},
        build_result={"generated_files": ["output/device.c"]},
        validation_result={"success": True, "errors": [], "warnings": []},
        probe_result={"success": True, "skipped": False},
    )

    actions = _final_phase_status_actions(result)

    assert ("set_phase_done", 4) in actions
    assert ("set_phase_done", 5) in actions


def test_schedule_progress_update_uses_call_from_thread_for_widget_updates():
    """Worker-thread progress must be marshalled onto Textual's UI thread."""
    calls: list[tuple[object, tuple[object, ...]]] = []
    log_messages: list[tuple[str, str]] = []
    phase_updates: list[int] = []

    class FakeApp:
        def call_from_thread(self, func, *args):
            calls.append((func, args))

    class FakePhases:
        def set_phase_running(self, phase):
            phase_updates.append(phase)

    class FakeLog:
        def log_kind(self, message, kind):
            log_messages.append((message, kind))

    progress = PipelineProgress(
        phase=5,
        phase_name="Testing driver probing",
        detail="Stage 5 command: ninja -C build qemu-system-aarch64",
        kind="compile",
    )

    tui_app._schedule_progress_update(FakeApp(), FakePhases(), FakeLog(), progress)

    assert len(calls) == 2
    assert phase_updates == []
    assert log_messages == []

    for func, args in calls:
        func(*args)

    assert phase_updates == [5]
    assert log_messages == [
        ("Stage 5 command: ninja -C build qemu-system-aarch64", "compile")
    ]
