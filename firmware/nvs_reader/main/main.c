/* Read the test's configuration through ESP-IDF's typed NVS API. Never erase or write NVS. */
#include <inttypes.h>
#include <stdio.h>

#include "esp_err.h"
#include "nvs.h"
#include "nvs_flash.h"

void app_main(void)
{
    ESP_ERROR_CHECK(nvs_flash_init());
    nvs_handle_t config;
    ESP_ERROR_CHECK(nvs_open("config", NVS_READONLY, &config));
    char host[256];
    size_t size = sizeof(host);
    ESP_ERROR_CHECK(nvs_get_str(config, "broker_host", host, &size));
    uint16_t port;
    ESP_ERROR_CHECK(nvs_get_u16(config, "broker_port", &port));
    uint8_t enabled;
    ESP_ERROR_CHECK(nvs_get_u8(config, "enabled", &enabled));
    uint32_t count;
    ESP_ERROR_CHECK(nvs_get_u32(config, "count", &count));
    int32_t delta;
    ESP_ERROR_CHECK(nvs_get_i32(config, "delta", &delta));
    uint8_t blob[256];
    size = sizeof(blob);
    ESP_ERROR_CHECK(nvs_get_blob(config, "broker_ca", blob, &size));
    printf("nvs_reader: host=%s port=%" PRIu16 " enabled=%" PRIu8
           " count=%" PRIu32 " delta=%" PRId32 "\n", host, port, enabled, count, delta);
    printf("nvs_reader: blob=");
    for (size_t i = 0; i < size; ++i) {
        printf("%02x", blob[i]);
    }
    printf("\n");
    nvs_close(config);
}
