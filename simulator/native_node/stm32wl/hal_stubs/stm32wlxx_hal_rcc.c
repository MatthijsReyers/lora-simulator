
#include "stm32wlxx_hal_rcc.h"
#include "stm32wlxx_hal_rcc_ex.h"

HAL_StatusTypeDef HAL_RCC_DeInit(void)
{
    return HAL_OK;
}

HAL_StatusTypeDef HAL_RCC_OscConfig(RCC_OscInitTypeDef *RCC_OscInitStruct)
{
    return HAL_OK;
}

HAL_StatusTypeDef HAL_RCC_ClockConfig(RCC_ClkInitTypeDef *RCC_ClkInitStruct, uint32_t FLatency)
{
    return HAL_OK;
}

void HAL_RCC_MCOConfig(uint32_t RCC_MCOx, uint32_t RCC_MCOSource, uint32_t RCC_MCODiv) {}
void HAL_RCC_EnableCSS(void) {}

void sim_stm32_hal_rcc_subghz_clk_enable() {}
void sim_stm32_hal_rcc_subghz_clk_disable() {}

uint32_t sim_stm32_hal_clk_enabled(uint32_t clk) {
    return 1;  /* pretend all clocks are enabled */
}
