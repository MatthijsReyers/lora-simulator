#include <stdint.h>

/* ---- PRIMASK state tracking for x86 simulator ------------------------- */
static uint32_t sim_primask = 0;  /* 0 = interrupts enabled, 1 = disabled */

uint32_t sim_get_PRIMASK(void) {
    return sim_primask;
}

void sim_set_PRIMASK(uint32_t primask) {
    sim_primask = primask;
}
