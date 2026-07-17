#ifndef __STM32_MEM_H__
#define __STM32_MEM_H__

#ifdef __cplusplus
extern "C"
{
#endif
/* Includes ------------------------------------------------------------------*/
#include <stdint.h>

/* Exported types ------------------------------------------------------------*/
/* Exported constants --------------------------------------------------------*/
/* Exported macro ------------------------------------------------------------*/
/* ---- Memory mapping macros ----------------------------------------------- */
#define UTIL_MEM_PLACE_IN_SECTION( __x__ ) UTIL_PLACE_IN_SECTION( __x__ )
#define UTIL_MEM_ALIGN ALIGN

/** This API copies one buffer to another */
void UTIL_MEM_cpy_8( void *dst, const void *src, uint16_t size );

/** This API copies one buffer to another in reverse */
void UTIL_MEM_cpyr_8( void *dst, const void *src, uint16_t size );

/** This API fills a buffer with value */
void UTIL_MEM_set_8( void *dst, uint8_t value, uint16_t size );

#ifdef __cplusplus
}
#endif

#endif /* __STM32_MEM_H__ */
