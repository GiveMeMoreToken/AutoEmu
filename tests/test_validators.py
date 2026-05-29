"""Tests for all validators (register, behavior, compile, meson)."""

from __future__ import annotations

from pathlib import Path


from autoemu.models.register import BitField, Register, RegisterBlock
from autoemu.models.peripheral import Peripheral
from autoemu.validators.register_validator import validate_register_block
from autoemu.validators.behavior_validator import validate_behavior, replay_register_sequence
from autoemu.validators.compile_validator import (
    QEMU_GIT_URL,
    find_qemu_include_paths,
    resolve_qemu_source_dir,
    validate_compile,
    validate_meson_build,
)


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
    def test_finds_qemu_source_from_environment(self, monkeypatch, tmp_path):
        fake_qemu = tmp_path / "external-qemu"
        (fake_qemu / "include" / "qemu").mkdir(parents=True)
        monkeypatch.setenv("AUTOEMU_QEMU_SRC", str(fake_qemu))

        assert resolve_qemu_source_dir() == fake_qemu
        assert str(fake_qemu / "include") in find_qemu_include_paths()

    def test_latest_qemu_source_clones_to_managed_cache(self, monkeypatch, tmp_path):
        cache_dir = tmp_path / "cache" / "qemu-latest"
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            (cache_dir / "include" / "qemu").mkdir(parents=True)

            class _Result:
                returncode = 0
                stderr = ""

            return _Result()

        monkeypatch.setenv("AUTOEMU_QEMU_CACHE_DIR", str(cache_dir))
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/git" if name == "git" else None)
        monkeypatch.setattr("subprocess.run", fake_run)

        assert resolve_qemu_source_dir("latest") == cache_dir
        assert Path(calls[0][0]).name == "git"
        assert calls[0][1:4] == ["clone", "--depth", "1"]
        assert QEMU_GIT_URL in calls[0]

    def test_no_qemu_source_warning_mentions_latest_resolver(self):
        result = validate_compile(["dummy.c"], qemu_src=Path("/nonexistent"))
        assert result["success"] is True
        assert result["files_checked"] == 0
        assert "AUTOEMU_QEMU_SRC=latest" in result["warnings"][0]

    def test_valid_c_file(self, tmp_path):
        c_file = tmp_path / "valid.c"
        c_file.write_text("int main(void) { return 0; }\n")
        fake_qemu = tmp_path / "qemu_src"
        (fake_qemu / "include" / "qemu").mkdir(parents=True)
        result = validate_compile([str(c_file)], qemu_src=fake_qemu)
        assert result["success"] is True
        assert result["files_checked"] == 1

    def test_invalid_c_file(self, tmp_path):
        c_file = tmp_path / "bad.c"
        c_file.write_text("int main(void { return 0; }\n")
        fake_qemu = tmp_path / "qemu_src"
        (fake_qemu / "include" / "qemu").mkdir(parents=True)
        result = validate_compile([str(c_file)], qemu_src=fake_qemu)
        assert result["success"] is False
        assert len(result["errors"]) == 1

    def test_header_file_is_checked_with_qemu_osdep_first(self, tmp_path):
        fake_qemu = tmp_path / "qemu_src"
        (fake_qemu / "include" / "qemu").mkdir(parents=True)
        (fake_qemu / "include" / "hw" / "core").mkdir(parents=True)
        (fake_qemu / "include" / "qom").mkdir(parents=True)
        (fake_qemu / "include" / "qemu" / "osdep.h").write_text(
            "#define QEMU_OSDEP_INCLUDED 1\n",
            encoding="utf-8",
        )
        (fake_qemu / "include" / "hw" / "core" / "sysbus.h").write_text(
            "#ifndef QEMU_OSDEP_INCLUDED\n"
            '#error "qemu/osdep.h must be included first"\n'
            "#endif\n"
            "typedef struct SysBusDevice SysBusDevice;\n",
            encoding="utf-8",
        )
        (fake_qemu / "include" / "qom" / "object.h").write_text(
            "#define OBJECT_DECLARE_SIMPLE_TYPE(TypeName, MODULE_OBJ_NAME) "
            "typedef struct TypeName TypeName;\n",
            encoding="utf-8",
        )
        header = tmp_path / "test_device.h"
        header.write_text(
            '#include "hw/core/sysbus.h"\n'
            '#include "qom/object.h"\n'
            "#define TYPE_TEST_DEVICE \"test-device\"\n"
            "OBJECT_DECLARE_SIMPLE_TYPE(TestDeviceState, TEST_DEVICE)\n",
            encoding="utf-8",
        )

        result = validate_compile([str(header)], qemu_src=fake_qemu)

        assert result["success"] is True
        assert result["files_checked"] == 1
        assert result["errors"] == []
        assert result["warnings"] == []

    def test_skips_non_c_files(self, tmp_path):
        (tmp_path / "readme.txt").write_text("hello")
        fake_qemu = tmp_path / "qemu_src"
        (fake_qemu / "include" / "qemu").mkdir(parents=True)
        result = validate_compile([str(tmp_path / "readme.txt")], qemu_src=fake_qemu)
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
