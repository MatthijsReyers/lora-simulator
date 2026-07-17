#include "stm32wlxx_hal.h"


HAL_StatusTypeDef HAL_Init(void) {
    return HAL_OK;
}

HAL_StatusTypeDef HAL_DeInit(void) {
    return HAL_OK;
}

__attribute__((weak))
void HAL_Delay(uint32_t delay_ms)
{
    sim_sleep(delay_ms / 1000.0);
}

uint32_t HAL_GetTick(void) {
    return (uint32_t)(sim_current_time() * 1000.0);
}

void HAL_IncTick(void) {
    sim_sleep(0.000001);
}

