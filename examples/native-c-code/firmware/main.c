
int main(void) 
{
    printf("c node %d is starting...\n", sim_radio_id());

    sim_radio_set_rx_config(125, 7, 5, 8, 64, 0, 0, 0, 0, 0);

    sim_radio_set_tx_config(10, 125, 7, 5, 8, 0, 1, 0, 0, 3000);

}