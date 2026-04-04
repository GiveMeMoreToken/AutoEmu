"""Tests for the minimal CLI surface."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

import autoemu.main as main_module
from autoemu.main import cli


def test_fetch_data_command(monkeypatch, tmp_path):
    runner = CliRunner()
    manifest_path = tmp_path / "manifests" / "stm32f407vg_eth.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "target_mcu": "STM32F407VG",
                "target_peripheral": "ETH",
                "manifest_path": str(manifest_path),
                "success": True,
            }
        ),
        encoding="utf-8",
    )

    def fake_fetch_data(self, *, target_mcu, target_peripheral, output_dir, refresh, offline=False):
        return {
            "target_mcu": target_mcu,
            "target_peripheral": target_peripheral,
            "manifest_path": str(manifest_path),
            "output_dir": str(output_dir),
            "success": True,
            "execution_backend": "harness",
            "execution_mode": "harness",
        }

    monkeypatch.setattr(main_module.AutoEmuAgentRuntime, "fetch_data", fake_fetch_data)

    result = runner.invoke(
        cli,
        [
            "fetch-data",
            "--target-mcu",
            "STM32F407VG",
            "--target-peripheral",
            "ETH",
            "--output",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["target_mcu"] == "STM32F407VG"
    assert data["target_peripheral"] == "ETH"
    assert data["execution_backend"] == "harness"


def test_build_qemu_peripheral_command(tmp_path):
    runner = CliRunner()
    data_dir = tmp_path / "data"
    output_dir = tmp_path / "bundle"
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

    reference_manual.write_text("ETH becomes ready after init.", encoding="utf-8")
    svd.write_text(
        """\
<?xml version="1.0" encoding="utf-8"?>
<device>
  <peripherals>
    <peripheral>
      <name>ETH</name>
      <baseAddress>0x40028000</baseAddress>
      <registers>
        <register>
          <name>DMABMR</name>
          <addressOffset>0x1000</addressOffset>
          <fields>
            <field>
              <name>SR</name>
              <bitOffset>0</bitOffset>
              <bitWidth>1</bitWidth>
            </field>
          </fields>
        </register>
        <register>
          <name>MACCR</name>
          <addressOffset>0x0000</addressOffset>
          <fields>
            <field>
              <name>RE</name>
              <bitOffset>2</bitOffset>
              <bitWidth>1</bitWidth>
            </field>
            <field>
              <name>TE</name>
              <bitOffset>3</bitOffset>
              <bitWidth>1</bitWidth>
            </field>
          </fields>
        </register>
        <register>
          <name>DMAOMR</name>
          <addressOffset>0x1018</addressOffset>
          <fields>
            <field>
              <name>ST</name>
              <bitOffset>13</bitOffset>
              <bitWidth>1</bitWidth>
            </field>
            <field>
              <name>SR</name>
              <bitOffset>1</bitOffset>
              <bitWidth>1</bitWidth>
            </field>
          </fields>
        </register>
      </registers>
    </peripheral>
  </peripherals>
</device>
""",
        encoding="utf-8",
    )
    header.write_text(
        """\
typedef struct {
  __IO uint32_t MACCR;
  uint32_t RESERVED0[1023];
  __IO uint32_t DMABMR;
  uint32_t RESERVED1[5];
  __IO uint32_t DMAOMR;
} ETH_TypeDef;

#define ETH_BASE (0x40028000UL)
""",
        encoding="utf-8",
    )
    driver.write_text(
        """\
HAL_StatusTypeDef HAL_ETH_Init(ETH_HandleTypeDef *heth)
{
    SET_BIT(heth->Instance->DMABMR, ETH_DMABMR_SR);
    return HAL_OK;
}

HAL_StatusTypeDef HAL_ETH_Start(ETH_HandleTypeDef *heth)
{
    SET_BIT(heth->Instance->MACCR, ETH_MACCR_RE);
    SET_BIT(heth->Instance->MACCR, ETH_MACCR_TE);
    SET_BIT(heth->Instance->DMAOMR, ETH_DMAOMR_ST);
    SET_BIT(heth->Instance->DMAOMR, ETH_DMAOMR_SR);
    return HAL_OK;
}
""",
        encoding="utf-8",
    )

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

    result = runner.invoke(
        cli,
        [
            "build-qemu-peripheral",
            "--target-mcu",
            "STM32F407VG",
            "--target-peripheral",
            "ETH",
            "--data-dir",
            str(data_dir),
            "--output-dir",
            str(output_dir),
        ],
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["target_mcu"] == "STM32F407VG"
    assert data["target_peripheral"] == "ETH"
    assert Path(data["registers_json"]).exists()
    assert Path(data["peripheral_json"]).exists()
    assert Path(data["validation_json"]).exists()
    assert data["validation_report"]["success"]


def test_build_qemu_peripheral_requires_fetched_inputs(tmp_path):
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "build-qemu-peripheral",
            "--target-mcu",
            "STM32F407VG",
            "--target-peripheral",
            "ETH",
            "--data-dir",
            str(tmp_path / "missing"),
        ],
    )

    assert result.exit_code != 0
    assert "Run fetch-data first" in result.output
