"""Source-data fetchers for AutoEmu."""

from autoemu.fetchers.stm32 import (
    AssetRequest,
    DuckDuckGoSearcher,
    FetchRequest,
    FetchResult,
    FetchedInputBundle,
    ResolvedArtifact,
    STM32DataFetcher,
    build_asset_requests,
    infer_stm32_device_stem,
    infer_stm32_driver_prefix,
    infer_stm32_header_name,
    infer_stm32_mcu_family,
    normalize_stm32_target_mcu,
    normalize_target_peripheral,
    peripheral_search_tokens,
    resolve_fetched_input_bundle,
)

__all__ = [
    "AssetRequest",
    "DuckDuckGoSearcher",
    "FetchRequest",
    "FetchResult",
    "FetchedInputBundle",
    "ResolvedArtifact",
    "STM32DataFetcher",
    "build_asset_requests",
    "infer_stm32_device_stem",
    "infer_stm32_driver_prefix",
    "infer_stm32_header_name",
    "infer_stm32_mcu_family",
    "normalize_stm32_target_mcu",
    "normalize_target_peripheral",
    "peripheral_search_tokens",
    "resolve_fetched_input_bundle",
]
