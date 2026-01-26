/**
 * Example embedded C code for the LoRa simulator.
 * 
 * This file demonstrates how to use the radio API to interact with
 * the simulated LoRa radio from C code.
 */

#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>

/* Radio API functions (implemented in Python) */
extern void radio_off(int node_id);
extern void radio_standby(int node_id);
extern void radio_transmit_data_blocking(int node_id, const uint8_t* data, size_t length);
extern size_t radio_receive_data_nowait(int node_id, uint8_t* buffer, size_t buffer_size);

/**
 * Entry point for the embedded C code.
 * Called by the simulator with the unique node_id for this instance.
 */
void embedded_main(int node_id) {
    uint8_t rx_buffer[256];
    size_t rx_len;
    
    printf("Node %d: Starting embedded code\n", node_id);
    
    /* Put radio in standby mode */
    radio_standby(node_id);
    printf("Node %d: Radio in standby\n", node_id);
    
    /* Send a hello message */
    const char* hello_msg = "Hello from C!";
    radio_transmit_data_blocking(node_id, (const uint8_t*)hello_msg, strlen(hello_msg));
    printf("Node %d: Transmitted hello message\n", node_id);
    
    /* Try to receive some data */
    rx_len = radio_receive_data_nowait(node_id, rx_buffer, sizeof(rx_buffer));
    if (rx_len > 0) {
        rx_buffer[rx_len] = '\0';  /* Null terminate for printing */
        printf("Node %d: Received %zu bytes: %s\n", node_id, rx_len, rx_buffer);
    } else {
        printf("Node %d: No data received\n", node_id);
    }
    
    /* Turn off the radio when done */
    radio_off(node_id);
    printf("Node %d: Radio off, done.\n", node_id);
}
