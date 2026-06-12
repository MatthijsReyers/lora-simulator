#ifndef __SYSTEM_STM32WLXX_H
#define __SYSTEM_STM32WLXX_H

#ifdef __cplusplus
 extern "C" {
#endif

#include <stdint.h>

extern uint32_t SystemCoreClock;            /*!< System Clock Frequency */

extern const uint32_t AHBPrescTable[16];    /*!< AHB prescalers table values */
extern const uint32_t APBPrescTable[8];     /*!< APB prescalers table values */
extern const uint32_t MSIRangeTable[16];    /*!< MSI ranges table values     */

extern void SystemInit(void);
extern void SystemCoreClockUpdate(void);

#ifdef __cplusplus
}
#endif

#endif /*__SYSTEM_STM32WLXX_H */
