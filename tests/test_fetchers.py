"""Tests for the generic data fetcher and input resolution."""

from __future__ import annotations

import json
from pathlib import Path

from autoemu.agent.prompts import build_system_prompt
from autoemu.fetchers.generic import (
    SearchResult,
    GenericDataFetcher,
    DuckDuckGoSearcher,
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


def test_build_system_prompt_includes_agents_constraints():
    prompt = build_system_prompt(mode="fetch")
    assert "Repository constraints from AGENTS.md" in prompt
    assert "Source Policy" in prompt


def test_infer_stm32_mcu_family():
    assert infer_stm32_mcu_family("STM32F407VG") == "STM32F4"
    assert infer_stm32_mcu_family("STM32WL55JC") == "STM32WL"
    assert infer_stm32_mcu_family("HIKEY960") == "HIKEY960"


def test_normalize_and_tokenize():
    assert normalize_target_peripheral("usb_otg_fs") == "USBOTGFS"
    assert peripheral_search_tokens("USB_OTG_FS") == ["usb", "otg", "fs", "usbotgfs"]


def test_generic_fetcher_discover_with_no_results():
    fetcher = GenericDataFetcher(searcher=FakeSearcher([]))
    candidates = fetcher.discover_candidates("HIKEY960", "GPU")
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
