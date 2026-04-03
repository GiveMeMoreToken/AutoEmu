"""Tests for cross-peripheral dependency inference."""

from __future__ import annotations

from autoemu.inference.dependency_inference import (
    infer_dependency_graph,
    infer_dependency_graph_from_driver_text,
)
from autoemu.models.dependency import DependencyType
from autoemu.models.interrupt import InterruptFlag, InterruptLine, InterruptModel, FlagBehavior


def test_infer_dependency_graph_detects_clock_dma_and_exti_edges():
    source = """\
void HAL_USART_MspInit(UART_HandleTypeDef *huart)
{
    __HAL_RCC_USART1_CLK_ENABLE();
    __HAL_RCC_DMA2_CLK_ENABLE();
    hdma_usart1_tx.Instance = DMA2_Stream7;
    hdma_usart1_tx.Init.Channel = DMA2_Channel4;
}

void HAL_USART_IRQHandler(UART_HandleTypeDef *huart)
{
    uint32_t exti_flag = READ_REG(EXTI->PR);
    if ((exti_flag & USART_WAKEUP_EXTI_LINE) != 0U)
    {
        HAL_USART_WakeUpCallback(huart);
    }
}
"""
    graph = infer_dependency_graph_from_driver_text(
        source,
        peripheral_name="USART1",
        mcu_name="STM32F4",
    )

    edges = {(edge.source, edge.target, edge.dep_type): edge for edge in graph.edges}
    assert ("RCC", "USART1", DependencyType.CLOCK_GATE) in edges
    assert ("DMA2", "USART1", DependencyType.DMA_CHANNEL) in edges
    assert ("EXTI", "USART1", DependencyType.IRQ_CHAIN) in edges
    assert "Stream7" in edges[("DMA2", "USART1", DependencyType.DMA_CHANNEL)].channel


def test_infer_dependency_graph_uses_docs_and_interrupt_model_for_gpio_and_triggers():
    interrupt_model = InterruptModel(
        peripheral_name="ADC1",
        lines=[
            InterruptLine(
                irq_number=18,
                name="ADC_IRQn",
                flags=[
                    InterruptFlag(
                        name="WAKEUP",
                        register_name="ISR",
                        bit_offset=0,
                        clear_behavior=FlagBehavior.SOFTWARE_CLEAR,
                    )
                ],
            )
        ],
        event_sources=["adc_wake_up"],
    )
    graph = infer_dependency_graph(
        {
            "peripheral_name": "ADC1",
            "register_accesses": [],
            "isr_patterns": [],
            "init_sequences": [],
            "dma_configs": [],
        },
        peripheral_name="ADC1",
        documentation_text=(
            "ADC1 uses GPIOA pins in alternate function mode and can be triggered by TIM2 TRGO."
        ),
        interrupt_model=interrupt_model,
        source_texts=[],
        mcu_name="STM32F4",
    )

    edges = {(edge.source, edge.target, edge.dep_type): edge for edge in graph.edges}
    assert ("GPIO", "ADC1", DependencyType.GPIO_AF) in edges
    assert ("TIM2", "ADC1", DependencyType.TRIGGER) in edges
    assert ("EXTI", "ADC1", DependencyType.IRQ_CHAIN) in edges
