#include "stm32wlxx_hal_subghz.h"

#define __weak      __attribute__((weak))


/**
 * Has the radio/sub gigahertz hardware been initialized yet?
 */
volatile int sim_stm32_hal_subghz_radio_init = 0;

/**
 * Global reference to callbacks object
 */
SUBGHZ_HandleTypeDef *sim_stm32_hal_subghz_callbacks = NULL;


HAL_StatusTypeDef HAL_SUBGHZ_Init(SUBGHZ_HandleTypeDef *hsubghz)
{
    sim_stm32_hal_subghz_callbacks = hsubghz;
    sim_stm32_hal_subghz_radio_init = 1;
    HAL_SUBGHZ_MspInit(hsubghz);
    return HAL_OK;
}

HAL_StatusTypeDef HAL_SUBGHZ_DeInit(SUBGHZ_HandleTypeDef *hsubghz)
{
    if (sim_stm32_hal_subghz_callbacks != hsubghz) {
        printf("ERROR: HAL_SUBGHZ_DeInit() called with a handle that does not match the registered callbacks.\n");
        return HAL_ERROR;
    }
    sim_stm32_hal_subghz_callbacks = NULL;
    sim_stm32_hal_subghz_radio_init = 0;
    HAL_SUBGHZ_MspDeInit(hsubghz);
    return HAL_OK;
}

__weak void HAL_SUBGHZ_MspInit(SUBGHZ_HandleTypeDef *hsubghz) 
{
    if (sim_stm32_hal_subghz_callbacks == NULL) {
        sim_stm32_hal_subghz_callbacks = hsubghz;
    }
    if (sim_stm32_hal_subghz_callbacks != hsubghz) {
        printf("ERROR: HAL_SUBGHZ_DeInit() called with a handle that does not match the registered callbacks.\n");
    }
}

__weak void HAL_SUBGHZ_MspDeInit(SUBGHZ_HandleTypeDef *hsubghz)
{
    if (sim_stm32_hal_subghz_callbacks == NULL) {
        return;
    }
    if (sim_stm32_hal_subghz_callbacks != hsubghz) {
        printf("ERROR: HAL_SUBGHZ_DeInit() called with a handle that does not match the registered callbacks.\n");
    }
}


#if (USE_HAL_SUBGHZ_REGISTER_CALLBACKS == 1)

HAL_StatusTypeDef HAL_SUBGHZ_RegisterCallback(
    SUBGHZ_HandleTypeDef *hsubghz,
    HAL_SUBGHZ_CallbackIDTypeDef CallbackID,
    pSUBGHZ_CallbackTypeDef pCallback
) {
    HAL_StatusTypeDef status = HAL_OK;

    if (pCallback == NULL)
    {
        printf("ERROR: HAL_SUBGHZ_RegisterCallback() received invalid callback.\n");
        hsubghz->ErrorCode |= HAL_SUBGHZ_ERROR_INVALID_CALLBACK;
        return HAL_ERROR;
    }

    if (HAL_SUBGHZ_STATE_READY == hsubghz->State)
    {
        switch (CallbackID)
        {
        case HAL_SUBGHZ_TX_COMPLETE_CB_ID :
            hsubghz->TxCpltCallback = pCallback;
            break;

        case HAL_SUBGHZ_RX_COMPLETE_CB_ID :
            hsubghz->RxCpltCallback = pCallback;
            break;

        case HAL_SUBGHZ_PREAMBLE_DETECTED_CB_ID :
            hsubghz->PreambleDetectedCallback = pCallback;
            break;

        case HAL_SUBGHZ_SYNCWORD_VALID_CB_ID :
            hsubghz->SyncWordValidCallback = pCallback;
            break;

        case HAL_SUBGHZ_HEADER_VALID_CB_ID :
            hsubghz->HeaderValidCallback = pCallback;
            break;

        case HAL_SUBGHZ_HEADER_ERROR_CB_ID :
            hsubghz->HeaderErrorCallback = pCallback;
            break;

        case HAL_SUBGHZ_CRC_ERROR_CB_ID :
            hsubghz->CRCErrorCallback = pCallback;
            break;

        case HAL_SUBGHZ_RX_TX_TIMEOUT_CB_ID :
            hsubghz->RxTxTimeoutCallback = pCallback;
            break;

        case HAL_SUBGHZ_MSPINIT_CB_ID :
            hsubghz->MspInitCallback = pCallback;
            break;

        case HAL_SUBGHZ_MSPDEINIT_CB_ID :
            hsubghz->MspDeInitCallback = pCallback;
            break;

        case HAL_SUBGHZ_LR_FHSS_HOP_CB_ID :
            hsubghz->LrFhssHopCallback = pCallback;
            break;

        default :
            printf("ERROR: HAL_SUBGHZ_RegisterCallback() received invalid callback.\n");
            hsubghz->ErrorCode = HAL_SUBGHZ_ERROR_INVALID_CALLBACK;
            status =  HAL_ERROR;
            break;
        }
    }
    else if (HAL_SUBGHZ_STATE_RESET == hsubghz->State)
    {
        switch (CallbackID)
        {
        case HAL_SUBGHZ_MSPINIT_CB_ID :
            hsubghz->MspInitCallback = pCallback;
            break;

        case HAL_SUBGHZ_MSPDEINIT_CB_ID :
            hsubghz->MspDeInitCallback = pCallback;
            break;

        default :
            hsubghz->ErrorCode = HAL_SUBGHZ_ERROR_INVALID_CALLBACK;
            status =  HAL_ERROR;
            break;
        }
    }
    else
    {
        hsubghz->ErrorCode = HAL_SUBGHZ_ERROR_INVALID_CALLBACK;
        status =  HAL_ERROR;
    }
    return status;
}

HAL_StatusTypeDef HAL_SUBGHZ_UnRegisterCallback(
    SUBGHZ_HandleTypeDef *hsubghz,
    HAL_SUBGHZ_CallbackIDTypeDef CallbackID
) {
    HAL_StatusTypeDef status = HAL_OK;
    if (HAL_SUBGHZ_STATE_READY == hsubghz->State)
    {
        switch (CallbackID)
        {
        case HAL_SUBGHZ_TX_COMPLETE_CB_ID :
            hsubghz->TxCpltCallback = HAL_SUBGHZ_TxCpltCallback;
            break;

        case HAL_SUBGHZ_RX_COMPLETE_CB_ID :
            hsubghz->RxCpltCallback = HAL_SUBGHZ_RxCpltCallback;
            break;

        case HAL_SUBGHZ_PREAMBLE_DETECTED_CB_ID :
            hsubghz->PreambleDetectedCallback = HAL_SUBGHZ_PreambleDetectedCallback;
            break;

        case HAL_SUBGHZ_SYNCWORD_VALID_CB_ID :
            hsubghz->SyncWordValidCallback = HAL_SUBGHZ_SyncWordValidCallback;
            break;

        case HAL_SUBGHZ_HEADER_VALID_CB_ID :
            hsubghz->HeaderValidCallback = HAL_SUBGHZ_HeaderValidCallback;
            break;

        case HAL_SUBGHZ_HEADER_ERROR_CB_ID :
            hsubghz->HeaderErrorCallback = HAL_SUBGHZ_HeaderErrorCallback;
            break;

        case HAL_SUBGHZ_CRC_ERROR_CB_ID :
            hsubghz->CRCErrorCallback = HAL_SUBGHZ_CRCErrorCallback;
            break;

        case HAL_SUBGHZ_RX_TX_TIMEOUT_CB_ID :
            hsubghz->RxTxTimeoutCallback = HAL_SUBGHZ_RxTxTimeoutCallback;
            break;

        case HAL_SUBGHZ_MSPINIT_CB_ID :
            hsubghz->MspInitCallback = HAL_SUBGHZ_MspInit;
            break;

        case HAL_SUBGHZ_MSPDEINIT_CB_ID :
            hsubghz->MspDeInitCallback = HAL_SUBGHZ_MspDeInit;
            break;

        case HAL_SUBGHZ_LR_FHSS_HOP_CB_ID :
            hsubghz->LrFhssHopCallback = HAL_SUBGHZ_LrFhssHopCallback;
            break;

        default :
            printf("ERROR: HAL_SUBGHZ_UnRegisterCallback() received invalid callback ID.\n");
            hsubghz->ErrorCode = HAL_SUBGHZ_ERROR_INVALID_CALLBACK;
            status =  HAL_ERROR;
            break;
        }
    }
    else if (HAL_SUBGHZ_STATE_RESET == hsubghz->State)
    {
        switch (CallbackID)
        {
        case HAL_SUBGHZ_MSPINIT_CB_ID :
            hsubghz->MspInitCallback = HAL_SUBGHZ_MspInit;
            break;

        case HAL_SUBGHZ_MSPDEINIT_CB_ID :
            hsubghz->MspDeInitCallback = HAL_SUBGHZ_MspDeInit;
            break;

        default :
            printf("ERROR: HAL_SUBGHZ_UnRegisterCallback() received invalid callback ID!.\n");
            hsubghz->ErrorCode = HAL_SUBGHZ_ERROR_INVALID_CALLBACK;
            status =  HAL_ERROR;
            break;
        }
    }
    else
    {
        hsubghz->ErrorCode = HAL_SUBGHZ_ERROR_INVALID_CALLBACK;
        status =  HAL_ERROR;
    }
    return status;
}

#endif // USE_HAL_SUBGHZ_REGISTER_CALLBACKS

HAL_StatusTypeDef HAL_SUBGHZ_WriteRegisters(
    SUBGHZ_HandleTypeDef *hsubghz,
    uint16_t Address,
    uint8_t *pBuffer,
    uint16_t Size
) {
    if (hsubghz->State == HAL_SUBGHZ_STATE_READY)
    {
        hsubghz->State = HAL_SUBGHZ_STATE_BUSY;

        if (!sim_stm32_hal_subghz_radio_init) {
            printf("ERROR: HAL_SUBGHZ_WriteRegisters called for uninitialized radio.\n");
            return HAL_ERROR;
        }

        // TODO: Actually handle the different registers and map them to simulator functions.
        printf("NOT IMPLEMENTED: Ignoring HAL_SUBGHZ_WriteRegisters call.\n");
        return HAL_ERROR;
    }
    else
    {
        return HAL_BUSY;
    }
    return HAL_ERROR;
}

HAL_StatusTypeDef HAL_SUBGHZ_ExecSetCmd(
    SUBGHZ_HandleTypeDef *hsubghz, 
    SUBGHZ_RadioSetCmd_t Command, 
    uint8_t *pBuffer,
    uint16_t Size
) {
    switch (Command) {
        case RADIO_SET_SLEEP:
            printf("NOT IMPLEMENTED: Ignoring RADIO_SET_SLEEP command.\n");
            break;
        case RADIO_SET_STANDBY:
            printf("NOT IMPLEMENTED: Ignoring RADIO_SET_STANDBY command.\n");
            break;
        case RADIO_SET_FS:
            printf("NOT IMPLEMENTED: Ignoring RADIO_SET_FS command.\n");
            break;
        case RADIO_SET_TX:
            printf("NOT IMPLEMENTED: Ignoring RADIO_SET_TX command.\n");
            break;
        case RADIO_SET_RX:
            printf("NOT IMPLEMENTED: Ignoring RADIO_SET_RX command.\n");
            break;
        default:
            printf("ERROR: HAL_SUBGHZ_ExecSetCmd received unhandled command %d\n", Command);
            return HAL_ERROR;
    }
    return HAL_ERROR;
}

HAL_StatusTypeDef HAL_SUBGHZ_ExecGetCmd(
    SUBGHZ_HandleTypeDef *hsubghz, 
    SUBGHZ_RadioGetCmd_t Command, 
    uint8_t *pBuffer,
    uint16_t Size
) {
    return HAL_ERROR;
}

// Default weak callbacks (nops), the user can override these by providing their own implementations.
__weak void HAL_SUBGHZ_IRQHandler(SUBGHZ_HandleTypeDef *hsubghz) {}
__weak void HAL_SUBGHZ_TxCpltCallback(SUBGHZ_HandleTypeDef *hsubghz) {}
__weak void HAL_SUBGHZ_RxCpltCallback(SUBGHZ_HandleTypeDef *hsubghz) {}
__weak void HAL_SUBGHZ_PreambleDetectedCallback(SUBGHZ_HandleTypeDef *hsubghz) {}
__weak void HAL_SUBGHZ_SyncWordValidCallback(SUBGHZ_HandleTypeDef *hsubghz) {}
__weak void HAL_SUBGHZ_HeaderValidCallback(SUBGHZ_HandleTypeDef *hsubghz) {}
__weak void HAL_SUBGHZ_HeaderErrorCallback(SUBGHZ_HandleTypeDef *hsubghz) {}
__weak void HAL_SUBGHZ_CRCErrorCallback(SUBGHZ_HandleTypeDef *hsubghz) {}
__weak void HAL_SUBGHZ_CADStatusCallback(SUBGHZ_HandleTypeDef *hsubghz, HAL_SUBGHZ_CadStatusTypeDef cadstatus) {}
__weak void HAL_SUBGHZ_RxTxTimeoutCallback(SUBGHZ_HandleTypeDef *hsubghz) {}
__weak void HAL_SUBGHZ_LrFhssHopCallback(SUBGHZ_HandleTypeDef *hsubghz) {}



