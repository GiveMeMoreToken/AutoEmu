#include "semihost.h"
#include "stm32f4xx_hal.h"

#include <stdbool.h>
#include <stdint.h>

#define DMA1_STREAM0_IRQN 11U
#define ETH_IRQN 61U
#define USB_OTG_FS_IRQN 67U
#define SUBGHZ_IRQN 85U

#define SUBGHZ_BASE 0x58010000UL
#define SUBGHZ_SPIDR_OFFSET 0x008UL
#define SUBGHZ_RADIO_IRQSTATUS_OFFSET 0x104UL
#define SUBGHZ_RADIO_IRQMASK_OFFSET 0x108UL
#define SUBGHZ_TX_DONE_BIT (1U << 0)

#define MMIO32(address) (*(volatile uint32_t *)(uintptr_t)(address))

static DMA_HandleTypeDef g_dma;
static ETH_HandleTypeDef g_eth;
static PCD_HandleTypeDef g_pcd;

static volatile uint32_t g_dma_complete;
static volatile uint32_t g_subghz_complete;

static uint32_t dma_src_words[4] __attribute__((aligned(4))) = {
    0x11111111U,
    0x22222222U,
    0x33333333U,
    0x44444444U,
};
static uint32_t dma_dst_words[4] __attribute__((aligned(4)));
static ETH_DMADescTypeDef eth_tx_desc[ETH_TX_DESC_CNT] __attribute__((aligned(4)));
static ETH_DMADescTypeDef eth_rx_desc[ETH_RX_DESC_CNT] __attribute__((aligned(4)));
static uint8_t eth_mac_addr[6] = {0x02U, 0x00U, 0x00U, 0x00U, 0x00U, 0x01U};

static void nvic_enable_irq(uint32_t irqn)
{
    volatile uint32_t *iser = (volatile uint32_t *)0xE000E100UL;
    iser[irqn / 32U] = 1UL << (irqn % 32U);
}

static void fail(const char *message)
{
    semihost_write0("FAIL: ");
    semihost_write0(message);
    semihost_write0("\n");
    semihost_exit(1);
}

static void pass(const char *message)
{
    semihost_write0("PASS: ");
    semihost_write0(message);
    semihost_write0("\n");
}

static void dma_complete_callback(DMA_HandleTypeDef *hdma)
{
    (void)hdma;
    g_dma_complete = 1U;
}

static void init_dma_handle(void)
{
    g_dma.Instance = DMA1_Stream0;
    g_dma.Init.Channel = DMA_CHANNEL_0;
    g_dma.Init.Direction = DMA_MEMORY_TO_MEMORY;
    g_dma.Init.PeriphInc = DMA_PINC_ENABLE;
    g_dma.Init.MemInc = DMA_MINC_ENABLE;
    g_dma.Init.PeriphDataAlignment = DMA_PDATAALIGN_WORD;
    g_dma.Init.MemDataAlignment = DMA_MDATAALIGN_WORD;
    g_dma.Init.Mode = DMA_NORMAL;
    g_dma.Init.Priority = DMA_PRIORITY_LOW;
    g_dma.Init.FIFOMode = DMA_FIFOMODE_DISABLE;
    g_dma.Init.FIFOThreshold = DMA_FIFO_THRESHOLD_FULL;
    g_dma.Init.MemBurst = DMA_MBURST_SINGLE;
    g_dma.Init.PeriphBurst = DMA_PBURST_SINGLE;
    g_dma.XferCpltCallback = dma_complete_callback;
}

static void probe_dma(void)
{
    init_dma_handle();

    if (HAL_DMA_Init(&g_dma) != HAL_OK) {
        fail("HAL_DMA_Init");
    }

    nvic_enable_irq(DMA1_STREAM0_IRQN);

    if (HAL_DMA_Start_IT(
            &g_dma,
            (uint32_t)(uintptr_t)dma_src_words,
            (uint32_t)(uintptr_t)dma_dst_words,
            4U) != HAL_OK) {
        fail("HAL_DMA_Start_IT");
    }

    for (uint32_t spin = 0; spin < 1000000U && g_dma_complete == 0U; ++spin) {
    }

    if (g_dma_complete == 0U) {
        fail("DMA IRQ did not complete");
    }

    if ((DMA1->LISR & DMA_LISR_TCIF0) != 0U) {
        fail("DMA TCIF0 not cleared by IRQ handler");
    }

    pass("DMA HAL init/start/IRQ");
}

static void init_eth_handle(void)
{
    g_eth.Instance = ETH;
    g_eth.Init.MACAddr = eth_mac_addr;
    g_eth.Init.MediaInterface = HAL_ETH_RMII_MODE;
    g_eth.Init.TxDesc = eth_tx_desc;
    g_eth.Init.RxDesc = eth_rx_desc;
    g_eth.Init.RxBuffLen = ETH_MAX_PACKET_SIZE;
}

static void probe_eth(void)
{
    init_eth_handle();
    nvic_enable_irq(ETH_IRQN);

    if (HAL_ETH_Init(&g_eth) != HAL_OK) {
        fail("HAL_ETH_Init");
    }

    if ((ETH->DMABMR & ETH_DMABMR_SR) != 0U) {
        fail("ETH DMABMR.SR did not self-clear");
    }

    if (HAL_ETH_Start(&g_eth) != HAL_OK) {
        fail("HAL_ETH_Start");
    }

    if ((ETH->MACCR & (ETH_MACCR_TE | ETH_MACCR_RE)) !=
        (ETH_MACCR_TE | ETH_MACCR_RE)) {
        fail("ETH MACCR transmit/receive bits not set");
    }

    if ((ETH->DMAOMR & (ETH_DMAOMR_ST | ETH_DMAOMR_SR)) !=
        (ETH_DMAOMR_ST | ETH_DMAOMR_SR)) {
        fail("ETH DMAOMR start bits not set");
    }

    if (eth_rx_desc[0].BackupAddr0 == 0U) {
        fail("ETH Rx buffer was not attached");
    }

    pass("ETH HAL init/start");
}

static void init_pcd_handle(void)
{
    g_pcd.Instance = USB_OTG_FS;
    g_pcd.Init.dev_endpoints = 4U;
    g_pcd.Init.Host_channels = 8U;
    g_pcd.Init.dma_enable = 0U;
    g_pcd.Init.speed = PCD_SPEED_FULL;
    g_pcd.Init.ep0_mps = USB_OTG_MAX_EP0_SIZE;
    g_pcd.Init.phy_itface = PCD_PHY_EMBEDDED;
    g_pcd.Init.Sof_enable = 0U;
    g_pcd.Init.low_power_enable = 0U;
    g_pcd.Init.lpm_enable = 0U;
    g_pcd.Init.battery_charging_enable = 0U;
    g_pcd.Init.vbus_sensing_enable = 0U;
    g_pcd.Init.use_dedicated_ep1 = 0U;
    g_pcd.Init.use_external_vbus = 0U;
}

static void probe_usb(void)
{
    init_pcd_handle();
    nvic_enable_irq(USB_OTG_FS_IRQN);

    if (HAL_PCD_Init(&g_pcd) != HAL_OK) {
        fail("HAL_PCD_Init");
    }

    if ((USB_OTG_FS->GRSTCTL & USB_OTG_GRSTCTL_AHBIDL) == 0U) {
        fail("USB AHB idle bit not asserted");
    }

    if (HAL_PCD_Start(&g_pcd) != HAL_OK) {
        fail("HAL_PCD_Start");
    }

    if ((USB_OTG_FS->GAHBCFG & USB_OTG_GAHBCFG_GINT) == 0U) {
        fail("USB global interrupt bit not set");
    }

    pass("USB PCD init/start");
}

static void probe_subghz(void)
{
    nvic_enable_irq(SUBGHZ_IRQN);

    MMIO32(SUBGHZ_BASE + SUBGHZ_RADIO_IRQMASK_OFFSET) = SUBGHZ_TX_DONE_BIT;
    MMIO32(SUBGHZ_BASE + SUBGHZ_SPIDR_OFFSET) = 0xAAU;

    for (uint32_t spin = 0; spin < 1000000U && g_subghz_complete == 0U; ++spin) {
    }

    if (g_subghz_complete == 0U) {
        fail("SUBGHZ IRQ did not fire");
    }

    if (MMIO32(SUBGHZ_BASE + SUBGHZ_RADIO_IRQSTATUS_OFFSET) != 0U) {
        fail("SUBGHZ IRQ status did not clear");
    }

    pass("SUBGHZ MMIO/IRQ");
}

void DMA1_Stream0_IRQHandler(void)
{
    HAL_DMA_IRQHandler(&g_dma);
}

void ETH_IRQHandler(void)
{
    HAL_ETH_IRQHandler(&g_eth);
}

void OTG_FS_IRQHandler(void)
{
    HAL_PCD_IRQHandler(&g_pcd);
}

void SUBGHZ_IRQHandler(void)
{
    uint32_t status = MMIO32(SUBGHZ_BASE + SUBGHZ_RADIO_IRQSTATUS_OFFSET);

    if ((status & SUBGHZ_TX_DONE_BIT) != 0U) {
        MMIO32(SUBGHZ_BASE + SUBGHZ_RADIO_IRQSTATUS_OFFSET) = status;
        g_subghz_complete = 1U;
    }
}

int main(void)
{
    semihost_write0("AutoEmu STM32F4 guest probe start\n");

    probe_dma();
    probe_eth();
    probe_usb();
    probe_subghz();

    semihost_write0("AutoEmu STM32F4 guest probe complete\n");
    semihost_exit(0);
}
