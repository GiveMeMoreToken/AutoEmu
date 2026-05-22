"""Tests for generic device-tree parsing."""

from __future__ import annotations

import textwrap

from autoemu.parsers.device_tree import parse_device_tree_string


def test_parse_device_tree_string_matches_label_and_64bit_reg_cells():
    content = textwrap.dedent(
        """\
        / {
            gpu: mali@E82C0000 {
                compatible = "arm,malit6xx", "arm,mali-midgard";
                reg = <0x0 0xE82C0000 0x0 0x4000>;
                interrupts = <0 258 4 0 259 4 0 260 4>;
                interrupt-names = "JOB", "MMU", "GPU";
            };
        };
        """
    )

    result = parse_device_tree_string(content, "GPU")

    assert "gpu" in result
    assert result["gpu"]["base_address"] == 0xE82C0000
    assert result["gpu"]["size"] == 0x4000
