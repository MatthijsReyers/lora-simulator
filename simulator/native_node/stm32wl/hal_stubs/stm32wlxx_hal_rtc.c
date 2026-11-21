#include "stm32wlxx_hal_conf.h"
#include "stm32wlxx.h"

#ifdef HAL_RTC_MODULE_ENABLED

/**
 * Real-time clock, the simulator only models its wake-up timer: firmware arms it with
 * HAL_RTCEx_SetWakeUpTimer_IT() and then sleeps or stops the CPU, see
 * sim_stm32_wait_for_interrupt() in stm32wlxx_hal_pwr.c. The clock is assumed to run on a
 * 32.768 kHz crystal with the usual 1 Hz calendar prescalers.
 */

/* Is the wake-up timer running, and how many seconds does it take to expire? */
int sim_stm32_rtc_wakeup_armed = 0;
double sim_stm32_rtc_wakeup_period = 0.0;

int sim_stm32_nvic_irq_enabled(int irqn);

/* The firmware's handler for the wake-up timer interrupt and its HAL callback, both optional. */
void RTC_WKUP_IRQHandler(void) __attribute__((weak));
void HAL_RTCEx_WakeUpTimerEventCallback(RTC_HandleTypeDef *hrtc) __attribute__((weak));

HAL_StatusTypeDef HAL_RTC_Init(RTC_HandleTypeDef *hrtc) {
    hrtc->State = HAL_RTC_STATE_READY;
    return HAL_OK;
}

HAL_StatusTypeDef HAL_RTCEx_SetWakeUpTimer_IT(
    RTC_HandleTypeDef *hrtc, uint32_t WakeUpCounter, uint32_t WakeUpClock, uint32_t WakeUpAutoClr
) {
    switch (WakeUpClock) {
        case RTC_WAKEUPCLOCK_RTCCLK_DIV16:   sim_stm32_rtc_wakeup_period = (WakeUpCounter + 1) * 16.0 / 32768.0; break;
        case RTC_WAKEUPCLOCK_RTCCLK_DIV8:    sim_stm32_rtc_wakeup_period = (WakeUpCounter + 1) * 8.0 / 32768.0; break;
        case RTC_WAKEUPCLOCK_RTCCLK_DIV4:    sim_stm32_rtc_wakeup_period = (WakeUpCounter + 1) * 4.0 / 32768.0; break;
        case RTC_WAKEUPCLOCK_RTCCLK_DIV2:    sim_stm32_rtc_wakeup_period = (WakeUpCounter + 1) * 2.0 / 32768.0; break;
        case RTC_WAKEUPCLOCK_CK_SPRE_16BITS: sim_stm32_rtc_wakeup_period = (WakeUpCounter + 1) * 1.0; break;
        default:
            printf("NOT IMPLEMENTED: HAL_RTCEx_SetWakeUpTimer_IT wake-up clock 0x%X\n", WakeUpClock);
            return HAL_ERROR;
    }
    sim_stm32_rtc_wakeup_armed = 1;
    return HAL_OK;
}

HAL_StatusTypeDef HAL_RTCEx_DeactivateWakeUpTimer(RTC_HandleTypeDef *hrtc) {
    sim_stm32_rtc_wakeup_armed = 0;
    return HAL_OK;
}

void HAL_RTCEx_WakeUpTimerIRQHandler(RTC_HandleTypeDef *hrtc) {
    if (HAL_RTCEx_WakeUpTimerEventCallback) {
        HAL_RTCEx_WakeUpTimerEventCallback(hrtc);
    }
    hrtc->State = HAL_RTC_STATE_READY;
}

/* The wake-up timer expired: run the firmware's interrupt handler. */
void sim_stm32_rtc_wakeup_fire(void) {
    if (!sim_stm32_nvic_irq_enabled(RTC_WKUP_IRQn)) {
        printf("WARNING: wake-up timer expired but RTC_WKUP_IRQn is not enabled in the NVIC.\n");
    } else if (RTC_WKUP_IRQHandler) {
        RTC_WKUP_IRQHandler();
    }
}

#endif /* HAL_RTC_MODULE_ENABLED */
