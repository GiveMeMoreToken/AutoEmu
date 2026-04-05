"""STM32 platform plugin."""
from __future__ import annotations

from autoemu.platforms import register_platform
from autoemu.platforms.base import (
    AssetDescriptor,
    InputBundle,
    NamingInfo,
    Platform,
    QEMUTargetInfo,
)
from autoemu.models.register import RegisterBlock
from autoemu.parsers.driver_parser import DriverAnalysis, analyze_driver_file
from autoemu.parsers.register_extractor import extract_register_blocks
from autoemu.pipeline import merge_driver_analyses


class STM32Platform(Platform):
    name = "stm32"

    def discover_inputs(self, mcu: str, peripheral: str) -> list[AssetDescriptor]:
        return [
            AssetDescriptor(key="svd", category="svd", description="CMSIS-SVD file",
                            file_extensions=(".svd",)),
            AssetDescriptor(key="header", category="headers", description="CMSIS device header",
                            file_extensions=(".h",)),
            AssetDescriptor(key="driver", category="drivers", description="HAL/LL driver sources",
                            file_extensions=(".c",), max_matches=5),
            AssetDescriptor(key="docs", category="docs", description="Reference manual",
                            file_extensions=(".pdf", ".txt")),
        ]

    def parse_registers(self, bundle: InputBundle) -> dict[str, RegisterBlock]:
        blocks, _warnings = extract_register_blocks(
            svd_path=bundle.svd_path,
            header_path=bundle.header_path,
            peripheral_name=bundle.peripheral,
        )
        return blocks

    def parse_drivers(self, bundle: InputBundle) -> DriverAnalysis:
        analyses = [analyze_driver_file(p, bundle.peripheral) for p in bundle.driver_paths]
        if not analyses:
            return DriverAnalysis(peripheral_name=bundle.peripheral, source_file="")
        return merge_driver_analyses(analyses, peripheral_name=bundle.peripheral)

    def qemu_target_info(self, mcu: str) -> QEMUTargetInfo:
        return QEMUTargetInfo(
            arch="arm",
            machine="stm32f4-board",
            cpu="cortex-m4",
            include_paths=[],
        )

    def naming_convention(self, peripheral: str) -> NamingInfo:
        return NamingInfo(
            file_prefix="stm32",
            type_prefix="STM32",
            qemu_type_fmt="{prefix}-{snake}",
        )


register_platform("stm32", STM32Platform)
