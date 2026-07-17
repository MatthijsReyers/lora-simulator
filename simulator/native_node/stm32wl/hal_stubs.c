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
#include "stm32wlxx.h"

#ifdef __cplusplus
extern "C" {
#endif

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

void sim_nop() {}

/* 256Kb buffer that we can use as the flash for any HAL functions that want to read it. */
#define SIM_FLASH_SIZE 256000
uint8_t flash_buf[SIM_FLASH_SIZE];



/* ---- NVIC backing store for x86 simulator ----------------------------- */
NVIC_Type sim_nvic = {0};  /* real memory for NVIC->ISER/ICER/ISPR/etc. */


#include "core_cm4.h"

SCB_Type sim_scb_stub;

#ifdef __cplusplus
}
#endif
