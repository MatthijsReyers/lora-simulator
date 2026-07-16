/**
  ******************************************************************************
  * @file    stm32wlxx_hal_pwr_ex.h
  * @author  MCD Application Team
  * @brief   Header file of PWR HAL Extended module.
  ******************************************************************************
  * @attention
  *
  * Copyright (c) 2020 STMicroelectronics.
  * All rights reserved.
  *
  * This software is licensed under terms that can be found in the LICENSE file
  * in the root directory of this software component.
  * If no LICENSE file comes with this software, it is provided AS-IS.
  *
  ******************************************************************************
  */

/* Define to prevent recursive inclusion -------------------------------------*/
#ifndef STM32WLxx_HAL_PWR_EX_H
#define STM32WLxx_HAL_PWR_EX_H

#ifdef __cplusplus
extern "C" {
#endif
#include "stm32wlxx_hal_def.h"
#include "stm32wl55xx.h"

typedef struct
{
  uint32_t PVMType;
  uint32_t Mode;
} PWR_PVMTypeDef;

#define PWR_WAKEUP_PIN1_HIGH                PWR_CR3_EWUP1  /*!< Wakeup pin 1 (with high level polarity) */
#define PWR_WAKEUP_PIN2_HIGH                PWR_CR3_EWUP2  /*!< Wakeup pin 2 (with high level polarity) */
#define PWR_WAKEUP_PIN3_HIGH                PWR_CR3_EWUP3  /*!< Wakeup pin 3 (with high level polarity) */

#define PWR_WAKEUP_PIN1_LOW                 ((PWR_CR4_WP1<<PWR_WUP_POLARITY_SHIFT) | PWR_CR3_EWUP1) /*!< Wakeup pin 1 (with low level polarity) */
#define PWR_WAKEUP_PIN2_LOW                 ((PWR_CR4_WP2<<PWR_WUP_POLARITY_SHIFT) | PWR_CR3_EWUP2) /*!< Wakeup pin 2 (with low level polarity) */
#define PWR_WAKEUP_PIN3_LOW                 ((PWR_CR4_WP3<<PWR_WUP_POLARITY_SHIFT) | PWR_CR3_EWUP3) /*!< Wakeup pin 3 (with low level polarity) */

#define PWR_WAKEUP_PIN1                     PWR_CR3_EWUP1  /*!< Wakeup pin 1 (with high level polarity) */
#define PWR_WAKEUP_PIN2                     PWR_CR3_EWUP2  /*!< Wakeup pin 2 (with high level polarity) */
#define PWR_WAKEUP_PIN3                     PWR_CR3_EWUP3  /*!< Wakeup pin 3 (with high level polarity) */

#define PWR_PVM_3                           PWR_CR2_PVME3  /*!< Peripheral Voltage Monitoring 3 enable: VDDA versus 1.62 V */

#define PWR_PVM_MODE_NORMAL                 (0x00000000UL)                              /*!< basic mode is used */

#define PWR_PVM_MODE_IT_RISING              (PVM_MODE_IT | PVM_RISING_EDGE)             /*!< External Interrupt Mode with Rising edge trigger detection */
#define PWR_PVM_MODE_IT_FALLING             (PVM_MODE_IT | PVM_FALLING_EDGE)            /*!< External Interrupt Mode with Falling edge trigger detection */
#define PWR_PVM_MODE_IT_RISING_FALLING      (PVM_MODE_IT | PVM_RISING_FALLING_EDGE)     /*!< External Interrupt Mode with Rising/Falling edge trigger detection */

#define PWR_FLASHPD_LPRUN                   PWR_CR1_FPDR     /*!< Enable Flash power down in low power run mode */
#define PWR_FLASHPD_LPSLEEP                 PWR_CR1_FPDS     /*!< Enable Flash power down in low power sleep mode */

#define PWR_REGULATOR_VOLTAGE_SCALE1        PWR_CR1_VOS_0     /*!< Regulator voltage output range 1 mode, typical output voltage at 1.2 V, system frequency up to 64 MHz */
#define PWR_REGULATOR_VOLTAGE_SCALE2        PWR_CR1_VOS_1     /*!< Regulator voltage output range 2 mode, typical output voltage at 1.0 V, system frequency up to 16 MHz */

#define PWR_BATTERY_CHARGING_RESISTOR_5     (0x00000000UL)         /*!< VBAT charging through a 5 kOhms resistor   */
#define PWR_BATTERY_CHARGING_RESISTOR_1_5   PWR_CR4_VBRS           /*!< VBAT charging through a 1.5 kOhms resistor */

#define PWR_BATTERY_CHARGING_DISABLE        (0x00000000UL)
#define PWR_BATTERY_CHARGING_ENABLE         PWR_CR4_VBE

#define PWR_GPIO_BIT_0                      PWR_PUCRB_PB0    /*!< GPIO port I/O pin 0  */
#define PWR_GPIO_BIT_1                      PWR_PUCRB_PB1    /*!< GPIO port I/O pin 1  */
#define PWR_GPIO_BIT_2                      PWR_PUCRB_PB2    /*!< GPIO port I/O pin 2  */
#define PWR_GPIO_BIT_3                      PWR_PUCRB_PB3    /*!< GPIO port I/O pin 3  */
#define PWR_GPIO_BIT_4                      PWR_PUCRB_PB4    /*!< GPIO port I/O pin 4  */
#define PWR_GPIO_BIT_5                      PWR_PUCRB_PB5    /*!< GPIO port I/O pin 5  */
#define PWR_GPIO_BIT_6                      PWR_PUCRB_PB6    /*!< GPIO port I/O pin 6  */
#define PWR_GPIO_BIT_7                      PWR_PUCRB_PB7    /*!< GPIO port I/O pin 7  */
#define PWR_GPIO_BIT_8                      PWR_PUCRB_PB8    /*!< GPIO port I/O pin 8  */
#define PWR_GPIO_BIT_9                      PWR_PUCRB_PB9    /*!< GPIO port I/O pin 9  */
#define PWR_GPIO_BIT_10                     PWR_PUCRB_PB10   /*!< GPIO port I/O pin 10 */
#define PWR_GPIO_BIT_11                     PWR_PUCRB_PB11   /*!< GPIO port I/O pin 11 */
#define PWR_GPIO_BIT_12                     PWR_PUCRB_PB12   /*!< GPIO port I/O pin 12 */
#define PWR_GPIO_BIT_13                     PWR_PUCRB_PB13   /*!< GPIO port I/O pin 14 */
#define PWR_GPIO_BIT_14                     PWR_PDCRB_PB14   /*!< GPIO port I/O pin 14 */
#define PWR_GPIO_BIT_15                     PWR_PUCRB_PB15   /*!< GPIO port I/O pin 15 */

#define PWR_GPIO_A                          (0x00000000UL)      /*!< GPIO port A */
#define PWR_GPIO_B                          (0x00000001UL)      /*!< GPIO port B */
#define PWR_GPIO_C                          (0x00000002UL)      /*!< GPIO port C */
#define PWR_GPIO_H                          (0x00000007UL)      /*!< GPIO port H */

#define PWR_RADIO_EOL_DISABLE               (0x00000000UL)    /*!< Monitoring of supply voltage for radio operating level (radio End Of Life) disable */
#define PWR_RADIO_EOL_ENABLE                (PWR_CR5_RFEOLEN) /*!< Monitoring of supply voltage for radio operating level (radio End Of Life) enable */

#define PWR_SMPS_BYPASS                     (0x00000000UL)    /*!< SMPS step down in bypass mode  */
#define PWR_SMPS_STEP_DOWN                  (PWR_CR5_SMPSEN)  /*!< SMPS step down in step down mode if system low power mode is run, LP run or stop0. If system low power mode is stop1, stop2, standby, shutdown, then SMPS is forced in mode open to preserve energy stored in decoupling capacitor as long as possible. Note: In case of a board without SMPS coil mounted, SMPS should not be activated. */

#define PWR_RADIO_BUSY_POLARITY_RISING      (0x00000000UL)     /*!< Radio busy signal polarity to rising edge (detection on high level). */
#define PWR_RADIO_BUSY_POLARITY_FALLING     (PWR_CR4_WRFBUSYP) /*!< Radio busy signal polarity to falling edge (detection on low level). */

#define PWR_RADIO_BUSY_TRIGGER_NONE         (0x00000000UL)     /*!< Radio busy trigger action: no wake-up from low-power mode and no interruption sent to the selected CPU. */
#define PWR_RADIO_BUSY_TRIGGER_WU_IT        (PWR_CR3_EWRFBUSY) /*!< Radio busy trigger action: wake-up from low-power mode Standby and interruption sent to the selected CPU. */

#define PWR_RADIO_IRQ_TRIGGER_NONE          (0x00000000UL)     /*!< Radio IRQ trigger action: no wake-up from low-power mode and no interruption sent to the selected CPU. */
#define PWR_RADIO_IRQ_TRIGGER_WU_IT         (PWR_CR3_EWRFIRQ)  /*!< Radio IRQ trigger action: wake-up from low-power mode Standby and interruption sent to the selected CPU. */

#define PWR_FLAG_WUF1                       (PWR_FLAG_REG_SR1 | PWR_SR1_WUF1_Pos)        /*!< Wakeup event on wakeup pin 1 */
#define PWR_FLAG_WUF2                       (PWR_FLAG_REG_SR1 | PWR_SR1_WUF2_Pos)        /*!< Wakeup event on wakeup pin 2 */
#define PWR_FLAG_WUF3                       (PWR_FLAG_REG_SR1 | PWR_SR1_WUF3_Pos)        /*!< Wakeup event on wakeup pin 3 */
#define PWR_FLAG_WU                         (PWR_FLAG_REG_SR1 | PWR_SR1_WUF)             /*!< Encompass wakeup event on all wakeup pins */
#define PWR_FLAG_WPVD                       (PWR_FLAG_REG_SR1 | PWR_SR1_WPVDF_Pos)       /*!< Wakeup PVD flag */
#define PWR_FLAG_HOLDC2I                    (PWR_FLAG_REG_SR1 | PWR_SR1_C2HF_Pos)        /*!< CPU2 on-Hold Interrupt Flag */
#define PWR_FLAG_WUFI                       (PWR_FLAG_REG_SR1 | PWR_SR1_WUFI_Pos)        /*!< Wakeup on internal wakeup line */
#define PWR_FLAG_WRFBUSY                    (PWR_FLAG_REG_SR1 | PWR_SR1_WRFBUSYF_Pos)    /*!< Wakeup radio busy flag (triggered status: wake-up event or interruption occurred at least once. Can be cleared by software) */
/*--------------------------------SR2-------------------------------*/
#define PWR_FLAG_LDORDY                     (PWR_FLAG_REG_SR2 | PWR_SR2_LDORDY_Pos)      /*!< Main LDO ready flag */
#define PWR_FLAG_SMPSRDY                    (PWR_FLAG_REG_SR2 | PWR_SR2_SMPSRDY_Pos)     /*!< SMPS ready Flag */
#define PWR_FLAG_REGLPS                     (PWR_FLAG_REG_SR2 | PWR_SR2_REGLPS_Pos)      /*!< Low-power regulator started and ready flag */
#define PWR_FLAG_REGLPF                     (PWR_FLAG_REG_SR2 | PWR_SR2_REGLPF_Pos)      /*!< Low-power regulator (main regulator or low-power regulator used) flag */
#define PWR_FLAG_REGMRS                     (PWR_FLAG_REG_SR2 | PWR_SR2_REGMRS_Pos)      /*!< Main regulator supply from LDO or SMPS or directly from VDD */
#define PWR_FLAG_FLASHRDY                   (PWR_FLAG_REG_SR2 | PWR_SR2_FLASHRDY_Pos)    /*!< Flash ready flag */
#define PWR_FLAG_VOSF                       (PWR_FLAG_REG_SR2 | PWR_SR2_VOSF_Pos)        /*!< Voltage scaling flag */
#define PWR_FLAG_PVDO                       (PWR_FLAG_REG_SR2 | PWR_SR2_PVDO_Pos)        /*!< Power Voltage Detector output flag */
#define PWR_FLAG_PVMO3                      (PWR_FLAG_REG_SR2 | PWR_SR2_PVMO3_Pos)       /*!< Power Voltage Monitoring 3 output flag */
#define PWR_FLAG_RFEOL                      (PWR_FLAG_REG_SR2 | PWR_SR2_RFEOLF_Pos)      /*!< Power Voltage Monitoring Radio end of life flag */
#define PWR_FLAG_RFBUSYS                    (PWR_FLAG_REG_SR2 | PWR_SR2_RFBUSYS_Pos)     /*!< Radio busy signal flag (current status) */
#define PWR_FLAG_RFBUSYMS                   (PWR_FLAG_REG_SR2 | PWR_SR2_RFBUSYMS_Pos)    /*!< Radio busy masked signal flag (current status) */
#define PWR_FLAG_C2BOOTS                    (PWR_FLAG_REG_SR2 | PWR_SR2_C2BOOTS_Pos)     /*!< CPU2 boot request source information flag */
/*------------------------------EXTSCR------------------------------*/
#define PWR_FLAG_SB                         (PWR_FLAG_REG_EXTSCR | PWR_EXTSCR_C1SBF_Pos | (PWR_EXTSCR_C1CSSF_Pos << PWR_FLAG_EXTSCR_CLR_POS))    /*!< System Standby flag for CPU1 */
#define PWR_FLAG_STOP2                      (PWR_FLAG_REG_EXTSCR | PWR_EXTSCR_C1STOP2F_Pos | (PWR_EXTSCR_C1CSSF_Pos << PWR_FLAG_EXTSCR_CLR_POS)) /*!< System Stop 2 flag for CPU1 */
#define PWR_FLAG_STOP                       (PWR_FLAG_REG_EXTSCR | PWR_EXTSCR_C1STOPF_Pos | (PWR_EXTSCR_C1CSSF_Pos << PWR_FLAG_EXTSCR_CLR_POS))  /*!< System Stop 0 or Stop 1 flag for CPU1 */
#if defined(DUAL_CORE)
#define PWR_FLAG_C2SB                       (PWR_FLAG_REG_EXTSCR | PWR_EXTSCR_C2SBF_Pos | (PWR_EXTSCR_C2CSSF_Pos << PWR_FLAG_EXTSCR_CLR_POS))    /*!< System Standby flag for CPU2 */
#define PWR_FLAG_C2STOP2                    (PWR_FLAG_REG_EXTSCR | PWR_EXTSCR_C2STOP2F_Pos | (PWR_EXTSCR_C2CSSF_Pos << PWR_FLAG_EXTSCR_CLR_POS)) /*!< System Stop 2 flag for CPU2 */
#define PWR_FLAG_C2STOP                     (PWR_FLAG_REG_EXTSCR | PWR_EXTSCR_C2STOPF_Pos | (PWR_EXTSCR_C2CSSF_Pos << PWR_FLAG_EXTSCR_CLR_POS))  /*!< System Stop 0 or Stop 1 flag for CPU2 */
#endif /* DUAL_CORE */

#define PWR_FLAG_LPMODES                    (PWR_FLAG_SB)                       /*!< System flag encompassing all low-powers flags (Stop0, 1, 2 and Standby) for CPU1, used when clearing flags */
#if defined(DUAL_CORE)
#define PWR_FLAG_C2LPMODES                  (PWR_FLAG_C2SB)                     /*!< System flag encompassing all low-powers flags (Stop0, 1, 2 and Standby) for CPU2, used when clearing flags */
#endif /* DUAL_CORE */

#define PWR_FLAG_C1DEEPSLEEP                (PWR_EXTSCR_C1DS_Pos | PWR_FLAG_REG_EXTSCR)     /*!< CPU1 DeepSleep Flag */
#if defined(DUAL_CORE)
#define PWR_FLAG_C2DEEPSLEEP                (PWR_EXTSCR_C2DS_Pos | PWR_FLAG_REG_EXTSCR)     /*!< CPU2 DeepSleep Flag */
#endif /* DUAL_CORE */

#define PWR_CORE_CPU1                       (0x00000000UL)
#if defined(DUAL_CORE)
#define PWR_CORE_CPU2                       (0x00000001UL)
#endif /* DUAL_CORE */

#define PWR_EXTI_LINE_PVM3                  (LL_EXTI_LINE_34)  /*!< External interrupt line 34 connected to PVM3 */


#define __HAL_PWR_PVM3_EXTI_ENABLE_IT()                     sim_nop()
#define __HAL_PWR_PVM3_EXTI_DISABLE_IT()                    sim_nop()
#define __HAL_PWR_PVM3_EXTI_ENABLE_EVENT()                  sim_nop()
#define __HAL_PWR_PVM3_EXTI_DISABLE_EVENT()                 sim_nop()
#define __HAL_PWR_PVM3_EXTI_ENABLE_RISING_EDGE()            sim_nop()
#define __HAL_PWR_PVM3_EXTI_DISABLE_RISING_EDGE()           sim_nop()
#define __HAL_PWR_PVM3_EXTI_ENABLE_FALLING_EDGE()           sim_nop()
#define __HAL_PWR_PVM3_EXTI_DISABLE_FALLING_EDGE()          sim_nop()
#define __HAL_PWR_PVM3_EXTI_ENABLE_RISING_FALLING_EDGE()    sim_nop()
#define __HAL_PWR_PVM3_EXTI_DISABLE_RISING_FALLING_EDGE()   sim_nop()
#define __HAL_PWR_PVM3_EXTI_GENERATE_SWIT()                 sim_nop()
#define __HAL_PWR_PVM3_EXTI_GET_FLAG()                      sim_nop()
#define __HAL_PWR_PVM3_EXTI_CLEAR_FLAG()                    sim_nop()
#define __HAL_PWR_VOLTAGESCALING_CONFIG(__REGULATOR__)      sim_nop()


uint32_t          HAL_PWREx_GetVoltageRange(void);
HAL_StatusTypeDef HAL_PWREx_ControlVoltageScaling(uint32_t VoltageScaling);

void              HAL_PWREx_EnableBatteryCharging(uint32_t ResistorSelection);
void              HAL_PWREx_DisableBatteryCharging(void);

void              HAL_PWREx_EnableInternalWakeUpLine(void);
void              HAL_PWREx_DisableInternalWakeUpLine(void);

void              HAL_PWREx_SetRadioBusyPolarity(uint32_t RadioBusyPolarity);
void              HAL_PWREx_SetRadioBusyTrigger(uint32_t RadioBusyTrigger);
void              HAL_PWREx_SetRadioIRQTrigger(uint32_t RadioIRQTrigger);

void              HAL_PWREx_EnableHOLDC2IT(void);
void              HAL_PWREx_DisableHOLDC2IT(void);

void              HAL_PWREx_HoldCore(uint32_t CPU);
void              HAL_PWREx_ReleaseCore(uint32_t CPU);

#ifdef CORE_CM0PLUS
void              HAL_PWREx_EnableWakeUp_ILAC(void);
void              HAL_PWREx_DisableWakeUp_ILAC(void);
uint32_t          HAL_PWREx_IsEnabledWakeUp_ILAC(void);
#endif /* CORE_CM0PLUS */

HAL_StatusTypeDef HAL_PWREx_EnableGPIOPullUp(uint32_t GPIO, uint32_t GPIONumber);
HAL_StatusTypeDef HAL_PWREx_DisableGPIOPullUp(uint32_t GPIO, uint32_t GPIONumber);
HAL_StatusTypeDef HAL_PWREx_EnableGPIOPullDown(uint32_t GPIO, uint32_t GPIONumber);
HAL_StatusTypeDef HAL_PWREx_DisableGPIOPullDown(uint32_t GPIO, uint32_t GPIONumber);
void              HAL_PWREx_EnablePullUpPullDownConfig(void);
void              HAL_PWREx_DisablePullUpPullDownConfig(void);

void              HAL_PWREx_EnableSRAMRetention(void);
void              HAL_PWREx_DisableSRAMRetention(void);

void              HAL_PWREx_EnableFlashPowerDown(uint32_t PowerMode);
void              HAL_PWREx_DisableFlashPowerDown(uint32_t PowerMode);

void              HAL_PWREx_EnableWPVD(void);
void              HAL_PWREx_DisableWPVD(void);
void              HAL_PWREx_EnableBORPVD_ULP(void);
void              HAL_PWREx_DisableBORPVD_ULP(void);

void              HAL_PWREx_EnablePVM3(void);
void              HAL_PWREx_DisablePVM3(void);

HAL_StatusTypeDef HAL_PWREx_ConfigPVM(const PWR_PVMTypeDef *sConfigPVM);

void              HAL_PWREx_SetRadioEOL(uint32_t RadioEOL);
void              HAL_PWREx_SMPS_SetMode(uint32_t OperatingMode);
uint32_t          HAL_PWREx_SMPS_GetEffectiveMode(void);

void              HAL_PWREx_EnableLowPowerRunMode(void);
HAL_StatusTypeDef HAL_PWREx_DisableLowPowerRunMode(void);

void              HAL_PWREx_EnterSTOP0Mode(uint8_t STOPEntry);
void              HAL_PWREx_EnterSTOP1Mode(uint8_t STOPEntry);
void              HAL_PWREx_EnterSTOP2Mode(uint8_t STOPEntry);
void              HAL_PWREx_EnterSHUTDOWNMode(void);

void              HAL_PWREx_PVD_PVM_IRQHandler(void);

void              HAL_PWREx_PVM3Callback(void);

#define GPIO_PIN_MASK              (0x0000FFFFu) 
#define IS_PWR_GPIO(__GPIO__) (((__GPIO__) == PWR_GPIO_A) ||\
                               ((__GPIO__) == PWR_GPIO_B) ||\
                               ((__GPIO__) == PWR_GPIO_C) ||\
                               ((__GPIO__) == PWR_GPIO_H))


#ifdef __cplusplus
}
#endif


#endif /* STM32WLxx_HAL_PWR_EX_H */

