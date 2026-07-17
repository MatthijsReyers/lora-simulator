/**
 * Simulator shim for stm32wlxx_ll_system.h
 *
 * Low-level system functions — stubs in the simulator.
 */
#ifndef __STM32WLxx_LL_SYSTEM_H
#define __STM32WLxx_LL_SYSTEM_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

static inline uint32_t LL_FLASH_GetUDN(void) { return 0x12345678U; }
static inline uint32_t LL_FLASH_GetDeviceID(void) { return 0x0497; }
static inline uint32_t LL_FLASH_GetSTCompanyID(void) { return 0x0080E1; }

#ifdef __cplusplus
}
#endif

#endif /* __STM32WLxx_LL_SYSTEM_H */
