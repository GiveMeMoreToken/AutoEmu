#include "stm32f4xx_hal.h"

#include <stdint.h>

uint32_t SystemCoreClock = 168000000U;

static volatile uint32_t g_hal_tick;
static uint8_t eth_rx_pool[ETH_RX_DESC_CNT][ETH_MAX_PACKET_SIZE]
    __attribute__((aligned(4)));
static uint32_t eth_rx_pool_index;

void SystemInit(void)
{
}

uint32_t HAL_GetTick(void)
{
    return g_hal_tick;
}

void HAL_IncTick(void)
{
    g_hal_tick++;
}

HAL_StatusTypeDef HAL_InitTick(uint32_t tick_priority)
{
    (void)tick_priority;
    return HAL_OK;
}

void HAL_Delay(uint32_t delay)
{
    g_hal_tick += delay;
}

void HAL_SuspendTick(void)
{
}

void HAL_ResumeTick(void)
{
}

void HAL_MspInit(void)
{
}

void HAL_ETH_MspInit(ETH_HandleTypeDef *heth)
{
    (void)heth;
}

void HAL_ETH_MspDeInit(ETH_HandleTypeDef *heth)
{
    (void)heth;
}

void HAL_PCD_MspInit(PCD_HandleTypeDef *hpcd)
{
    (void)hpcd;
}

void HAL_PCD_MspDeInit(PCD_HandleTypeDef *hpcd)
{
    (void)hpcd;
}

uint32_t HAL_RCC_GetHCLKFreq(void)
{
    return SystemCoreClock;
}

void HAL_PCDEx_LPM_Callback(PCD_HandleTypeDef *hpcd, PCD_LPM_MsgTypeDef msg)
{
    (void)hpcd;
    (void)msg;
}

void HAL_ETH_RxAllocateCallback(uint8_t **buffer)
{
    *buffer = eth_rx_pool[eth_rx_pool_index % ETH_RX_DESC_CNT];
    eth_rx_pool_index++;
}
