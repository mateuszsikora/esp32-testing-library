/* A position alarm: NVS supplies the destination, NMEA the current position.
 * GPIO18 drives an active buzzer (through a suitable driver on real hardware).
 * No valid fix, a stale fix or missing configuration always leaves it off. */
#include <math.h>
#include <stdio.h>
#include <string.h>

#include "driver/gpio.h"
#include "driver/uart.h"
#include "esp_timer.h"
#include "nmea.h"
#include "nvs.h"
#include "nvs_flash.h"

#define BUZZER GPIO_NUM_18
#define GNSS UART_NUM_1
#define FIX_MAX_AGE_US 2500000

static bool destination(int32_t *lat, int32_t *lon, uint32_t *radius) {
    nvs_handle_t handle;
    if (nvs_flash_init() != ESP_OK || nvs_open("alarm", NVS_READONLY, &handle) != ESP_OK) {
        return false;
    }
    bool ok = nvs_get_i32(handle, "lat_e6", lat) == ESP_OK &&
              nvs_get_i32(handle, "lon_e6", lon) == ESP_OK &&
              nvs_get_u32(handle, "radius_m", radius) == ESP_OK;
    nvs_close(handle);
    return ok && *lat >= -90000000 && *lat <= 90000000 &&
           *lon >= -180000000 && *lon <= 180000000 && *radius > 0;
}

static double distance_m(const nmea_fix_t *fix, int32_t lat, int32_t lon) {
    const double radians = 3.14159265358979323846 / 180000000.0;
    double a = sin((fix->lat_e6 - (double)lat) * radians / 2);
    double b = sin((fix->lon_e6 - (double)lon) * radians / 2);
    double h = a * a + cos(lat * radians) * cos(fix->lat_e6 * radians) * b * b;
    /* Roundoff at antipodes must not take sqrt outside its domain. */
    h = fmin(1.0, fmax(0.0, h));
    return 6371000.0 * 2 * atan2(sqrt(h), sqrt(1 - h));
}

void app_main(void) {
    ESP_ERROR_CHECK(gpio_reset_pin(BUZZER));
    ESP_ERROR_CHECK(gpio_set_level(BUZZER, 0));
    ESP_ERROR_CHECK(gpio_set_direction(BUZZER, GPIO_MODE_OUTPUT));
    int32_t lat, lon;
    uint32_t radius;
    if (!destination(&lat, &lon, &radius)) {
        printf("gps_alarm_clock: invalid or missing NVS alarm configuration\n");
        return;
    }
    const uart_config_t config = {
        .baud_rate = 9600,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    ESP_ERROR_CHECK(uart_driver_install(GNSS, 1024, 0, 0, NULL, 0));
    ESP_ERROR_CHECK(uart_param_config(GNSS, &config));
    ESP_ERROR_CHECK(uart_set_pin(GNSS, 4, 5, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE));
    printf("gps_alarm_clock: ready\n");

    char sentence[NMEA_SENTENCE_MAX - 1];
    size_t length = 0;
    bool reading = false;
    int64_t last_fix = -FIX_MAX_AGE_US;
    for (;;) {
        uint8_t byte;
        int count = uart_read_bytes(GNSS, &byte, 1, pdMS_TO_TICKS(100));
        int64_t now = esp_timer_get_time();
        if (now - last_fix >= FIX_MAX_AGE_US) ESP_ERROR_CHECK(gpio_set_level(BUZZER, 0));
        if (count != 1) continue;
        if (byte == '$') {
            sentence[0] = '$';
            length = 1;
            reading = true;
        } else if (byte == '\r' || byte == '\n') {
            if (!reading) continue;
            reading = false;
            sentence[length] = '\0';
            /* Ignore other NMEA sentence types, e.g. GSV satellite reports. */
            if (length < 7 || sentence[6] != ',' ||
                (strncmp(sentence + 3, "GGA", 3) && strncmp(sentence + 3, "RMC", 3))) {
                continue;
            }
            nmea_fix_t fix;
            bool valid = nmea_parse_fix(sentence, &fix);
            if (valid) last_fix = now;
            bool inside = valid && distance_m(&fix, lat, lon) <= radius;
            ESP_ERROR_CHECK(gpio_set_level(BUZZER, inside));
        } else if (reading) {
            if (length < sizeof sentence - 1) sentence[length++] = (char)byte;
            else reading = false;
        }
    }
}
