#include "stm32_tiny_vsnprintf.h"

#include <stdio.h>
#include <stdarg.h>

/* tiny vsnprintf replacement mapped back to normal vsnprintf but with bytes written as return value */
int tiny_vsnprintf_like(char *buf, const int size, const char *fmt, va_list args)
{
    vsnprintf(buf, size, fmt, args);
    return size;
}
