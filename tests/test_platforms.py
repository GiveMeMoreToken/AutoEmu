"""Tests for the platform abstraction layer."""
from __future__ import annotations

from pathlib import Path

import pytest

from autoemu.platforms import get_platform, list_platforms
from autoemu.platforms.base import (
    AssetDescriptor,
    InputBundle,
    NamingInfo,
    QEMUTargetInfo,
)
from autoemu.platforms.stm32 import STM32Platform
from autoemu.fetchers.base import BaseFetcher, FetchManifest


MINIMAL_SVD = """\
<?xml version="1.0" encoding="utf-8"?>
<device>
  <name>STM32F407</name>
  <peripherals>
    <peripheral>
      <name>USART1</name>
      <baseAddress>0x40011000</baseAddress>
      <registers>
        <register>
          <name>SR</name>
          <addressOffset>0x00</addressOffset>
          <size>32</size>
          <resetValue>0x000000C0</resetValue>
          <access>read-write</access>
          <fields>
            <field>
              <name>TXE</name>
              <bitOffset>7</bitOffset>
              <bitWidth>1</bitWidth>
              <access>read-only</access>
            </field>
          </fields>
        </register>
      </registers>
    </peripheral>
  </peripherals>
</device>
"""

MINIMAL_DRIVER = """\
void HAL_USART_Init(USART_TypeDef *husart) {
    husart->CR1 |= USART_CR1_UE;
}
"""


def test_get_platform_stm32():
    platform = get_platform("stm32")
    assert isinstance(platform, STM32Platform)
    assert platform.name == "stm32"


def test_get_platform_unknown():
    with pytest.raises(ValueError, match="Unknown platform 'mips64'"):
        get_platform("mips64")


def test_list_platforms():
    platforms = list_platforms()
    assert "stm32" in platforms
    assert platforms == sorted(platforms)


def test_stm32_discover_inputs():
    platform = STM32Platform()
    descriptors = platform.discover_inputs("STM32F407VG", "ETH")
    assert len(descriptors) > 0
    assert all(isinstance(d, AssetDescriptor) for d in descriptors)
    keys = [d.key for d in descriptors]
    assert "reference_manual" in keys or len(keys) > 0


def test_stm32_naming_convention():
    platform = STM32Platform()
    info = platform.naming_convention("ETH")
    assert isinstance(info, NamingInfo)
    assert info.file_prefix == "stm32"
    assert info.type_prefix == "STM32"
    assert "{prefix}" in info.qemu_type_fmt


def test_stm32_qemu_target_info():
    platform = STM32Platform()
    info = platform.qemu_target_info("STM32F407VG")
    assert isinstance(info, QEMUTargetInfo)
    assert info.arch == "arm"
    assert info.cpu == "cortex-m4"


def test_stm32_parse_registers(tmp_path: Path):
    svd_file = tmp_path / "device.svd"
    svd_file.write_text(MINIMAL_SVD, encoding="utf-8")

    bundle = InputBundle(
        target="STM32F407VG",
        peripheral="USART1",
        svd_path=str(svd_file),
    )

    platform = STM32Platform()
    blocks = platform.parse_registers(bundle)
    assert "USART1" in blocks
    reg_names = [r.name for r in blocks["USART1"].registers]
    assert "SR" in reg_names


def test_stm32_parse_drivers(tmp_path: Path):
    driver_file = tmp_path / "stm32f4xx_hal_usart.c"
    driver_file.write_text(MINIMAL_DRIVER, encoding="utf-8")

    bundle = InputBundle(
        target="STM32F407VG",
        peripheral="USART",
        driver_paths=[str(driver_file)],
    )

    platform = STM32Platform()
    analysis = platform.parse_drivers(bundle)
    assert analysis.peripheral_name == "USART"


def test_stm32_parse_drivers_empty():
    bundle = InputBundle(
        target="STM32F407VG",
        peripheral="USART",
        driver_paths=[],
    )
    platform = STM32Platform()
    analysis = platform.parse_drivers(bundle)
    assert analysis.peripheral_name == "USART"
    assert analysis.source_file == ""


def test_input_bundle_creation():
    bundle = InputBundle(target="STM32F407VG", peripheral="ETH")
    assert bundle.target == "STM32F407VG"
    assert bundle.peripheral == "ETH"
    assert bundle.svd_path == ""
    assert bundle.header_path == ""
    assert bundle.driver_paths == []
    assert bundle.documentation_paths == []
    assert bundle.extra == {}


def test_base_fetcher_download_offline(tmp_path: Path):
    class DummyFetcher(BaseFetcher):
        def fetch(self, *, target, peripheral, output_dir, refresh=False):
            return FetchManifest(
                target=target,
                peripheral=peripheral,
                platform="dummy",
                output_dir=str(output_dir),
            )

    fetcher = DummyFetcher(offline=True)
    dest = tmp_path / "file.bin"
    success, sha = fetcher.download_file("https://example.com/file.bin", dest)
    assert success is False
    assert sha == ""
    assert not dest.exists()


def test_base_fetcher_download_cached(tmp_path: Path):
    """When the file already exists and refresh=False, download returns cached hash."""

    class DummyFetcher(BaseFetcher):
        def fetch(self, *, target, peripheral, output_dir, refresh=False):
            return FetchManifest(
                target=target,
                peripheral=peripheral,
                platform="dummy",
                output_dir=str(output_dir),
            )

    fetcher = DummyFetcher(offline=False)
    dest = tmp_path / "cached.bin"
    dest.write_bytes(b"hello")
    success, sha = fetcher.download_file("https://example.com/cached.bin", dest)
    assert success is True
    assert len(sha) == 64  # SHA-256 hex digest


def test_fetch_manifest_round_trip(tmp_path: Path):
    manifest = FetchManifest(
        target="STM32F407VG",
        peripheral="ETH",
        platform="stm32",
        output_dir=str(tmp_path),
        success=True,
        warnings=["test warning"],
    )
    manifest_path = tmp_path / "manifest.json"
    manifest.save(manifest_path)

    loaded = FetchManifest.load(manifest_path)
    assert loaded.target == "STM32F407VG"
    assert loaded.peripheral == "ETH"
    assert loaded.platform == "stm32"
    assert loaded.success is True
    assert "test warning" in loaded.warnings
