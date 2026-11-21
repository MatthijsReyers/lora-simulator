#include <stdint.h>
#include "stm32wlxx.h" /* defines IRQn_Type and then includes core_cm4.h */

uint32_t sim_stm32_core_hardware[0x0F0000UL] = {0};

