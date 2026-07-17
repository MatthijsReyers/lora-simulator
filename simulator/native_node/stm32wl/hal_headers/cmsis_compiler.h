/**************************************************************************//**
 * @file     cmsis_compiler.h
 * @brief    CMSIS compiler generic header file
 * @version  V5.1.0
 * @date     09. October 2018
 ******************************************************************************/
/*
 * Copyright (c) 2009-2018 Arm Limited. All rights reserved.
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * Licensed under the Apache License, Version 2.0 (the License); you may
 * not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 * www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an AS IS BASIS, WITHOUT
 * WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#ifndef __CMSIS_COMPILER_H
#define __CMSIS_COMPILER_H

#include <stdint.h>


/* ignore some GCC warnings */
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wsign-conversion"
#pragma GCC diagnostic ignored "-Wconversion"
#pragma GCC diagnostic ignored "-Wunused-parameter"

/* Fallback for __has_builtin */
#ifndef __has_builtin
  #define __has_builtin(x) (0)
#endif

/* CMSIS compiler specific defines */
#ifndef   __ASM
  #define __ASM                                  __asm
#endif
#ifndef   __INLINE
  #define __INLINE                               inline
#endif
#ifndef   __STATIC_INLINE
  #define __STATIC_INLINE                        static inline
#endif
#ifndef   __STATIC_FORCEINLINE
  #define __STATIC_FORCEINLINE                   __attribute__((always_inline)) static inline
#endif
#ifndef   __NO_RETURN
  #define __NO_RETURN                            __attribute__((__noreturn__))
#endif
#ifndef   __USED
  #define __USED                                 __attribute__((used))
#endif
#ifndef   __WEAK
  #define __WEAK                                 __attribute__((weak))
#endif
#ifndef   __PACKED
  #define __PACKED                               __attribute__((packed, aligned(1)))
#endif
#ifndef   __PACKED_STRUCT
  #define __PACKED_STRUCT                        struct __attribute__((packed, aligned(1)))
#endif
#ifndef   __PACKED_UNION
  #define __PACKED_UNION                         union __attribute__((packed, aligned(1)))
#endif
#ifndef   __UNALIGNED_UINT32        /* deprecated */
  #pragma GCC diagnostic push
  #pragma GCC diagnostic ignored "-Wpacked"
  #pragma GCC diagnostic ignored "-Wattributes"
  struct __attribute__((packed)) T_UINT32 { uint32_t v; };
  #pragma GCC diagnostic pop
  #define __UNALIGNED_UINT32(x)                  (((struct T_UINT32 *)(x))->v)
#endif
#ifndef   __UNALIGNED_UINT16_WRITE
  #pragma GCC diagnostic push
  #pragma GCC diagnostic ignored "-Wpacked"
  #pragma GCC diagnostic ignored "-Wattributes"
  __PACKED_STRUCT T_UINT16_WRITE { uint16_t v; };
  #pragma GCC diagnostic pop
  #define __UNALIGNED_UINT16_WRITE(addr, val)    (void)((((struct T_UINT16_WRITE *)(void *)(addr))->v) = (val))
#endif
#ifndef   __UNALIGNED_UINT16_READ
  #pragma GCC diagnostic push
  #pragma GCC diagnostic ignored "-Wpacked"
  #pragma GCC diagnostic ignored "-Wattributes"
  __PACKED_STRUCT T_UINT16_READ { uint16_t v; };
  #pragma GCC diagnostic pop
  #define __UNALIGNED_UINT16_READ(addr)          (((const struct T_UINT16_READ *)(const void *)(addr))->v)
#endif
#ifndef   __UNALIGNED_UINT32_WRITE
  #pragma GCC diagnostic push
  #pragma GCC diagnostic ignored "-Wpacked"
  #pragma GCC diagnostic ignored "-Wattributes"
  __PACKED_STRUCT T_UINT32_WRITE { uint32_t v; };
  #pragma GCC diagnostic pop
  #define __UNALIGNED_UINT32_WRITE(addr, val)    (void)((((struct T_UINT32_WRITE *)(void *)(addr))->v) = (val))
#endif
#ifndef   __UNALIGNED_UINT32_READ
  #pragma GCC diagnostic push
  #pragma GCC diagnostic ignored "-Wpacked"
  #pragma GCC diagnostic ignored "-Wattributes"
  __PACKED_STRUCT T_UINT32_READ { uint32_t v; };
  #pragma GCC diagnostic pop
  #define __UNALIGNED_UINT32_READ(addr)          (((const struct T_UINT32_READ *)(const void *)(addr))->v)
#endif
#ifndef   __ALIGNED
  #define __ALIGNED(x)                           __attribute__((aligned(x)))
#endif
#ifndef   __RESTRICT
  #define __RESTRICT                             __restrict
#endif

/* ---- x86 simulator forward declarations (defined in hal_stubs.c) ------- */
void     sim_nop(void);
uint32_t sim_get_PRIMASK(void);
void     sim_set_PRIMASK(uint32_t primask);

#ifndef   __COMPILER_BARRIER
  #define __COMPILER_BARRIER()        sim_nop()
#endif

/* ---- memory barrier / instruction stubs for x86 simulator ------------- */
#ifndef   __DSB
  #define __DSB()          sim_nop()
#endif
#ifndef   __ISB
  #define __ISB()          sim_nop()
#endif
#ifndef   __NOP
  #define __NOP()          sim_nop()
#endif
#ifndef   __DMB
  #define __DMB()          sim_nop()
#endif

#ifndef   __get_PRIMASK
  #define __get_PRIMASK()             sim_get_PRIMASK()
#endif
#ifndef   __set_PRIMASK
  #define __set_PRIMASK(primask)      sim_set_PRIMASK(primask)
#endif

/* ---- LDREX/STREX x86 stubs (single-threaded, always succeed) ---------- */
#ifndef   __LDREXW
  #define __LDREXW(ptr)              (*(ptr))
#endif
#ifndef   __STREXW
  #define __STREXW(val, ptr)         (*(ptr) = (val), 0U)
#endif
#ifndef   __LDREXH
  #define __LDREXH(ptr)              (*(ptr))
#endif
#ifndef   __STREXH
  #define __STREXH(val, ptr)         (*(ptr) = (val), 0U)
#endif

/* ---- CLZ / RBIT x86 stubs --------------------------------------------- */
#ifndef   __CLZ
  #define __CLZ(x)                   ((x) ? (uint8_t)__builtin_clz(x) : 32U)
#endif
#ifndef   __RBIT
  #define __RBIT(x)                  sim_rbit(x)
#endif

static inline uint32_t sim_rbit(uint32_t x) {
    x = ((x & 0x55555555U) << 1)  | ((x & 0xAAAAAAAAU) >> 1);
    x = ((x & 0x33333333U) << 2)  | ((x & 0xCCCCCCCCU) >> 2);
    x = ((x & 0x0F0F0F0FU) << 4)  | ((x & 0xF0F0F0F0U) >> 4);
    x = ((x & 0x00FF00FFU) << 8)  | ((x & 0xFF00FF00U) >> 8);
    x = ( x << 16 ) | ( x >> 16 );
    return x;
}


static inline void __disable_irq(void) { sim_set_PRIMASK(1); }
static inline void __enable_irq(void)  { sim_set_PRIMASK(0); }



#endif /* __CMSIS_COMPILER_H */

