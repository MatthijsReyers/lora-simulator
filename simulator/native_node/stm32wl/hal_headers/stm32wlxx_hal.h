/**
 * Simulator shim for stm32wlxx_hal.h
 *
 * Drop-in replacement for the real STM32WL HAL header. Provides declarations
 * for HAL functions that are implemented by the simulator's stm32_hal.c.
 */
#ifndef __STM32WLxx_HAL_H
#define __STM32WLxx_HAL_H

#ifdef __cplusplus
extern "C" {
#endif

#include "stm32wlxx_hal_def.h"
#include "stm32wlxx_hal_conf.h"

typedef enum
{
  HAL_TICK_FREQ_10HZ         = 100U,
  HAL_TICK_FREQ_100HZ        = 10U,
  HAL_TICK_FREQ_1KHZ         = 1U,
  HAL_TICK_FREQ_DEFAULT      = HAL_TICK_FREQ_1KHZ
} HAL_TickFreqTypeDef;

/* ---- HAL Init / DeInit -------------------------------------------------- */

HAL_StatusTypeDef HAL_Init(void);
HAL_StatusTypeDef HAL_DeInit(void);
void HAL_MspInit(void);
void HAL_MspDeInit(void);
HAL_StatusTypeDef HAL_InitTick(uint32_t TickPriority);


/* ---- Timing ------------------------------------------------------------- */

void     HAL_Delay(uint32_t Delay);
uint32_t HAL_GetTick(void);
void     HAL_IncTick(void);

/* ---- Device UID (96-bit unique device identifier) ----------------------- */

static inline uint32_t HAL_GetUIDw0(void) { return sim_radio_id(); }
static inline uint32_t HAL_GetUIDw1(void) { return 0xDEADBEEF; }
static inline uint32_t HAL_GetUIDw2(void) { return 0x12345678; }


#ifdef __cplusplus
}
#endif

#endif /* __STM32WLxx_HAL_H */
