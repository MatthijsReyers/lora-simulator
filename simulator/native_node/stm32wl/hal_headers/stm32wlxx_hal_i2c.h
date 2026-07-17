/**
 * Simulator shim for stm32wlxx_hal_i2c.h
 *
 * Provides I2C handle types and function stubs.
 */
#ifndef __STM32WLxx_HAL_I2C_H
#define __STM32WLxx_HAL_I2C_H

#include "stm32wlxx_hal_def.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    uint32_t Timing;
    uint32_t OwnAddress1;
    uint32_t AddressingMode;
    uint32_t DualAddressMode;
    uint32_t OwnAddress2;
    uint32_t OwnAddress2Masks;
    uint32_t GeneralCallMode;
    uint32_t NoStretchMode;
} I2C_InitTypeDef;

typedef enum {
    HAL_I2C_STATE_RESET = 0x00,
    HAL_I2C_STATE_READY = 0x20,
    HAL_I2C_STATE_BUSY  = 0x24,
} HAL_I2C_StateTypeDef;

typedef struct {
    void              *Instance;
    I2C_InitTypeDef    Init;
    HAL_LockTypeDef    Lock;
    HAL_I2C_StateTypeDef State;
} I2C_HandleTypeDef;

#define I2C_ADDRESSINGMODE_7BIT  0x00000001U
#define I2C_DUALADDRESS_DISABLE  0x00000000U
#define I2C_GENERALCALL_DISABLE  0x00000000U
#define I2C_NOSTRETCH_DISABLE    0x00000000U

static inline HAL_StatusTypeDef HAL_I2C_Init(I2C_HandleTypeDef *hi2c) {
    UNUSED(hi2c); return HAL_OK;
}
static inline HAL_StatusTypeDef HAL_I2C_Mem_Read(I2C_HandleTypeDef *hi2c, uint16_t DevAddress, uint16_t MemAddress, uint16_t MemAddSize, uint8_t *pData, uint16_t Size, uint32_t Timeout) {
    UNUSED(hi2c); UNUSED(DevAddress); UNUSED(MemAddress); UNUSED(MemAddSize); UNUSED(pData); UNUSED(Size); UNUSED(Timeout); return HAL_OK;
}
static inline HAL_StatusTypeDef HAL_I2C_Mem_Write(I2C_HandleTypeDef *hi2c, uint16_t DevAddress, uint16_t MemAddress, uint16_t MemAddSize, uint8_t *pData, uint16_t Size, uint32_t Timeout) {
    UNUSED(hi2c); UNUSED(DevAddress); UNUSED(MemAddress); UNUSED(MemAddSize); UNUSED(pData); UNUSED(Size); UNUSED(Timeout); return HAL_OK;
}

#define I2C_MEMADD_SIZE_8BIT  0x00000001U
#define I2C_MEMADD_SIZE_16BIT 0x00000002U

#ifdef __cplusplus
}
#endif

#endif /* __STM32WLxx_HAL_I2C_H */
