/* gpio_echo: reads one GPIO line and drives another to its level, a validation image of esp32tl
 * (plan M3). The lines come from sdkconfig (Kconfig menu "gpio_echo"); the defaults are the
 * lilygo-t-a7670g-r2 profile's RI (GPIO 33, input) and PWRKEY (GPIO 4, output). It prints
 *
 *   gpio_echo: in GPIO33 out GPIO4     once, at boot
 *   in=0                               the input's level at boot, then at every change
 *
 * and drives the output to the level it printed before it prints it. The input is read with the
 * chip's own pulls off, so an undriven line reads what the board's wiring gives it. A change
 * raises an interrupt on either edge; the level is read again in a task, so a change that comes
 * back before the task runs prints nothing. */
#include <stdio.h>

#include "driver/gpio.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "sdkconfig.h"

#define IN CONFIG_GPIO_ECHO_IN_PIN
#define OUT CONFIG_GPIO_ECHO_OUT_PIN

static QueueHandle_t edges;

static void IRAM_ATTR on_edge(void *arg) {
    BaseType_t woken = pdFALSE;
    uint8_t edge = 1;
    xQueueSendFromISR(edges, &edge, &woken);
    if (woken) portYIELD_FROM_ISR();
}

static void echo(int level) {
    ESP_ERROR_CHECK(gpio_set_level(OUT, level));
    printf("in=%d\n", level);
    fflush(stdout);
}

void app_main(void) {
    edges = xQueueCreate(16, sizeof(uint8_t));
    const gpio_config_t out = {
        .pin_bit_mask = 1ULL << OUT,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&out));
    const gpio_config_t in = {
        .pin_bit_mask = 1ULL << IN,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_ANYEDGE,
    };
    ESP_ERROR_CHECK(gpio_config(&in));
    ESP_ERROR_CHECK(gpio_install_isr_service(0));
    ESP_ERROR_CHECK(gpio_isr_handler_add(IN, on_edge, NULL));
    printf("gpio_echo: in GPIO%d out GPIO%d\n", IN, OUT);

    /* After the interrupt is on: a change from here on is seen, by this read or the next. */
    int level = gpio_get_level(IN);
    echo(level);
    for (;;) {
        uint8_t edge;
        xQueueReceive(edges, &edge, portMAX_DELAY);
        int now = gpio_get_level(IN);
        if (now == level) continue;
        level = now;
        echo(level);
    }
}
