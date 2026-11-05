/* An OTA update and a rollback, with no network: the app copies itself to the next slot, sets the
 * boot partition and restarts. Every boot reports what it runs, what the OTA data selects and the
 * state of its image, then stands still for STEP_MS, for a test to read the flash at that state.
 *
 *   factory:                copy to ota_0, restart
 *   ota_0, pending_verify:  mark valid, copy to ota_1, restart
 *   ota_1, pending_verify:  mark invalid and roll back, which restarts into ota_0
 *   ota_0, valid:           done
 */
#include <stdio.h>
#include <stdlib.h>

#include "esp_err.h"
#include "esp_image_format.h"
#include "esp_ota_ops.h"
#include "esp_partition.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#define STEP_MS 200
#define COPY_CHUNK 4096

static const char *state_name(esp_ota_img_states_t state) {
    switch (state) {
    case ESP_OTA_IMG_NEW: return "new";
    case ESP_OTA_IMG_PENDING_VERIFY: return "pending_verify";
    case ESP_OTA_IMG_VALID: return "valid";
    case ESP_OTA_IMG_INVALID: return "invalid";
    case ESP_OTA_IMG_ABORTED: return "aborted";
    case ESP_OTA_IMG_UNDEFINED: return "undefined";
    }
    return "unknown";
}

/* The state of the image in `partition`; the factory app has none. */
static const char *state_of(const esp_partition_t *partition) {
    esp_ota_img_states_t state;
    if (esp_ota_get_state_partition(partition, &state) != ESP_OK) return "none";
    return state_name(state);
}

static void report(const char *step) {
    const esp_partition_t *running = esp_ota_get_running_partition();
    printf("ota_state: %s running=%s boot=%s state=%s\n", step, running->label,
           esp_ota_get_boot_partition()->label, state_of(running));
    fflush(stdout);
    vTaskDelay(pdMS_TO_TICKS(STEP_MS));
}

/* Copies the running app's image to `target`, and selects it for the next boot. */
static void copy_app_to(const esp_partition_t *target) {
    const esp_partition_t *running = esp_ota_get_running_partition();
    const esp_partition_pos_t position = {.offset = running->address, .size = running->size};
    esp_image_metadata_t image;
    ESP_ERROR_CHECK(esp_image_verify(ESP_IMAGE_VERIFY_SILENT, &position, &image));
    esp_ota_handle_t handle;
    ESP_ERROR_CHECK(esp_ota_begin(target, image.image_len, &handle));
    char *chunk = malloc(COPY_CHUNK);
    ESP_ERROR_CHECK(chunk == NULL ? ESP_ERR_NO_MEM : ESP_OK);
    for (size_t at = 0; at < image.image_len; at += COPY_CHUNK) {
        size_t size = image.image_len - at < COPY_CHUNK ? image.image_len - at : COPY_CHUNK;
        ESP_ERROR_CHECK(esp_partition_read(running, at, chunk, size));
        ESP_ERROR_CHECK(esp_ota_write(handle, chunk, size));
    }
    free(chunk);
    ESP_ERROR_CHECK(esp_ota_end(handle));
    ESP_ERROR_CHECK(esp_ota_set_boot_partition(target));
}

void app_main(void) {
    const esp_partition_t *running = esp_ota_get_running_partition();
    esp_ota_img_states_t state = ESP_OTA_IMG_UNDEFINED;
    esp_ota_get_state_partition(running, &state);
    report("booted");
    if (running->subtype == ESP_PARTITION_SUBTYPE_APP_FACTORY) {
        copy_app_to(esp_ota_get_next_update_partition(NULL));
        report("updated");
        esp_restart();
    } else if (state != ESP_OTA_IMG_PENDING_VERIFY) {
        report("done");
    } else if (running->subtype == ESP_PARTITION_SUBTYPE_APP_OTA_0) {
        ESP_ERROR_CHECK(esp_ota_mark_app_valid_cancel_rollback());
        report("marked_valid");
        copy_app_to(esp_ota_get_next_update_partition(NULL));
        report("updated");
        esp_restart();
    } else {
        printf("ota_state: rolling_back running=%s\n", running->label);
        fflush(stdout);
        ESP_ERROR_CHECK(esp_ota_mark_app_invalid_rollback_and_reboot());
    }
}
