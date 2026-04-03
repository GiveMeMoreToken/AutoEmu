"""Tests for generic STM32 source-data fetching."""

from __future__ import annotations

import json
from pathlib import Path

from autoemu.agent.prompts import build_system_prompt
from autoemu.fetchers.stm32 import (
    FetchRequest,
    SearchResult,
    STM32DataFetcher,
    _materialize_download_url,
    _token_present,
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


class FakeSearcher:
    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = mapping

    def search(self, query: str, *, max_results: int = 8):
        query_lower = query.lower()
        for token, path in self.mapping.items():
            if token in query_lower:
                return [SearchResult(title=Path(path).name, url=Path(path).as_uri())]
        return []


def test_build_system_prompt_includes_agents_constraints():
    prompt = build_system_prompt(mode="fetch")
    assert "Repository constraints from AGENTS.md" in prompt
    assert "Source Policy" in prompt


def test_infer_target_helpers():
    assert normalize_stm32_target_mcu("stm32f407vg") == "STM32F407VG"
    assert normalize_target_peripheral("usb_otg_fs") == "USBOTGFS"
    assert infer_stm32_mcu_family("STM32F407VG") == "STM32F4"
    assert infer_stm32_mcu_family("STM32WL55JC") == "STM32WL"
    assert infer_stm32_driver_prefix("STM32F407VG") == "stm32f4xx"
    assert infer_stm32_driver_prefix("STM32WL55JC") == "stm32wlxx"
    assert infer_stm32_device_stem("STM32F407VG") == "STM32F407"
    assert infer_stm32_header_name("STM32WL55JC") == "stm32wl55xx.h"
    assert peripheral_search_tokens("USB_OTG_FS") == ["usb", "otg", "fs", "usbotgfs"]


def test_build_asset_requests_are_generic():
    requests = build_asset_requests(
        FetchRequest(target_mcu="STM32F407VG", target_peripheral="ETH")
    )
    keys = {request.key for request in requests}
    assert keys == {
        "reference_manual",
        "datasheet",
        "svd",
        "cmsis_header",
        "hal_driver",
        "ll_driver",
        "rtos_driver",
    }
    assert all("stm32f407vg" in request.relative_path for request in requests)


def test_fetch_data_with_file_search_results(tmp_path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    reference_manual = source_dir / "reference_manual.txt"
    datasheet = source_dir / "datasheet.txt"
    svd = source_dir / "device.svd"
    header = source_dir / "stm32f407xx.h"
    hal_driver = source_dir / "stm32f4xx_hal_eth.c"

    reference_manual.write_text("reference manual", encoding="utf-8")
    datasheet.write_text("datasheet", encoding="utf-8")
    svd.write_text("<device></device>", encoding="utf-8")
    header.write_text("#define STM32F407 1\n", encoding="utf-8")
    hal_driver.write_text("void HAL_ETH_Init(void) {}\n", encoding="utf-8")

    searcher = FakeSearcher(
        {
            "reference manual": str(reference_manual),
            "datasheet": str(datasheet),
            ".svd": str(svd),
            ".h": str(header),
            " hal ": str(hal_driver),
        }
    )
    fetcher = STM32DataFetcher(searcher=searcher, enable_fallbacks=False)
    output_dir = tmp_path / "out"

    result = fetcher.fetch_data(
        target_mcu="STM32F407VG",
        target_peripheral="ETH",
        output_dir=output_dir,
    )

    assert result.success
    assert Path(result.manifest_path).exists()
    manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    assert manifest["target_mcu"] == "STM32F407VG"
    assert manifest["target_peripheral"] == "ETH"

    bundle = resolve_fetched_input_bundle(
        target_mcu="STM32F407VG",
        target_peripheral="ETH",
        data_dir=output_dir,
    )
    assert Path(bundle.header_path).exists()
    assert Path(bundle.svd_path).exists()
    assert len(bundle.driver_paths) == 1
    assert Path(bundle.driver_paths[0]).name == "stm32f4xx_hal_eth.c"


def test_resolve_fetched_input_bundle_from_manifest(tmp_path):
    data_dir = tmp_path / "data"
    target_root = data_dir / "stm32f407vg"
    (target_root / "docs").mkdir(parents=True)
    (target_root / "svd").mkdir(parents=True)
    (target_root / "headers").mkdir(parents=True)
    (target_root / "drivers" / "hal").mkdir(parents=True)
    (data_dir / "manifests").mkdir(parents=True)

    reference_manual = target_root / "docs" / "reference_manual.txt"
    svd = target_root / "svd" / "device.svd"
    header = target_root / "headers" / "stm32f407xx.h"
    driver = target_root / "drivers" / "hal" / "stm32f4xx_hal_eth.c"

    reference_manual.write_text("manual", encoding="utf-8")
    svd.write_text("<device></device>", encoding="utf-8")
    header.write_text("#define STM32F407 1\n", encoding="utf-8")
    driver.write_text("void HAL_ETH_Init(void) {}\n", encoding="utf-8")

    manifest = {
        "target_mcu": "STM32F407VG",
        "target_peripheral": "ETH",
        "artifacts": [
            {"category": "docs", "status": "downloaded", "local_path": str(reference_manual)},
            {"category": "svd", "status": "downloaded", "local_path": str(svd)},
            {"category": "headers", "status": "downloaded", "local_path": str(header)},
            {"category": "drivers_hal", "status": "downloaded", "local_path": str(driver)},
        ],
    }
    manifest_path = data_dir / "manifests" / "stm32f407vg_eth.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    bundle = resolve_fetched_input_bundle(
        target_mcu="STM32F407VG",
        target_peripheral="ETH",
        data_dir=data_dir,
    )

    assert bundle.manifest_path == str(manifest_path)
    assert bundle.svd_path == str(svd)
    assert bundle.header_path == str(header)
    assert bundle.driver_paths == (str(driver),)
    assert bundle.documentation_paths == (str(reference_manual),)


def test_single_match_asset_retries_fallback_candidates(tmp_path, monkeypatch):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    header = source_dir / "stm32f407xx.h"
    header.write_text("#define STM32F407 1\n", encoding="utf-8")

    missing_header = source_dir / "missing.h"
    fetcher = STM32DataFetcher(searcher=FakeSearcher({}), enable_fallbacks=True)

    def fake_resolve_fallback_urls(asset):
        if asset.category == "headers":
            return [missing_header.as_uri(), header.as_uri()]
        return []

    monkeypatch.setattr(fetcher, "_resolve_fallback_urls", fake_resolve_fallback_urls)
    output_dir = tmp_path / "out"

    result = fetcher.fetch_data(
        target_mcu="STM32F407VG",
        target_peripheral="ETH",
        output_dir=output_dir,
    )

    header_artifact = next(
        artifact for artifact in result.artifacts if artifact.category == "headers"
    )
    assert header_artifact.status == "downloaded"
    assert Path(header_artifact.local_path).name == "stm32f407xx.h"


def test_materialize_download_url_normalizes_https_pdf_proxy():
    url = "https://www.st.com/resource/en/reference_manual/rm0090.pdf?via=search"
    materialized = _materialize_download_url(url, "docs")

    assert materialized == (
        "https://r.jina.ai/http://www.st.com/resource/en/reference_manual/rm0090.pdf?via=search"
    )


def test_token_present_uses_word_boundaries_for_short_tokens():
    assert _token_present("stm32f4xx_hal_eth.c", "eth")
    assert _token_present("HAL_ETH_Init", "eth")
    assert not _token_present("update packet length", "eth")
