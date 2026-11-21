/**
 * Power profile sequence for the STM32WL55 (NUCLEO-WL55JC1), in a single file.
 *
 * Walks the radio through every state the simulator's power profile needs, each held long enough
 * to read its level from a Joulescope on the board's 5 V supply. extract_power_profile.py finds
 * the phases back in the capture, the durations here must match the constants in that script.
 *
 * After boot: BOARD_INDEX + 1 blips of 200 ms RX (tells the boards apart in the capture), then
 * CYCLES times:
 *
 *     marker       3 x (100 ms RX + 100 ms standby)
 *     mcu run      3 s    radio asleep, CPU busy waiting (4 MHz MSI)
 *     sleep        4 s    radio asleep, CPU stopped (as in all following phases)
 *     toggles      3 x (500 ms standby + 500 ms sleep)
 *     standby RC   3 s
 *     standby XOSC 2 s
 *     RX           2 s each, 100 ms standby in between: BW 125, 250, 500 kHz at SF7,
 *                  SF12 at 125 kHz, boosted gain at SF7 125 kHz
 *     TX sweep 1   0..22 dBm set up the way the ST SubGHz_Phy middleware does on this board:
 *                  low power amplifier up to 15 dBm, high power amplifier above, 40 us ramp
 *     TX sweep 2   0..14 dBm on the low power amplifier tuned for 14 dBm with a 200 us ramp, the
 *                  setup of examples/native-stm32-ping-pong
 *                  (every pulse: SF7, 125 kHz, 64 bytes, followed by 500 ms standby)
 *
 * and then the radio sleeps and the CPU stays stopped. The CPU is stopped (Stop 1) during every
 * phase but "mcu run", so what a phase adds over "sleep" is the radio alone.
 *
 * Builds like examples/native-stm32-ping-pong/firmware/main.c (stm32-ping-pong-example branch),
 * with the stm32wlxx_hal_conf.h of that example.
 */
#include <stdio.h>
#include <string.h>
#include "stm32wlxx_hal.h"

#define BOARD_INDEX         0           /* 0, 1, ...: number of extra blips after boot */
#define CYCLES              3
#define RF_FREQUENCY        869525000   /* Hz, the 10 % duty cycle, 500 mW band */

#define SPREADING_FACTOR    7
#define CODING_RATE         0x01        /* 4/5 */
#define PREAMBLE_LENGTH     8           /* symbols */
#define PAYLOAD_LENGTH      64          /* bytes */

#define BW_125              0x04
#define BW_250              0x05
#define BW_500              0x06

/* ---- Radio interrupt flags (SX126x GetIrqStatus) ------------------------------------------ */
#define IRQ_TX_DONE         0x0001
#define IRQ_RX_DONE         0x0002
#define IRQ_HEADER_ERROR    0x0020
#define IRQ_CRC_ERROR       0x0040
#define IRQ_TIMEOUT         0x0200
#define IRQ_ALL             (IRQ_TX_DONE | IRQ_RX_DONE | IRQ_HEADER_ERROR | IRQ_CRC_ERROR | IRQ_TIMEOUT)

/* ---- Radio registers ---------------------------------------------------------------------- */
#define REG_RX_GAIN         0x08AC      /* 0x94 = power saving, 0x96 = boosted */
#define REG_TX_CLAMP        0x08D8
#define REG_OCP             0x08E7      /* over current protection of the amplifier */
#define REG_XTA_TRIM        0x0911
#define REG_SMPS_DRIVE      0x0923      /* bits 2:1: 0 = 20 mA, 1 = 40 mA, 2 = 60 mA, 3 = 100 mA */

/* ---- Pins of the NUCLEO-WL55JC1 ----------------------------------------------------------- */
#define RF_SWITCH_PORT      GPIOC       /* antenna switch */
#define RF_SWITCH_CTRL1     GPIO_PIN_4
#define RF_SWITCH_CTRL2     GPIO_PIN_5
#define RF_SWITCH_CTRL3     GPIO_PIN_3
#define UART_PORT           GPIOA       /* USART2, wired to the ST-LINK virtual COM port */
#define UART_TX             GPIO_PIN_2
#define UART_RX             GPIO_PIN_3

enum antenna { ANTENNA_OFF, ANTENNA_RX, ANTENNA_TX_LP, ANTENNA_TX_HP };

static SUBGHZ_HandleTypeDef hsubghz;
static UART_HandleTypeDef huart2;
static RTC_HandleTypeDef hrtc;

static volatile uint16_t radio_irq = 0;
static volatile uint16_t timer_irq = 0;
static volatile uint16_t never = 0;


/* ---- Interrupt handlers ------------------------------------------------------------------- */

void SysTick_Handler(void)
{
    HAL_IncTick();
}

void SUBGHZ_Radio_IRQHandler(void)
{
    uint8_t status[2] = {0, 0};
    HAL_SUBGHZ_ExecGetCmd(&hsubghz, RADIO_GET_IRQSTATUS, status, 2);
    HAL_SUBGHZ_ExecSetCmd(&hsubghz, RADIO_CLR_IRQSTATUS, status, 2);
    radio_irq |= (uint16_t)((status[0] << 8) | status[1]);
}

void RTC_WKUP_IRQHandler(void)
{
    HAL_RTCEx_WakeUpTimerIRQHandler(&hrtc);
    timer_irq = 1;
}

int __io_putchar(int ch)
{
    HAL_UART_Transmit(&huart2, (uint8_t *)&ch, 1, HAL_MAX_DELAY);
    return ch;
}


/* ---- Sleeping ----------------------------------------------------------------------------- */

/* Stop the CPU (Stop 1 mode) until an interrupt handler has set *flag. */
static void stop_until(volatile uint16_t *flag)
{
    __disable_irq();
    while (*flag == 0) {
        HAL_PWREx_EnterSTOP1Mode(PWR_STOPENTRY_WFI);
        __enable_irq();
        __disable_irq();
    }
    __enable_irq();
}

/* Stop the CPU for the given time, the wake-up timer of the real-time clock wakes it up. */
static void sleep_ms(uint32_t ms)
{
    timer_irq = 0;
    /* The timer counts at 32768 Hz / 16 = 2048 Hz. */
    HAL_RTCEx_SetWakeUpTimer_IT(&hrtc, ms * 2048 / 1000 - 1, RTC_WAKEUPCLOCK_RTCCLK_DIV16, 0);
    stop_until(&timer_irq);
    HAL_RTCEx_DeactivateWakeUpTimer(&hrtc);
}


/* ---- Board setup -------------------------------------------------------------------------- */

static void board_init(void)
{
    GPIO_InitTypeDef gpio = {0};
    RCC_OscInitTypeDef osc = {0};

    __HAL_RCC_GPIOA_CLK_ENABLE();
    __HAL_RCC_GPIOC_CLK_ENABLE();
    __HAL_RCC_USART2_CLK_ENABLE();

    gpio.Mode = GPIO_MODE_OUTPUT_PP;
    gpio.Pull = GPIO_NOPULL;
    gpio.Speed = GPIO_SPEED_FREQ_LOW;
    gpio.Pin = RF_SWITCH_CTRL1 | RF_SWITCH_CTRL2 | RF_SWITCH_CTRL3;
    HAL_GPIO_Init(RF_SWITCH_PORT, &gpio);

    gpio.Mode = GPIO_MODE_AF_PP;
    gpio.Alternate = GPIO_AF7_USART2;
    gpio.Pin = UART_TX | UART_RX;
    HAL_GPIO_Init(UART_PORT, &gpio);

    huart2.Instance = USART2;
    huart2.Init.BaudRate = 115200;
    huart2.Init.WordLength = UART_WORDLENGTH_8B;
    huart2.Init.StopBits = UART_STOPBITS_1;
    huart2.Init.Parity = UART_PARITY_NONE;
    huart2.Init.Mode = UART_MODE_TX_RX;
    huart2.Init.HwFlowCtl = UART_HWCONTROL_NONE;
    huart2.Init.OverSampling = UART_OVERSAMPLING_16;
    HAL_UART_Init(&huart2);

    HAL_PWR_EnableBkUpAccess();
    osc.OscillatorType = RCC_OSCILLATORTYPE_LSE;
    osc.LSEState = RCC_LSE_ON;
    HAL_RCC_OscConfig(&osc);
    __HAL_RCC_RTC_CONFIG(RCC_RTCCLKSOURCE_LSE);
    __HAL_RCC_RTC_ENABLE();
    __HAL_RCC_RTCAPB_CLK_ENABLE();
    hrtc.Instance = RTC;
    hrtc.Init.AsynchPrediv = 127;
    hrtc.Init.SynchPrediv = 255;
    HAL_RTC_Init(&hrtc);
    HAL_NVIC_SetPriority(RTC_WKUP_IRQn, 0, 0);
    HAL_NVIC_EnableIRQ(RTC_WKUP_IRQn);
}


/* ---- Radio -------------------------------------------------------------------------------- */

static void radio_command(SUBGHZ_RadioSetCmd_t command, uint8_t *params, uint16_t size)
{
    if (HAL_SUBGHZ_ExecSetCmd(&hsubghz, command, params, size) != HAL_OK) {
        printf("Radio command 0x%02X failed\r\n", (unsigned)command);
    }
}

static void radio_antenna_switch(enum antenna antenna)
{
    HAL_GPIO_WritePin(RF_SWITCH_PORT, RF_SWITCH_CTRL3, antenna != ANTENNA_OFF ? GPIO_PIN_SET : GPIO_PIN_RESET);
    HAL_GPIO_WritePin(RF_SWITCH_PORT, RF_SWITCH_CTRL1,
                      antenna == ANTENNA_RX || antenna == ANTENNA_TX_LP ? GPIO_PIN_SET : GPIO_PIN_RESET);
    HAL_GPIO_WritePin(RF_SWITCH_PORT, RF_SWITCH_CTRL2,
                      antenna == ANTENNA_TX_LP || antenna == ANTENNA_TX_HP ? GPIO_PIN_SET : GPIO_PIN_RESET);
}

/* How much current the radio's switching regulator may deliver: 1 = 40 mA, 2 = 60 mA. */
static void radio_smps_drive(uint8_t level)
{
    uint8_t value = 0;
    HAL_SUBGHZ_ReadRegister(&hsubghz, REG_SMPS_DRIVE, &value);
    HAL_SUBGHZ_WriteRegister(&hsubghz, REG_SMPS_DRIVE, (value & ~0x06) | (level << 1));
}

static void radio_set_modulation(uint8_t spreading_factor, uint8_t bandwidth)
{
    radio_command(RADIO_SET_MODULATIONPARAMS, (uint8_t[]){
        spreading_factor, bandwidth, CODING_RATE, (spreading_factor >= 11 && bandwidth == BW_125),
    }, 4);
}

static void radio_set_payload_length(uint8_t length)
{
    radio_command(RADIO_SET_PACKETPARAMS, (uint8_t[]){
        PREAMBLE_LENGTH >> 8, PREAMBLE_LENGTH & 0xFF,
        0x00,           /* explicit header */
        length,
        0x01,           /* CRC on */
        0x00,           /* standard IQ */
    }, 6);
}

static void radio_init(void)
{
    __HAL_RCC_SUBGHZSPI_CLK_ENABLE();
    hsubghz.Init.BaudratePrescaler = SUBGHZSPI_BAUDRATEPRESCALER_4;
    HAL_SUBGHZ_Init(&hsubghz);
    HAL_NVIC_SetPriority(SUBGHZ_Radio_IRQn, 0, 0);
    HAL_NVIC_EnableIRQ(SUBGHZ_Radio_IRQn);

    radio_command(RADIO_SET_STANDBY, (uint8_t[]){0x00}, 1);

    /* Radio on its switching regulator (SMPS), the board has the inductor for it. */
    radio_command(RADIO_SET_REGULATORMODE, (uint8_t[]){0x01}, 1);
    radio_smps_drive(1);

    /* The board's 32 MHz TCXO is powered by the radio: 1.7 V, 10 ms to start up. */
    radio_command(RADIO_SET_TCXOMODE, (uint8_t[]){0x01, 0x00, 0x02, 0x80}, 4);
    HAL_SUBGHZ_WriteRegister(&hsubghz, REG_XTA_TRIM, 0x00);
    radio_command(RADIO_CALIBRATE, (uint8_t[]){0x7F}, 1);
    radio_command(RADIO_CALIBRATEIMAGE, (uint8_t[]){0xD7, 0xDB}, 2); /* for 863-870 MHz */

    radio_command(RADIO_SET_PACKETTYPE, (uint8_t[]){0x01}, 1); /* LoRa */

    uint32_t freq = (uint32_t)(((uint64_t)RF_FREQUENCY << 25) / 32000000);
    radio_command(RADIO_SET_RFFREQUENCY, (uint8_t[]){freq >> 24, freq >> 16, freq >> 8, freq}, 4);

    radio_set_modulation(SPREADING_FACTOR, BW_125);
    radio_set_payload_length(255);
    radio_command(RADIO_SET_BUFFERBASEADDRESS, (uint8_t[]){0x00, 0x00}, 2);
    radio_command(RADIO_CFG_DIOIRQ, (uint8_t[]){
        IRQ_ALL >> 8, IRQ_ALL & 0xFF, IRQ_ALL >> 8, IRQ_ALL & 0xFF, 0, 0, 0, 0,
    }, 8);
}

/* Sleep with the configuration kept (warm start). The next command wakes the radio up again. */
static void radio_sleep(void)
{
    radio_antenna_switch(ANTENNA_OFF);
    radio_smps_drive(1);
    radio_command(RADIO_SET_SLEEP, (uint8_t[]){0x04}, 1);
    HAL_Delay(2);
}

static void radio_standby(void)
{
    radio_command(RADIO_SET_STANDBY, (uint8_t[]){0x00}, 1); /* RC oscillator, TCXO off */
}

/* Receive continuously for the given time, then go back to standby. */
static void radio_receive_for(uint32_t ms, uint8_t spreading_factor, uint8_t bandwidth, int boosted)
{
    radio_set_modulation(spreading_factor, bandwidth);
    radio_set_payload_length(255);
    HAL_SUBGHZ_WriteRegister(&hsubghz, REG_RX_GAIN, boosted ? 0x96 : 0x94);
    radio_antenna_switch(ANTENNA_RX);
    radio_command(RADIO_SET_RX, (uint8_t[]){0xFF, 0xFF, 0xFF}, 3); /* until told otherwise */
    sleep_ms(ms);
    radio_standby();
    radio_irq = 0;
}

/* Transmit one packet and wait until it has been sent. */
static void radio_send(void)
{
    static uint8_t data[PAYLOAD_LENGTH];

    radio_set_modulation(SPREADING_FACTOR, BW_125);
    radio_set_payload_length(PAYLOAD_LENGTH);
    HAL_SUBGHZ_WriteBuffer(&hsubghz, 0, data, PAYLOAD_LENGTH);
    radio_irq = 0;
    radio_command(RADIO_SET_TX, (uint8_t[]){0, 0, 0}, 3); /* no timeout */
    stop_until(&radio_irq);
    radio_irq = 0;
}

/* Transmit at the given power the way the ST SubGHz_Phy middleware sets it up on this board
   (SUBGRF_SetRfTxPower with the NUCLEO-WL55JC board support). */
static void radio_send_middleware(int power)
{
    if (power <= 15) {
        /* Low power amplifier tuned for 15 dBm. */
        radio_command(RADIO_SET_PACONFIG, (uint8_t[]){0x07, 0x00, 0x01, 0x01}, 4);
        HAL_SUBGHZ_WriteRegister(&hsubghz, REG_OCP, 0x18);
        radio_command(RADIO_SET_TXPARAMS, (uint8_t[]){(uint8_t)(int8_t)(power - 1), 0x02}, 2);
        radio_smps_drive(2);
        radio_antenna_switch(ANTENNA_TX_LP);
    } else {
        /* High power amplifier tuned for 22 dBm, with the antenna mismatch workaround of the
           STM32WL errata sheet. */
        uint8_t clamp = 0;
        HAL_SUBGHZ_ReadRegister(&hsubghz, REG_TX_CLAMP, &clamp);
        HAL_SUBGHZ_WriteRegister(&hsubghz, REG_TX_CLAMP, clamp | 0x1E);
        radio_command(RADIO_SET_PACONFIG, (uint8_t[]){0x04, 0x07, 0x00, 0x01}, 4);
        HAL_SUBGHZ_WriteRegister(&hsubghz, REG_OCP, 0x38);
        radio_command(RADIO_SET_TXPARAMS, (uint8_t[]){(uint8_t)power, 0x02}, 2);
        radio_smps_drive(1);
        radio_antenna_switch(ANTENNA_TX_HP);
    }
    radio_send();
}

/* Transmit at the given power (up to 14 dBm) the way examples/native-stm32-ping-pong does. */
static void radio_send_ping_pong(int power)
{
    radio_command(RADIO_SET_PACONFIG, (uint8_t[]){0x04, 0x00, 0x01, 0x01}, 4);
    radio_command(RADIO_SET_TXPARAMS, (uint8_t[]){(uint8_t)power, 0x04}, 2);
    radio_smps_drive(2);
    radio_antenna_switch(ANTENNA_TX_LP);
    radio_send();
}


/* ---- The sequence ------------------------------------------------------------------------- */

int main(void)
{
    uint32_t id = (HAL_GetUIDw0() ^ HAL_GetUIDw1() ^ HAL_GetUIDw2()) & 0xFFFF;

    HAL_Init(); /* We stay on the 4 MHz clock the chip boots with. */
    board_init();
    radio_init();
    printf("[%04X] Power profile sequence started, board index %d\r\n", (unsigned)id, BOARD_INDEX);

    /* Which board is this in the capture. */
    sleep_ms(1000);
    for (int i = 0; i <= BOARD_INDEX; i++) {
        radio_receive_for(200, SPREADING_FACTOR, BW_125, 0);
        sleep_ms(200);
    }
    sleep_ms(1000);

    for (int cycle = 0; cycle < CYCLES; cycle++) {
        printf("[%04X] cycle %d\r\n", (unsigned)id, cycle);

        /* Marker */
        for (int i = 0; i < 3; i++) {
            radio_receive_for(100, SPREADING_FACTOR, BW_125, 0);
            sleep_ms(100);
        }

        /* Radio asleep: CPU running, then CPU stopped */
        radio_sleep();
        HAL_Delay(3000);
        sleep_ms(4000);

        /* Sleep to standby edges */
        for (int i = 0; i < 3; i++) {
            radio_standby();
            sleep_ms(500);
            radio_sleep();
            sleep_ms(500);
        }

        /* Standby on the RC oscillator, then with the TCXO running */
        radio_standby();
        sleep_ms(3000);
        radio_command(RADIO_SET_STANDBY, (uint8_t[]){0x01}, 1);
        sleep_ms(2000);
        radio_standby();
        sleep_ms(100);

        /* Receiving */
        radio_receive_for(2000, 7, BW_125, 0);
        sleep_ms(100);
        radio_receive_for(2000, 7, BW_250, 0);
        sleep_ms(100);
        radio_receive_for(2000, 7, BW_500, 0);
        sleep_ms(100);
        radio_receive_for(2000, 12, BW_125, 0);
        sleep_ms(100);
        radio_receive_for(2000, 7, BW_125, 1);
        HAL_SUBGHZ_WriteRegister(&hsubghz, REG_RX_GAIN, 0x94);
        sleep_ms(500);

        /* Transmitting */
        for (int power = 0; power <= 22; power++) {
            radio_send_middleware(power);
            sleep_ms(500);
        }
        for (int power = 0; power <= 14; power++) {
            radio_send_ping_pong(power);
            sleep_ms(500);
        }
        sleep_ms(1000);
    }

    printf("[%04X] done\r\n", (unsigned)id);
    radio_sleep();
    stop_until(&never);
}
