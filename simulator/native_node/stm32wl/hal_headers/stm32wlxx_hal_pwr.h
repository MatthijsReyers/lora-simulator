#ifndef STM32WLxx_HAL_PWR_H
#define STM32WLxx_HAL_PWR_H

#ifdef __cplusplus
extern "C" {
#endif

#include "stm32wlxx_hal_def.h"

typedef struct
{
  uint32_t PVDLevel;       /*!< PVDLevel: Specifies the PVD detection level.
                                This parameter can be a value of @ref PWR_PVD_detection_level. */

  uint32_t Mode;           /*!< Mode: Specifies the operating mode for the selected pins.
                                This parameter can be a value of @ref PWR_PVD_Mode. */
} PWR_PVDTypeDef;

#define PWR_PVDLEVEL_0                      (0x00000000UL)                                   /*!< PVD threshold around 2.0 V */
#define PWR_PVDLEVEL_1                      (                                PWR_CR2_PLS_0)  /*!< PVD threshold around 2.2 V */
#define PWR_PVDLEVEL_2                      (                PWR_CR2_PLS_1                )  /*!< PVD threshold around 2.4 V */
#define PWR_PVDLEVEL_3                      (                PWR_CR2_PLS_1 | PWR_CR2_PLS_0)  /*!< PVD threshold around 2.5 V */
#define PWR_PVDLEVEL_4                      (PWR_CR2_PLS_2                                )  /*!< PVD threshold around 2.6 V */
#define PWR_PVDLEVEL_5                      (PWR_CR2_PLS_2                 | PWR_CR2_PLS_0)  /*!< PVD threshold around 2.8 V */
#define PWR_PVDLEVEL_6                      (PWR_CR2_PLS_2 | PWR_CR2_PLS_1                )  /*!< PVD threshold around 2.9 V */
#define PWR_PVDLEVEL_7                      (PWR_CR2_PLS_2 | PWR_CR2_PLS_1 | PWR_CR2_PLS_0)  /*!< External input analog voltage (compared internally to VREFINT) */


#define PWR_PVD_MODE_NORMAL                 (0x00000000UL)
#define PWR_PVD_MODE_IT_RISING              (PVD_MODE_IT | PVD_RISING_EDGE)
#define PWR_PVD_MODE_IT_FALLING             (PVD_MODE_IT | PVD_FALLING_EDGE)
#define PWR_PVD_MODE_IT_RISING_FALLING      (PVD_MODE_IT | PVD_RISING_FALLING_EDGE)

#ifdef CORE_CM0PLUS
#define PWR_LOWPOWERMODE_STOP0              (0x00000000UL)
#define PWR_LOWPOWERMODE_STOP1              (PWR_C2CR1_LPMS_0)
#define PWR_LOWPOWERMODE_STOP2              (PWR_C2CR1_LPMS_1)
#define PWR_LOWPOWERMODE_STANDBY            (PWR_C2CR1_LPMS_0 | PWR_C2CR1_LPMS_1)
#define PWR_LOWPOWERMODE_SHUTDOWN           (PWR_C2CR1_LPMS_2 | PWR_C2CR1_LPMS_1 | PWR_C2CR1_LPMS_0)
#else
#define PWR_LOWPOWERMODE_STOP0              (0x00000000UL)
#define PWR_LOWPOWERMODE_STOP1              (PWR_CR1_LPMS_0)
#define PWR_LOWPOWERMODE_STOP2              (PWR_CR1_LPMS_1)
#define PWR_LOWPOWERMODE_STANDBY            (PWR_CR1_LPMS_0 | PWR_CR1_LPMS_1)
#define PWR_LOWPOWERMODE_SHUTDOWN           (PWR_CR1_LPMS_2 | PWR_CR1_LPMS_1 | PWR_CR1_LPMS_0)
#endif /* CORE_CM0PLUS */


#define PWR_MAINREGULATOR_ON                (0x00000000UL)              /*!< Regulator in main mode      */
#define PWR_LOWPOWERREGULATOR_ON            (PWR_CR1_LPR)               /*!< Regulator in low-power mode */

#define PWR_SLEEPENTRY_WFI                  ((uint8_t)0x01)         /*!< Wait For Interruption instruction to enter Sleep mode */
#define PWR_SLEEPENTRY_WFE                  ((uint8_t)0x02)         /*!< Wait For Event instruction to enter Sleep mode        */

#define PWR_STOPENTRY_WFI                   ((uint8_t)0x01)         /*!< Wait For Interruption instruction to enter Stop mode */
#define PWR_STOPENTRY_WFE                   ((uint8_t)0x02)         /*!< Wait For Event instruction to enter Stop mode        */

#define PWR_EXTI_LINE_PVD                   (LL_EXTI_LINE_16)   /*!< External interrupt line 16 Connected to the PWR PVD */

#define PVD_MODE_IT                         (0x00010000UL)  /*!< Mask for interruption yielded by PVD threshold crossing */
#define PVD_RISING_EDGE                     (0x00000001UL)  /*!< Mask for rising edge set as PVD trigger                 */
#define PVD_FALLING_EDGE                    (0x00000002UL)  /*!< Mask for falling edge set as PVD trigger                */
#define PVD_RISING_FALLING_EDGE             (0x00000003UL)  /*!< Mask for rising and falling edges set as PVD trigger    */

#if defined(CORE_CM0PLUS)
#define __HAL_PWR_PVD_EXTI_ENABLE_IT()      LL_C2_EXTI_EnableIT_0_31(PWR_EXTI_LINE_PVD)
#else
#define __HAL_PWR_PVD_EXTI_ENABLE_IT()      LL_EXTI_EnableIT_0_31(PWR_EXTI_LINE_PVD)
#endif /* CORE_CM0PLUS */

#if defined(CORE_CM0PLUS)
#define __HAL_PWR_PVD_EXTI_DISABLE_IT()     LL_C2_EXTI_DisableIT_0_31(PWR_EXTI_LINE_PVD)
#else
#define __HAL_PWR_PVD_EXTI_DISABLE_IT()     LL_EXTI_DisableIT_0_31(PWR_EXTI_LINE_PVD)
#endif /* CORE_CM0PLUS */

#define __HAL_PWR_PVD_EXTI_ENABLE_RISING_EDGE()           hal_nop()
#define __HAL_PWR_PVD_EXTI_DISABLE_RISING_EDGE()          hal_nop()
#define __HAL_PWR_PVD_EXTI_ENABLE_FALLING_EDGE()          hal_nop()
#define __HAL_PWR_PVD_EXTI_DISABLE_FALLING_EDGE()         hal_nop()
#define __HAL_PWR_PVD_EXTI_ENABLE_RISING_FALLING_EDGE()   hal_nop()
#define __HAL_PWR_PVD_EXTI_DISABLE_RISING_FALLING_EDGE()  hal_nop()
#define __HAL_PWR_PVD_EXTI_GENERATE_SWIT()                hal_nop()
#define __HAL_PWR_PVD_EXTI_GET_FLAG()                     hal_nop()
#define __HAL_PWR_PVD_EXTI_CLEAR_FLAG()                   hal_nop()
#define __HAL_PWR_PVD_EXTI_CLEAR_FLAG()                   hal_nop()

#include "stm32wlxx_hal_pwr_ex.h"

void              HAL_PWR_DeInit(void);
void              HAL_PWR_EnableBkUpAccess(void);
void              HAL_PWR_DisableBkUpAccess(void);

/* Peripheral Control functions  ************************************************/
HAL_StatusTypeDef HAL_PWR_ConfigPVD(const PWR_PVDTypeDef *sConfigPVD);
void              HAL_PWR_EnablePVD(void);
void              HAL_PWR_DisablePVD(void);

/* WakeUp pins configuration functions ****************************************/
void              HAL_PWR_EnableWakeUpPin(uint32_t WakeUpPinPolarity);
void              HAL_PWR_DisableWakeUpPin(uint32_t WakeUpPinx);

/* Low Power modes configuration functions ************************************/
void              HAL_PWR_EnterSTOPMode(uint32_t Regulator, uint8_t STOPEntry);
void              HAL_PWR_EnterSLEEPMode(uint32_t Regulator, uint8_t SLEEPEntry);
void              HAL_PWR_EnterSTANDBYMode(void);

void              HAL_PWR_EnableSleepOnExit(void);
void              HAL_PWR_DisableSleepOnExit(void);

void              HAL_PWR_EnableSEVOnPend(void);
void              HAL_PWR_DisableSEVOnPend(void);

void              HAL_PWR_PVDCallback(void);

#ifdef __cplusplus
}
#endif


#endif /* STM32WLxx_HAL_PWR_H */

