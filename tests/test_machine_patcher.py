"""Tests for the QEMU machine patcher."""

from __future__ import annotations

from pathlib import Path

import pytest

from autoemu.generators.machine_patcher import (
    _replace_function_or_insert,
    apply_machine_patch,
    generate_virt_patch,
)
from autoemu.models.peripheral import Peripheral
from autoemu.models.register import RegisterBlock


def test_replace_function_or_insert_allman_style():
    """Allman-style function (brace on its own line) should be fully replaced."""
    lines = [
        "static void foo(void)\n",
        "{\n",
        "    return;\n",
        "}\n",
        "\n",
        "static void bar(void)\n",
    ]
    new_body = [
        "static void foo(void)\n",
        "{\n",
        "    /* replaced */\n",
        "}\n",
    ]
    _replace_function_or_insert(lines, "static void foo(", new_body, 0)
    assert lines == [
        "static void foo(void)\n",
        "{\n",
        "    /* replaced */\n",
        "}\n",
        "\n",
        "static void bar(void)\n",
    ]


def test_replace_function_or_insert_knr_style():
    """K&R-style function (brace on same line as signature) should be fully replaced."""
    lines = [
        "static void foo(void) {\n",
        "    return;\n",
        "}\n",
        "\n",
    ]
    new_body = [
        "static void foo(void)\n",
        "{\n",
        "    /* replaced */\n",
        "}\n",
    ]
    _replace_function_or_insert(lines, "static void foo(", new_body, 0)
    assert lines == [
        "static void foo(void)\n",
        "{\n",
        "    /* replaced */\n",
        "}\n",
        "\n",
    ]


def test_replace_function_or_insert_corrupted_no_brace():
    """A corrupted function with no opening brace should only replace the signature."""
    lines = [
        "static void foo(void)\n",
        "    return;\n",
        "}\n",
        "\n",
    ]
    new_body = [
        "static void foo(void)\n",
        "{\n",
        "    /* replaced */\n",
        "}\n",
    ]
    _replace_function_or_insert(lines, "static void foo(", new_body, 0)
    assert lines == [
        "static void foo(void)\n",
        "{\n",
        "    /* replaced */\n",
        "}\n",
        "    return;\n",
        "}\n",
        "\n",
    ]


def test_generate_virt_patch_skips_when_already_patched(tmp_path):
    """If virt.c already contains create_gpu, patch generation should skip."""
    qemu_src = tmp_path / "qemu"
    qemu_src.mkdir()
    virt_c = qemu_src / "hw" / "arm"
    virt_c.mkdir(parents=True)
    virt_h = qemu_src / "include" / "hw" / "arm"
    virt_h.mkdir(parents=True)

    virt_c_file = virt_c / "virt.c"
    virt_c_file.write_text(
        'static void create_gpu(const VirtMachineState *vms)\n{\n}\n',
        encoding="utf-8",
    )
    # Write a minimal virt.h with the required enum
    virt_h_file = virt_h / "virt.h"
    virt_h_file.write_text(
        'enum { VIRT_LOWMEMMAP_LAST };\n',
        encoding="utf-8",
    )

    peripheral = Peripheral(
        name="GPU",
        base_address=0xE82C0000,
        address_size=0x4000,
        register_block=RegisterBlock(name="GPU", base_address=0xE82C0000),
    )

    result = generate_virt_patch(peripheral, tmp_path / "output", qemu_src)

    assert result["already_patched"] is True
    assert "Patch skipped" in result["patch_text"]


def test_apply_machine_patch_uses_external_patch_command(tmp_path, monkeypatch):
    """apply_machine_patch should prefer the external patch command."""
    qemu_src = tmp_path / "qemu"
    qemu_src.mkdir()
    target = qemu_src / "foo.c"
    target.write_text("int main() { return 0; }\n", encoding="utf-8")

    patch = tmp_path / "test.patch"
    patch.write_text(
        "--- a/foo.c\n"
        "+++ b/foo.c\n"
        "@@ -1 +1 @@\n"
        "-int main() { return 0; }\n"
        "+int main() { return 1; }\n",
        encoding="utf-8",
    )

    ran_external = False

    def fake_run(cmd, **kwargs):
        nonlocal ran_external
        if cmd[0].endswith("patch"):
            ran_external = True
            import subprocess
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise RuntimeError("unexpected command")

    monkeypatch.setattr("subprocess.run", fake_run)

    result = apply_machine_patch(qemu_src, patch)
    assert result is True
    assert ran_external is True
