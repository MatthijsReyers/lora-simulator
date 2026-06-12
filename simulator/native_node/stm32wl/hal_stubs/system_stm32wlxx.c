/**
 * Note: the simulator provides weak bindings for these variables/functions because some STM32
 * projects like to provide their own system_stm32wlxx.c file.
 */
#include "system_stm32wlxx.h"

uint32_t __attribute__((weak)) SystemCoreClock = 4000000UL; /*CPU1: M4 on MSI clock after startup (4MHz)*/

const uint32_t __attribute__((weak)) AHBPrescTable[16UL] = {
    1UL, 3UL, 5UL, 1UL, 1UL, 6UL, 10UL, 32UL, 2UL, 4UL, 8UL, 16UL, 64UL, 128UL, 256UL, 512UL
};
const uint32_t __attribute__((weak)) APBPrescTable[8UL] = {
    0UL, 0UL, 0UL, 0UL, 1UL, 2UL, 3UL, 4UL
};
const uint32_t __attribute__((weak)) MSIRangeTable[16UL] = {
    100000UL, 200000UL, 400000UL, 800000UL, 1000000UL, 2000000UL, 4000000UL, 8000000UL, 16000000UL, \
    24000000UL, 32000000UL, 48000000UL, 0UL, 0UL, 0UL, 0UL
};

void __attribute__((weak)) SystemInit(void) {};

void __attribute__((weak)) SystemCoreClockUpdate(void) {};
