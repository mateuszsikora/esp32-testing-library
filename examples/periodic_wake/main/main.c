/* A periodic wake: the chip sleeps in deep sleep for 15 minutes at a time. At every boot it
 * counts the wake in RTC memory, pulses GPIO18 high for 20 ms, prints the count and goes back to
 * sleep. A power-on or a reset starts the count again at 0. */
#include <inttypes.h>
#include <stdio.h>

#include "driver/gpio.h"
#include "esp_attr.h"
#include "esp_rom_sys.h"
#include "esp_sleep.h"

#define PULSE GPIO_NUM_18
#define PULSE_US (20 * 1000)
#define SLEEP_US (15ULL * 60 * 1000 * 1000)

/* RTC slow memory keeps it through a deep sleep; the bootloader loads it as 0 at any other boot. */
static RTC_DATA_ATTR uint32_t wakes;

static void pulse(void) {
    ESP_ERROR_CHECK(gpio_reset_pin(PULSE));
    ESP_ERROR_CHECK(gpio_set_level(PULSE, 0));
    ESP_ERROR_CHECK(gpio_set_direction(PULSE, GPIO_MODE_OUTPUT));
    ESP_ERROR_CHECK(gpio_set_level(PULSE, 1));
    esp_rom_delay_us(PULSE_US);
    ESP_ERROR_CHECK(gpio_set_level(PULSE, 0));
}

void app_main(void) {
    if (esp_sleep_get_wakeup_cause() == ESP_SLEEP_WAKEUP_TIMER) {
        wakes++;
    }
    pulse();
    printf("periodic_wake: wakes=%" PRIu32 "\n", wakes);
    esp_sleep_enable_timer_wakeup(SLEEP_US);
    esp_deep_sleep_start();
}
