
int HAL_GetTick() {
    return sim_current_time() * 1000.0;
}

void HAL_Delay(int delay_ms) {
    double duration = delay_ms / 1000.0;
    sim_sleep(duration);
}

int main(void) {
    int subghz;
    HAL_SUBGHZ_Init(&subghz);

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

