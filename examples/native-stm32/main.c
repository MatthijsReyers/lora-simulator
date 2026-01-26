#include <pthread.h>
#include <Python.h>

// Mutex that blocks while this thread is supposed to be sleeping.
pthread_mutex_t sim_sleep_mutex = PTHREAD_MUTEX_INITIALIZER;

void HAL_Delay(int delay_ms) {
    // Acquire the GIL (and initialize thread state if needed)
    PyGILState_STATE gstate;
    gstate = PyGILState_Ensure();

    double duration = delay_ms / 1000.0;
    sim_sleep(duration);
    printf("sim_sleep finished...\n");

    PyGILState_Release(gstate);

    // Try to lock the sleep mutex, this will block until the simulator reaches the desired time.
    printf("HAL_Delay: waiting for %d ms\n", delay_ms);
    printf("lock\n");
    pthread_mutex_lock(&sim_sleep_mutex);
    pthread_mutex_unlock(&sim_sleep_mutex);
}

/** 
 * DO NOT CALL DIRECTLY!
 * Callback for simulator to indicate that sleep has started/ended.
 */
extern void sim_sleep_start(void) {
    printf("sim_sleep_start\n");
    printf("lock\n");
    pthread_mutex_lock(&sim_sleep_mutex);
}

/** 
 * DO NOT CALL DIRECTLY!
 * Callback for simulator to indicate that sleep has started/ended.
 */
extern void sim_sleep_end(void) {
    printf("sim_sleep_end\n");
    pthread_mutex_unlock(&sim_sleep_mutex);
}



void main() {
    HAL_SUBGHZ_Init();
    HAL_Delay(1000);
}

extern void run_sensor() {
    main();
}
