# STM32 ping pong

Two simulated STM32WL55 boards playing LoRa ping pong, running firmware that also builds for a
real NUCLEO-WL55JC1. The whole firmware is `firmware/main.c`: it only uses the STM32 HAL and
drives the radio directly with SX126x commands through `HAL_SUBGHZ_*`.

## Running in the simulator

```bash
pipenv run python examples/native-stm32-ping-pong/main.py
```

## Building for a real board

1. Create an empty STM32CubeIDE project for the NUCLEO-WL55JC1 (Cortex-M4 core), or take any
   project that has the STM32WL HAL drivers, the startup file and a linker script.
2. Replace its `main.c` with `firmware/main.c` and its `stm32wlxx_hal_conf.h` with the one from
   `firmware/` (it enables the GPIO, UART, SUBGHZ, PWR, RCC and CORTEX HAL modules).
3. Remove `stm32wlxx_it.c` and `stm32wlxx_hal_msp.c` if the project has them, `main.c` has its
   own `SysTick_Handler` and `SUBGHZ_Radio_IRQHandler`.
4. Flash two boards. The log comes out of the ST-LINK virtual COM port at 115200 baud, the blue
   LED toggles on every transmitted packet and the green LED on every received one.

The pins (LEDs, antenna switch, UART), the TCXO setup and the 868 MHz image calibration at the
top of `main.c` are for the NUCLEO-WL55JC1, adjust them for other boards.

## What the simulator does with it

`simulator/native_node/stm32wl/hal_stubs/stm32wlxx_hal_subghz.c` translates the radio commands to
the simulated radio. One rule to keep in mind when changing the firmware: after starting a
transmission or reception, wait for the radio interrupt with `HAL_PWR_EnterSLEEPMode()` (as
`radio_wait_irq()` does). That is the moment the simulator runs the radio operation and calls the
interrupt handler, a busy loop or `HAL_Delay()` there would not let simulation time pass correctly.
