/* Changes a UART's line rate while it runs, as a firmware does that tells a modem AT+IPR: UART1
 * on GPIO 26 (TX) and 27 (RX), modem_at's wires, to a part that starts at 9600 baud. NVS keeps
 * the rate the firmware last switched the part to. Every boot tries that rate first, then 9600:
 * after a power loss the part is back at 9600, and the first try hears nothing. A part found at
 * 9600 is told AT+IPR=115200, and the firmware follows it to 115200. Each step is a console
 * line. */
#include <inttypes.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>

#include "driver/uart.h"
#include "esp_err.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs.h"
#include "nvs_flash.h"

#define UART UART_NUM_1
#define DEFAULT_BAUD 9600
#define FAST_BAUD 115200
/* How long a command waits for its OK. */
#define ANSWER_MS 500

/* Writes `text` and its CR LF, and reads until the part's OK or ANSWER_MS have passed. */
static bool command(const char *text) {
    char reply[128] = {0};
    size_t used = 0;
    uart_flush_input(UART);
    uart_write_bytes(UART, text, strlen(text));
    uart_write_bytes(UART, "\r\n", 2);
    const TickType_t deadline = xTaskGetTickCount() + pdMS_TO_TICKS(ANSWER_MS);
    while (used < sizeof(reply) - 1) {
        const TickType_t now = xTaskGetTickCount();
        if ((int32_t)(deadline - now) <= 0) break;
        int count = uart_read_bytes(UART, reply + used, 1, deadline - now);
        if (count <= 0) break;
        used += count;
        reply[used] = 0;
        if (strstr(reply, "\r\nOK\r\n")) return true;
    }
    return false;
}

/* The UART at `baud`, once what it was sending has gone out at the rate before. */
static void set_rate(uint32_t baud) {
    ESP_ERROR_CHECK(uart_wait_tx_done(UART, pdMS_TO_TICKS(100)));
    ESP_ERROR_CHECK(uart_set_baudrate(UART, baud));
    uart_flush_input(UART);
}

static nvs_handle_t open_store(void) {
    ESP_ERROR_CHECK(nvs_flash_init());
    nvs_handle_t handle;
    ESP_ERROR_CHECK(nvs_open("rate_switch", NVS_READWRITE, &handle));
    return handle;
}

static void idle(const char *last) {
    puts(last);
    fflush(stdout);
    for (;;) vTaskDelay(pdMS_TO_TICKS(1000));
}

void app_main(void) {
    nvs_handle_t store = open_store();
    uint32_t stored = 0;
    esp_err_t found_stored = nvs_get_u32(store, "baud", &stored);
    if (found_stored != ESP_ERR_NVS_NOT_FOUND) ESP_ERROR_CHECK(found_stored);

    const uart_config_t config = {
        .baud_rate = DEFAULT_BAUD, .data_bits = UART_DATA_8_BITS, .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1, .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    ESP_ERROR_CHECK(uart_param_config(UART, &config));
    ESP_ERROR_CHECK(uart_set_pin(UART, 26, 27, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE));
    ESP_ERROR_CHECK(uart_driver_install(UART, 1024, 0, 0, NULL, 0));

    uint32_t candidates[2];
    size_t tries = 0;
    if (stored != 0 && stored != DEFAULT_BAUD) candidates[tries++] = stored;
    candidates[tries++] = DEFAULT_BAUD;
    uint32_t found = 0;
    for (size_t i = 0; i < tries && found == 0; ++i) {
        set_rate(candidates[i]);
        printf("rate_switch: trying %" PRIu32 "\n", candidates[i]);
        fflush(stdout);
        if (command("AT")) {
            found = candidates[i];
            printf("rate_switch: found at %" PRIu32 "\n", found);
        } else {
            printf("rate_switch: no answer at %" PRIu32 "\n", candidates[i]);
        }
        fflush(stdout);
    }
    if (found == 0) idle("rate_switch: no part found");
    if (found != FAST_BAUD) {
        if (!command("AT+IPR=115200")) idle("rate_switch: AT+IPR=115200 not answered");
        puts("rate_switch: AT+IPR=115200 ok");
        fflush(stdout);
        set_rate(FAST_BAUD);
        if (!command("AT")) idle("rate_switch: no answer at 115200");
        printf("rate_switch: %d ok\n", FAST_BAUD);
        fflush(stdout);
        ESP_ERROR_CHECK(nvs_set_u32(store, "baud", FAST_BAUD));
        ESP_ERROR_CHECK(nvs_commit(store));
    }
    nvs_close(store);
    idle("rate_switch: done");
}
