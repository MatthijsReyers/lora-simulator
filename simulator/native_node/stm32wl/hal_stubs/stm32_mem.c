#include "./stm32_mem.h"

void UTIL_MEM_cpy_8( void *dst, const void *src, uint16_t size )
{
  uint8_t* dst8= (uint8_t *) dst;
  uint8_t* src8= (uint8_t *) src;

  while( size-- )
    {
        *dst8++ = *src8++;
    }
}

void UTIL_MEM_cpyr_8( void *dst, const void *src, uint16_t size )
{
    uint8_t* dst8= (uint8_t *) dst;
    uint8_t* src8= (uint8_t *) src;

    dst8 = dst8 + ( size - 1 );
    while( size-- )
    {
        *dst8-- = *src8++;
    }
}

void UTIL_MEM_set_8( void *dst, uint8_t value, uint16_t size )
{
  uint8_t* dst8= (uint8_t *) dst;
  while( size-- )
  {
    *dst8++ = value;
  }
}