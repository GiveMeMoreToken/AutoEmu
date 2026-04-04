"""Tests for the MIPS platform plugin and parsers."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from autoemu.platforms.mips import MIPSPlatform
from autoemu.platforms.mips.naming import mips_snake, mips_type_name
from autoemu.platforms.mips.parsers.dt_parser import parse_device_tree_string
from autoemu.platforms.mips.parsers.header_parser import parse_mips_header_string
from autoemu.platforms.mips.parsers.kernel_driver_parser import (
    analyze_kernel_driver_string,
)
from autoemu.platforms.mips.parsers.pdf_parser import _parse_register_text
from autoemu.platforms.base import InputBundle


# ---------------------------------------------------------------------------
# PDF parser
# ---------------------------------------------------------------------------

SAMPLE_PDF_TEXT = textwrap.dedent("""\
    Register: 0x00 CTRL
    31:16 RSVD RO Reserved
    15:8 DIV R/W Clock divider
    1 EN RW Enable
    0 RST W1C Reset done

    Register: 0x04 STATUS
    7:0 FLAGS RO Status flags
""")


def test_parse_pdf_register_text():
    blocks = _parse_register_text(SAMPLE_PDF_TEXT, "UART")
    assert "UART" in blocks
    block = blocks["UART"]
    assert len(block.registers) == 2

    ctrl = block.registers[0]
    assert ctrl.name == "CTRL"
    assert ctrl.offset == 0x00
    assert len(ctrl.fields) == 4
    # Check field details
    field_names = {f.name for f in ctrl.fields}
    assert "EN" in field_names
    assert "RST" in field_names
    assert "DIV" in field_names

    # Check W1C access
    rst_field = next(f for f in ctrl.fields if f.name == "RST")
    assert rst_field.access.value == "W1C"

    status = block.registers[1]
    assert status.name == "STATUS"
    assert status.offset == 0x04


# ---------------------------------------------------------------------------
# Device tree parser
# ---------------------------------------------------------------------------

SAMPLE_DT = textwrap.dedent("""\
    / {
        uart0@10000100 {
            compatible = "ingenic,jz4780-uart";
            reg = <0x10000100 0x100>;
            interrupts = <5 6>;
            clocks = <&cgu JZ4780_CLK_UART0>;
            status = "okay";
        };
        spi@10000200 {
            compatible = "ingenic,jz4780-spi";
            reg = <0x10000200 0x40>;
            interrupts = <10>;
        };
    };
""")


def test_parse_device_tree():
    result = parse_device_tree_string(SAMPLE_DT, "uart")
    assert "uart0" in result
    info = result["uart0"]
    assert info["base_address"] == 0x10000100
    assert info["size"] == 0x100
    assert info["interrupts"] == [5, 6]
    assert "JZ4780_CLK_UART0" in info["clocks"]
    assert info["status"] == "okay"
    assert info["compatible"] == "ingenic,jz4780-uart"


def test_parse_device_tree_no_filter():
    result = parse_device_tree_string(SAMPLE_DT, "")
    # Both nodes should be returned when no filter
    assert len(result) >= 2


def test_parse_device_tree_filter_excludes():
    result = parse_device_tree_string(SAMPLE_DT, "uart")
    # SPI node should be excluded when filtering for UART
    assert "spi" not in result


# ---------------------------------------------------------------------------
# MIPS header parser
# ---------------------------------------------------------------------------

SAMPLE_HEADER = textwrap.dedent("""\
    #define UART_BASE  0x10000100

    #define UART_CTRL   (UART_BASE + 0x00)
    #define UART_STATUS (UART_BASE + 0x04)
    #define UART_DATA   (UART_BASE + 0x08)

    #define UART_CTRL_EN   (1 << 0)
    #define UART_CTRL_RST  (1 << 1)
    #define UART_CTRL_LOOP (1 << 4)
""")


def test_parse_mips_header():
    blocks = parse_mips_header_string(SAMPLE_HEADER, "UART")
    assert "UART" in blocks
    block = blocks["UART"]
    assert block.base_address == 0x10000100
    assert len(block.registers) == 3

    # Registers should be sorted by offset
    assert block.registers[0].offset == 0x00
    assert block.registers[1].offset == 0x04
    assert block.registers[2].offset == 0x08

    # Check bit fields on CTRL register
    ctrl = block.registers[0]
    assert ctrl.name == "CTRL"
    field_names = {f.name for f in ctrl.fields}
    assert "EN" in field_names
    assert "RST" in field_names
    assert "LOOP" in field_names


def test_parse_mips_header_peripheral_filter():
    header = textwrap.dedent("""\
        #define SPI_BASE  0x20000000
        #define SPI_CTRL  (SPI_BASE + 0x00)

        #define UART_BASE 0x10000100
        #define UART_CTRL (UART_BASE + 0x00)
    """)
    blocks = parse_mips_header_string(header, "UART")
    assert "UART" in blocks
    assert "SPI" not in blocks


# ---------------------------------------------------------------------------
# Kernel driver parser
# ---------------------------------------------------------------------------

SAMPLE_KERNEL_DRIVER = textwrap.dedent("""\
    #include <linux/io.h>

    static int jz4780_uart_probe(struct platform_device *pdev)
    {
        void __iomem *base;
        u32 val;
        base = devm_ioremap(&pdev->dev, res->start, resource_size(res));
        val = readl(base + UART_CTRL);
        writel(val | UART_CTRL_EN, base + UART_CTRL);
        writel(0, base + UART_STATUS);
        return 0;
    }

    static irqreturn_t jz4780_uart_irq_handler(int irq, void *dev_id)
    {
        void __iomem *base = dev_id;
        u32 status;
        status = readl(base + UART_ISR);
        writel(status, base + UART_ICR);
        return IRQ_HANDLED;
    }

    static void jz4780_uart_remove(struct platform_device *pdev)
    {
        void __iomem *base = platform_get_drvdata(pdev);
        writel(0, base + UART_CTRL);
    }
""")


def test_analyze_kernel_driver():
    analysis = analyze_kernel_driver_string(
        SAMPLE_KERNEL_DRIVER, "UART", "jz4780_uart.c"
    )
    assert analysis.peripheral_name == "UART"
    assert analysis.source_file == "jz4780_uart.c"

    # Should detect register accesses
    assert len(analysis.register_accesses) > 0
    access_types = {a.access_type for a in analysis.register_accesses}
    assert "read" in access_types
    assert "write" in access_types

    # Should detect ISR pattern
    assert len(analysis.isr_patterns) == 1
    isr = analysis.isr_patterns[0]
    assert "irq" in isr.function_name.lower()
    assert len(isr.checked_flags) > 0  # UART_ISR should be detected
    assert len(isr.cleared_flags) > 0  # UART_ICR should be detected

    # Should detect init sequence from probe
    assert len(analysis.init_sequences) >= 1
    init_names = {s.function_name for s in analysis.init_sequences}
    assert "jz4780_uart_probe" in init_names


def test_analyze_kernel_driver_infers_peripheral():
    analysis = analyze_kernel_driver_string(
        SAMPLE_KERNEL_DRIVER, "", "ingenic_uart.c"
    )
    assert analysis.peripheral_name == "UART"


# ---------------------------------------------------------------------------
# Naming conventions
# ---------------------------------------------------------------------------

def test_mips_platform_naming():
    assert mips_snake("UartController") == "uart_controller"
    assert mips_snake("SPI") == "spi"  # all-upper stays lowercase
    assert mips_snake("spi") == "spi"
    assert mips_type_name("uart") == "MIPSUARTState"
    assert mips_type_name("EthMac") == "MIPSETH_MACState"


# ---------------------------------------------------------------------------
# Platform class
# ---------------------------------------------------------------------------

def test_mips_platform_qemu_target_info():
    platform = MIPSPlatform()
    info = platform.qemu_target_info("JZ4780")
    assert info.arch == "mipsel"
    assert info.machine == "malta"
    assert info.cpu == "24Kf"
    assert "-DTARGET_MIPS" in info.extra_cflags


def test_mips_platform_naming_convention():
    platform = MIPSPlatform()
    naming = platform.naming_convention("uart")
    assert naming.file_prefix == "mips"
    assert naming.type_prefix == "MIPS"
    assert "{prefix}" in naming.qemu_type_fmt


def test_mips_platform_discover_inputs():
    platform = MIPSPlatform()
    assets = platform.discover_inputs("JZ4780", "UART")
    keys = {a.key for a in assets}
    assert "datasheet_pdf" in keys
    assert "device_tree" in keys
    assert "vendor_header" in keys
    assert "kernel_driver" in keys
    # kernel_driver should allow multiple matches
    kd = next(a for a in assets if a.key == "kernel_driver")
    assert kd.max_matches == 3


def test_mips_parse_registers_from_header(tmp_path: Path):
    """End-to-end: MIPSPlatform.parse_registers() with a header file."""
    header_file = tmp_path / "jz4780_uart.h"
    header_file.write_text(SAMPLE_HEADER)

    platform = MIPSPlatform()
    bundle = InputBundle(
        target="JZ4780",
        peripheral="UART",
        header_path=str(header_file),
    )
    blocks = platform.parse_registers(bundle)
    assert "UART" in blocks
    assert len(blocks["UART"].registers) == 3


def test_mips_parse_drivers_from_kernel_source(tmp_path: Path):
    """End-to-end: MIPSPlatform.parse_drivers() with a kernel driver file."""
    driver_file = tmp_path / "jz4780_uart.c"
    driver_file.write_text(SAMPLE_KERNEL_DRIVER)

    platform = MIPSPlatform()
    bundle = InputBundle(
        target="JZ4780",
        peripheral="UART",
        driver_paths=[str(driver_file)],
    )
    analysis = platform.parse_drivers(bundle)
    assert analysis.peripheral_name == "UART"
    assert len(analysis.register_accesses) > 0
    assert len(analysis.isr_patterns) > 0


def test_mips_parse_drivers_empty():
    """parse_drivers returns empty analysis when no driver paths given."""
    platform = MIPSPlatform()
    bundle = InputBundle(target="JZ4780", peripheral="UART")
    analysis = platform.parse_drivers(bundle)
    assert analysis.peripheral_name == "UART"
    assert len(analysis.register_accesses) == 0


def test_mips_platform_registered():
    """MIPS platform should be registered in the global registry."""
    from autoemu.platforms import get_platform
    platform = get_platform("mips")
    assert platform.name == "mips"
    assert isinstance(platform, MIPSPlatform)
