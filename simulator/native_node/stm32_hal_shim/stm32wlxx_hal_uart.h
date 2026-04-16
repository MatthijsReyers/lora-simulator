/**
 * Simulator shim for stm32wlxx_hal_uart.h
 *
 * Provides UART handle types and function stubs.
 */
#ifndef __STM32WLxx_HAL_UART_H
#define __STM32WLxx_HAL_UART_H

#include "stm32wlxx_hal_def.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    uint32_t BaudRate;
    uint32_t WordLength;
    uint32_t StopBits;
    uint32_t Parity;
    uint32_t Mode;
    uint32_t HwFlowCtl;
    uint32_t OverSampling;
    uint32_t OneBitSampling;
    uint32_t ClockPrescaler;
} UART_InitTypeDef;

typedef enum {
    HAL_UART_STATE_RESET = 0x00,
    HAL_UART_STATE_READY = 0x20,
    HAL_UART_STATE_BUSY  = 0x24,
} HAL_UART_StateTypeDef;

typedef struct {
    void               *Instance;
    UART_InitTypeDef    Init;
    HAL_LockTypeDef     Lock;
    HAL_UART_StateTypeDef State;
} UART_HandleTypeDef;

#define UART_WORDLENGTH_8B   0x00000000U
#define UART_STOPBITS_1      0x00000000U
#define UART_PARITY_NONE     0x00000000U
#define UART_MODE_TX_RX      0x0000000CU
#define UART_HWCONTROL_NONE  0x00000000U
#define UART_OVERSAMPLING_16 0x00000000U

static inline HAL_StatusTypeDef HAL_UART_Init(UART_HandleTypeDef *huart) {
    UNUSED(huart); return HAL_OK;
}
static inline HAL_StatusTypeDef HAL_UART_Transmit(UART_HandleTypeDef *huart, const uint8_t *pData, uint16_t Size, uint32_t Timeout) {
    UNUSED(huart); UNUSED(pData); UNUSED(Size); UNUSED(Timeout); return HAL_OK;
}
static inline HAL_StatusTypeDef HAL_UART_Receive(UART_HandleTypeDef *huart, uint8_t *pData, uint16_t Size, uint32_t Timeout) {
    UNUSED(huart); UNUSED(pData); UNUSED(Size); UNUSED(Timeout); return HAL_OK;
}

#ifdef __cplusplus
}
#endif

#endif /* __STM32WLxx_HAL_UART_H */
