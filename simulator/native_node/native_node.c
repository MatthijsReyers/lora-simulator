#include <pthread.h>

// Flag that indicates if the sleep condition variable below has emitted yet, we need this because
// of the unavoidable race condition where the Python thread may emit on the condition varible 
// just before the C-code thread starts listening on it in the HAL_Delay function.
volatile int sim_sleep_ready = 0;

// Condition variable that works when async code has locked the mutex above.
pthread_cond_t sim_sleep_finished_cond = PTHREAD_COND_INITIALIZER;

// Accompanying lock for condition variable.
pthread_mutex_t sim_sleep_mutex = PTHREAD_MUTEX_INITIALIZER;

/**
 * Sleep for the given duration (in seconds), this is equivalent to calling sim.sleep() in Python.
 */
void sim_sleep(double duration) {
    // Tell the Python side code to create a new async sleep task.
    sim_sleep_start(duration);

    // Wait for the Python/simulator side of things to setup the simulation in the async runtime
    // and then wait for the sleep to finish.
    pthread_mutex_lock(&sim_sleep_mutex);
    while (!sim_sleep_ready) {
        pthread_cond_wait(&sim_sleep_finished_cond, &sim_sleep_mutex);
    }
    pthread_mutex_unlock(&sim_sleep_mutex);

    // Reset flag for the next sleep.
    sim_sleep_ready = 0;
}

/** 
 * DO NOT CALL DIRECTLY!
 * Callback for simulator to indicate that sleep has started/ended.
 */
extern void sim_sleep_end(void) {
    pthread_mutex_lock(&sim_sleep_mutex);
    sim_sleep_ready = 1;
    pthread_cond_signal(&sim_sleep_finished_cond);
    pthread_mutex_unlock(&sim_sleep_mutex);
}

int main(void);

/**
 * DO NOT CALL DIRECTLY!
 * Callback for simulator for when the simulation should start running the sensor code.
 */
extern void run_sensor() {
    main();
}

