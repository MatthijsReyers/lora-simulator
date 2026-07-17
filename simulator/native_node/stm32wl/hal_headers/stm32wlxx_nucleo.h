/**
 * Simulator shim for stm32wlxx_nucleo.h
 *
 * Stub for NUCLEO board-specific definitions. The simulator doesn't need
 * board-specific LED/button mappings, so this is mostly empty.
 */
#ifndef __STM32WLxx_NUCLEO_H
#define __STM32WLxx_NUCLEO_H

#include "stm32wlxx_hal.h"

#ifdef __cplusplus
extern "C" {
#endif

/* LED definitions — no-op in simulator */
#define LED1_PIN  GPIO_PIN_15
#define LED2_PIN  GPIO_PIN_9
#define LED3_PIN  GPIO_PIN_11

static inline void BSP_LED_Init(int Led) { UNUSED(Led); }
static inline void BSP_LED_On(int Led)   { UNUSED(Led); }
static inline void BSP_LED_Off(int Led)  { UNUSED(Led); }
static inline void BSP_LED_Toggle(int Led) { UNUSED(Led); }

#ifdef __cplusplus
}
#endif

#endif /* __STM32WLxx_NUCLEO_H */
