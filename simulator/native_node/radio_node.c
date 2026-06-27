#include <pthread.h>
#include <stdint.h>
#include <stdint.h>

// Flag that indicates if the sleep condition variable below has emitted yet, we need this because
// of the unavoidable race condition where the Python thread may emit on the condition varible 
// just before the C-code thread starts listening on it in the HAL_Delay function.
volatile int sim_tx_done_ready = 0;

// Condition variable that works when async code has locked the mutex above.
pthread_cond_t sim_tx_done_finished_cond = PTHREAD_COND_INITIALIZER;

// Accompanying lock for condition variable.
pthread_mutex_t sim_tx_done_mutex = PTHREAD_MUTEX_INITIALIZER;

// Transmit error state
volatile int sim_tx_err = 0;


volatile int sim_rx_done_ready = 0;
pthread_cond_t sim_rx_done_finished_cond = PTHREAD_COND_INITIALIZER;
pthread_mutex_t sim_rx_done_mutex = PTHREAD_MUTEX_INITIALIZER;
volatile int sim_rx_bytes_received = 0;
volatile int sim_rx_err = 0;


int SIM_ERR_SIM_ENDED = -1;
int SIM_ERR_ALREADY_TRANSMITTING = -2;
int SIM_ERR_TIMEOUT = -3;
int SIM_ERR_UNKNOWN_ERR = -4;


/**
 * Wait for the active transmission to finish (if one is active, returns immediately otherwise).
 */
int sim_wait_transmit_finished() {

    // Cannot wait for transmit to finish if we're not transmitting!
    if (sim_already_transmitting()) {
        return SIM_ERR_ALREADY_TRANSMITTING;
    }

    // Wait for the Python/simulator side of things to setup the simulation in the async runtime
    // and then wait for the sleep to finish.
    pthread_mutex_lock(&sim_tx_done_mutex);
    while (!sim_tx_done_ready) {
        pthread_cond_wait(&sim_tx_done_finished_cond, &sim_tx_done_mutex);
    }
    pthread_mutex_unlock(&sim_tx_done_mutex);

    // Reset flag for the next sleep.
    sim_tx_done_ready = 0;

    return sim_tx_err;
}

/**
 * Non-blocking check for if the transmission has finished
 */
int sim_is_transmit_finished() {
    return sim_tx_done_ready;
}


/**
 * Wait for the active receive event to finish
 */
int sim_wait_receive_finished() {

    // Wait for the Python/simulator side of things to finish receiving (or timeout)
    pthread_mutex_lock(&sim_rx_done_mutex);
    while (!sim_rx_done_ready) {
        pthread_cond_wait(&sim_rx_done_finished_cond, &sim_rx_done_mutex);
    }
    pthread_mutex_unlock(&sim_rx_done_mutex);

    // Reset flag for the next receive.
    sim_rx_done_ready = 0;

    return sim_rx_err;
}

int sim_transmit_blocking(uint8_t* data, int len) {
    // Tell the Python side code to create a new async transmit task.
    int err = sim_transmit_start(data, len);

    // Handle error case where transmit couldn't be started, for example because the radio is off.
    if (err != 0) {
        return err;
    }

    // Wait for the transmission to finish before returning
    return sim_wait_transmit_finished();
}

int sim_receive_blocking(uint8_t* data, int len, int timeout_ms) {

    // Tell the Python side code to create a new async receive task.
    int err = sim_start_receive(data, len, timeout_ms);

    // Handle error case where receive couldn't be started, for example because the radio is off.
    if (err != 0) {
        return err;
    }

    // Wait for the receive to complete or timeout before returning
    err = sim_wait_receive_finished();

    if (err != 0) {
        return err;
    }

    return sim_rx_bytes_received;
}

int sim_receive_blocking_no_timeout(uint8_t* data, int len) {
    return sim_receive_blocking(data, len, -1);
}

/** 
 * DO NOT CALL DIRECTLY!
 * Callback for simulator to indicate when the radio state machine changes.
 */
extern void sim_tx_done_callback(int err) {
    pthread_mutex_lock(&sim_tx_done_mutex);
    sim_tx_done_ready = 1;
    sim_tx_err = err;
    pthread_cond_signal(&sim_tx_done_finished_cond);
    pthread_mutex_unlock(&sim_tx_done_mutex);
}

/** 
 * DO NOT CALL DIRECTLY!
 * Callback for simulator to indicate when the radio state machine changes.
 */
extern void sim_rx_done_callback(int bytes_received, int err) {
    pthread_mutex_lock(&sim_rx_done_mutex);
    sim_rx_bytes_received = bytes_received;
    sim_rx_done_ready = 1;
    sim_rx_err = err;
    pthread_cond_signal(&sim_rx_done_finished_cond);
    pthread_mutex_unlock(&sim_rx_done_mutex);
}
