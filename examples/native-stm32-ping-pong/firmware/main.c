/**
 * LoRa ping pong for the STM32WL55 (NUCLEO-WL55JC1), in a single file.
 *
 * Every board runs this same firmware. A board listens for a packet, when it hears "PING" it
 * answers "PONG" and the other way around. A board that hears nothing for a while sends a
 * "PING" itself, which is what gets the game started.
 *
 * The radio is driven directly with SX126x commands through the STM32 HAL (HAL_SUBGHZ_*), no
 * radio middleware, sequencer or timer server is needed. This exact file runs in the simulator
 * (see ../main.py) and on a real board (see ../README.md).
 */
#include <stdio.h>
#include <string.h>
#include "stm32wlxx_hal.h"

/* ---- Radio settings, both boards must use the same ---------------------------------------- */
#define RF_FREQUENCY        868000000   /* Hz */
#define TX_POWER            14          /* dBm, -17 to 14 (low power amplifier output) */
#define SPREADING_FACTOR    7           /* 7 to 12 */
#define BANDWIDTH           0x04        /* 0x04 = 125 kHz, 0x05 = 250 kHz, 0x06 = 500 kHz */
#define CODING_RATE         0x01        /* 0x01 = 4/5 ... 0x04 = 4/8 */
#define PREAMBLE_LENGTH     8           /* symbols */
#define PAYLOAD_LENGTH      4           /* bytes per packet, 4 to 255: "PING" or "PONG", then zeros */

#define REPLY_DELAY         1000        /* ms to wait before answering a packet */
#define RX_TIMEOUT          3000        /* ms without a packet before sending a PING ourselves */

/* ---- Radio interrupt flags (SX126x GetIrqStatus) ------------------------------------------ */
#define IRQ_TX_DONE         0x0001
#define IRQ_RX_DONE         0x0002
#define IRQ_HEADER_ERROR    0x0020
#define IRQ_CRC_ERROR       0x0040
#define IRQ_TIMEOUT         0x0200
#define IRQ_ALL             (IRQ_TX_DONE | IRQ_RX_DONE | IRQ_HEADER_ERROR | IRQ_CRC_ERROR | IRQ_TIMEOUT)

/* ---- Pins of the NUCLEO-WL55JC1 ----------------------------------------------------------- */
#define LED_PORT            GPIOB
#define LED_BLUE            GPIO_PIN_15 /* toggles on every transmitted packet */
#define LED_GREEN           GPIO_PIN_9  /* toggles on every received packet */
#define RF_SWITCH_PORT      GPIOC       /* antenna switch */
#define RF_SWITCH_CTRL1     GPIO_PIN_4
#define RF_SWITCH_CTRL2     GPIO_PIN_5
#define RF_SWITCH_CTRL3     GPIO_PIN_3
#define UART_PORT           GPIOA       /* USART2, wired to the ST-LINK virtual COM port */
#define UART_TX             GPIO_PIN_2
#define UART_RX             GPIO_PIN_3

static SUBGHZ_HandleTypeDef hsubghz;
static UART_HandleTypeDef huart2;

/* Interrupt flags the radio raised, set by the interrupt handler and consumed by the main loop. */
static volatile uint16_t radio_irq = 0;


/* ---- Interrupt handlers ------------------------------------------------------------------- */

/* 1 ms tick, used by HAL_Delay() and HAL_GetTick(). */
void SysTick_Handler(void)
{
    HAL_IncTick();
}

/* The radio has something to tell us: remember what and acknowledge it. */
void SUBGHZ_Radio_IRQHandler(void)
{
    uint8_t status[2] = {0, 0};
    HAL_SUBGHZ_ExecGetCmd(&hsubghz, RADIO_GET_IRQSTATUS, status, 2);
    HAL_SUBGHZ_ExecSetCmd(&hsubghz, RADIO_CLR_IRQSTATUS, status, 2);
    radio_irq |= (uint16_t)((status[0] << 8) | status[1]);
}

/* printf() ends up here, one character at a time. */
int __io_putchar(int ch)
{
    HAL_UART_Transmit(&huart2, (uint8_t *)&ch, 1, HAL_MAX_DELAY);
    return ch;
}


/* ---- Board setup -------------------------------------------------------------------------- */

static void board_init(void)
{
    GPIO_InitTypeDef gpio = {0};

    __HAL_RCC_GPIOA_CLK_ENABLE();
    __HAL_RCC_GPIOB_CLK_ENABLE();
    __HAL_RCC_GPIOC_CLK_ENABLE();
    __HAL_RCC_USART2_CLK_ENABLE();

    /* LEDs and antenna switch are plain outputs. */
    gpio.Mode = GPIO_MODE_OUTPUT_PP;
    gpio.Pull = GPIO_NOPULL;
    gpio.Speed = GPIO_SPEED_FREQ_LOW;
    gpio.Pin = LED_BLUE | LED_GREEN;
    HAL_GPIO_Init(LED_PORT, &gpio);
    gpio.Pin = RF_SWITCH_CTRL1 | RF_SWITCH_CTRL2 | RF_SWITCH_CTRL3;
    HAL_GPIO_Init(RF_SWITCH_PORT, &gpio);

    /* Serial port for printf(): 115200 baud, 8 data bits, no parity, 1 stop bit. */
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
}


/* ---- Radio -------------------------------------------------------------------------------- */

/* Send one command with its parameter bytes to the radio. */
static void radio_command(SUBGHZ_RadioSetCmd_t command, uint8_t *params, uint16_t size)
{
    if (HAL_SUBGHZ_ExecSetCmd(&hsubghz, command, params, size) != HAL_OK) {
        printf("Radio command 0x%02X failed\r\n", (unsigned)command);
    }
}

/* Connect the antenna to the receiver (tx = 0) or to the low power amplifier output (tx = 1). */
static void radio_antenna_switch(int tx)
{
    HAL_GPIO_WritePin(RF_SWITCH_PORT, RF_SWITCH_CTRL3, GPIO_PIN_SET);
    HAL_GPIO_WritePin(RF_SWITCH_PORT, RF_SWITCH_CTRL1, GPIO_PIN_SET);
    HAL_GPIO_WritePin(RF_SWITCH_PORT, RF_SWITCH_CTRL2, tx ? GPIO_PIN_SET : GPIO_PIN_RESET);
}

/* Tell the radio how long the payload is (for receiving this is the maximum length). */
static void radio_set_payload_length(uint8_t length)
{
    radio_command(RADIO_SET_PACKETPARAMS, (uint8_t[]){
        PREAMBLE_LENGTH >> 8, PREAMBLE_LENGTH & 0xFF,
        0x00,           /* variable length packets (explicit header) */
        length,
        0x01,           /* CRC on */
        0x00,           /* standard IQ */
    }, 6);
}

/* Sleep until the radio raises an interrupt, returns the interrupt flags. */
static uint16_t radio_wait_irq(void)
{
    uint16_t irq;

    while (radio_irq == 0) {
        /* The CPU sleeps until any interrupt fires, at the latest the next 1 ms SysTick. */
        HAL_PWR_EnterSLEEPMode(PWR_MAINREGULATOR_ON, PWR_SLEEPENTRY_WFI);
    }
    irq = radio_irq;
    radio_irq = 0;
    return irq;
}

static void radio_init(void)
{
    /* The radio sits behind an internal SPI bus and has its own interrupt line. */
    __HAL_RCC_SUBGHZSPI_CLK_ENABLE();
    hsubghz.Init.BaudratePrescaler = SUBGHZSPI_BAUDRATEPRESCALER_4;
    HAL_SUBGHZ_Init(&hsubghz);
    HAL_NVIC_SetPriority(SUBGHZ_Radio_IRQn, 0, 0);
    HAL_NVIC_EnableIRQ(SUBGHZ_Radio_IRQn);

    radio_command(RADIO_SET_STANDBY, (uint8_t[]){0x00}, 1);

    /* The board's 32 MHz TCXO is powered by the radio: 1.7 V, give it 10 ms to start up. Then
       recalibrate everything now that the radio has its proper clock. */
    radio_command(RADIO_SET_TCXOMODE, (uint8_t[]){0x01, 0x00, 0x02, 0x80}, 4);
    HAL_SUBGHZ_WriteRegister(&hsubghz, 0x0911, 0x00); /* no crystal trimming caps with a TCXO */
    radio_command(RADIO_CALIBRATE, (uint8_t[]){0x7F}, 1);
    radio_command(RADIO_CALIBRATEIMAGE, (uint8_t[]){0xD7, 0xDB}, 2); /* for 863-870 MHz */

    radio_command(RADIO_SET_PACKETTYPE, (uint8_t[]){0x01}, 1); /* LoRa */

    /* Frequency is set in steps of 32 MHz / 2^25. */
    uint32_t freq = (uint32_t)(((uint64_t)RF_FREQUENCY << 25) / 32000000);
    radio_command(RADIO_SET_RFFREQUENCY, (uint8_t[]){freq >> 24, freq >> 16, freq >> 8, freq}, 4);

    /* Low power amplifier (up to 14 dBm) with a 200 us ramp. */
    radio_command(RADIO_SET_PACONFIG, (uint8_t[]){0x04, 0x00, 0x01, 0x01}, 4);
    radio_command(RADIO_SET_TXPARAMS, (uint8_t[]){(uint8_t)TX_POWER, 0x04}, 2);

    /* Last byte is the low data rate optimisation, needed for SF11 and SF12 at 125 kHz. */
    radio_command(RADIO_SET_MODULATIONPARAMS, (uint8_t[]){
        SPREADING_FACTOR, BANDWIDTH, CODING_RATE, (SPREADING_FACTOR >= 11 && BANDWIDTH == 0x04),
    }, 4);
    radio_set_payload_length(255);

    radio_command(RADIO_SET_BUFFERBASEADDRESS, (uint8_t[]){0x00, 0x00}, 2);

    /* Which events raise the radio interrupt. */
    radio_command(RADIO_CFG_DIOIRQ, (uint8_t[]){
        IRQ_ALL >> 8, IRQ_ALL & 0xFF, IRQ_ALL >> 8, IRQ_ALL & 0xFF, 0, 0, 0, 0,
    }, 8);
}

/* Transmit a packet and wait until it has been sent. */
static void radio_send(const uint8_t *data, uint8_t length)
{
    radio_set_payload_length(length);
    HAL_SUBGHZ_WriteBuffer(&hsubghz, 0, (uint8_t *)data, length);
    radio_antenna_switch(1);
    radio_command(RADIO_SET_TX, (uint8_t[]){0, 0, 0}, 3); /* no timeout */
    radio_wait_irq();
    HAL_GPIO_TogglePin(LED_PORT, LED_BLUE);
}

/* Listen for a packet for at most timeout_ms. Returns its length, or -1 if nothing (valid) came in. */
static int radio_receive(uint8_t *data, uint8_t max_length, uint32_t timeout_ms)
{
    uint32_t timeout = timeout_ms * 64; /* in steps of 15.625 us */
    uint8_t rx_status[2] = {0, 0};      /* payload length, offset in the radio's buffer */
    uint16_t irq;

    radio_set_payload_length(255);
    radio_antenna_switch(0);
    radio_command(RADIO_SET_RX, (uint8_t[]){timeout >> 16, timeout >> 8, timeout}, 3);
    irq = radio_wait_irq();

    if (!(irq & IRQ_RX_DONE) || (irq & (IRQ_CRC_ERROR | IRQ_HEADER_ERROR))) {
        return -1;
    }
    HAL_SUBGHZ_ExecGetCmd(&hsubghz, RADIO_GET_RXBUFFERSTATUS, rx_status, 2);
    if (rx_status[0] > max_length) {
        rx_status[0] = max_length;
    }
    HAL_SUBGHZ_ReadBuffer(&hsubghz, rx_status[1], data, rx_status[0]);
    HAL_GPIO_TogglePin(LED_PORT, LED_GREEN);
    return rx_status[0];
}


/* ---- Ping pong ---------------------------------------------------------------------------- */

int main(void)
{
    uint8_t rx_data[255];
    uint8_t tx_data[PAYLOAD_LENGTH] = {0};
    int rx_length;

    /* Every chip has a unique ID. We use it as a name in the log and to give each board a
       slightly different timeout, so two boards do not keep sending their PING at the same time. */
    uint32_t id = (HAL_GetUIDw0() ^ HAL_GetUIDw1() ^ HAL_GetUIDw2()) & 0xFFFF;
    uint32_t rx_timeout = RX_TIMEOUT + (id % 16) * 100;

    HAL_Init(); /* We stay on the 4 MHz clock the chip boots with, so no clock setup is needed. */
    board_init();
    radio_init();

    printf("[%04X] Ping pong started\r\n", (unsigned)id);

    while (1) {
        rx_length = radio_receive(rx_data, sizeof(rx_data), rx_timeout);

        if (rx_length == PAYLOAD_LENGTH && memcmp(rx_data, "PING", 4) == 0) {
            printf("[%04X] %8lu ms: received PING, sending PONG\r\n", (unsigned)id, (unsigned long)HAL_GetTick());
            HAL_Delay(REPLY_DELAY);
            memcpy(tx_data, "PONG", 4);
            radio_send(tx_data, PAYLOAD_LENGTH);
        }
        else if (rx_length == PAYLOAD_LENGTH && memcmp(rx_data, "PONG", 4) == 0) {
            printf("[%04X] %8lu ms: received PONG, sending PING\r\n", (unsigned)id, (unsigned long)HAL_GetTick());
            HAL_Delay(REPLY_DELAY);
            memcpy(tx_data, "PING", 4);
            radio_send(tx_data, PAYLOAD_LENGTH);
        }
        else {
            printf("[%04X] %8lu ms: heard nothing, sending PING\r\n", (unsigned)id, (unsigned long)HAL_GetTick());
            memcpy(tx_data, "PING", 4);
            radio_send(tx_data, PAYLOAD_LENGTH);
        }
    }
}
