"""Generate QEMU machine patches and device-tree overlays for peripherals.

Patches the ARM 'virt' machine to instantiate a new sysbus device at its
MMIO base address with a GIC IRQ line, and emits a DT overlay snippet.
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

from autoemu.models.peripheral import Peripheral
from autoemu.modeling_utils import snake_case as _snake, upper_case as _upper, normalize_name


def _device_prefix(peripheral: Peripheral) -> str:
    family = (peripheral.mcu_family or "").strip()
    if not family:
        return "autoemu"
    return normalize_name(family)


def _find_enum_insert_index(lines: list[str]) -> int:
    """Find the line index before VIRT_LOWMEMMAP_LAST in virt.h enum."""
    for i, line in enumerate(lines):
        if "VIRT_LOWMEMMAP_LAST" in line:
            return i
    return -1


def _find_memmap_insert_index(lines: list[str]) -> int:
    """Find the line index after VIRT_MMIO in base_memmap[]."""
    for i, line in enumerate(lines):
        if "[VIRT_MMIO]" in line:
            return i + 1
    return -1


def _find_irqmap_insert_index(lines: list[str]) -> int:
    """Find the line index after VIRT_MMIO in a15irqmap[]."""
    for i, line in enumerate(lines):
        if "[VIRT_MMIO]" in line and "a15irqmap" not in line:
            # Need to find it in a15irqmap specifically
            pass
    # Better: scan from a15irqmap declaration
    in_irqmap = False
    for i, line in enumerate(lines):
        if "a15irqmap[]" in line:
            in_irqmap = True
        if in_irqmap and "[VIRT_MMIO]" in line:
            return i + 1
    return -1


def _find_init_function_call_index(lines: list[str]) -> int:
    """Find a good place inside virt_machine_init to call create_*()."""
    # Look for create_platform_bus(vms) call and insert after it
    for i, line in enumerate(lines):
        if "create_platform_bus(vms)" in line:
            return i + 1
    # Fallback: look for create_gpio(vms) call
    for i, line in enumerate(lines):
        if "create_gpio(vms)" in line:
            return i + 1
    return -1


def _find_create_function_insert_index(lines: list[str]) -> int:
    """Find a place to insert the new create_* helper function."""
    # Insert before machvirt_init function (QEMU 9.x naming)
    for i, line in enumerate(lines):
        if "static void machvirt_init" in line or "static void virt_machine_init" in line:
            return i
    return -1


def _replace_or_insert(
    lines: list[str],
    token: str,
    new_line: str,
    insert_index: int,
    predicate: Any | None = None,
) -> None:
    """Replace the first line containing *token* (and optionally matching
    *predicate*) with *new_line*, or insert *new_line* at *insert_index*
    when no matching line is found."""
    for i, line in enumerate(lines):
        if token in line and (predicate is None or predicate(line)):
            lines[i] = new_line
            return
    if insert_index >= 0:
        lines.insert(insert_index, new_line)


def _replace_function_or_insert(
    lines: list[str],
    sig_token: str,
    new_body: list[str],
    insert_index: int,
) -> None:
    """Replace an existing function starting with *sig_token* with *new_body*,
    or insert *new_body* at *insert_index* when the signature is absent."""
    start = -1
    for i, line in enumerate(lines):
        if sig_token in line:
            start = i
            break
    if start >= 0:
        # Find the matching closing brace.  We assume the function uses
        # brace-delimited blocks and track depth from the opening '{'.
        end = start + 1
        depth = 0
        while end < len(lines):
            for ch in lines[end]:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
            if depth <= 0 and "{" in lines[start:end + 1]:
                # end now points at the line with the final '}'
                break
            end += 1
        # Replace [start:end+1] with new_body
        lines[start:end + 1] = list(new_body)
    elif insert_index >= 0:
        for line in reversed(new_body):
            lines.insert(insert_index, line)


def generate_virt_patch(
    peripheral: Peripheral,
    output_dir: str | Path,
    qemu_src: str | Path,
    irq: int = 123,
) -> dict[str, Any]:
    """Generate a unified diff patch for the ARM virt machine.

    Returns dict with paths to generated patch file and overlay file.
    """
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    qemu_src_path = Path(qemu_src)

    snake = _snake(peripheral.name)
    upper = _upper(peripheral.name)
    pfx = _device_prefix(peripheral)
    pfx_upper = pfx.upper()
    type_name = f"{pfx_upper}_{upper}"
    device_type = f"{pfx}-{snake}"

    base_addr = peripheral.base_address
    addr_size = peripheral.address_size or 0x1000000
    n_irq = len(peripheral.interrupt_model.lines) if peripheral.interrupt_model else 0

    patch_parts = []

    # --- Patch include/hw/arm/virt.h ---
    virt_h = qemu_src_path / "include" / "hw" / "arm" / "virt.h"
    if virt_h.exists():
        orig_h_lines = virt_h.read_text(encoding="utf-8").splitlines(keepends=True)
        mod_h_lines = list(orig_h_lines)

        idx = _find_enum_insert_index(mod_h_lines)
        enum_line = f"    VIRT_{upper},"
        if idx >= 0 and not any(enum_line in line for line in mod_h_lines):
            mod_h_lines.insert(idx, f"    VIRT_{upper},\n")

        if orig_h_lines != mod_h_lines:
            diff_h = difflib.unified_diff(
                orig_h_lines,
                mod_h_lines,
                fromfile="include/hw/arm/virt.h",
                tofile="include/hw/arm/virt.h",
            )
            patch_parts.extend(diff_h)

    # --- Patch hw/arm/virt.c ---
    virt_c = qemu_src_path / "hw" / "arm" / "virt.c"
    if virt_c.exists():
        orig_c_lines = virt_c.read_text(encoding="utf-8").splitlines(keepends=True)
        mod_c_lines = list(orig_c_lines)

        # Update or insert into base_memmap[]
        mem_idx = _find_memmap_insert_index(mod_c_lines)
        mem_token = f"[VIRT_{upper}]"
        mem_new_line = f"    [VIRT_{upper}] =           {{ 0x{base_addr:08X}, 0x{addr_size:08X} }},\n"
        _replace_or_insert(mod_c_lines, mem_token, mem_new_line, mem_idx, predicate=lambda ln: "{" in ln)

        # Update or insert into a15irqmap[]
        irq_idx = _find_irqmap_insert_index(mod_c_lines)
        irq_new_line = f"    [VIRT_{upper}] = {irq},\n"
        _replace_or_insert(mod_c_lines, mem_token, irq_new_line, irq_idx, predicate=lambda ln: "{" not in ln)

        # Update or insert create_* helper function
        func_idx = _find_create_function_insert_index(mod_c_lines)
        func_sig = f"static void create_{snake}("
        create_func = [
            f"static void create_{snake}(const VirtMachineState *vms)\n",
            "{\n",
            f"    DeviceState *dev = qdev_new(\"{device_type}\");\n",
            "    SysBusDevice *s = SYS_BUS_DEVICE(dev);\n",
            "\n",
            "    sysbus_realize_and_unref(s, &error_fatal);\n",
            f"    sysbus_mmio_map(s, 0, vms->memmap[VIRT_{upper}].base);\n",
        ]
        for i in range(n_irq):
            create_func.append(
                f"    sysbus_connect_irq(s, {i}, qdev_get_gpio_in(vms->gic, "
                f"vms->irqmap[VIRT_{upper}] + {i}));\n"
            )
        create_func.append("}\n")
        create_func.append("\n")
        _replace_function_or_insert(mod_c_lines, func_sig, create_func, func_idx)

        # Insert call inside virt_machine_init
        call_idx = _find_init_function_call_index(mod_c_lines)
        call_line = f"create_{snake}(vms)"
        if call_idx >= 0 and not any(call_line in line for line in mod_c_lines):
            mod_c_lines.insert(call_idx, f"    create_{snake}(vms);\n")

        if orig_c_lines != mod_c_lines:
            diff_c = difflib.unified_diff(
                orig_c_lines,
                mod_c_lines,
                fromfile="hw/arm/virt.c",
                tofile="hw/arm/virt.c",
            )
            patch_parts.extend(diff_c)

    patch_text = "".join(patch_parts)
    patch_path = output_path / f"virt_{snake}.patch"
    patch_path.write_text(patch_text, encoding="utf-8")

    # --- Device tree overlay snippet ---
    dtso_lines = [
        f"/* Device-tree overlay for {peripheral.name} on virt machine */",
        "",
        "/ {",
        f"    gpu@{base_addr:08x} {{",
        f'        compatible = "{device_type}";',
        f"        reg = <0x0 0x{base_addr:08x} 0x0 0x{addr_size:08x}>;",
    ]
    if n_irq:
        dtso_lines.append(
            f"        interrupts = <GIC_SPI {irq} IRQ_TYPE_LEVEL_HIGH>;"
        )
    dtso_lines.extend([
        "    };",
        "};",
        "",
    ])
    dtso_path = output_path / f"{snake}.dtso"
    dtso_path.write_text("\n".join(dtso_lines), encoding="utf-8")

    return {
        "patch_path": str(patch_path),
        "dtso_path": str(dtso_path),
        "patch_text": patch_text,
    }


def apply_machine_patch(
    qemu_src: str | Path,
    patch_path: str | Path,
) -> bool:
    """Apply a generated unified diff patch to the QEMU source tree.

    Uses Python's difflib patch application (simple line-based).
    """
    qemu_src_path = Path(qemu_src)
    patch_file = Path(patch_path)

    if not patch_file.exists():
        return False

    patch_text = patch_file.read_text(encoding="utf-8")
    if not patch_text.strip():
        return True

    # Simple unified diff application
    # We parse the @@ headers and apply hunks line by line
    current_file: Path | None = None
    orig_lines: list[str] = []
    new_lines: list[str] = []
    in_hunk = False
    hunk_orig: list[str] = []
    hunk_new: list[str] = []
    hunk_start = 0
    hunk_orig_line = 0
    hunk_new_line = 0

    def _flush_hunk() -> None:
        nonlocal new_lines, hunk_start, hunk_orig_line, hunk_new_line
        if not current_file or not hunk_orig:
            return
        # Replace orig_lines[hunk_start:hunk_start+len(hunk_orig)] with hunk_new
        new_lines = new_lines[:hunk_start] + hunk_new + new_lines[hunk_start + len(hunk_orig):]
        hunk_start = 0
        hunk_orig.clear()
        hunk_new.clear()

    lines = patch_text.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- "):
            _flush_hunk()
            if current_file and orig_lines:
                current_file.write_text("".join(new_lines), encoding="utf-8")
            path_str = line[4:].strip().split("\t")[0]
            current_file = qemu_src_path / path_str
            orig_lines = []
            new_lines = []
            if current_file.exists():
                orig_lines = current_file.read_text(encoding="utf-8").splitlines(keepends=True)
                new_lines = list(orig_lines)
        elif line.startswith("+++ "):
            pass
        elif line.startswith("@@"):
            _flush_hunk()
            in_hunk = True
            # Parse @@ -start,count +start,count @@
            parts = line.split("@@")
            if len(parts) >= 3:
                minus = parts[1].split()[0]  # -start,count
                start_str = minus.split(",")[0].lstrip("-")
                try:
                    hunk_start = int(start_str) - 1
                except ValueError:
                    hunk_start = 0
            hunk_orig_line = 0
            hunk_new_line = 0
        elif in_hunk and current_file:
            if line.startswith(" "):
                hunk_orig.append(line[1:])
                hunk_new.append(line[1:])
            elif line.startswith("-"):
                hunk_orig.append(line[1:])
            elif line.startswith("+"):
                hunk_new.append(line[1:])
            elif line.strip() == "":
                # Empty line in hunk context
                hunk_orig.append(line)
                hunk_new.append(line)
        i += 1

    _flush_hunk()
    if current_file and new_lines:
        current_file.write_text("".join(new_lines), encoding="utf-8")
        return True
    return False
