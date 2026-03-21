"""Tests for parsers."""

import pytest
from autoemu.parsers.svd_parser import parse_svd_string
from autoemu.parsers.header_parser import (
    parse_typedef_structs,
    parse_base_addresses,
    parse_bit_definitions,
)
from autoemu.parsers.driver_parser import analyze_driver_string


class TestSVDParser:
    SVD_SAMPLE = """\
<?xml version="1.0" encoding="utf-8"?>
<device>
  <name>STM32F407</name>
  <peripherals>
    <peripheral>
      <name>USART1</name>
      <description>Universal synchronous asynchronous receiver transmitter</description>
      <baseAddress>0x40011000</baseAddress>
      <registers>
        <register>
          <name>SR</name>
          <description>Status register</description>
          <addressOffset>0x00</addressOffset>
          <size>32</size>
          <resetValue>0x000000C0</resetValue>
          <access>read-write</access>
          <fields>
            <field>
              <name>PE</name>
              <description>Parity error</description>
              <bitOffset>0</bitOffset>
              <bitWidth>1</bitWidth>
              <access>read-only</access>
            </field>
            <field>
              <name>TXE</name>
              <description>Transmit data register empty</description>
              <bitOffset>7</bitOffset>
              <bitWidth>1</bitWidth>
              <access>read-only</access>
            </field>
            <field>
              <name>TC</name>
              <description>Transmission complete</description>
              <bitOffset>6</bitOffset>
              <bitWidth>1</bitWidth>
              <access>read-write</access>
            </field>
          </fields>
        </register>
        <register>
          <name>DR</name>
          <addressOffset>0x04</addressOffset>
          <size>32</size>
          <resetValue>0x00000000</resetValue>
        </register>
      </registers>
    </peripheral>
  </peripherals>
</device>
"""

    def test_parse_basic(self):
        blocks = parse_svd_string(self.SVD_SAMPLE)
        assert "USART1" in blocks

        usart = blocks["USART1"]
        assert usart.base_address == 0x40011000
        assert len(usart.registers) == 2

    def test_parse_register_fields(self):
        blocks = parse_svd_string(self.SVD_SAMPLE)
        sr = blocks["USART1"].get_register("SR")
        assert sr is not None
        assert sr.reset_value == 0xC0
        assert len(sr.fields) == 3

        pe = sr.get_field("PE")
        assert pe is not None
        assert pe.bit_offset == 0
        assert pe.bit_width == 1
        assert pe.access.value == "RO"


class TestHeaderParser:
    HEADER_SAMPLE = """\
typedef struct {
  __IO uint32_t CR1;
  __IO uint32_t CR2;
  __IO uint32_t SR;
  __IO uint32_t DR;
  uint32_t RESERVED0[2];
  __IO uint32_t CCR;
} ADC_TypeDef;

#define PERIPH_BASE       (0x40000000UL)
#define APB2PERIPH_BASE   (PERIPH_BASE + 0x00010000UL)
#define ADC1_BASE         (APB2PERIPH_BASE + 0x2000UL)

#define ADC_CR1_EOCIE_Pos  (5U)
#define ADC_CR1_EOCIE_Msk  (0x1U << ADC_CR1_EOCIE_Pos)
#define ADC_CR1_SCAN_Pos   (8U)
#define ADC_CR1_SCAN_Msk   (0x1U << ADC_CR1_SCAN_Pos)
"""

    def test_parse_struct(self):
        structs = parse_typedef_structs(self.HEADER_SAMPLE)
        assert len(structs) == 1
        assert structs[0].name == "ADC_TypeDef"

        fields = [f for f in structs[0].fields if not f.is_reserved]
        assert len(fields) == 5  # CR1, CR2, SR, DR, CCR
        assert fields[0].name == "CR1"
        assert fields[0].offset == 0

    def test_parse_base_addresses(self):
        addrs = parse_base_addresses(self.HEADER_SAMPLE)
        assert "ADC1_BASE" in addrs
        assert addrs["ADC1_BASE"] == 0x40012000

    def test_parse_bit_definitions(self):
        bits = parse_bit_definitions(self.HEADER_SAMPLE, "ADC")
        assert "CR1" in bits
        fields = bits["CR1"]
        names = [f.name for f in fields]
        assert "EOCIE" in names
        assert "SCAN" in names


class TestDriverParser:
    DRIVER_SAMPLE = """\
HAL_StatusTypeDef HAL_ADC_Init(ADC_HandleTypeDef *hadc)
{
    SET_BIT(hadc->Instance->CR1, ADC_CR1_SCAN);
    MODIFY_REG(hadc->Instance->CR2, ADC_CR2_CONT_Msk, ADC_CR2_CONT);
    SET_BIT(hadc->Instance->CR2, ADC_CR2_ADON);
    return HAL_OK;
}

void HAL_ADC_IRQHandler(ADC_HandleTypeDef *hadc)
{
    if (__HAL_ADC_GET_FLAG(hadc, ADC_FLAG_EOC))
    {
        if (__HAL_ADC_GET_IT_SOURCE(hadc, ADC_IT_EOC))
        {
            __HAL_ADC_CLEAR_FLAG(hadc, ADC_FLAG_EOC);
            HAL_ADC_ConvCpltCallback(hadc);
        }
    }
}
"""

    def test_analyze_init(self):
        analysis = analyze_driver_string(self.DRIVER_SAMPLE, "ADC")
        assert analysis.peripheral_name == "ADC"
        assert len(analysis.init_sequences) >= 1

        init = analysis.init_sequences[0]
        assert init.function_name == "HAL_ADC_Init"
        reg_names = [a.register for a in init.accesses]
        assert "CR1" in reg_names

    def test_analyze_isr(self):
        analysis = analyze_driver_string(self.DRIVER_SAMPLE, "ADC")
        assert len(analysis.isr_patterns) == 1

        isr = analysis.isr_patterns[0]
        assert "ADC_FLAG_EOC" in isr.checked_flags
        assert "ADC_FLAG_EOC" in isr.cleared_flags
        assert any("ConvCplt" in cb for cb in isr.callbacks)
