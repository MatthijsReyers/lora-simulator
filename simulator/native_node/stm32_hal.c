/**
 * STM32 HAL shim for the lora-simulator.
 *
 * Maps common STM32 HAL functions to the simulator's native node primitives
 * (sim_sleep, sim_current_time). This file is automatically included when
 * using STM32Node.
 *
 * sim_sleep() and sim_current_time() are provided by native_node.c which is
 * concatenated before this file.
 */

#include "stm32wlxx_hal_def.h"

#ifdef __cplusplus
extern "C" {
#endif

void HAL_Delay(uint32_t Delay) {
    sim_sleep(Delay / 1000.0);
}

uint32_t HAL_GetTick(void) {
    return (uint32_t)(sim_current_time() * 1000.0);
}

void HAL_IncTick(void) {
    /* No-op: simulator manages time */
}

HAL_StatusTypeDef HAL_Init(void) {
    return HAL_OK;
}

HAL_StatusTypeDef HAL_DeInit(void) {
    return HAL_OK;
}

#ifdef __cplusplus
}
#endif
