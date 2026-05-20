"""Tests for the generic data fetcher and input resolution."""

from __future__ import annotations

import json

from autoemu.agent.prompts import build_system_prompt
from autoemu.fetchers.generic import (
    SearchResult,
    GenericDataFetcher,
    DuckDuckGoSearcher,
    _score_result,
    infer_stm32_mcu_family,
    normalize_target_peripheral,
    peripheral_search_tokens,
    resolve_fetched_input_bundle,
)


class FakeSearcher(DuckDuckGoSearcher):
    """Searcher that returns canned results without hitting the network."""

    def __init__(self, results: list[SearchResult] | None = None) -> None:
        super().__init__()
        self._results = results or []

    def search(self, query: str, *, max_results: int = 8):
        return self._results[:max_results]


def test_build_system_prompt_includes_fetch_source_policy():
    prompt = build_system_prompt(mode="fetch")
    assert "trustworthy STM32 input data" in prompt
    assert "Never invent URLs" in prompt


def test_infer_stm32_mcu_family():
    assert infer_stm32_mcu_family("STM32F407VG") == "STM32F4"
    assert infer_stm32_mcu_family("STM32WL55JC") == "STM32WL"
    assert infer_stm32_mcu_family("HIKEY960") == "HIKEY960"


def test_normalize_and_tokenize():
    assert normalize_target_peripheral("usb_otg_fs") == "USBOTGFS"
    assert peripheral_search_tokens("USB_OTG_FS") == ["usb", "otg", "fs", "usbotgfs"]


def test_generic_fetcher_discover_with_no_results():
    fetcher = GenericDataFetcher(searcher=FakeSearcher([]))
    candidates = fetcher.discover_candidates("UNKNOWN_MCU", "UNKNOWN_PERIPHERAL")
    assert candidates == []


def test_generic_fetcher_discover_scores_candidates():
    fake_results = [
        SearchResult(title="Kirin 960 GPU Mali driver", url="https://github.com/foo/mali.c"),
        SearchResult(title="Random page", url="https://example.com/page"),
    ]
    fetcher = GenericDataFetcher(searcher=FakeSearcher(fake_results))
    candidates = fetcher.discover_candidates("HIKEY960", "GPU")
    assert len(candidates) > 0
    # Higher-scored candidates come first
    scores = [c.score for c in candidates]
    assert scores == sorted(scores, reverse=True)


def test_header_scoring_prefers_linux_regmap_headers():
    linux_header = SearchResult(
        title="Linux kernel panfrost GPU register definitions",
        url="https://raw.githubusercontent.com/torvalds/linux/master/drivers/gpu/drm/panfrost/panfrost_regs.h",
    )
    generic_page = SearchResult(
        title="GPU overview",
        url="https://example.com/gpu",
    )

    assert _score_result(linux_header, "header", "ExampleSoC", "GPU") > _score_result(
        generic_page, "header", "ExampleSoC", "GPU"
    )


def test_build_queries_include_generic_linux_regmap_headers():
    fetcher = GenericDataFetcher(searcher=FakeSearcher([]))

    queries = fetcher._build_queries("ExampleSoC", "GPU")
    header_queries = [query for query, category in queries if category == "header"]

    assert any("linux" in query.lower() and "regs.h" in query.lower() for query in header_queries)
    assert any("regmap" in query.lower() for query in header_queries)


def test_generic_fetcher_has_no_hardware_specific_seed_urls():
    assert GenericDataFetcher._KNOWN_DRIVER_URLS == []


def test_resolve_fetched_input_bundle_from_manifest(tmp_path):
    data_dir = tmp_path / "data"
    (data_dir / "manifests").mkdir(parents=True)

    svd = data_dir / "device.svd"
    header = data_dir / "device.h"
    driver = data_dir / "driver.c"
    svd.write_text("<device></device>")
    header.write_text("#define TEST 1\n")
    driver.write_text("void init(void) {}\n")

    manifest = {
        "target_mcu": "TEST_MCU",
        "target_peripheral": "X",
        "artifacts": [
            {"category": "svd", "status": "downloaded", "local_path": str(svd)},
            {"category": "headers", "status": "downloaded", "local_path": str(header)},
            {"category": "drivers_hal", "status": "downloaded", "local_path": str(driver)},
        ],
    }
    manifest_path = data_dir / "manifests" / "test_mcu_x.json"
    manifest_path.write_text(json.dumps(manifest))

    bundle = resolve_fetched_input_bundle(
        target_mcu="TEST_MCU", target_peripheral="X", data_dir=data_dir,
    )
    assert bundle.svd_path == str(svd)
    assert bundle.header_path == str(header)
    assert len(bundle.driver_paths) == 1


def test_resolve_fetched_input_bundle_flat_layout(tmp_path):
    """Resolves files from generic flat layout: data/{svd,driver,...}/"""
    data_dir = tmp_path / "data"
    (data_dir / "svd").mkdir(parents=True)
    (data_dir / "driver").mkdir(parents=True)

    svd = data_dir / "svd" / "test.svd"
    driver = data_dir / "driver" / "test.c"
    svd.write_text("<device></device>")
    driver.write_text("void init(void) {}\n")

    bundle = resolve_fetched_input_bundle(
        target_mcu="TEST_MCU", target_peripheral="X", data_dir=data_dir,
    )
    assert bundle.svd_path == str(svd)
    assert len(bundle.driver_paths) == 1


def test_resolve_fetched_input_bundle_prefers_peripheral_register_header(tmp_path):
    data_dir = tmp_path / "data"
    (data_dir / "header").mkdir(parents=True)
    (data_dir / "driver").mkdir(parents=True)

    stale_board_header = data_dir / "header" / "board_private.h"
    register_header = data_dir / "header" / "panfrost_regs.h"
    driver = data_dir / "driver" / "panfrost_device.c"
    stale_board_header.write_text("#define BOARD_ID 0x960\n", encoding="utf-8")
    register_header.write_text(
        "#define GPU_ID 0x000\n"
        "#define GPU_STATUS 0x034\n"
        "#define GPU_INT_MASK 0x028\n",
        encoding="utf-8",
    )
    driver.write_text("void panfrost_gpu_init(void) {}\n", encoding="utf-8")

    bundle = resolve_fetched_input_bundle(
        target_mcu="Hikey960", target_peripheral="GPU", data_dir=data_dir,
    )

    assert bundle.header_path == str(register_header)
