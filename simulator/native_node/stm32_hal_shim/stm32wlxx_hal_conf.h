/**
 * Simulator shim for stm32wlxx_hal_conf.h
 *
 * Module enable defines and conditional sub-header includes.
 * Mirrors the structure of the real HAL config template.
 */
#ifndef __STM32WLxx_HAL_CONF_H
#define __STM32WLxx_HAL_CONF_H

#ifdef __cplusplus
extern "C" {
#endif

/* ---- Module enable defines ---------------------------------------------- */

#define HAL_MODULE_ENABLED
#define HAL_GPIO_MODULE_ENABLED
#define HAL_SPI_MODULE_ENABLED
#define HAL_I2C_MODULE_ENABLED
#define HAL_UART_MODULE_ENABLED
#define HAL_SUBGHZ_MODULE_ENABLED

/* ---- Oscillator values -------------------------------------------------- */

#if !defined(HSE_VALUE)
#define HSE_VALUE 32000000U
#endif

#if !defined(LSE_VALUE)
#define LSE_VALUE 32768U
#endif

#if !defined(MSI_VALUE)
#define MSI_VALUE 4000000U
#endif

#if !defined(HSI_VALUE)
#define HSI_VALUE 16000000U
#endif

#if !defined(LSI_VALUE)
#define LSI_VALUE 32000U
#endif

/* ---- Conditional includes ----------------------------------------------- */

#ifdef HAL_SPI_MODULE_ENABLED
#include "stm32wlxx_hal_spi.h"
#endif

#ifdef HAL_I2C_MODULE_ENABLED
#include "stm32wlxx_hal_i2c.h"
#endif

#ifdef HAL_UART_MODULE_ENABLED
#include "stm32wlxx_hal_uart.h"
#endif

#ifdef __cplusplus
}
#endif

#endif /* __STM32WLxx_HAL_CONF_H */
