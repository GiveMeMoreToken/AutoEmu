"""End-to-end integration tests for the modeling pipeline.

These tests run the full pipeline on synthetic STM32 target data.
Run with: pytest -m integration
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoemu.pipeline import run_model_pipeline


# Register the integration marker
pytestmark = pytest.mark.integration


# --- Fixtures: synthetic input data ---

MINIMAL_SVD = """<?xml version="1.0" encoding="utf-8"?>
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
            <field>
              <name>TC</name>
              <bitOffset>6</bitOffset>
              <bitWidth>1</bitWidth>
              <modifiedWriteValues>oneToClear</modifiedWriteValues>
            </field>
            <field>
              <name>RXNE</name>
              <bitOffset>5</bitOffset>
              <bitWidth>1</bitWidth>
              <access>read-only</access>
            </field>
            <field>
              <name>ORE</name>
              <bitOffset>3</bitOffset>
              <bitWidth>1</bitWidth>
              <access>read-only</access>
            </field>
          </fields>
        </register>
        <register>
          <name>DR</name>
          <addressOffset>0x04</addressOffset>
          <size>32</size>
          <resetValue>0x00000000</resetValue>
          <access>read-write</access>
          <fields>
            <field><name>DR</name><bitOffset>0</bitOffset><bitWidth>9</bitWidth></field>
          </fields>
        </register>
        <register>
          <name>BRR</name>
          <addressOffset>0x08</addressOffset>
          <size>32</size>
          <resetValue>0x00000000</resetValue>
          <access>read-write</access>
          <fields>
            <field><name>DIV_Mantissa</name><bitOffset>4</bitOffset><bitWidth>12</bitWidth></field>
            <field><name>DIV_Fraction</name><bitOffset>0</bitOffset><bitWidth>4</bitWidth></field>
          </fields>
        </register>
        <register>
          <name>CR1</name>
          <addressOffset>0x0C</addressOffset>
          <size>32</size>
          <resetValue>0x00000000</resetValue>
          <access>read-write</access>
          <fields>
            <field><name>UE</name><bitOffset>13</bitOffset><bitWidth>1</bitWidth></field>
            <field><name>TXEIE</name><bitOffset>7</bitOffset><bitWidth>1</bitWidth></field>
            <field><name>TCIE</name><bitOffset>6</bitOffset><bitWidth>1</bitWidth></field>
            <field><name>RXNEIE</name><bitOffset>5</bitOffset><bitWidth>1</bitWidth></field>
            <field><name>TE</name><bitOffset>3</bitOffset><bitWidth>1</bitWidth></field>
            <field><name>RE</name><bitOffset>2</bitOffset><bitWidth>1</bitWidth></field>
          </fields>
        </register>
        <register>
          <name>CR2</name>
          <addressOffset>0x10</addressOffset>
          <size>32</size>
          <resetValue>0x00000000</resetValue>
          <access>read-write</access>
        </register>
        <register>
          <name>CR3</name>
          <addressOffset>0x14</addressOffset>
          <size>32</size>
          <resetValue>0x00000000</resetValue>
          <access>read-write</access>
          <fields>
            <field><name>DMAT</name><bitOffset>7</bitOffset><bitWidth>1</bitWidth></field>
            <field><name>DMAR</name><bitOffset>6</bitOffset><bitWidth>1</bitWidth></field>
          </fields>
        </register>
      </registers>
    </peripheral>
  </peripherals>
</device>
"""

MINIMAL_DRIVER = """
/* Minimal HAL USART driver for testing */

HAL_StatusTypeDef HAL_USART_Init(USART_HandleTypeDef *husart) {
    SET_BIT(husart->Instance->CR1, USART_CR1_UE);
    SET_BIT(husart->Instance->CR1, USART_CR1_TE);
    SET_BIT(husart->Instance->CR1, USART_CR1_RE);
    MODIFY_REG(husart->Instance->BRR, 0xFFFF, 0x0683);
    return HAL_OK;
}

void HAL_USART_IRQHandler(USART_HandleTypeDef *husart) {
    uint32_t isrflags = READ_REG(husart->Instance->SR);
    uint32_t cr1its   = READ_REG(husart->Instance->CR1);

    if ((isrflags & USART_SR_TXE) != 0U) {
        if ((cr1its & USART_CR1_TXEIE) != 0U) {
            husart->Instance->DR = *(husart->pTxBuffPtr);
            HAL_USART_TxCpltCallback(husart);
        }
    }
    if ((isrflags & USART_SR_RXNE) != 0U) {
        if ((cr1its & USART_CR1_RXNEIE) != 0U) {
            *(husart->pRxBuffPtr) = (uint8_t)(husart->Instance->DR);
            HAL_USART_RxCpltCallback(husart);
        }
    }
    if ((isrflags & USART_SR_ORE) != 0U) {
        HAL_USART_ErrorCallback(husart);
    }
}

HAL_StatusTypeDef HAL_USART_Transmit_IT(USART_HandleTypeDef *husart, uint8_t *pData, uint16_t Size) {
    SET_BIT(husart->Instance->CR1, USART_CR1_TXEIE);
    return HAL_OK;
}

HAL_StatusTypeDef HAL_USART_Receive_IT(USART_HandleTypeDef *husart, uint8_t *pData, uint16_t Size) {
    SET_BIT(husart->Instance->CR1, USART_CR1_RXNEIE);
    return HAL_OK;
}

HAL_StatusTypeDef HAL_USART_DeInit(USART_HandleTypeDef *husart) {
    CLEAR_BIT(husart->Instance->CR1, USART_CR1_UE);
    return HAL_OK;
}
"""

# Also create an SPI and TIM variant for multi-peripheral coverage

MINIMAL_SPI_SVD = """<?xml version="1.0" encoding="utf-8"?>
<device>
  <name>STM32F407</name>
  <peripherals>
    <peripheral>
      <name>SPI1</name>
      <baseAddress>0x40013000</baseAddress>
      <registers>
        <register>
          <name>CR1</name><addressOffset>0x00</addressOffset><size>32</size><resetValue>0x00000000</resetValue>
          <access>read-write</access>
          <fields>
            <field><name>SPE</name><bitOffset>6</bitOffset><bitWidth>1</bitWidth></field>
            <field><name>MSTR</name><bitOffset>2</bitOffset><bitWidth>1</bitWidth></field>
            <field><name>BR</name><bitOffset>3</bitOffset><bitWidth>3</bitWidth></field>
          </fields>
        </register>
        <register>
          <name>CR2</name><addressOffset>0x04</addressOffset><size>32</size><resetValue>0x00000000</resetValue>
          <access>read-write</access>
          <fields>
            <field><name>TXEIE</name><bitOffset>7</bitOffset><bitWidth>1</bitWidth></field>
            <field><name>RXNEIE</name><bitOffset>6</bitOffset><bitWidth>1</bitWidth></field>
            <field><name>ERRIE</name><bitOffset>5</bitOffset><bitWidth>1</bitWidth></field>
          </fields>
        </register>
        <register>
          <name>SR</name><addressOffset>0x08</addressOffset><size>32</size><resetValue>0x00000002</resetValue>
          <access>read-write</access>
          <fields>
            <field><name>TXE</name><bitOffset>1</bitOffset><bitWidth>1</bitWidth><access>read-only</access></field>
            <field><name>RXNE</name><bitOffset>0</bitOffset><bitWidth>1</bitWidth><access>read-only</access></field>
            <field><name>BSY</name><bitOffset>7</bitOffset><bitWidth>1</bitWidth><access>read-only</access></field>
            <field><name>OVR</name><bitOffset>6</bitOffset><bitWidth>1</bitWidth><access>read-only</access></field>
          </fields>
        </register>
        <register>
          <name>DR</name><addressOffset>0x0C</addressOffset><size>32</size><resetValue>0x00000000</resetValue>
          <access>read-write</access>
        </register>
      </registers>
    </peripheral>
  </peripherals>
</device>
"""

MINIMAL_SPI_DRIVER = """
HAL_StatusTypeDef HAL_SPI_Init(SPI_HandleTypeDef *hspi) {
    MODIFY_REG(hspi->Instance->CR1, 0xFFFF, 0x0344);
    SET_BIT(hspi->Instance->CR1, SPI_CR1_SPE);
    return HAL_OK;
}

void HAL_SPI_IRQHandler(SPI_HandleTypeDef *hspi) {
    uint32_t sr = READ_REG(hspi->Instance->SR);
    if ((sr & SPI_SR_TXE) != 0U) {
        HAL_SPI_TxCpltCallback(hspi);
    }
    if ((sr & SPI_SR_RXNE) != 0U) {
        HAL_SPI_RxCpltCallback(hspi);
    }
    if ((sr & SPI_SR_OVR) != 0U) {
        HAL_SPI_ErrorCallback(hspi);
    }
}

HAL_StatusTypeDef HAL_SPI_DeInit(SPI_HandleTypeDef *hspi) {
    CLEAR_BIT(hspi->Instance->CR1, SPI_CR1_SPE);
    return HAL_OK;
}
"""

MINIMAL_TIM_SVD = """<?xml version="1.0" encoding="utf-8"?>
<device>
  <name>STM32F407</name>
  <peripherals>
    <peripheral>
      <name>TIM2</name>
      <baseAddress>0x40000000</baseAddress>
      <registers>
        <register>
          <name>CR1</name><addressOffset>0x00</addressOffset><size>32</size><resetValue>0x00000000</resetValue>
          <access>read-write</access>
          <fields>
            <field><name>CEN</name><bitOffset>0</bitOffset><bitWidth>1</bitWidth></field>
            <field><name>DIR</name><bitOffset>4</bitOffset><bitWidth>1</bitWidth></field>
            <field><name>ARPE</name><bitOffset>7</bitOffset><bitWidth>1</bitWidth></field>
          </fields>
        </register>
        <register>
          <name>DIER</name><addressOffset>0x0C</addressOffset><size>32</size><resetValue>0x00000000</resetValue>
          <access>read-write</access>
          <fields>
            <field><name>UIE</name><bitOffset>0</bitOffset><bitWidth>1</bitWidth></field>
          </fields>
        </register>
        <register>
          <name>SR</name><addressOffset>0x10</addressOffset><size>32</size><resetValue>0x00000000</resetValue>
          <access>read-write</access>
          <fields>
            <field><name>UIF</name><bitOffset>0</bitOffset><bitWidth>1</bitWidth><modifiedWriteValues>oneToClear</modifiedWriteValues></field>
          </fields>
        </register>
        <register>
          <name>CNT</name><addressOffset>0x24</addressOffset><size>32</size><resetValue>0x00000000</resetValue>
          <access>read-write</access>
        </register>
        <register>
          <name>PSC</name><addressOffset>0x28</addressOffset><size>32</size><resetValue>0x00000000</resetValue>
          <access>read-write</access>
        </register>
        <register>
          <name>ARR</name><addressOffset>0x2C</addressOffset><size>32</size><resetValue>0xFFFFFFFF</resetValue>
          <access>read-write</access>
        </register>
      </registers>
    </peripheral>
  </peripherals>
</device>
"""

MINIMAL_TIM_DRIVER = """
HAL_StatusTypeDef HAL_TIM_Base_Init(TIM_HandleTypeDef *htim) {
    htim->Instance->PSC = 0x0000;
    htim->Instance->ARR = 0xFFFF;
    htim->Instance->CR1 = 0x0000;
    return HAL_OK;
}

HAL_StatusTypeDef HAL_TIM_Base_Start_IT(TIM_HandleTypeDef *htim) {
    SET_BIT(htim->Instance->DIER, TIM_DIER_UIE);
    SET_BIT(htim->Instance->CR1, TIM_CR1_CEN);
    return HAL_OK;
}

HAL_StatusTypeDef HAL_TIM_Base_Stop(TIM_HandleTypeDef *htim) {
    CLEAR_BIT(htim->Instance->CR1, TIM_CR1_CEN);
    CLEAR_BIT(htim->Instance->DIER, TIM_DIER_UIE);
    return HAL_OK;
}
"""


@pytest.fixture
def usart_inputs(tmp_path):
    """Create synthetic USART input files."""
    svd = tmp_path / "usart.svd"
    svd.write_text(MINIMAL_SVD, encoding="utf-8")
    driver = tmp_path / "stm32f4xx_hal_usart.c"
    driver.write_text(MINIMAL_DRIVER, encoding="utf-8")
    return {"svd": svd, "driver": driver, "output": tmp_path / "output"}


@pytest.fixture
def spi_inputs(tmp_path):
    svd = tmp_path / "spi.svd"
    svd.write_text(MINIMAL_SPI_SVD, encoding="utf-8")
    driver = tmp_path / "stm32f4xx_hal_spi.c"
    driver.write_text(MINIMAL_SPI_DRIVER, encoding="utf-8")
    return {"svd": svd, "driver": driver, "output": tmp_path / "output_spi"}


@pytest.fixture
def tim_inputs(tmp_path):
    svd = tmp_path / "tim.svd"
    svd.write_text(MINIMAL_TIM_SVD, encoding="utf-8")
    driver = tmp_path / "stm32f4xx_hal_tim.c"
    driver.write_text(MINIMAL_TIM_DRIVER, encoding="utf-8")
    return {"svd": svd, "driver": driver, "output": tmp_path / "output_tim"}


# --- Tests ---

def test_usart_full_pipeline(usart_inputs):
    """Full pipeline for USART peripheral."""
    result = run_model_pipeline(
        "USART1",
        output_dir=usart_inputs["output"],
        svd_path=usart_inputs["svd"],
        driver_paths=[usart_inputs["driver"]],
        mcu_family="STM32F4",
    )
    _assert_pipeline_success(result, usart_inputs["output"])


def test_spi_full_pipeline(spi_inputs):
    """Full pipeline for SPI peripheral."""
    result = run_model_pipeline(
        "SPI1",
        output_dir=spi_inputs["output"],
        svd_path=spi_inputs["svd"],
        driver_paths=[spi_inputs["driver"]],
        mcu_family="STM32F4",
    )
    _assert_pipeline_success(result, spi_inputs["output"])


def test_tim_full_pipeline(tim_inputs):
    """Full pipeline for TIM peripheral."""
    result = run_model_pipeline(
        "TIM2",
        output_dir=tim_inputs["output"],
        svd_path=tim_inputs["svd"],
        driver_paths=[tim_inputs["driver"]],
        mcu_family="STM32F4",
    )
    _assert_pipeline_success(result, tim_inputs["output"])


def test_pipeline_with_no_isr(tmp_path):
    """Pipeline handles driver with no ISR patterns."""
    svd = tmp_path / "device.svd"
    svd.write_text(MINIMAL_TIM_SVD, encoding="utf-8")
    driver = tmp_path / "minimal.c"
    driver.write_text("""
HAL_StatusTypeDef HAL_TIM_Init(TIM_HandleTypeDef *htim) {
    htim->Instance->PSC = 0;
    htim->Instance->ARR = 0xFFFF;
    return HAL_OK;
}
""", encoding="utf-8")
    output = tmp_path / "output"
    result = run_model_pipeline(
        "TIM2",
        output_dir=output,
        svd_path=svd,
        driver_paths=[driver],
    )
    _assert_pipeline_success(result, output)


def test_pipeline_validation_report(usart_inputs):
    """Validation report structure is correct."""
    result = run_model_pipeline(
        "USART1",
        output_dir=usart_inputs["output"],
        svd_path=usart_inputs["svd"],
        driver_paths=[usart_inputs["driver"]],
    )
    report = result["validation_report"]
    assert "register_issues" in report
    assert "behavior_issues" in report
    assert "driver_replay" in report
    assert isinstance(report["register_issues"], list)
    assert isinstance(report["behavior_issues"], list)


def _assert_pipeline_success(result, output_dir):
    """Common assertions for pipeline output."""
    output_path = Path(output_dir)

    # Pipeline completed
    assert result["peripheral_name"]

    # All expected output files exist
    generated = result["generated_files"]
    assert len(generated) >= 4  # At minimum: peripheral JSON, .c, .h, meson.build

    for fpath in generated:
        assert Path(fpath).exists(), f"Expected file not found: {fpath}"

    # Check for C and header files
    c_files = [f for f in generated if f.endswith(".c")]
    h_files = [f for f in generated if f.endswith(".h")]
    assert len(c_files) >= 1, "No .c files generated"
    assert len(h_files) >= 1, "No .h files generated"

    # Check model JSON files exist
    assert Path(result["registers_json"]).exists()
    assert Path(result["state_machine_json"]).exists()
    assert Path(result["interrupt_model_json"]).exists()
    assert Path(result["dependency_graph_json"]).exists()
    assert Path(result["peripheral_json"]).exists()

    # Validation report has no errors (warnings ok)
    report = result["validation_report"]
    errors = [
        issue for issue in report.get("register_issues", [])
        if issue.get("severity") == "error"
    ]
    assert len(errors) == 0, f"Validation errors found: {errors}"
