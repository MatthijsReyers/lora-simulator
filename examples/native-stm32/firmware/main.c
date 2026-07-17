#define USE_HAL_SUBGHZ_REGISTER_CALLBACKS 1

#include "stm32wlxx_hal_conf.h"
#include "radio.h"

SUBGHZ_HandleTypeDef subghz;

void on_tx_complete() {
    printf("on_tx_complete callback called!\n");
}

void on_rx_complete() {
    printf("on_rx_complete callback called!\n");
}


int main(void) {
    HAL_SUBGHZ_Init(&subghz);

    HAL_SUBGHZ_RegisterCallback(&subghz, HAL_SUBGHZ_TX_COMPLETE_CB_ID, &on_tx_complete);
    HAL_SUBGHZ_RegisterCallback(&subghz, HAL_SUBGHZ_RX_COMPLETE_CB_ID, &on_rx_complete);


    printf("1\n");
    HAL_Delay(1000);
    printf("2\n");
    HAL_Delay(1000);
    printf("3\n");
    HAL_Delay(1000);
    printf("4\n");
    HAL_Delay(1000);
    printf("5\n");
}

