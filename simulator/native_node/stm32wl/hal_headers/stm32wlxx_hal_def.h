/**
 * Simulator shim for stm32wlxx_hal_def.h
 * 
 * Provides core HAL type definitions (HAL_StatusTypeDef, HAL_LockTypeDef)
 * and common macros needed by STM32WL firmware.
 */
#ifndef __STM32WLxx_HAL_DEF
#define __STM32WLxx_HAL_DEF

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    HAL_OK       = 0x00,
    HAL_ERROR    = 0x01,
    HAL_BUSY     = 0x02,
    HAL_TIMEOUT  = 0x03
} HAL_StatusTypeDef;

typedef enum {
    HAL_UNLOCKED = 0x00,
    HAL_LOCKED   = 0x01
} HAL_LockTypeDef;

#define HAL_MAX_DELAY      0xFFFFFFFFU

#define UNUSED(X) (void)X

#define __HAL_LOCK(__HANDLE__)    do { (void)(__HANDLE__); } while(0)
#define __HAL_UNLOCK(__HANDLE__)  do { (void)(__HANDLE__); } while(0)

#define __weak   __attribute__((weak))
#define __packed __attribute__((__packed__))

#define __ALIGN_BEGIN
#define __ALIGN_END __attribute__((aligned(4)))

#ifndef __IO
#define __IO volatile
#endif

#ifndef __I
#define __I volatile const
#endif

#ifndef __O
#define __O volatile
#endif

#ifdef __cplusplus
}
#endif

#endif /* __STM32WLxx_HAL_DEF */
