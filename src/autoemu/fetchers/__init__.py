"""Source-data fetchers for AutoEmu."""

from autoemu.fetchers.generic import (
    DuckDuckGoSearcher,
    FetchedInputBundle,
    GenericDataFetcher,
    GenericFetchResult,
    SearchCandidate,
    collect_cached_input_artifacts,
    infer_stm32_mcu_family,
    normalize_target_peripheral,
    peripheral_search_tokens,
    resolve_fetched_input_bundle,
)

__all__ = [
    "DuckDuckGoSearcher",
    "FetchedInputBundle",
    "GenericDataFetcher",
    "GenericFetchResult",
    "SearchCandidate",
    "collect_cached_input_artifacts",
    "infer_stm32_mcu_family",
    "normalize_target_peripheral",
    "peripheral_search_tokens",
    "resolve_fetched_input_bundle",
]
