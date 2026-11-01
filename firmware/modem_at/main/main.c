/* AT dialogue on the LilyGO T-A7670G R2 wires, without PPP or network dependencies. */
#include <stdio.h>
#include <string.h>

#include "driver/gpio.h"
#include "driver/uart.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#define UART UART_NUM_1
#define PWRKEY GPIO_NUM_4
#define RI GPIO_NUM_33

static void pulse(int ms) {
    ESP_ERROR_CHECK(gpio_set_level(PWRKEY, 1));
    vTaskDelay(pdMS_TO_TICKS(ms));
    ESP_ERROR_CHECK(gpio_set_level(PWRKEY, 0));
    vTaskDelay(pdMS_TO_TICKS(20));
}

static void command(const char *text) {
    char reply[256] = {0};
    size_t used = 0;
    uart_write_bytes(UART, text, strlen(text));
    uart_write_bytes(UART, "\r\n", 2);
    while (used < sizeof(reply) - 1) {
        int count = uart_read_bytes(UART, reply + used, 1,
                                    pdMS_TO_TICKS(1000));
        if (count <= 0) break;
        used += count;
        reply[used] = 0;
        if (strstr(reply, "\r\nOK\r\n") || strstr(reply, "\r\nCONNECT\r\n") ||
            strstr(reply, "\r\nNO CARRIER\r\n") || strstr(reply, "\r\nERROR\r\n")) break;
    }
    printf("modem_at: %s => %s\n", text, used ? reply : "TIMEOUT");
    fflush(stdout);
}

void app_main(void) {
    const uart_config_t config = {
        .baud_rate = 115200, .data_bits = UART_DATA_8_BITS, .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1, .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    ESP_ERROR_CHECK(uart_param_config(UART, &config));
    ESP_ERROR_CHECK(uart_set_pin(UART, 26, 27, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE));
    ESP_ERROR_CHECK(uart_driver_install(UART, 2048, 0, 0, NULL, 0));
    const gpio_config_t output = {
        .pin_bit_mask = 1ULL << PWRKEY, .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE, .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    const gpio_config_t input = {
        .pin_bit_mask = 1ULL << RI, .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE, .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&output));
    ESP_ERROR_CHECK(gpio_set_level(PWRKEY, 0));
    ESP_ERROR_CHECK(gpio_config(&input));
    if (gpio_get_level(RI)) {
        pulse(3000);
        printf("modem_at: powered off RI=%d\n", gpio_get_level(RI));
        /* SIMCom Toff-on: at least 2 s between power-off and power-on. */
        vTaskDelay(pdMS_TO_TICKS(2000));
    }
    pulse(100);
    printf("modem_at: powered on RI=%d\n", gpio_get_level(RI));
    /* A command-mode escape must not leave an OK for the next command to consume. */
    uart_write_bytes(UART, "+++", 3);
    vTaskDelay(pdMS_TO_TICKS(20));
    static const char *dialogue[] = {
        "AT", "ATE0", "AT+CPIN?", "AT+CREG?", "AT+CGREG?", "AT+CEREG?", "AT+CSQ",
        "AT+CGDCONT=1,\"IP\",\"internet\"", "AT+CFUN=1", "AT+CTZU=1", "AT+COPS?",
        "AT+UNIMPLEMENTED", "ATD*99#", "+++", "AT+CGDATA=\"PPP\",1", "+++",
    };
    for (unsigned i = 0; i < sizeof(dialogue) / sizeof(dialogue[0]); ++i) command(dialogue[i]);
    puts("modem_at: dialogue complete");
    fflush(stdout);
    for (;;) {
        printf("modem_at: RI=%d\n", gpio_get_level(RI));
        command("AT+CSQ");
        command("AT+CEREG?");
        vTaskDelay(pdMS_TO_TICKS(250));
    }
}
