/**
 * Simulator shim for stm32wlxx_hal_spi.h
 *
 * Provides SPI handle types and function stubs. SPI transactions in the
 * simulator return HAL_OK and do nothing — sensor drivers that depend on
 * SPI data should be mocked at a higher level.
 */
#ifndef __STM32WLxx_HAL_SPI_H
#define __STM32WLxx_HAL_SPI_H

#include "stm32wlxx_hal_def.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    uint32_t Mode;
    uint32_t Direction;
    uint32_t DataSize;
    uint32_t CLKPolarity;
    uint32_t CLKPhase;
    uint32_t NSS;
    uint32_t BaudRatePrescaler;
    uint32_t FirstBit;
    uint32_t TIMode;
    uint32_t CRCCalculation;
    uint32_t CRCPolynomial;
    uint32_t CRCLength;
    uint32_t NSSPMode;
} SPI_InitTypeDef;

typedef enum {
    HAL_SPI_STATE_RESET = 0x00,
    HAL_SPI_STATE_READY = 0x01,
    HAL_SPI_STATE_BUSY  = 0x02,
} HAL_SPI_StateTypeDef;

typedef struct {
    void               *Instance;
    SPI_InitTypeDef     Init;
    HAL_LockTypeDef     Lock;
    HAL_SPI_StateTypeDef State;
} SPI_HandleTypeDef;

/* SPI mode/direction/etc. defines */
#define SPI_MODE_MASTER         0x00000104U
#define SPI_MODE_SLAVE          0x00000000U
#define SPI_DIRECTION_2LINES    0x00000000U
#define SPI_DATASIZE_8BIT       0x00000700U
#define SPI_POLARITY_LOW        0x00000000U
#define SPI_POLARITY_HIGH       0x00000002U
#define SPI_PHASE_1EDGE         0x00000000U
#define SPI_PHASE_2EDGE         0x00000001U
#define SPI_NSS_SOFT            0x00000200U
#define SPI_BAUDRATEPRESCALER_2 0x00000000U
#define SPI_FIRSTBIT_MSB        0x00000000U

static inline HAL_StatusTypeDef HAL_SPI_Init(SPI_HandleTypeDef *hspi) {
    UNUSED(hspi); return HAL_OK;
}
static inline HAL_StatusTypeDef HAL_SPI_Transmit(SPI_HandleTypeDef *hspi, uint8_t *pData, uint16_t Size, uint32_t Timeout) {
    UNUSED(hspi); UNUSED(pData); UNUSED(Size); UNUSED(Timeout); return HAL_OK;
}
static inline HAL_StatusTypeDef HAL_SPI_Receive(SPI_HandleTypeDef *hspi, uint8_t *pData, uint16_t Size, uint32_t Timeout) {
    UNUSED(hspi); UNUSED(pData); UNUSED(Size); UNUSED(Timeout); return HAL_OK;
}
static inline HAL_StatusTypeDef HAL_SPI_TransmitReceive(SPI_HandleTypeDef *hspi, uint8_t *pTxData, uint8_t *pRxData, uint16_t Size, uint32_t Timeout) {
    UNUSED(hspi); UNUSED(pTxData); UNUSED(pRxData); UNUSED(Size); UNUSED(Timeout); return HAL_OK;
}

#ifdef __cplusplus
}
#endif

#endif /* __STM32WLxx_HAL_SPI_H */
