/**
 * LoRa Radio API for embedded C code
 * 
 * This header defines the interface between the Python simulator and embedded C code.
 * Each function takes a node_id parameter to identify which radio instance to control.
 */

#ifndef RADIO_API_H
#define RADIO_API_H

#include <stdint.h>
#include <stddef.h>

/**
 * Turn the radio off.
 * 
 * @param node_id The unique identifier for the node instance
 */
void radio_off(int node_id);

/**
 * Put the radio into standby mode.
 * 
 * @param node_id The unique identifier for the node instance
 */
void radio_standby(int node_id);

/**
 * Transmit data over the radio (blocking).
 * This call blocks until the transmission is complete.
 * 
 * @param node_id The unique identifier for the node instance
 * @param data Pointer to the data buffer to transmit
 * @param length Length of the data in bytes
 */
void radio_transmit_data_blocking(int node_id, const uint8_t* data, size_t length);

/**
 * Try to receive data from the radio without blocking.
 * Returns immediately with available data or indicates no data available.
 * 
 * @param node_id The unique identifier for the node instance
 * @param buffer Pointer to the buffer where received data will be stored
 * @param buffer_size Maximum size of the buffer
 * @return Number of bytes received, or 0 if no data available
 */
size_t radio_receive_data_nowait(int node_id, uint8_t* buffer, size_t buffer_size);

/**
 * Entry point for the embedded C code.
 * This function will be called by the simulator to start the embedded code.
 * 
 * @param node_id The unique identifier for the node instance
 */
void embedded_main(int node_id);

#endif /* RADIO_API_H */
