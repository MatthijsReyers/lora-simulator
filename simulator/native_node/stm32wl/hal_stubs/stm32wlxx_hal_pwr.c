
#include "stm32wlxx_hal_pwr.h"
#include "stm32wlxx_hal_pwr_ex.h"


uint32_t HAL_PWREx_GetVoltageRange(void) {
    return PWR_REGULATOR_VOLTAGE_SCALE1;
}

HAL_StatusTypeDef HAL_PWREx_ControlVoltageScaling(uint32_t VoltageScaling) {
    return HAL_OK;
}

void HAL_PWREx_EnableBatteryCharging(uint32_t ResistorSelection) {}
void HAL_PWREx_DisableBatteryCharging(void) {}

void HAL_PWREx_EnableInternalWakeUpLine(void) {}
void HAL_PWREx_DisableInternalWakeUpLine(void) {}

void HAL_PWREx_SetRadioBusyPolarity(uint32_t RadioBusyPolarity) {}
void HAL_PWREx_SetRadioBusyTrigger(uint32_t RadioBusyTrigger) {}
void HAL_PWREx_SetRadioIRQTrigger(uint32_t RadioIRQTrigger) {}

void HAL_PWREx_EnableHOLDC2IT(void) {}
void HAL_PWREx_DisableHOLDC2IT(void) {}

void HAL_PWREx_HoldCore(uint32_t CPU) {}
void HAL_PWREx_ReleaseCore(uint32_t CPU) {}

#ifdef CORE_CM0PLUS
volatile int sim_hal_stm32_pwr_ex_wake_up_ilac_enabled = 0; 

void HAL_PWREx_EnableWakeUp_ILAC(void) {
    sim_hal_stm32_pwr_ex_wake_up_ilac_enabled = 1;
}
void HAL_PWREx_DisableWakeUp_ILAC(void) {
    sim_hal_stm32_pwr_ex_wake_up_ilac_enabled = 0;
}
uint32_t HAL_PWREx_IsEnabledWakeUp_ILAC(void) {
    return sim_hal_stm32_pwr_ex_wake_up_ilac_enabled;
}
#endif /* CORE_CM0PLUS */

HAL_StatusTypeDef sim_hal_stm32_pwr_gpio(uint32_t GPIO, uint32_t GPIONumber)
{
    if (!IS_PWR_GPIO(GPIO)) {
        printf("ERROR: HAL_PWREx_EnableGPIOPullUp: Invalid GPIO port %u", GPIO);
        return HAL_ERROR;
    }
    // This this a PWR GPIO bit number?
    if ((GPIONumber & GPIO_PIN_MASK) == 0) {
        printf("ERROR: HAL_PWREx_EnableGPIOPullUp: Invalid GPIO port %u", GPIO);
        return HAL_ERROR;
    }
    return HAL_OK;
}

HAL_StatusTypeDef HAL_PWREx_EnableGPIOPullUp(uint32_t GPIO, uint32_t GPIONumber) {
    return sim_hal_stm32_pwr_gpio(GPIO, GPIONumber);
}
HAL_StatusTypeDef HAL_PWREx_DisableGPIOPullUp(uint32_t GPIO, uint32_t GPIONumber) {
    return sim_hal_stm32_pwr_gpio(GPIO, GPIONumber);
}
HAL_StatusTypeDef HAL_PWREx_EnableGPIOPullDown(uint32_t GPIO, uint32_t GPIONumber) {
    return sim_hal_stm32_pwr_gpio(GPIO, GPIONumber);
}
HAL_StatusTypeDef HAL_PWREx_DisableGPIOPullDown(uint32_t GPIO, uint32_t GPIONumber) {
    return sim_hal_stm32_pwr_gpio(GPIO, GPIONumber);
}

void HAL_PWREx_EnablePullUpPullDownConfig(void) {}
void HAL_PWREx_DisablePullUpPullDownConfig(void) {}

void HAL_PWREx_EnableSRAMRetention(void) {}
void HAL_PWREx_DisableSRAMRetention(void) {}

void HAL_PWREx_EnableFlashPowerDown(uint32_t PowerMode) {}
void HAL_PWREx_DisableFlashPowerDown(uint32_t PowerMode) {}

void HAL_PWREx_EnableWPVD(void) {}
void HAL_PWREx_DisableWPVD(void) {}
void HAL_PWREx_EnableBORPVD_ULP(void) {}
void HAL_PWREx_DisableBORPVD_ULP(void) {}

void HAL_PWREx_EnablePVM3(void) {}
void HAL_PWREx_DisablePVM3(void) {}

HAL_StatusTypeDef HAL_PWREx_ConfigPVM(const PWR_PVMTypeDef *sConfigPVM) {
    return HAL_OK;
}

void HAL_PWREx_SetRadioEOL(uint32_t RadioEOL) {}
void HAL_PWREx_SMPS_SetMode(uint32_t OperatingMode) {}

uint32_t HAL_PWREx_SMPS_GetEffectiveMode(void) {
    return PWR_SMPS_BYPASS; // SIM: todo: does this make sense to use a default value?
}

void HAL_PWREx_EnableLowPowerRunMode(void) {}
HAL_StatusTypeDef HAL_PWREx_DisableLowPowerRunMode(void) {
    return HAL_OK;
}

void HAL_PWREx_EnterSTOP0Mode(uint8_t STOPEntry) {}
void HAL_PWREx_EnterSTOP1Mode(uint8_t STOPEntry) {}
void HAL_PWREx_EnterSTOP2Mode(uint8_t STOPEntry) {}
void HAL_PWREx_EnterSHUTDOWNMode(void) {}

void HAL_PWREx_PVD_PVM_IRQHandler(void) {}

void HAL_PWREx_PVM3Callback(void) {}

