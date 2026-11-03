/* sd_remount: keeps appending to a file on an SPI SD card, and mounts the card again after a
 * failure, a validation image of esp32tl (#106) for a card taken out and put back while the
 * firmware runs. The card is on the esp32-devkitc profile's slot (SCLK 14, MOSI 15, MISO 2,
 * CS 13), the pins of ESP-IDF's sdspi example. Every 100 ms, with the card mounted, it appends
 * the next line number to LOG.TXT and closes the file, and only then prints
 *
 *   sd_remount: wrote N                the line N is on the card
 *
 * The first failure to open, write or close the file prints
 *
 *   sd_remount: write failed (errno)   the line is not counted, and is written again later
 *
 * and unmounts the card. Without a mounted card it tries to mount one every 200 ms, printing
 *
 *   sd_remount: mounted K              the K-th successful mount since boot
 *   sd_remount: no card (error)        once per run of failed mounts
 *
 * The SPI bus stays initialised throughout; a mount adds the card's device to it and an unmount
 * takes it off. It never formats a card. */
#include <errno.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>

#include "driver/sdspi_host.h"
#include "driver/spi_common.h"
#include "esp_vfs_fat.h"
#include "freertos/FreeRTOS.h"
#include "sdmmc_cmd.h"

#define MOUNT "/sdcard"
#define LOG MOUNT "/LOG.TXT"

static sdmmc_host_t host = SDSPI_HOST_DEFAULT();
static sdmmc_card_t *card;
/* The error of the last failed mount, ESP_OK after a mount. */
static esp_err_t failed = ESP_OK;

static bool mount_card(void) {
    sdspi_device_config_t slot = SDSPI_DEVICE_CONFIG_DEFAULT();
    slot.gpio_cs = 13;
    slot.host_id = host.slot;
    const esp_vfs_fat_sdmmc_mount_config_t config = {
        .format_if_mount_failed = false,
        .max_files = 1,
        .allocation_unit_size = 16 * 1024,
    };
    esp_err_t result = esp_vfs_fat_sdspi_mount(MOUNT, &host, &slot, &config, &card);
    if (result != ESP_OK) {
        card = NULL;
        if (result != failed) printf("sd_remount: no card (%s)\n", esp_err_to_name(result));
        failed = result;
        return false;
    }
    failed = ESP_OK;
    return true;
}

static bool append(unsigned line) {
    FILE *file = fopen(LOG, "a");
    if (file == NULL) return false;
    bool ok = fprintf(file, "%u\n", line) > 0;
    /* Close each line: flush stdio and sync the FAT file before announcing it. */
    if (fclose(file) != 0) ok = false;
    return ok;
}

void app_main(void) {
    const spi_bus_config_t bus = {
        .mosi_io_num = 15,
        .miso_io_num = 2,
        .sclk_io_num = 14,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = 4000,
    };
    ESP_ERROR_CHECK(spi_bus_initialize(host.slot, &bus, SDSPI_DEFAULT_DMA));
    unsigned mounts = 0, line = 1;
    for (;;) {
        if (card == NULL) {
            if (mount_card()) printf("sd_remount: mounted %u\n", ++mounts);
            else {
                vTaskDelay(pdMS_TO_TICKS(200));
                continue;
            }
        }
        errno = 0;
        if (append(line)) {
            printf("sd_remount: wrote %u\n", line++);
        } else {
            printf("sd_remount: write failed (%s)\n", strerror(errno));
            esp_vfs_fat_sdcard_unmount(MOUNT, card);
            card = NULL;
        }
        vTaskDelay(pdMS_TO_TICKS(100));
    }
}
