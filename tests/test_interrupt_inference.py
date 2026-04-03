"""Tests for automatic interrupt-model inference."""

from __future__ import annotations

from autoemu.inference.interrupt_inference import infer_interrupt_model
from autoemu.models.register import AccessType, BitField, Register, RegisterBlock
from autoemu.parsers.driver_parser import analyze_driver_string


DRIVER_SAMPLE = """\
void HAL_ETH_IRQHandler(ETH_HandleTypeDef *heth)
{
    if (__HAL_ETH_GET_FLAG(heth, ETH_FLAG_NIS))
    {
        if (__HAL_ETH_GET_IT_SOURCE(heth, ETH_IT_NIS))
        {
            __HAL_ETH_CLEAR_FLAG(heth, ETH_FLAG_NIS);
            HAL_ETH_TxCpltCallback(heth);
        }
    }

    if (__HAL_ETH_GET_FLAG(heth, ETH_FLAG_AIS))
    {
        if (__HAL_ETH_GET_IT_SOURCE(heth, ETH_IT_AIS))
        {
            __HAL_ETH_CLEAR_FLAG(heth, ETH_FLAG_AIS);
            HAL_ETH_ErrorCallback(heth);
        }
    }
}
"""


def test_infer_interrupt_model_from_driver_and_registers():
    analysis = analyze_driver_string(DRIVER_SAMPLE, "ETH")
    blocks = {
        "Ethernet_DMA": RegisterBlock(
            name="Ethernet_DMA",
            base_address=0x40029000,
            registers=[
                Register(
                    name="DMASR",
                    offset=0x14,
                    fields=[
                        BitField(name="AIS", bit_offset=15, bit_width=1, access=AccessType.W1C),
                        BitField(name="NIS", bit_offset=16, bit_width=1, access=AccessType.W1C),
                    ],
                ),
                Register(
                    name="DMAIER",
                    offset=0x1C,
                    fields=[
                        BitField(name="AISE", bit_offset=15, bit_width=1, access=AccessType.RW),
                        BitField(name="NISE", bit_offset=16, bit_width=1, access=AccessType.RW),
                    ],
                ),
            ],
        )
    }

    model = infer_interrupt_model(analysis, blocks, peripheral_name="ETH")
    assert model.peripheral_name == "ETH"
    assert len(model.lines) == 1
    line = model.lines[0]
    assert line.name == "ETH_IRQn"
    assert line.irq_number == 61

    flags = {flag.name: flag for flag in line.flags}
    assert "NIS" in flags
    assert flags["NIS"].register_name == "DMASR"
    assert flags["NIS"].bit_offset == 16
    assert flags["NIS"].enable_register == "DMAIER"
    assert flags["NIS"].enable_bit_offset == 16
    assert flags["NIS"].clear_behavior.value == "w1c"

    assert model.flag_to_event_map["eth_tx_cplt"] == ["NIS"]
    assert model.flag_to_event_map["eth_error"] == ["AIS"]


def test_infer_interrupt_model_supports_unknown_irq_number():
    analysis = analyze_driver_string(
        """\
void HAL_UART_IRQHandler(UART_HandleTypeDef *huart)
{
    if (__HAL_UART_GET_FLAG(huart, UART_FLAG_TC))
    {
        if (__HAL_UART_GET_IT_SOURCE(huart, UART_IT_TC))
        {
            __HAL_UART_CLEAR_FLAG(huart, UART_FLAG_TC);
            HAL_UART_TxCpltCallback(huart);
        }
    }
}
""",
        "UART",
    )
    blocks = {
        "UART": RegisterBlock(
            name="UART",
            registers=[
                Register(
                    name="SR",
                    offset=0x00,
                    fields=[BitField(name="TC", bit_offset=6, bit_width=1, access=AccessType.W1C)],
                ),
                Register(
                    name="CR1",
                    offset=0x04,
                    fields=[BitField(name="TCIE", bit_offset=6, bit_width=1, access=AccessType.RW)],
                ),
            ],
        )
    }

    model = infer_interrupt_model(analysis, blocks, peripheral_name="UART")
    assert model.lines[0].name == "UART_IRQn"
    assert model.lines[0].irq_number == -1


def test_infer_interrupt_model_handles_direct_register_mask_isr():
    analysis = analyze_driver_string(
        """\
void HAL_ETH_IRQHandler(ETH_HandleTypeDef *heth)
{
    uint32_t dma_flag = READ_REG(heth->Instance->DMASR);
    uint32_t dma_itsource = READ_REG(heth->Instance->DMAIER);
    uint32_t mac_flag = READ_REG(heth->Instance->MACSR);

    if (((dma_flag & ETH_DMASR_RS) != 0U) && ((dma_itsource & ETH_DMAIER_RIE) != 0U))
    {
        __HAL_ETH_DMA_CLEAR_IT(heth, ETH_DMASR_RS | ETH_DMASR_NIS);
        HAL_ETH_RxCpltCallback(heth);
    }

    if (((dma_flag & ETH_DMASR_TS) != 0U) && ((dma_itsource & ETH_DMAIER_TIE) != 0U))
    {
        __HAL_ETH_DMA_CLEAR_IT(heth, ETH_DMASR_TS | ETH_DMASR_NIS);
        HAL_ETH_TxCpltCallback(heth);
    }

    if (((dma_flag & ETH_DMASR_AIS) != 0U) && ((dma_itsource & ETH_DMAIER_AISE) != 0U))
    {
        __HAL_ETH_DMA_CLEAR_IT(heth, ETH_DMASR_AIS);
        HAL_ETH_ErrorCallback(heth);
    }

    if ((mac_flag & ETH_MAC_PMT_IT) != 0U)
    {
        HAL_ETH_PMTCallback(heth);
    }
}
""",
        "ETH",
    )
    blocks = {
        "Ethernet_DMA": RegisterBlock(
            name="Ethernet_DMA",
            base_address=0x40029000,
            registers=[
                Register(
                    name="DMASR",
                    offset=0x14,
                    fields=[
                        BitField(name="RS", bit_offset=6, bit_width=1, access=AccessType.W1C),
                        BitField(name="TS", bit_offset=0, bit_width=1, access=AccessType.W1C),
                        BitField(name="AIS", bit_offset=15, bit_width=1, access=AccessType.W1C),
                    ],
                ),
                Register(
                    name="DMAIER",
                    offset=0x1C,
                    fields=[
                        BitField(name="RIE", bit_offset=6, bit_width=1, access=AccessType.RW),
                        BitField(name="TIE", bit_offset=0, bit_width=1, access=AccessType.RW),
                        BitField(name="AISE", bit_offset=15, bit_width=1, access=AccessType.RW),
                    ],
                ),
                Register(
                    name="MACSR",
                    offset=0x38,
                    fields=[
                        BitField(name="PMTS", bit_offset=3, bit_width=1, access=AccessType.RW),
                    ],
                ),
            ],
        )
    }

    model = infer_interrupt_model(analysis, blocks, peripheral_name="ETH")
    flags = {flag.name: flag for flag in model.lines[0].flags}

    assert {"RS", "TS", "AIS"} <= set(flags)
    assert flags["RS"].enable_register == "DMAIER"
    assert flags["TS"].enable_register == "DMAIER"
    assert flags["AIS"].enable_register == "DMAIER"
    assert flags["PMT"].register_name == "MACSR"
    assert model.flag_to_event_map["eth_rx_cplt"] == ["RS"]
    assert model.flag_to_event_map["eth_tx_cplt"] == ["TS"]
    assert model.flag_to_event_map["eth_error"] == ["AIS"]
    assert model.flag_to_event_map["eth_pmt"] == ["PMT"]


def test_infer_interrupt_model_keeps_cleared_only_flags():
    analysis = analyze_driver_string(
        """\
void HAL_ETH_IRQHandler(ETH_HandleTypeDef *heth)
{
    uint32_t dma_flag = READ_REG(heth->Instance->DMASR);
    uint32_t dma_itsource = READ_REG(heth->Instance->DMAIER);

    if (((dma_flag & ETH_DMASR_TS) != 0U) && ((dma_itsource & ETH_DMAIER_TIE) != 0U))
    {
        __HAL_ETH_DMA_CLEAR_IT(heth, ETH_DMASR_TS | ETH_DMASR_NIS);
        HAL_ETH_TxCpltCallback(heth);
    }
}
""",
        "ETH",
    )
    blocks = {
        "Ethernet_DMA": RegisterBlock(
            name="Ethernet_DMA",
            base_address=0x40029000,
            registers=[
                Register(
                    name="DMASR",
                    offset=0x14,
                    fields=[
                        BitField(name="TS", bit_offset=0, bit_width=1, access=AccessType.W1C),
                        BitField(name="NIS", bit_offset=16, bit_width=1, access=AccessType.W1C),
                    ],
                ),
                Register(
                    name="DMAIER",
                    offset=0x1C,
                    fields=[
                        BitField(name="TIE", bit_offset=0, bit_width=1, access=AccessType.RW),
                        BitField(name="NISE", bit_offset=16, bit_width=1, access=AccessType.RW),
                    ],
                ),
            ],
        )
    }

    model = infer_interrupt_model(analysis, blocks, peripheral_name="ETH")
    flags = {flag.name for flag in model.lines[0].flags}

    assert "TS" in flags
    assert "NIS" in flags
