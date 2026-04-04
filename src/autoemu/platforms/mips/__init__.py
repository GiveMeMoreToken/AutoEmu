"""MIPS platform plugin for AutoEmu."""
from __future__ import annotations

from autoemu.platforms.base import (
    AssetDescriptor,
    InputBundle,
    NamingInfo,
    Platform,
    QEMUTargetInfo,
)
from autoemu.models.register import RegisterBlock
from autoemu.parsers.driver_parser import DriverAnalysis


class MIPSPlatform(Platform):
    name = "mips"

    def discover_inputs(self, mcu: str, peripheral: str) -> list[AssetDescriptor]:
        """Return fetchable asset descriptors for MIPS targets.

        MIPS inputs: PDF datasheets, device tree files, vendor headers,
        kernel drivers.
        """
        return [
            AssetDescriptor(
                key="datasheet_pdf",
                category="docs",
                description="Vendor datasheet PDF",
                file_extensions=(".pdf",),
            ),
            AssetDescriptor(
                key="device_tree",
                category="dt",
                description="Device tree source",
                file_extensions=(".dts", ".dtsi"),
            ),
            AssetDescriptor(
                key="vendor_header",
                category="headers",
                description="Vendor C header",
                file_extensions=(".h",),
            ),
            AssetDescriptor(
                key="kernel_driver",
                category="drivers_kernel",
                description="Linux kernel driver",
                file_extensions=(".c",),
                max_matches=3,
            ),
        ]

    def parse_registers(self, bundle: InputBundle) -> dict[str, RegisterBlock]:
        """Parse register descriptions from MIPS-specific input formats."""
        from autoemu.platforms.mips.parsers.dt_parser import parse_device_tree
        from autoemu.platforms.mips.parsers.header_parser import parse_mips_header
        from autoemu.platforms.mips.parsers.pdf_parser import parse_pdf_register_tables

        blocks: dict[str, RegisterBlock] = {}
        # Try each source in priority order
        if bundle.extra.get("pdf_path"):
            blocks.update(
                parse_pdf_register_tables(bundle.extra["pdf_path"], bundle.peripheral)
            )
        if bundle.header_path:
            blocks.update(parse_mips_header(bundle.header_path, bundle.peripheral))
        if bundle.extra.get("dt_path"):
            dt_info = parse_device_tree(bundle.extra["dt_path"], bundle.peripheral)
            # DT provides base addresses and interrupt info, not full register maps
            for name, block in blocks.items():
                if name in dt_info and not block.base_address:
                    block.base_address = dt_info[name].get("base_address", 0)
        return blocks

    def parse_drivers(self, bundle: InputBundle) -> DriverAnalysis:
        """Analyze Linux kernel drivers in MIPS style (readl/writel)."""
        from autoemu.platforms.mips.parsers.kernel_driver_parser import (
            analyze_kernel_driver,
        )

        analyses = [
            analyze_kernel_driver(p, bundle.peripheral) for p in bundle.driver_paths
        ]
        if not analyses:
            return DriverAnalysis(peripheral_name=bundle.peripheral, source_file="")
        # Merge multiple analyses
        merged = analyses[0]
        for a in analyses[1:]:
            merged.register_accesses.extend(a.register_accesses)
            merged.isr_patterns.extend(a.isr_patterns)
            merged.init_sequences.extend(a.init_sequences)
        return merged

    def qemu_target_info(self, mcu: str) -> QEMUTargetInfo:
        """Return QEMU target info for MIPS."""
        return QEMUTargetInfo(
            arch="mipsel",
            machine="malta",  # common MIPS dev board
            cpu="24Kf",
            include_paths=[],
            extra_cflags=["-DTARGET_MIPS"],
        )

    def naming_convention(self, peripheral: str) -> NamingInfo:
        """Return MIPS naming conventions."""
        return NamingInfo(
            file_prefix="mips",
            type_prefix="MIPS",
            qemu_type_fmt="{prefix}-{snake}",
        )


# Register with platform registry
try:
    from autoemu.platforms import register_platform

    register_platform("mips", MIPSPlatform)
except ImportError:
    pass
