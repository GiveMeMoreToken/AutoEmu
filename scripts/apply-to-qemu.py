#!/usr/bin/env python3
"""Apply AutoEmu generated peripheral files into a QEMU source tree.

This script copies the generated C/H files, rewrites includes to match QEMU
conventions, and updates meson.build (and Kconfig when present) so the new
device is built in-tree.

Example:
    ./scripts/apply-to-qemu.py output/ --qemu-src ~/src/qemu --target-dir hw/arm
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

from autoemu.generators.machine_patcher import apply_machine_patch


def _snake(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]", "_", name)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    return s.lower().strip("_")


def _device_prefix(mcu_family: str) -> str:
    family = (mcu_family or "").strip()
    if not family:
        return "autoemu"
    return _snake(family)


def _find_generated_files(output_dir: Path, peripheral_name: str) -> dict[str, Path]:
    """Locate generated C, H, and meson.build files for a peripheral."""
    snake = _snake(peripheral_name)
    files: dict[str, Path] = {}

    # Try to find files with a known prefix pattern
    candidates = list(output_dir.glob(f"*_{snake}.c"))
    if not candidates:
        # Fall back to any .c file that looks like it belongs to this peripheral
        candidates = [p for p in output_dir.glob("*.c") if snake in p.name]

    if candidates:
        c_file = candidates[0]
        pfx = c_file.name[: c_file.name.index(f"_{snake}.c")]
        files["c"] = c_file
        h_file = output_dir / f"{pfx}_{snake}.h"
        if h_file.exists():
            files["h"] = h_file
        meson_file = output_dir / "meson.build"
        if meson_file.exists():
            files["meson"] = meson_file
    return files


def _infer_target_dir(peripheral_json: Path) -> str:
    """Infer QEMU target subdirectory from peripheral model metadata."""
    platform_hint = "generic"
    if peripheral_json.exists():
        try:
            data = json.loads(peripheral_json.read_text(encoding="utf-8"))
            mcu = (data.get("mcu_family", "") or "").lower()
            platform_hint = mcu
        except Exception:
            pass

    if "stm32" in platform_hint or "arm" in platform_hint:
        return "hw/arm"
    if "mips" in platform_hint:
        return "hw/mips"
    if "riscv" in platform_hint:
        return "hw/riscv"
    if "x86" in platform_hint:
        return "hw/i386"
    return "hw/misc"


def _rewrite_include(c_text: str, include_path: str) -> str:
    """Rewrite the local header include to the QEMU tree path."""
    # Match #include "something.h" where something.h is the generated header
    pattern = re.compile(r'(#include\s+")([^"]+\.h)(")')

    def replacer(m: re.Match) -> str:
        header_name = m.group(2)
        # Only rewrite if it's a simple basename (not already a tree path)
        if "/" not in header_name:
            return f'{m.group(1)}{include_path}{m.group(3)}'
        return m.group(0)

    return pattern.sub(replacer, c_text)


def _extract_meson_line(meson_path: Path) -> str | None:
    """Extract the actual system_ss.add line from the generated meson.build."""
    if not meson_path.exists():
        return None
    text = meson_path.read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("system_ss.add("):
            return line
    return None


def _extract_kconfig_block(meson_path: Path) -> str | None:
    """Extract Kconfig snippet from meson.build comments."""
    if not meson_path.exists():
        return None
    text = meson_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    in_kconfig = False
    kconfig_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# Add to hw/") and "Kconfig" in stripped:
            in_kconfig = True
            continue
        if in_kconfig:
            if stripped.startswith("#"):
                kconfig_lines.append(stripped.lstrip("# ").lstrip("#").lstrip())
            elif not stripped:
                break
            else:
                break
    if kconfig_lines:
        return "\n".join(kconfig_lines)
    return None


def _read_existing_entries(path: Path) -> set[str]:
    """Return a set of existing file basenames already referenced in meson.build."""
    if not path.exists():
        return set()
    text = path.read_text(encoding="utf-8")
    return set(re.findall(r"files\(\s*'([^']+\.c)'\s*\)", text))


def _kconfig_has_entry(path: Path, symbol: str) -> bool:
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    return re.search(rf"\bconfig\s+{re.escape(symbol)}\b", text) is not None


def apply(output_dir: Path, qemu_src: Path, target_dir: str, dry_run: bool) -> int:
    """Perform the apply operation. Returns exit code."""
    if not qemu_src.is_dir():
        print(f"ERROR: QEMU source directory not found: {qemu_src}", file=sys.stderr)
        return 1

    qemu_include = qemu_src / "include" / "qemu"
    if not qemu_include.is_dir():
        print(
            f"ERROR: {qemu_src} does not look like a QEMU source tree "
            f"(missing include/qemu)",
            file=sys.stderr,
        )
        return 1

    dest_dir = qemu_src / target_dir
    if not dest_dir.is_dir():
        print(f"ERROR: Target directory not found: {dest_dir}", file=sys.stderr)
        return 1

    # Find peripheral JSON to get the name and metadata
    peripheral_jsons = list(output_dir.glob("*_peripheral.json"))
    if not peripheral_jsons:
        print(f"ERROR: No *_peripheral.json found in {output_dir}", file=sys.stderr)
        return 1

    peripheral_json = peripheral_jsons[0]
    try:
        pdata = json.loads(peripheral_json.read_text(encoding="utf-8"))
        peripheral_name = pdata.get("name", peripheral_json.name.replace("_peripheral.json", ""))
    except Exception:
        peripheral_name = peripheral_json.name.replace("_peripheral.json", "")

    files = _find_generated_files(output_dir, peripheral_name)
    if "c" not in files:
        print(f"ERROR: No generated C file found for {peripheral_name}", file=sys.stderr)
        return 1

    c_src = files["c"]
    h_src = files.get("h")
    meson_src = files.get("meson")

    pfx = c_src.name[: c_src.name.index(f"_{_snake(peripheral_name)}.c")]
    snake = _snake(peripheral_name)
    c_dest_name = f"{pfx}_{snake}.c"
    h_dest_name = f"{pfx}_{snake}.h"

    # Determine include path inside QEMU tree
    include_path = f"{target_dir}/{h_dest_name}"

    print(f"[apply] Peripheral: {peripheral_name}")
    print(f"[apply] QEMU source: {qemu_src}")
    print(f"[apply] Target dir:  {target_dir}")
    print(f"[apply] C file:      {c_src.name} -> {target_dir}/{c_dest_name}")
    if h_src:
        print(f"[apply] H file:      {h_src.name} -> {target_dir}/{h_dest_name}")

    # Check for duplicates
    existing_meson = dest_dir / "meson.build"
    existing_entries = _read_existing_entries(existing_meson)
    if c_dest_name in existing_entries:
        print(f"[apply] WARNING: {c_dest_name} already referenced in {existing_meson}")
        print("[apply] Skipping meson.build update (use --force to override)")
        # We still copy files in case they were modified, but warn

    actions: list[str] = []

    # Copy and rewrite C file
    c_text = c_src.read_text(encoding="utf-8")
    c_text = _rewrite_include(c_text, include_path)
    c_dest = dest_dir / c_dest_name

    if dry_run:
        print(f"[dry-run] Would write {c_dest} (with rewritten include)")
    else:
        c_dest.write_text(c_text, encoding="utf-8")
        actions.append(f"written {c_dest}")

    # Copy H file
    if h_src:
        h_dest = dest_dir / h_dest_name
        if dry_run:
            print(f"[dry-run] Would copy {h_src} -> {h_dest}")
        else:
            shutil.copy2(h_src, h_dest)
            actions.append(f"copied {h_dest}")

    # Update meson.build
    meson_line = _extract_meson_line(meson_src) if meson_src else None
    if meson_line and c_dest_name not in existing_entries:
        if dry_run:
            print(f"[dry-run] Would append to {existing_meson}:")
            print(f"    {meson_line}")
        else:
            with existing_meson.open("a", encoding="utf-8") as f:
                f.write(f"\n{meson_line}\n")
            actions.append(f"updated {existing_meson}")

    # Update Kconfig if present
    kconfig_path = dest_dir / "Kconfig"
    kconfig_block = _extract_kconfig_block(meson_src) if meson_src else None
    if kconfig_block and kconfig_path.exists():
        # Extract the config symbol from the block
        m = re.search(r"config\s+(\S+)", kconfig_block)
        symbol = m.group(1) if m else ""
        if symbol and not _kconfig_has_entry(kconfig_path, symbol):
            if dry_run:
                print(f"[dry-run] Would append to {kconfig_path}:")
                for line in kconfig_block.splitlines():
                    print(f"    {line}")
            else:
                with kconfig_path.open("a", encoding="utf-8") as f:
                    f.write(f"\n{kconfig_block}\n")
                actions.append(f"updated {kconfig_path}")
        elif symbol:
            print(f"[apply] Kconfig already has {symbol}, skipping")

    print()
    if dry_run:
        print("[dry-run] No changes made. Re-run without --dry-run to apply.")
    else:
        print("[apply] Done. Actions taken:")
        for a in actions:
            print(f"  - {a}")

    print()
    print("Next steps:")
    print(f"  1. Review the files in {dest_dir}")
    print(f"  2. Run: cd {qemu_src} && ./configure --target-list=<your-target>-softmmu")
    print(f"  3. Run: cd {qemu_src} && ninja -C build (or make)")
    if meson_line:
        print(f"  4. Build the QTest: cd {qemu_src}/build && ninja {target_dir.replace('/', '_')}_qtest")
    print()
    return 0


def _find_qtest_file(output_dir: Path, peripheral_name: str) -> Path | None:
    snake = _snake(peripheral_name)
    candidates = list(output_dir.glob(f"qtest_*_{snake}.c"))
    if candidates:
        return candidates[0]
    candidates = [p for p in output_dir.glob("qtest_*.c") if snake in p.name]
    return candidates[0] if candidates else None


def _find_machine_patch(output_dir: Path, peripheral_name: str) -> Path | None:
    snake = _snake(peripheral_name)
    patch = output_dir / f"virt_{snake}.patch"
    return patch if patch.exists() else None


def _apply_machine_patch(qemu_src: Path, patch_path: Path) -> bool:
    print(f"[apply] Applying machine patch: {patch_path.name}")
    result = apply_machine_patch(qemu_src, patch_path)
    if result:
        print(f"[apply] Patch applied successfully")
    else:
        print(f"[apply] WARNING: Patch application may have failed")
    return result


def _copy_qtest(output_dir: Path, qemu_src: Path, peripheral_name: str) -> int:
    qtest_src = _find_qtest_file(output_dir, peripheral_name)
    if not qtest_src:
        print(f"[apply] ERROR: No QTest file found for {peripheral_name}", file=sys.stderr)
        return 1

    qtest_dir = qemu_src / "tests" / "qtest"
    if not qtest_dir.is_dir():
        print(f"[apply] ERROR: QTest directory not found: {qtest_dir}", file=sys.stderr)
        return 1

    qtest_dest = qtest_dir / qtest_src.name
    print(f"[apply] Copying QTest: {qtest_src.name} -> tests/qtest/{qtest_dest.name}")
    shutil.copy2(qtest_src, qtest_dest)

    # Update tests/qtest/meson.build
    meson_path = qtest_dir / "meson.build"
    if meson_path.exists():
        meson_text = meson_path.read_text(encoding="utf-8")
        # Find pattern like: qtests = { ... } or qtests += { ... }
        # Insert a new entry for our test
        test_name = qtest_src.name.replace(".c", "")
        entry_line = f"    '{test_name}': files('{qtest_src.name}'),"
        if test_name not in meson_text:
            # Find the closing brace of the qtests dict and insert before it
            lines = meson_text.splitlines(keepends=True)
            brace_depth = 0
            insert_idx = -1
            in_qtests = False
            for i, line in enumerate(lines):
                if "qtests" in line and "{" in line:
                    in_qtests = True
                if in_qtests:
                    brace_depth += line.count("{") - line.count("}")
                    if brace_depth == 0 and "}" in line:
                        insert_idx = i
                        break
            if insert_idx >= 0:
                lines.insert(insert_idx, f"{entry_line}\n")
                meson_path.write_text("".join(lines), encoding="utf-8")
                print(f"[apply] Updated {meson_path}")
            else:
                print(f"[apply] WARNING: Could not find insertion point in {meson_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Apply AutoEmu generated peripherals into a QEMU source tree."
    )
    parser.add_argument(
        "output_dir",
        type=Path,
        help="AutoEmu output directory containing generated files",
    )
    parser.add_argument(
        "--qemu-src",
        required=True,
        type=Path,
        help="Path to QEMU source tree (e.g. ~/src/qemu)",
    )
    parser.add_argument(
        "--target-dir",
        default="",
        help="QEMU subdirectory (e.g. hw/arm, hw/misc). Auto-detected if omitted.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )
    parser.add_argument(
        "--apply-machine-patch",
        action="store_true",
        help="Apply the generated virt machine patch (if present)",
    )
    parser.add_argument(
        "--copy-qtest",
        action="store_true",
        help="Copy generated QTest into tests/qtest/ and update meson.build",
    )
    args = parser.parse_args(argv)

    output_dir = args.output_dir.resolve()
    if not output_dir.is_dir():
        print(f"ERROR: Output directory not found: {output_dir}", file=sys.stderr)
        return 1

    target_dir = args.target_dir
    if not target_dir:
        peripheral_jsons = list(output_dir.glob("*_peripheral.json"))
        target_dir = _infer_target_dir(peripheral_jsons[0] if peripheral_jsons else Path())
        print(f"[apply] Auto-detected target dir: {target_dir}")

    qemu_src = args.qemu_src.resolve()
    rc = apply(output_dir, qemu_src, target_dir, args.dry_run)
    if rc != 0:
        return rc

    peripheral_name = ""
    peripheral_jsons = list(output_dir.glob("*_peripheral.json"))
    if peripheral_jsons:
        try:
            pdata = json.loads(peripheral_jsons[0].read_text(encoding="utf-8"))
            peripheral_name = pdata.get("name", peripheral_jsons[0].name.replace("_peripheral.json", ""))
        except Exception:
            pass

    if not args.dry_run and args.apply_machine_patch:
        patch_path = _find_machine_patch(output_dir, peripheral_name)
        if patch_path:
            _apply_machine_patch(qemu_src, patch_path)
        else:
            print(f"[apply] No machine patch found for {peripheral_name}")

    if not args.dry_run and args.copy_qtest:
        rc = _copy_qtest(output_dir, qemu_src, peripheral_name)
        if rc != 0:
            return rc

    return 0


if __name__ == "__main__":
    sys.exit(main())
