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


/* ---- Simulated SX126x radio ------------------------------------------------------------------
 *
 * The STM32WL talks to its radio by sending it SX126x commands, firmware does that with
 * HAL_SUBGHZ_ExecSetCmd() and friends. Below we keep track of what the firmware configured and
 * translate it to the simulator's radio (the sim_radio_* / sim_transmit_* / sim_receive_*
 * functions of radio_node.c).
 *
 * Timing: RADIO_SET_TX and RADIO_SET_RX only *arm* the radio. The transmission/reception itself
 * runs when the firmware goes to sleep waiting for the radio interrupt, which is when
 * HAL_PWR_EnterSLEEPMode() calls sim_stm32_hal_subghz_run_pending(). Once the operation is
 * finished the firmware's SUBGHZ_Radio_IRQHandler() is called, just like the real interrupt.
 * So firmware has to sleep with HAL_PWR_EnterSLEEPMode() right after starting a TX/RX, calling
 * HAL_Delay() first postpones the radio operation by that delay.
 *
 * Not modelled: FSK, CAD, RX duty cycle, RSSI/SNR readout, and the radio falling back to standby
 * after a reception (the simulated radio stays in receive mode until the next transmission).
 */
#include <string.h>
#include "stm32wlxx.h"

#define SIM_SUBGHZ_IRQ_TX_DONE  0x0001
#define SIM_SUBGHZ_IRQ_RX_DONE  0x0002
#define SIM_SUBGHZ_IRQ_TIMEOUT  0x0200

#define SIM_SUBGHZ_OP_NONE 0
#define SIM_SUBGHZ_OP_TX   1
#define SIM_SUBGHZ_OP_RX   2

struct {
    uint8_t  buffer[256];       /* The radio's data buffer (shared by TX and RX) */
    uint8_t  tx_base, rx_base;  /* RADIO_SET_BUFFERBASEADDRESS */
    uint8_t  rx_len;            /* Length of the last received payload */
    uint16_t irq_mask;          /* Interrupts enabled with RADIO_CFG_DIOIRQ */
    uint16_t irq_status;        /* Interrupts that fired and have not been cleared yet */
    int      pending_op;        /* SIM_SUBGHZ_OP_*, armed by RADIO_SET_TX / RADIO_SET_RX */
    int      rx_timeout_ms;     /* -1 means no timeout */
    int      rx_continuous;
    int      power;             /* dBm, RADIO_SET_TXPARAMS */
    int      sf, bw_khz, cr;    /* RADIO_SET_MODULATIONPARAMS (cr is the denominator: 5..8) */
    int      preamble_len, fixed_len, payload_len, crc_on, iq_inverted; /* RADIO_SET_PACKETPARAMS */
} sim_subghz;

/* These live in radio_node.c / native_node.c / the Python side. */
int sim_transmit_blocking(uint8_t *data, int len);
int sim_receive_blocking(uint8_t *data, int len, int timeout_ms);
void sim_exit(void);
int sim_stm32_nvic_irq_enabled(int irqn);
extern int SIM_ERR_TIMEOUT;

/* The firmware's radio interrupt handler, optional. */
void SUBGHZ_Radio_IRQHandler(void) __attribute__((weak));

/**
 * Run the transmission/reception that was armed with RADIO_SET_TX / RADIO_SET_RX (blocking the
 * firmware until it is done) and fire the radio interrupt.
 * Returns 0 if nothing was pending.
 */
int sim_stm32_hal_subghz_run_pending(void)
{
    int op = sim_subghz.pending_op;
    sim_subghz.pending_op = SIM_SUBGHZ_OP_NONE;

    if (op == SIM_SUBGHZ_OP_TX) {
        sim_radio_set_tx_config(
            sim_subghz.power, sim_subghz.bw_khz, sim_subghz.sf, sim_subghz.cr,
            sim_subghz.preamble_len, sim_subghz.fixed_len, sim_subghz.crc_on, 0,
            sim_subghz.iq_inverted, 0
        );
        if (sim_transmit_blocking(&sim_subghz.buffer[sim_subghz.tx_base], sim_subghz.payload_len) != 0) {
            sim_exit(); /* The simulation is over */
        }
        sim_subghz.irq_status |= SIM_SUBGHZ_IRQ_TX_DONE;
    }
    else if (op == SIM_SUBGHZ_OP_RX) {
        sim_radio_set_rx_config(
            sim_subghz.bw_khz, sim_subghz.sf, sim_subghz.cr, sim_subghz.preamble_len,
            sim_subghz.payload_len, 0, sim_subghz.fixed_len, sim_subghz.crc_on,
            sim_subghz.iq_inverted, sim_subghz.rx_continuous
        );
        int res = sim_receive_blocking(
            &sim_subghz.buffer[sim_subghz.rx_base], 256 - sim_subghz.rx_base,
            sim_subghz.rx_timeout_ms
        );
        if (res >= 0) {
            sim_subghz.rx_len = res > 255 ? 255 : res;
            sim_subghz.irq_status |= SIM_SUBGHZ_IRQ_RX_DONE;
            if (sim_subghz.rx_continuous) {
                sim_subghz.pending_op = SIM_SUBGHZ_OP_RX;
            }
        } else if (res == SIM_ERR_TIMEOUT && !sim_is_finished()) {
            sim_subghz.irq_status |= SIM_SUBGHZ_IRQ_TIMEOUT;
        } else {
            sim_exit();
        }
    }
    else {
        return 0;
    }

    sim_subghz.irq_status &= sim_subghz.irq_mask;
    if (sim_subghz.irq_status != 0) {
        if (!sim_stm32_nvic_irq_enabled(SUBGHZ_Radio_IRQn)) {
            printf("WARNING: radio interrupt fired but SUBGHZ_Radio_IRQn is not enabled in the NVIC.\n");
        } else if (SUBGHZ_Radio_IRQHandler) {
            SUBGHZ_Radio_IRQHandler();
        }
    }
    return 1;
}


HAL_StatusTypeDef HAL_SUBGHZ_Init(SUBGHZ_HandleTypeDef *hsubghz)
{
    sim_stm32_hal_subghz_callbacks = hsubghz;
    sim_stm32_hal_subghz_radio_init = 1;
    HAL_SUBGHZ_MspInit(hsubghz);
    memset(&sim_subghz, 0, sizeof(sim_subghz));
    hsubghz->ErrorCode = HAL_SUBGHZ_ERROR_NONE;
    hsubghz->State = HAL_SUBGHZ_STATE_READY;
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

/* Registers are only used for chip workarounds and fine tuning, the simulator has no use for them. */
HAL_StatusTypeDef HAL_SUBGHZ_WriteRegisters(
    SUBGHZ_HandleTypeDef *hsubghz,
    uint16_t Address,
    uint8_t *pBuffer,
    uint16_t Size
) {
    return HAL_OK;
}

HAL_StatusTypeDef HAL_SUBGHZ_ReadRegisters(
    SUBGHZ_HandleTypeDef *hsubghz,
    uint16_t Address,
    uint8_t *pBuffer,
    uint16_t Size
) {
    memset(pBuffer, 0, Size);
    return HAL_OK;
}

HAL_StatusTypeDef HAL_SUBGHZ_WriteRegister(SUBGHZ_HandleTypeDef *hsubghz, uint16_t Address, uint8_t Value) {
    return HAL_SUBGHZ_WriteRegisters(hsubghz, Address, &Value, 1);
}

HAL_StatusTypeDef HAL_SUBGHZ_ReadRegister(SUBGHZ_HandleTypeDef *hsubghz, uint16_t Address, uint8_t *pValue) {
    return HAL_SUBGHZ_ReadRegisters(hsubghz, Address, pValue, 1);
}

HAL_StatusTypeDef HAL_SUBGHZ_WriteBuffer(
    SUBGHZ_HandleTypeDef *hsubghz,
    uint8_t Offset,
    uint8_t *pBuffer,
    uint16_t Size
) {
    if (Offset + Size > 256) {
        return HAL_ERROR;
    }
    memcpy(&sim_subghz.buffer[Offset], pBuffer, Size);
    return HAL_OK;
}

HAL_StatusTypeDef HAL_SUBGHZ_ReadBuffer(
    SUBGHZ_HandleTypeDef *hsubghz,
    uint8_t Offset,
    uint8_t *pBuffer,
    uint16_t Size
) {
    if (Offset + Size > 256) {
        return HAL_ERROR;
    }
    memcpy(pBuffer, &sim_subghz.buffer[Offset], Size);
    return HAL_OK;
}

HAL_StatusTypeDef HAL_SUBGHZ_ExecSetCmd(
    SUBGHZ_HandleTypeDef *hsubghz, 
    SUBGHZ_RadioSetCmd_t Command, 
    uint8_t *pBuffer,
    uint16_t Size
) {
    if (!sim_stm32_hal_subghz_radio_init) {
        printf("ERROR: HAL_SUBGHZ_ExecSetCmd called for uninitialized radio.\n");
        return HAL_ERROR;
    }

    /* Number of parameter bytes the simulator needs for each command. */
    uint16_t needed = 0;
    switch (Command) {
        case RADIO_SET_PACKETTYPE:          needed = 1; break;
        case RADIO_SET_RFFREQUENCY:         needed = 4; break;
        case RADIO_SET_TXPARAMS:            needed = 2; break;
        case RADIO_SET_BUFFERBASEADDRESS:   needed = 2; break;
        case RADIO_SET_MODULATIONPARAMS:    needed = 4; break;
        case RADIO_SET_PACKETPARAMS:        needed = 6; break;
        case RADIO_CFG_DIOIRQ:              needed = 2; break;
        case RADIO_CLR_IRQSTATUS:           needed = 2; break;
        case RADIO_SET_TX:                  needed = 3; break;
        case RADIO_SET_RX:                  needed = 3; break;
        default: break;
    }
    if (Size < needed) {
        printf("ERROR: HAL_SUBGHZ_ExecSetCmd command 0x%02X needs %d parameter bytes, got %d\n", Command, needed, Size);
        return HAL_ERROR;
    }

    switch (Command) {
        case RADIO_SET_PACKETTYPE:
            if (pBuffer[0] != 0x01) {
                sim_error("Lora is the only packet type supported by lora-simulator");
            }
            break;

        case RADIO_SET_RFFREQUENCY: {
            /* The radio takes the frequency in steps of 32MHz / 2^25. */
            uint64_t steps = ((uint64_t)pBuffer[0] << 24) | (pBuffer[1] << 16) | (pBuffer[2] << 8) | pBuffer[3];
            sim_radio_set_channel((uint32_t)((steps * 32000000ULL + (1ULL << 24)) >> 25));
            break;
        }

        case RADIO_SET_TXPARAMS:
            sim_subghz.power = (int8_t)pBuffer[0];
            break;

        case RADIO_SET_BUFFERBASEADDRESS:
            sim_subghz.tx_base = pBuffer[0];
            sim_subghz.rx_base = pBuffer[1];
            break;

        case RADIO_SET_MODULATIONPARAMS:
            sim_subghz.sf = pBuffer[0];
            switch (pBuffer[1]) {
                case 0x04: sim_subghz.bw_khz = 125; break;
                case 0x05: sim_subghz.bw_khz = 250; break;
                case 0x06: sim_subghz.bw_khz = 500; break;
                default: sim_error("lora-simulator only supports 125, 250 and 500 kHz bandwidth");
            }
            sim_subghz.cr = pBuffer[2] + 4;
            break;

        case RADIO_SET_PACKETPARAMS:
            sim_subghz.preamble_len = (pBuffer[0] << 8) | pBuffer[1];
            sim_subghz.fixed_len    = pBuffer[2];
            sim_subghz.payload_len  = pBuffer[3];
            sim_subghz.crc_on       = pBuffer[4];
            sim_subghz.iq_inverted  = pBuffer[5];
            break;

        case RADIO_CFG_DIOIRQ:
            sim_subghz.irq_mask = (pBuffer[0] << 8) | pBuffer[1];
            break;

        case RADIO_CLR_IRQSTATUS:
            sim_subghz.irq_status &= ~((pBuffer[0] << 8) | pBuffer[1]);
            break;

        case RADIO_SET_TX:
            if (sim_subghz.sf == 0 || sim_subghz.preamble_len == 0) {
                sim_error("RADIO_SET_TX: set the modulation and packet parameters first");
            }
            sim_subghz.pending_op = SIM_SUBGHZ_OP_TX;
            break;

        case RADIO_SET_RX: {
            if (sim_subghz.sf == 0 || sim_subghz.preamble_len == 0) {
                sim_error("RADIO_SET_RX: set the modulation and packet parameters first");
            }
            /* Timeout is in steps of 15.625us: 0 means no timeout, 0xFFFFFF continuous receive. */
            uint32_t timeout = (pBuffer[0] << 16) | (pBuffer[1] << 8) | pBuffer[2];
            sim_subghz.rx_continuous = (timeout == 0xFFFFFF);
            sim_subghz.rx_timeout_ms = (timeout == 0 || timeout == 0xFFFFFF) ? -1 : (int)((timeout + 63) / 64);
            sim_subghz.pending_op = SIM_SUBGHZ_OP_RX;
            break;
        }

        case RADIO_SET_SLEEP:
        case RADIO_SET_STANDBY:
        case RADIO_SET_FS:
            sim_subghz.pending_op = SIM_SUBGHZ_OP_NONE;
            break;

        /* Hardware setup that has no equivalent in the simulator. */
        case RADIO_SET_PACONFIG:
        case RADIO_CALIBRATE:
        case RADIO_CALIBRATEIMAGE:
        case RADIO_SET_REGULATORMODE:
        case RADIO_SET_TCXOMODE:
        case RADIO_SET_TXFALLBACKMODE:
        case RADIO_SET_RFSWITCHMODE:
        case RADIO_SET_STOPRXTIMERONPREAMBLE:
        case RADIO_SET_LORASYMBTIMEOUT:
        case RADIO_RESET_STATS:
        case RADIO_CLR_ERROR:
            break;

        default:
            printf("NOT IMPLEMENTED: HAL_SUBGHZ_ExecSetCmd command 0x%02X\n", Command);
            return HAL_ERROR;
    }
    return HAL_OK;
}

HAL_StatusTypeDef HAL_SUBGHZ_ExecGetCmd(
    SUBGHZ_HandleTypeDef *hsubghz, 
    SUBGHZ_RadioGetCmd_t Command, 
    uint8_t *pBuffer,
    uint16_t Size
) {
    uint8_t reply[2] = {0, 0};
    switch (Command) {
        case RADIO_GET_IRQSTATUS:
            reply[0] = sim_subghz.irq_status >> 8;
            reply[1] = sim_subghz.irq_status & 0xFF;
            break;
        case RADIO_GET_RXBUFFERSTATUS:
            reply[0] = sim_subghz.rx_len;
            reply[1] = sim_subghz.rx_base;
            break;
        case RADIO_GET_PACKETTYPE:
            reply[0] = 0x01; /* LoRa */
            break;
        case RADIO_GET_ERROR:
            break;
        default:
            printf("NOT IMPLEMENTED: HAL_SUBGHZ_ExecGetCmd command 0x%02X\n", Command);
            return HAL_ERROR;
    }
    memset(pBuffer, 0, Size);
    memcpy(pBuffer, reply, Size < 2 ? Size : 2);
    return HAL_OK;
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



