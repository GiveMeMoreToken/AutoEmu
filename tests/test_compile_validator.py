"""Tests for compile_validator module."""

from __future__ import annotations

from pathlib import Path

from autoemu.validators.compile_validator import find_qemu_include_paths, validate_compile


def test_find_qemu_include_paths_missing_source():
    """Returns empty list when QEMU source directory does not exist."""
    result = find_qemu_include_paths(Path("/nonexistent/qemu/path"))
    assert result == []


def test_validate_compile_no_qemu_source():
    """Returns success with warning when QEMU source tree is absent."""
    result = validate_compile(
        ["dummy.c"],
        qemu_src=Path("/nonexistent/qemu/path"),
    )
    assert result["success"] is True
    assert result["files_checked"] == 0
    assert len(result["warnings"]) == 1
    assert "not found" in result["warnings"][0]


def test_validate_compile_valid_c_file(tmp_path):
    """A trivially valid C file compiles without errors."""
    c_file = tmp_path / "valid.c"
    c_file.write_text("int main(void) { return 0; }\n")

    # Create a fake QEMU include tree so the validator does not skip
    fake_qemu = tmp_path / "qemu_src"
    (fake_qemu / "include" / "qemu").mkdir(parents=True)

    result = validate_compile([str(c_file)], qemu_src=fake_qemu)
    assert result["success"] is True
    assert result["files_checked"] == 1
    assert result["errors"] == []


def test_validate_compile_invalid_c_file(tmp_path):
    """A C file with a syntax error is reported as an error."""
    c_file = tmp_path / "bad.c"
    c_file.write_text("int main(void { return 0; }\n")  # missing closing paren

    fake_qemu = tmp_path / "qemu_src"
    (fake_qemu / "include" / "qemu").mkdir(parents=True)

    result = validate_compile([str(c_file)], qemu_src=fake_qemu)
    assert result["success"] is False
    assert result["files_checked"] == 1
    assert len(result["errors"]) == 1
    assert result["errors"][0]["file"] == str(c_file)
    assert result["errors"][0]["returncode"] != 0


def test_validate_compile_skips_non_c_files(tmp_path):
    """Non .c/.h files are silently skipped."""
    txt_file = tmp_path / "readme.txt"
    txt_file.write_text("hello")

    fake_qemu = tmp_path / "qemu_src"
    (fake_qemu / "include" / "qemu").mkdir(parents=True)

    result = validate_compile([str(txt_file)], qemu_src=fake_qemu)
    assert result["success"] is True
    assert result["files_checked"] == 0


def test_validate_compile_h_file(tmp_path):
    """A valid .h file is checked and passes."""
    h_file = tmp_path / "test.h"
    h_file.write_text("#ifndef TEST_H\n#define TEST_H\ntypedef int foo_t;\n#endif\n")

    fake_qemu = tmp_path / "qemu_src"
    (fake_qemu / "include" / "qemu").mkdir(parents=True)

    result = validate_compile([str(h_file)], qemu_src=fake_qemu)
    assert result["success"] is True
    assert result["files_checked"] == 1
