/* ESP-IDF's bt_spp_initiator after an OTA update, with the Bluetooth controller started in every
 * boot. The boot's reset reason and the slot it runs from say what it does:
 *
 *   power-on, ota_0:       copy the app to ota_1, select it, start the controller, restart
 *   software reset, ota_1: start the controller, stop it, deep sleep for SLEEP_US
 *   deep-sleep wake, ota_1: the example's app_main, which finds the peer and streams to it over SPP
 *
 * Built as bt_spp_ota_v2 (CONFIG_BT_SPP_OTA_UPDATE), the first boot copies the image in the update
 * partition, bt_spp_ota's, into ota_1 rather than its own, and prints "bt_spp_ota: v2" first: the
 * boots from ota_1 then run bt_spp_ota, a build of another ELF than this one.
 *
 * Every boot first prints "bt_spp_ota: booted reason=<reason> running=<label>", and each start of
 * the controller "bt_spp_ota: controller enabled running=<label>". */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_bt.h"
#include "esp_err.h"
#include "esp_image_format.h"
#include "esp_ota_ops.h"
#include "esp_partition.h"
#include "esp_sleep.h"
#include "esp_system.h"
#include "nvs_flash.h"

#define COPY_CHUNK 4096
#define SLEEP_US (100 * 1000)

void spp_initiator_main(void);

static const char *reason_name(esp_reset_reason_t reason) {
    switch (reason) {
    case ESP_RST_POWERON: return "poweron";
    case ESP_RST_SW: return "sw";
    case ESP_RST_DEEPSLEEP: return "deepsleep";
    default: return "other";
    }
}

/* Starts the controller in BR/EDR mode, as the example does, then stops and frees it, so that the
 * boot can go on to restart or sleep. */
static void start_and_stop_controller(const char *running) {
    ESP_ERROR_CHECK(esp_bt_controller_mem_release(ESP_BT_MODE_BLE));
    esp_bt_controller_config_t config = BT_CONTROLLER_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_bt_controller_init(&config));
    ESP_ERROR_CHECK(esp_bt_controller_enable(ESP_BT_MODE_CLASSIC_BT));
    printf("bt_spp_ota: controller enabled running=%s\n", running);
    fflush(stdout);
    ESP_ERROR_CHECK(esp_bt_controller_disable());
    ESP_ERROR_CHECK(esp_bt_controller_deinit());
}

/* Copies the image in `source` to `target`, and selects it for the next boot. */
static void copy_app_to(const esp_partition_t *source, const esp_partition_t *target) {
    const esp_partition_pos_t position = {.offset = source->address, .size = source->size};
    esp_image_metadata_t image;
    ESP_ERROR_CHECK(esp_image_verify(ESP_IMAGE_VERIFY_SILENT, &position, &image));
    esp_ota_handle_t handle;
    ESP_ERROR_CHECK(esp_ota_begin(target, image.image_len, &handle));
    char *chunk = malloc(COPY_CHUNK);
    ESP_ERROR_CHECK(chunk == NULL ? ESP_ERR_NO_MEM : ESP_OK);
    for (size_t at = 0; at < image.image_len; at += COPY_CHUNK) {
        size_t size = image.image_len - at < COPY_CHUNK ? image.image_len - at : COPY_CHUNK;
        ESP_ERROR_CHECK(esp_partition_read(source, at, chunk, size));
        ESP_ERROR_CHECK(esp_ota_write(handle, chunk, size));
    }
    free(chunk);
    ESP_ERROR_CHECK(esp_ota_end(handle));
    ESP_ERROR_CHECK(esp_ota_set_boot_partition(target));
}

void app_main(void) {
    const esp_partition_t *running = esp_ota_get_running_partition();
    esp_reset_reason_t reason = esp_reset_reason();
#if CONFIG_BT_SPP_OTA_UPDATE
    printf("bt_spp_ota: v2\n");
#endif
    printf("bt_spp_ota: booted reason=%s running=%s\n", reason_name(reason), running->label);
    fflush(stdout);
    if (reason == ESP_RST_DEEPSLEEP) {
        /* the example initialises NVS itself */
        spp_initiator_main();
        return;
    }
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);
    if (running->subtype == ESP_PARTITION_SUBTYPE_APP_OTA_0) {
#if CONFIG_BT_SPP_OTA_UPDATE
        const esp_partition_t *source =
            esp_partition_find_first(ESP_PARTITION_TYPE_DATA, 0x40, "update");
        ESP_ERROR_CHECK(source == NULL ? ESP_ERR_NOT_FOUND : ESP_OK);
#else
        const esp_partition_t *source = running;
#endif
        copy_app_to(source, esp_ota_get_next_update_partition(NULL));
        printf("bt_spp_ota: updated boot=%s\n", esp_ota_get_boot_partition()->label);
        fflush(stdout);
        start_and_stop_controller(running->label);
        esp_restart();
    }
    start_and_stop_controller(running->label);
    esp_deep_sleep(SLEEP_US);
}
