/**
 * Simulator shim for stm32wlxx_hal_iwdg.h — IWDG (watchdog) stubs
 */
#ifndef __STM32WLxx_HAL_IWDG_H
#define __STM32WLxx_HAL_IWDG_H

#include "stm32wlxx_hal_def.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct { int dummy; } IWDG_TypeDef;

typedef struct {
    uint32_t Prescaler;
    uint32_t Reload;
    uint32_t Window;
} IWDG_InitTypeDef;

typedef struct {
    IWDG_TypeDef     *Instance;
    IWDG_InitTypeDef  Init;
} IWDG_HandleTypeDef;

static IWDG_TypeDef _sim_iwdg __attribute__((unused));
#define IWDG (&_sim_iwdg)

#define IWDG_PRESCALER_256  6U
#define IWDG_WINDOW_DISABLE 0x00000FFFU

static inline HAL_StatusTypeDef HAL_IWDG_Init(IWDG_HandleTypeDef *hiwdg) {
    UNUSED(hiwdg); return HAL_OK;
}
static inline HAL_StatusTypeDef HAL_IWDG_Refresh(IWDG_HandleTypeDef *hiwdg) {
    UNUSED(hiwdg); return HAL_OK;
}

#define __HAL_DBGMCU_FREEZE_IWDG() do {} while(0)

#ifdef __cplusplus
}
#endif

#endif /* __STM32WLxx_HAL_IWDG_H */
