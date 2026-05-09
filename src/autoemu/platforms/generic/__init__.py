"""Generic platform plugin for arbitrary MCU targets."""
from __future__ import annotations

from autoemu.platforms.base import (
    AssetDescriptor,
    InputBundle,
    NamingInfo,
    Platform,
    QEMUTargetInfo,
)
from autoemu.models.register import RegisterBlock
from autoemu.parsers.driver_parser import DriverAnalysis, analyze_driver_file


class GenericPlatform(Platform):
    """Platform handler for any MCU not covered by a dedicated plugin."""

    name = "generic"

    def discover_inputs(self, mcu: str, peripheral: str) -> list[AssetDescriptor]:
        """Return generic asset descriptors suitable for any MCU target."""
        return [
            AssetDescriptor(
                key="svd_file",
                category="svd",
                description=f"SVD register description for {mcu}",
                queries=(
                    f'"{mcu}" svd site:github.com',
                    f'"{mcu}" cmsis svd',
                ),
                preferred_domains=("github.com", "raw.githubusercontent.com"),
                file_extensions=(".svd", ".xml"),
            ),
            AssetDescriptor(
                key="register_header",
                category="headers",
                description=f"C header with register definitions for {mcu}",
                queries=(
                    f'"{mcu}" register header .h site:github.com',
                ),
                preferred_domains=("github.com",),
                file_extensions=(".h",),
            ),
            AssetDescriptor(
                key="datasheet",
                category="docs",
                description=f"Datasheet or reference manual for {mcu} {peripheral}",
                queries=(
                    f'"{mcu}" "{peripheral}" datasheet register map pdf',
                ),
                file_extensions=(".pdf",),
            ),
            AssetDescriptor(
                key="driver_source",
                category="drivers",
                description=f"Driver source code for {mcu} {peripheral}",
                queries=(
                    f'"{mcu}" "{peripheral}" driver .c site:github.com',
                    f'site:github.com torvalds/linux "{mcu}" OR "{peripheral}"',
                ),
                preferred_domains=("github.com",),
                file_extensions=(".c",),
                max_matches=5,
            ),
        ]

    def parse_registers(self, bundle: InputBundle) -> dict[str, RegisterBlock]:
        """Parse registers from SVD or headers using existing parsers."""
        from autoemu.parsers.register_extractor import extract_register_blocks

        blocks, _warnings = extract_register_blocks(
            svd_path=bundle.svd_path,
            header_path=bundle.header_path,
            peripheral_name=bundle.peripheral,
        )
        return blocks

    def parse_drivers(self, bundle: InputBundle) -> DriverAnalysis:
        """Analyze driver files using the generic driver parser."""
        analyses = [analyze_driver_file(p, bundle.peripheral) for p in bundle.driver_paths]
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
        """Return a generic ARM target (most common for MCUs)."""
        return QEMUTargetInfo(
            arch="arm",
            machine="virt",
            cpu="cortex-a15",
            include_paths=[],
        )

    def naming_convention(self, peripheral: str) -> NamingInfo:
        """Use a generic naming convention based on the peripheral name."""
        return NamingInfo(
            file_prefix="generic",
            type_prefix="GENERIC",
            qemu_type_fmt="{prefix}-{snake}",
        )


# Register with platform registry
try:
    from autoemu.platforms import register_platform

    register_platform("generic", GenericPlatform)
except ImportError:
    pass
