"""Quick smoke test: verify hal_stubs.c compiles with the new NVIC backing store."""
import cffi

ffi = cffi.FFI()
ffi.cdef("""
    void sim_nop(void);
    uint32_t sim_get_PRIMASK(void);
    void sim_set_PRIMASK(uint32_t);
""")
ffi.set_source(
    '_test_nvic',
    '#include "stm32wlxx_hal_def.h"\n#include "stm32wlxx.h"',
    include_dirs=['simulator/native_node/stm32wl/hal_headers'],
    sources=['simulator/native_node/stm32wl/hal_stubs.c'],
)
ffi.compile(verbose=True)
print('COMPILE SUCCESS')
