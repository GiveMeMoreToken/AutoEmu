"""General generated artifact and QEMU hardware structure validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from autoemu.models.qemu import QEMUHardwareModel


Issue = dict[str, Any]


def validate_qemu_hardware_model(
    hardware_model: QEMUHardwareModel | dict[str, Any],
    *,
    source: str = "QEMU hardware model",
) -> list[Issue]:
    """Validate generic QEMU hardware structure beyond schema typing."""
    issues: list[Issue] = []
    model: QEMUHardwareModel
    try:
        model = (
            hardware_model
            if isinstance(hardware_model, QEMUHardwareModel)
            else QEMUHardwareModel.model_validate(hardware_model)
        )
    except ValidationError as exc:
        return [
            _issue(
                "error",
                f"{source} is incomplete or invalid: {exc.errors()[0]['msg']}",
                code="invalid_qemu_hardware",
                path=source,
            )
        ]

    for field_name in (
        "source_path",
        "header_path",
        "meson_path",
        "meson_snippet_path",
        "qtest_path",
    ):
        value = getattr(model.file_layout, field_name, "")
        if not str(value).strip():
            issues.append(
                _issue(
                    "error",
                    f"{source} file_layout.{field_name} is required",
                    code="missing_file_layout_path",
                    path=source,
                )
            )

    if not model.mmio_regions:
        issues.append(
            _issue(
                "error",
                f"{source} has no MMIO regions",
                code="missing_mmio_region",
                path=source,
            )
        )
    else:
        for region in model.mmio_regions:
            label = region.name or "unnamed"
            if region.size <= 0:
                issues.append(
                    _issue(
                        "error",
                        f"{source} MMIO region '{label}' has zero size",
                        code="zero_mmio_size",
                        path=source,
                    )
                )
            if region.register_count <= 0:
                issues.append(
                    _issue(
                        "error",
                        f"{source} MMIO region '{label}' has register_count=0",
                        code="zero_mmio_register_count",
                        path=source,
                    )
                )

        if not any(region.size > 0 and region.register_count > 0 for region in model.mmio_regions):
            issues.append(
                _issue(
                    "error",
                    f"{source} has no usable MMIO region with positive size and register_count",
                    code="no_usable_mmio_region",
                    path=source,
                )
            )

    if not model.device_tree.reg:
        issues.append(
            _issue(
                "error",
                f"{source} device_tree.reg is empty",
                code="missing_device_tree_reg",
                path=source,
            )
        )
    elif not any(region.size > 0 for region in model.device_tree.reg):
        issues.append(
            _issue(
                "error",
                f"{source} device_tree.reg has no positive-size region",
                code="zero_device_tree_reg_size",
                path=source,
            )
        )

    return issues


def validate_qemu_hardware_json(path: str | Path) -> list[Issue]:
    """Validate a generated ``*_qemu_hardware.json`` file."""
    hardware_path = Path(path)
    if not hardware_path.exists():
        return [
            _issue(
                "error",
                f"QEMU hardware JSON is missing: {hardware_path}",
                code="missing_qemu_hardware_json",
                path=str(hardware_path),
            )
        ]
    if not hardware_path.is_file():
        return [
            _issue(
                "error",
                f"QEMU hardware JSON path is not a file: {hardware_path}",
                code="invalid_qemu_hardware_json_path",
                path=str(hardware_path),
            )
        ]
    try:
        raw = hardware_path.read_text(encoding="utf-8")
    except OSError as exc:
        return [
            _issue(
                "error",
                f"QEMU hardware JSON could not be read: {exc}",
                code="unreadable_qemu_hardware_json",
                path=str(hardware_path),
            )
        ]
    if not raw.strip():
        return [
            _issue(
                "error",
                f"QEMU hardware JSON is empty: {hardware_path}",
                code="empty_qemu_hardware_json",
                path=str(hardware_path),
            )
        ]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return [
            _issue(
                "error",
                f"QEMU hardware JSON is invalid: {exc.msg}",
                code="invalid_qemu_hardware_json",
                path=str(hardware_path),
            )
        ]
    return validate_qemu_hardware_model(data, source=str(hardware_path))


def validate_generated_artifact_files(paths: list[str | Path]) -> list[Issue]:
    """Validate that generated artifact paths exist as non-empty files."""
    issues: list[Issue] = []
    seen: set[Path] = set()
    for raw_path in paths:
        path = Path(raw_path)
        if path in seen:
            continue
        seen.add(path)
        if not path.exists():
            issues.append(
                _issue(
                    "error",
                    f"Generated artifact is missing: {path}",
                    code="missing_generated_artifact",
                    path=str(path),
                )
            )
            continue
        if not path.is_file():
            issues.append(
                _issue(
                    "error",
                    f"Generated artifact is not a file: {path}",
                    code="invalid_generated_artifact_path",
                    path=str(path),
                )
            )
            continue
        try:
            if path.stat().st_size == 0:
                issues.append(
                    _issue(
                        "error",
                        f"Generated artifact is empty: {path}",
                        code="empty_generated_artifact",
                        path=str(path),
                    )
                )
        except OSError as exc:
            issues.append(
                _issue(
                    "error",
                    f"Generated artifact could not be inspected: {exc}",
                    code="unreadable_generated_artifact",
                    path=str(path),
                )
            )
    return issues


def validate_output_directory_artifacts(output_dir: str | Path) -> dict[str, Any]:
    """Validate generic generated artifacts in an output directory."""
    root = Path(output_dir)
    artifact_issues: list[Issue] = []
    hardware_issues: list[Issue] = []
    source_files: list[Path] = []
    qemu_hardware_files: list[Path] = []

    if not root.exists():
        artifact_issues.append(
            _issue(
                "error",
                f"Output directory is missing: {root}",
                code="missing_output_directory",
                path=str(root),
            )
        )
    elif not root.is_dir():
        artifact_issues.append(
            _issue(
                "error",
                f"Output path is not a directory: {root}",
                code="invalid_output_directory",
                path=str(root),
            )
        )
    else:
        qemu_hardware_files = sorted(root.rglob("*_qemu_hardware.json"))
        if not qemu_hardware_files:
            hardware_issues.append(
                _issue(
                    "error",
                    f"No QEMU hardware JSON found under {root}",
                    code="missing_qemu_hardware_json",
                    path=str(root),
                )
            )
        for hardware_file in qemu_hardware_files:
            hardware_issues.extend(validate_qemu_hardware_json(hardware_file))

        source_files = sorted(
            path
            for path in root.rglob("*")
            if path.is_file() and path.suffix.lower() in {".c", ".h"}
        )
        if not source_files:
            artifact_issues.append(
                _issue(
                    "error",
                    f"No generated C/H files found under {root}",
                    code="missing_generated_sources",
                    path=str(root),
                )
            )
        artifact_issues.extend(validate_generated_artifact_files(source_files))

    issues = artifact_issues + hardware_issues
    return {
        "success": not any(issue["severity"] == "error" for issue in issues),
        "artifact_issues": artifact_issues,
        "hardware_issues": hardware_issues,
        "source_files": [str(path) for path in source_files],
        "qemu_hardware_files": [str(path) for path in qemu_hardware_files],
    }


def error_messages(issues: list[Issue]) -> list[str]:
    """Extract error messages from issue dictionaries."""
    return [issue["message"] for issue in issues if issue.get("severity") == "error"]


def warning_messages(issues: list[Issue]) -> list[str]:
    """Extract warning messages from issue dictionaries."""
    return [issue["message"] for issue in issues if issue.get("severity") == "warning"]


def _issue(severity: str, message: str, *, code: str, path: str | None = None) -> Issue:
    issue: Issue = {
        "severity": severity,
        "code": code,
        "message": message,
    }
    if path is not None:
        issue["path"] = path
    return issue
