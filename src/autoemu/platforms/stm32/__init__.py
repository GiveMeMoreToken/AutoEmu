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
from autoemu.fetchers.stm32 import (
    FetchRequest,
    build_asset_requests,
    infer_stm32_mcu_family,
)
from autoemu.pipeline import merge_driver_analyses


class STM32Platform(Platform):
    name = "stm32"

    def discover_inputs(self, mcu: str, peripheral: str) -> list[AssetDescriptor]:
        request = FetchRequest(target_mcu=mcu, target_peripheral=peripheral)
        assets = build_asset_requests(request)
        return [
            AssetDescriptor(
                key=a.key,
                category=a.category,
                description=a.description,
                queries=a.queries,
                preferred_domains=a.preferred_domains,
                required=a.required,
                max_matches=a.max_matches,
                file_extensions=a.file_extensions,
            )
            for a in assets
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
