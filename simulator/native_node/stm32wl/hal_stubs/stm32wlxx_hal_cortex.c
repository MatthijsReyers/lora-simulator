#include "stm32wlxx_hal_cortex.h"

/* Which interrupts the firmware enabled, so that the stubs only call enabled handlers. */
uint8_t sim_stm32_nvic_enabled[128] = {0};

int sim_stm32_nvic_irq_enabled(int irqn) {
    return irqn >= 0 && irqn < 128 && sim_stm32_nvic_enabled[irqn];
}

void HAL_NVIC_SetPriority(IRQn_Type IRQn, uint32_t PreemptPriority, uint32_t SubPriority) {}

void HAL_NVIC_EnableIRQ(IRQn_Type IRQn) {
    if (IRQn >= 0 && IRQn < 128) sim_stm32_nvic_enabled[IRQn] = 1;
}

void HAL_NVIC_DisableIRQ(IRQn_Type IRQn) {
    if (IRQn >= 0 && IRQn < 128) sim_stm32_nvic_enabled[IRQn] = 0;
}
