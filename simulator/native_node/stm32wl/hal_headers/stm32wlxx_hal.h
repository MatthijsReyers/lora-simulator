/**
 * Simulator shim for stm32wlxx_hal.h
 *
 * Drop-in replacement for the real STM32WL HAL header. Provides declarations
 * for HAL functions that are implemented by the simulator's stm32_hal.c.
 */
#ifndef __STM32WLxx_HAL_H
#define __STM32WLxx_HAL_H

#ifdef __cplusplus
extern "C" {
#endif

#include "stm32wlxx_hal_def.h"
#include "stm32wlxx_hal_conf.h"

typedef enum
{
  HAL_TICK_FREQ_10HZ         = 100U,
  HAL_TICK_FREQ_100HZ        = 10U,
  HAL_TICK_FREQ_1KHZ         = 1U,
  HAL_TICK_FREQ_DEFAULT      = HAL_TICK_FREQ_1KHZ
} HAL_TickFreqTypeDef;

/* ---- HAL Init / DeInit -------------------------------------------------- */

HAL_StatusTypeDef HAL_Init(void);
HAL_StatusTypeDef HAL_DeInit(void);

/* ---- Timing ------------------------------------------------------------- */

void     HAL_Delay(uint32_t Delay);
uint32_t HAL_GetTick(void);
void     HAL_IncTick(void);

/* ---- Device UID (96-bit unique device identifier) ----------------------- */

static inline uint32_t HAL_GetUIDw0(void) { return 0xDEADBEEF; }
static inline uint32_t HAL_GetUIDw1(void) { return 0xCAFEBABE; }
static inline uint32_t HAL_GetUIDw2(void) { return 0x12345678; }

/* ---- Flash stubs -------------------------------------------------------- */

typedef struct {
    uint32_t OPTR;
} _SIM_FLASH_TypeDef;

static _SIM_FLASH_TypeDef _sim_flash_regs __attribute__((unused)) = { 0 };
#define FLASH (&_sim_flash_regs)

#define FLASH_OPTR_IWDG_STOP   0x00020000U
#define FLASH_OPTR_IWDG_STDBY  0x00040000U

typedef struct {
    uint32_t OptionType;
    uint32_t UserType;
    uint32_t UserConfig;
} FLASH_OBProgramInitTypeDef;

#define OPTIONBYTE_USER        0x00000002U
#define OB_USER_IWDG_STOP     0x00020000U
#define OB_USER_IWDG_STDBY    0x00040000U
#define OB_IWDG_STOP_FREEZE   0x00000000U
#define OB_IWDG_STDBY_FREEZE  0x00000000U

static inline HAL_StatusTypeDef HAL_FLASH_Unlock(void) { return HAL_OK; }
static inline HAL_StatusTypeDef HAL_FLASH_OB_Unlock(void) { return HAL_OK; }
static inline HAL_StatusTypeDef HAL_FLASHEx_OBProgram(FLASH_OBProgramInitTypeDef *pOBInit) {
    UNUSED(pOBInit); return HAL_OK;
}
static inline HAL_StatusTypeDef HAL_FLASH_OB_Launch(void) { return HAL_OK; }

/* ---- NVIC stubs --------------------------------------------------------- */

#define __NVIC_PRIO_BITS 4U

static inline void HAL_NVIC_SetPriority(int IRQn, uint32_t PreemptPriority, uint32_t SubPriority) {
    uint32_t prioritygroup = 0U;  /* reset default: all bits are preempt priority */
    uint32_t priority = NVIC_EncodePriority(prioritygroup, PreemptPriority, SubPriority);
    __NVIC_SetPriority((IRQn_Type)IRQn, priority);
}
static inline void HAL_NVIC_EnableIRQ(int IRQn)  { __NVIC_EnableIRQ((IRQn_Type)IRQn); }
static inline void HAL_NVIC_DisableIRQ(int IRQn) { __NVIC_DisableIRQ((IRQn_Type)IRQn); }

/* ---- GPIO stubs --------------------------------------------------------- */

typedef struct { int dummy; } GPIO_TypeDef;
typedef struct {
    uint32_t Pin;
    uint32_t Mode;
    uint32_t Pull;
    uint32_t Speed;
    uint32_t Alternate;
} GPIO_InitTypeDef;

typedef enum {
    GPIO_PIN_RESET = 0,
    GPIO_PIN_SET   = 1
} GPIO_PinState;

#define GPIO_PIN_0   ((uint16_t)0x0001)
#define GPIO_PIN_1   ((uint16_t)0x0002)
#define GPIO_PIN_2   ((uint16_t)0x0004)
#define GPIO_PIN_3   ((uint16_t)0x0008)
#define GPIO_PIN_4   ((uint16_t)0x0010)
#define GPIO_PIN_5   ((uint16_t)0x0020)
#define GPIO_PIN_6   ((uint16_t)0x0040)
#define GPIO_PIN_7   ((uint16_t)0x0080)
#define GPIO_PIN_8   ((uint16_t)0x0100)
#define GPIO_PIN_9   ((uint16_t)0x0200)
#define GPIO_PIN_10  ((uint16_t)0x0400)
#define GPIO_PIN_11  ((uint16_t)0x0800)
#define GPIO_PIN_12  ((uint16_t)0x1000)
#define GPIO_PIN_13  ((uint16_t)0x2000)
#define GPIO_PIN_14  ((uint16_t)0x4000)
#define GPIO_PIN_15  ((uint16_t)0x8000)
#define GPIO_PIN_All ((uint16_t)0xFFFF)

#define GPIO_MODE_INPUT     0x00000000U
#define GPIO_MODE_OUTPUT_PP 0x00000001U
#define GPIO_MODE_OUTPUT_OD 0x00000011U
#define GPIO_MODE_AF_PP     0x00000002U
#define GPIO_MODE_AF_OD     0x00000012U
#define GPIO_MODE_ANALOG    0x00000003U

#define GPIO_NOPULL   0x00000000U
#define GPIO_PULLUP   0x00000001U
#define GPIO_PULLDOWN 0x00000002U

#define GPIO_SPEED_FREQ_LOW       0x00000000U
#define GPIO_SPEED_FREQ_MEDIUM    0x00000001U
#define GPIO_SPEED_FREQ_HIGH      0x00000002U
#define GPIO_SPEED_FREQ_VERY_HIGH 0x00000003U

static inline void HAL_GPIO_Init(GPIO_TypeDef *GPIOx, GPIO_InitTypeDef *GPIO_Init) {
    UNUSED(GPIOx); UNUSED(GPIO_Init);
}
static inline void HAL_GPIO_WritePin(GPIO_TypeDef *GPIOx, uint16_t GPIO_Pin, GPIO_PinState PinState) {
    UNUSED(GPIOx); UNUSED(GPIO_Pin); UNUSED(PinState);
}
static inline GPIO_PinState HAL_GPIO_ReadPin(GPIO_TypeDef *GPIOx, uint16_t GPIO_Pin) {
    UNUSED(GPIOx); UNUSED(GPIO_Pin);
    return GPIO_PIN_RESET;
}
static inline void HAL_GPIO_TogglePin(GPIO_TypeDef *GPIOx, uint16_t GPIO_Pin) {
    UNUSED(GPIOx); UNUSED(GPIO_Pin);
}

#define RCC_OSCILLATORTYPE_MSI   0x00000004U
#define RCC_MSI_ON               0x00000001U
#define RCC_MSICALIBRATION_DEFAULT 0U
#define RCC_MSIRANGE_11          0x000000B0U
#define RCC_PLL_NONE             0x00000000U

#define RCC_CLOCKTYPE_SYSCLK 0x00000001U
#define RCC_CLOCKTYPE_HCLK   0x00000002U
#define RCC_CLOCKTYPE_PCLK1  0x00000004U
#define RCC_CLOCKTYPE_PCLK2  0x00000008U
#define RCC_CLOCKTYPE_HCLK3  0x00000020U

#define RCC_SYSCLKSOURCE_MSI 0x00000000U
#define RCC_SYSCLK_DIV1      0x00000000U
#define RCC_HCLK_DIV1        0x00000000U

#define FLASH_LATENCY_2 0x00000002U

#define __HAL_PWR_VOLTAGESCALING_CONFIG(x) do { UNUSED(x); } while(0)
#define PWR_REGULATOR_VOLTAGE_SCALE1 0x00000001U

static inline void HAL_PWR_EnableBkUpAccess(void)  {}
static inline void HAL_PWR_DisableBkUpAccess(void) {}

static inline HAL_StatusTypeDef HAL_RCC_OscConfig(RCC_OscInitTypeDef *osc) {
    UNUSED(osc); return HAL_OK;
}
static inline HAL_StatusTypeDef HAL_RCC_ClockConfig(RCC_ClkInitTypeDef *clk, uint32_t FLatency) {
    UNUSED(clk); UNUSED(FLatency); return HAL_OK;
}

static inline void __HAL_RCC_GPIOA_CLK_ENABLE(int) {}
static inline void __HAL_RCC_GPIOB_CLK_ENABLE(int) {}
static inline void __HAL_RCC_GPIOC_CLK_ENABLE(int) {}
static inline void __HAL_RCC_SPI1_CLK_ENABLE(int)  {}
static inline void __HAL_RCC_I2C1_CLK_ENABLE(int)  {}
static inline void __HAL_RCC_USART2_CLK_ENABLE(int) {}

/* ---- Peripheral GPIO port placeholders ---------------------------------- */

typedef struct { int _unused; } _SIM_GPIO_TypeDef;
static _SIM_GPIO_TypeDef _sim_gpioa __attribute__((unused)),
                         _sim_gpiob __attribute__((unused)),
                         _sim_gpioc __attribute__((unused));
#define GPIOA (( GPIO_TypeDef*)&_sim_gpioa)
#define GPIOB ((GPIO_TypeDef*)&_sim_gpiob)
#define GPIOC ((GPIO_TypeDef*)&_sim_gpioc)

#ifdef __cplusplus
}
#endif

#endif /* __STM32WLxx_HAL_H */
