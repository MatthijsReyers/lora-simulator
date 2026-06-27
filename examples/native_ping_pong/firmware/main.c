#include "stdint.h"

uint8_t* ping = (uint8_t*)"Ping";
uint8_t* pong = (uint8_t*)"Pong";

int wait_for(uint8_t* msg) {
    uint8_t rx_data[256] = {0};
    int res = sim_receive_blocking_no_timeout(rx_data, 256);
    if (res <= 0) {
        return 0;
    }
    if (res == 4 && memcmp(rx_data, msg, 4) == 0) {
        printf("%d Received '%s'\n", sim_radio_id(), msg);
        return 1;
    } 
    printf("%d Received unkown message: '%s' (len %d) \n", sim_radio_id(), rx_data, res);
    return 0;
}

int send_msg(uint8_t* msg) {
    printf("%d Sending  '%s'\n", sim_radio_id(), msg);
    int err = sim_transmit_blocking(msg, 4);
    if (err != 0) {
        printf("%d Error sending '%s': %d\n", sim_radio_id(), msg, err);
        return err;
    }
    return 0;
}

int main(void) 
{
    int node_id = sim_radio_id();
    printf("%d Starting node\n", node_id);
    
    sim_radio_set_rx_config(125, 7, 5, 8, 64, 0, 0, 1, 0, 1);
    sim_radio_set_tx_config(10, 125, 7, 5, 8, 0, 1, 0, 0, 3000);

    if (node_id % 2 == 0) {
        sim_sleep(1);
        send_msg(ping);
        while (wait_for(pong)) {
            sim_sleep(4);
            if (send_msg(ping) != 0) {
                break;
            }
        }
    }
    else while (wait_for(ping)) {
        sim_sleep(2);
        if (send_msg(pong) != 0) {
            break;
        }
    }

    printf("%d Sim done\n", node_id);
}
