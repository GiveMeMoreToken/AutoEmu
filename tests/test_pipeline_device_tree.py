"""Pipeline regressions for generic DTS/DTSI MMIO inference."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

from autoemu.pipeline import run_model_pipeline


def test_run_model_pipeline_applies_dtsi_reg_base_and_size(tmp_path):
    header = tmp_path / "uart_regs.h"
    header.write_text(
        textwrap.dedent(
            """\
            typedef struct {
              volatile uint32_t CTRL;
              volatile uint32_t STATUS;
            } UART_TypeDef;
            """
        ),
        encoding="utf-8",
    )
    dtsi = tmp_path / "board.dtsi"
    dtsi.write_text(
        textwrap.dedent(
            """\
            / {
                uart0: serial@40011000 {
                    compatible = "vendor,uart";
                    reg = <0x0 0x40011000 0x0 0x100>;
                };
            };
            """
        ),
        encoding="utf-8",
    )

    result = run_model_pipeline(
        "UART",
        output_dir=tmp_path / "output",
        header_path=header,
        documentation_paths=[dtsi],
        mcu_family="TESTMCU",
    )

    peripheral = json.loads(Path(result["peripheral_json"]).read_text(encoding="utf-8"))
    assert peripheral["base_address"] == 0x40011000
    assert peripheral["address_size"] == 0x100
    assert peripheral["register_block"]["base_address"] == 0x40011000


def test_run_model_pipeline_extracts_macro_only_register_map(tmp_path):
    header = tmp_path / "gpu_regs.h"
    header.write_text(
        textwrap.dedent(
            """\
            #define GPU_ID              0x00
            #define GPU_INT_RAWSTAT     0x20
            #define GPU_INT_CLEAR       0x24
            #define JOB_INT_RAWSTAT     0x1000
            #define JOB_INT_CLEAR       0x1004
            #define JS_BASE             0x1800
            #define JS_SLOT_STRIDE      0x80
            #define JS_HEAD_LO(n)       (JS_BASE + ((n) * JS_SLOT_STRIDE) + 0x00)
            #define JS_STATUS(n)        (JS_BASE + ((n) * JS_SLOT_STRIDE) + 0x24)
            #define JS_COMMAND_START    0x01
            #define JS_CONFIG_END_FLUSH_CLEAN_INVALIDATE (3u << 12)
            #define JS_CONFIG_THREAD_PRI(n) ((n) << 16)
            """
        ),
        encoding="utf-8",
    )
    dtsi = tmp_path / "board.dtsi"
    dtsi.write_text(
        textwrap.dedent(
            """\
            / {
                gpu: mali@E82C0000 {
                    compatible = "arm,malit6xx", "arm,mali-midgard";
                    reg = <0x0 0xE82C0000 0x0 0x4000>;
                };
            };
            """
        ),
        encoding="utf-8",
    )

    result = run_model_pipeline(
        "GPU",
        output_dir=tmp_path / "output",
        header_path=header,
        documentation_paths=[dtsi],
        mcu_family="TESTMCU",
    )

    peripheral = json.loads(Path(result["peripheral_json"]).read_text(encoding="utf-8"))
    registers = peripheral["register_block"]["registers"]
    names = {register["name"] for register in registers}
    assert peripheral["base_address"] == 0xE82C0000
    assert peripheral["address_size"] == 0x4000
    assert "GPU_ID" in names
    assert "JOB_INT_RAWSTAT" in names
    assert "JS_HEAD_LO_0" in names
    assert "JS_COMMAND_START" not in names
    assert "JS_CONFIG_END_FLUSH_CLEAN_INVALIDATE" not in names
    assert "JS_CONFIG_THREAD_PRI_1" not in names
