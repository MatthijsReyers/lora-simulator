/**
 * Simulator shim for stm32wlxx_hal_rtc.h — RTC stubs
 */
#ifndef __STM32WLxx_HAL_RTC_H
#define __STM32WLxx_HAL_RTC_H

#include "stm32wlxx_hal_def.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct { int dummy; } RTC_TypeDef;

typedef struct {
    void *Instance;
} RTC_HandleTypeDef;

static RTC_TypeDef _sim_rtc __attribute__((unused));
#define RTC_INST (&_sim_rtc)

#ifdef __cplusplus
}
#endif

#endif /* __STM32WLxx_HAL_RTC_H */
