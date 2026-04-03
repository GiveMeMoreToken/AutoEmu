#include "semihost.h"

#include <stdint.h>

int main(void);
void SystemInit(void);

extern uint32_t _sidata;
extern uint32_t _sdata;
extern uint32_t _edata;
extern uint32_t _sbss;
extern uint32_t _ebss;
extern uint32_t _stack_top;

void Reset_Handler(void);
void Default_Handler(void);
void DMA1_Stream0_IRQHandler(void);
void ETH_IRQHandler(void);
void OTG_FS_IRQHandler(void);
void SUBGHZ_IRQHandler(void);

__attribute__((section(".isr_vector"), used))
const uintptr_t vector_table[16 + 96] = {
    [0] = (uintptr_t)&_stack_top,
    [1] = (uintptr_t)Reset_Handler,
    [2] = (uintptr_t)Default_Handler,
    [3] = (uintptr_t)Default_Handler,
    [4] = (uintptr_t)Default_Handler,
    [5] = (uintptr_t)Default_Handler,
    [6] = (uintptr_t)Default_Handler,
    [7] = (uintptr_t)Default_Handler,
    [8] = (uintptr_t)Default_Handler,
    [9] = (uintptr_t)Default_Handler,
    [10] = (uintptr_t)Default_Handler,
    [11] = (uintptr_t)Default_Handler,
    [12] = (uintptr_t)Default_Handler,
    [13] = (uintptr_t)Default_Handler,
    [14] = (uintptr_t)Default_Handler,
    [15] = (uintptr_t)Default_Handler,
    [16 + 11] = (uintptr_t)DMA1_Stream0_IRQHandler,
    [16 + 61] = (uintptr_t)ETH_IRQHandler,
    [16 + 67] = (uintptr_t)OTG_FS_IRQHandler,
    [16 + 85] = (uintptr_t)SUBGHZ_IRQHandler,
};

void Reset_Handler(void)
{
    uint32_t *src = &_sidata;
    uint32_t *dst = &_sdata;

    while (dst < &_edata) {
        *dst++ = *src++;
    }

    dst = &_sbss;
    while (dst < &_ebss) {
        *dst++ = 0U;
    }

    SystemInit();

    if (main() != 0) {
        semihost_exit(1);
    }

    semihost_exit(0);
}

void Default_Handler(void)
{
    semihost_write0("unexpected-exception\n");
    semihost_exit(1);
}
