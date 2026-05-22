"""Tests for the generic data fetcher and input resolution."""

from __future__ import annotations

import base64
import json

from autoemu.agent.prompts import build_system_prompt
from autoemu.agent.runtime import _cleanup_stale_files
from autoemu.fetchers.generic import (
    SearchResult,
    SearchCandidate,
    GenericDataFetcher,
    DuckDuckGoSearcher,
    _check_content,
    _normalize_download_url,
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
    (data_dir / "docs").mkdir(parents=True)

    svd = data_dir / "svd" / "test.svd"
    driver = data_dir / "driver" / "test.c"
    dtsi = data_dir / "docs" / "board-gpu.dtsi"
    svd.write_text("<device></device>")
    driver.write_text("void init(void) {}\n")
    dtsi.write_text('/ { gpu: mali@1000 { reg = <0x1000 0x100>; }; };\n')

    bundle = resolve_fetched_input_bundle(
        target_mcu="TEST_MCU", target_peripheral="X", data_dir=data_dir,
    )
    assert bundle.svd_path == str(svd)
    assert len(bundle.driver_paths) == 1
    assert str(dtsi) in bundle.documentation_paths


def test_resolve_fetched_input_bundle_prefers_header_with_peripheral_registers(tmp_path):
    data_dir = tmp_path / "data"
    (data_dir / "header").mkdir(parents=True)

    board_header = data_dir / "header" / "board.h"
    regs_header = data_dir / "header" / "panfrost_regs.h"
    board_header.write_text("#define BOARD_NAME 1\n", encoding="utf-8")
    regs_header.write_text("#define GPU_ID 0x00\n#define GPU_STATUS 0x04\n", encoding="utf-8")

    bundle = resolve_fetched_input_bundle(
        target_mcu="TEST_MCU",
        target_peripheral="GPU",
        data_dir=data_dir,
    )

    assert bundle.header_path == str(regs_header)


def test_fetch_queries_request_device_tree_docs():
    fetcher = GenericDataFetcher(searcher=FakeSearcher([]))

    queries = [query for query, category in fetcher._build_queries("Hikey960", "GPU") if category == "docs"]

    assert any("device tree" in query.lower() for query in queries)
    assert any("dts" in query.lower() or "dtsi" in query.lower() for query in queries)


def test_normalizes_gitiles_device_tree_url_to_text_format():
    url = (
        "https://android.googlesource.com/kernel/hikey-linaro/+/"
        "android-hikey-linaro-4.4/arch/arm64/boot/dts/hisilicon/hi3660-gpu.dtsi"
    )

    normalized = _normalize_download_url(url)

    assert normalized.endswith("hi3660-gpu.dtsi?format=TEXT")


def test_rejects_html_device_tree_doc():
    html = b"<!DOCTYPE html><html><body>gpu: mali@E82C0000 { reg = <0x0 0xE82C0000 0x0 0x4000>; };</body></html>"

    assert _check_content(html, "docs", "hi3660-gpu.dtsi") == "HTML page, not DTS/DTSI source"


def test_fetch_selected_decodes_gitiles_text_device_tree(monkeypatch, tmp_path):
    fetcher = GenericDataFetcher(searcher=FakeSearcher([]))
    dtsi_text = b"/ { gpu: mali@E82C0000 { reg = <0x0 0xE82C0000 0x0 0x4000>; }; };\n"

    class _Response:
        def read(self):
            return base64.b64encode(dtsi_text)

    monkeypatch.setattr("autoemu.fetchers.generic._urlopen_with_retry", lambda request, timeout: _Response())

    result = fetcher.fetch_selected(
        [
            SearchCandidate(
                title="hi3660 gpu dtsi",
                url=(
                    "https://android.googlesource.com/kernel/hikey-linaro/+/"
                    "android-hikey-linaro-4.4/arch/arm64/boot/dts/hisilicon/hi3660-gpu.dtsi"
                ),
                category="docs",
                score=100,
                description="device tree",
            )
        ],
        tmp_path,
        target_mcu="Hikey960",
        target_peripheral="GPU",
    )

    assert not result.errors
    saved = tmp_path / "docs" / "hi3660-gpu.dtsi"
    assert saved.read_bytes() == dtsi_text


def test_cleanup_removes_stale_html_device_tree_docs(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    stale = docs / "hi3660-gpu.dtsi"
    stale.write_text("<!DOCTYPE html><html></html>", encoding="utf-8")

    _cleanup_stale_files(str(tmp_path), lambda message, kind="info": None)

    assert not stale.exists()


def test_fetch_selection_uses_top_k_per_file_type():
    fetcher = GenericDataFetcher(searcher=FakeSearcher([]))
    candidates = [
        SearchCandidate(
            title=f"driver {idx}",
            url=f"https://example.com/driver{idx}.c",
            category="driver",
            score=100 - idx,
            description="driver",
        )
        for idx in range(10)
    ]
    candidates.extend([
        SearchCandidate(
            title=f"header {idx}",
            url=f"https://example.com/header{idx}.h",
            category="header",
            score=80 - idx,
            description="header",
        )
        for idx in range(4)
    ])
    candidates.extend([
        SearchCandidate(
            title=f"device tree {idx}",
            url=f"https://example.com/board{idx}.dtsi",
            category="docs",
            score=20 - idx,
            description="device tree source",
        )
        for idx in range(3)
    ])
    candidates.append(SearchCandidate(
        title="reference manual",
        url="https://example.com/manual.pdf",
        category="docs",
        score=70,
        description="reference manual",
    ))

    selected = fetcher.select_candidates(candidates, limit=2)
    selected_urls = {candidate.url for candidate in selected}

    assert [candidate.url for candidate in selected if candidate.category == "driver"] == [
        "https://example.com/driver0.c",
        "https://example.com/driver1.c",
    ]
    assert "https://example.com/header0.h" in selected_urls
    assert "https://example.com/header1.h" in selected_urls
    assert "https://example.com/board0.dtsi" in selected_urls
    assert "https://example.com/board1.dtsi" in selected_urls
    assert "https://example.com/manual.pdf" in selected_urls
