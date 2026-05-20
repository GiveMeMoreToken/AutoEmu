"""Tests for all validators (register, behavior, compile, meson)."""

from __future__ import annotations

from pathlib import Path


from autoemu.models.register import BitField, Register, RegisterBlock
from autoemu.models.peripheral import Peripheral
from autoemu.validators.register_validator import validate_register_block
from autoemu.validators.behavior_validator import validate_behavior, replay_register_sequence
from autoemu.validators.compile_validator import validate_compile, validate_meson_build


# ---------------------------------------------------------------------------
# Register validator
# ---------------------------------------------------------------------------

class TestRegisterValidator:
    def test_valid_block(self):
        block = RegisterBlock(
            name="TEST",
            registers=[Register(name="CR", offset=0x00), Register(name="SR", offset=0x04)],
        )
        assert len(validate_register_block(block)) == 0

    def test_duplicate_name(self):
        block = RegisterBlock(
            name="TEST",
            registers=[Register(name="CR", offset=0x00), Register(name="CR", offset=0x04)],
        )
        errors = [i for i in validate_register_block(block) if i["severity"] == "error"]
        assert any("Duplicate" in i["message"] for i in errors)

    def test_overlapping_fields(self):
        block = RegisterBlock(
            name="TEST",
            registers=[Register(name="CR", offset=0x00, fields=[
                BitField(name="A", bit_offset=0, bit_width=4),
                BitField(name="B", bit_offset=2, bit_width=4),
            ])],
        )
        assert any("overlapping" in i["message"] for i in validate_register_block(block))

    def test_field_exceeds_width(self):
        block = RegisterBlock(
            name="TEST",
            registers=[Register(name="CR", offset=0x00, size=8, fields=[
                BitField(name="BIG", bit_offset=4, bit_width=8),
            ])],
        )
        assert any("extends beyond" in i["message"] for i in validate_register_block(block))


# ---------------------------------------------------------------------------
# Behavior validator
# ---------------------------------------------------------------------------

class TestBehaviorValidator:
    def test_missing_register(self):
        peripheral = Peripheral(
            name="TEST",
            register_block=RegisterBlock(name="TEST", registers=[Register(name="CR", offset=0x00)]),
        )
        driver_data = {"register_accesses": [{"register": "NONEXIST"}], "isr_patterns": [], "init_sequences": []}
        issues = validate_behavior(peripheral, driver_data)
        assert any("not in the peripheral model" in i["message"] for i in issues)

    def test_replay_register_sequence(self):
        peripheral = Peripheral(
            name="TEST",
            register_block=RegisterBlock(name="TEST", registers=[
                Register(name="CR", offset=0x00, reset_value=0),
                Register(name="DR", offset=0x04, reset_value=0),
            ]),
        )
        sequence = [
            {"type": "write", "offset": 0x00, "value": 0x1234},
            {"type": "read", "offset": 0x00, "expected": 0x1234},
            {"type": "read", "offset": 0x04, "expected": 0},
        ]
        assert len(replay_register_sequence(peripheral, sequence)) == 0

    def test_replay_detects_mismatch(self):
        peripheral = Peripheral(
            name="TEST",
            register_block=RegisterBlock(name="TEST", registers=[Register(name="CR", offset=0x00, reset_value=0)]),
        )
        mismatches = replay_register_sequence(peripheral, [{"type": "read", "offset": 0x00, "expected": 0xFFFF}])
        assert len(mismatches) == 1
        assert mismatches[0]["actual"] == 0


# ---------------------------------------------------------------------------
# Compile validator
# ---------------------------------------------------------------------------

class TestCompileValidator:
    def test_valid_c_file(self, tmp_path):
        c_file = tmp_path / "valid.c"
        c_file.write_text("int main(void) { return 0; }\n")
        fake_qemu = tmp_path / "qemu_src"
        (fake_qemu / "include" / "qemu").mkdir(parents=True)
        result = validate_compile([str(c_file)], qemu_src=fake_qemu)
        assert result["success"] is True
        assert result["files_checked"] == 1

    def test_generated_tree_source_resolves_headers_from_tree_include_root(self, tmp_path):
        fake_qemu = tmp_path / "qemu_src"
        (fake_qemu / "include" / "qemu").mkdir(parents=True)

        generated_root = tmp_path / "generated"
        source = generated_root / "hw" / "misc" / "demo_device.c"
        header = generated_root / "include" / "hw" / "misc" / "demo_device.h"
        source.parent.mkdir(parents=True)
        header.parent.mkdir(parents=True)
        source.write_text(
            '#include "hw/misc/demo_device.h"\n'
            "int demo_device_value(void) { return DEMO_DEVICE_VALUE; }\n",
            encoding="utf-8",
        )
        header.write_text(
            "#pragma once\n"
            "#define DEMO_DEVICE_VALUE 7\n",
            encoding="utf-8",
        )

        result = validate_compile([source], qemu_src=fake_qemu)

        assert result["success"] is True
        assert result["files_checked"] == 1
        assert result["errors"] == []

    def test_invalid_c_file(self, tmp_path):
        c_file = tmp_path / "bad.c"
        c_file.write_text("int main(void { return 0; }\n")
        fake_qemu = tmp_path / "qemu_src"
        (fake_qemu / "include" / "qemu").mkdir(parents=True)
        result = validate_compile([str(c_file)], qemu_src=fake_qemu)
        assert result["success"] is False
        assert len(result["errors"]) == 1

    def test_skips_non_c_files(self, tmp_path):
        (tmp_path / "readme.txt").write_text("hello")
        fake_qemu = tmp_path / "qemu_src"
        (fake_qemu / "include" / "qemu").mkdir(parents=True)
        result = validate_compile([str(tmp_path / "readme.txt")], qemu_src=fake_qemu)
        assert result["files_checked"] == 0

    def test_no_qemu_source(self):
        result = validate_compile(["dummy.c"], qemu_src=Path("/nonexistent"))
        assert result["success"] is True
        assert result["files_checked"] == 0


# ---------------------------------------------------------------------------
# Meson build validator
# ---------------------------------------------------------------------------

class TestMesonValidator:
    def test_valid_meson(self, tmp_path):
        (tmp_path / "stm32.c").write_text("/* stub */\n")
        meson = tmp_path / "meson.build"
        meson.write_text("system_ss.add(when: 'CONFIG_X', if_true: files('stm32.c'))\n")
        assert validate_meson_build(meson)["valid"] is True

    def test_missing_file(self, tmp_path):
        assert validate_meson_build(tmp_path / "nonexistent")["valid"] is False

    def test_empty(self, tmp_path):
        (tmp_path / "meson.build").write_text("")
        assert validate_meson_build(tmp_path / "meson.build")["valid"] is False

    def test_missing_system_ss(self, tmp_path):
        meson = tmp_path / "meson.build"
        meson.write_text("other_call(when: 'X', if_true: files('a.c'))\n")
        assert validate_meson_build(meson)["valid"] is False

    def test_missing_c_source(self, tmp_path):
        meson = tmp_path / "meson.build"
        meson.write_text("system_ss.add(when: 'CONFIG_X', if_true: files('missing.c'))\n")
        result = validate_meson_build(meson)
        assert result["valid"] is False
        assert any("missing.c" in e for e in result["errors"])
