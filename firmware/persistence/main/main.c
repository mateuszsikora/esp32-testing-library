/* What a boot finds of the boots before it: a count in NVS, in RTC memory and in RAM that no
 * reset initialises, a line per boot on the SD card when there is one, and the reset reason.
 * On its first boot, as NVS counts them, it restarts itself once with esp_restart. A string the
 * test wrote beside the count, `note`, it prints when there is one. */
#include <inttypes.h>
#include <stdio.h>

#include "driver/sdspi_host.h"
#include "driver/spi_common.h"
#include "esp_attr.h"
#include "esp_err.h"
#include "esp_rom_sys.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_vfs_fat.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "sdmmc_cmd.h"

#define MOUNT "/sdcard"
#define BOOTS MOUNT "/BOOTS.TXT"
/* Marks a count as one a boot before wrote: what a power-on leaves there is not. */
#define MAGIC 0x5e5510f1u

RTC_NOINIT_ATTR static uint32_t rtc_magic;
RTC_NOINIT_ATTR static uint32_t rtc_count;
__NOINIT_ATTR static uint32_t ram_magic;
__NOINIT_ATTR static uint32_t ram_count;

static uint32_t next(uint32_t *magic, uint32_t *count) {
    if (*magic != MAGIC) {
        *magic = MAGIC;
        *count = 0;
    }
    return ++*count;
}

/* The boots counted in NVS, this one included. */
static uint32_t count_boot(void) {
    ESP_ERROR_CHECK(nvs_flash_init());
    nvs_handle_t handle;
    ESP_ERROR_CHECK(nvs_open("persistence", NVS_READWRITE, &handle));
    uint32_t boots = 0;
    esp_err_t found = nvs_get_u32(handle, "boots", &boots);
    if (found != ESP_ERR_NVS_NOT_FOUND) ESP_ERROR_CHECK(found);
    boots++;
    ESP_ERROR_CHECK(nvs_set_u32(handle, "boots", boots));
    ESP_ERROR_CHECK(nvs_commit(handle));
    nvs_close(handle);
    return boots;
}

/* The string under `note` in the count's namespace, as a line, when there is one. */
static void print_note(void) {
    nvs_handle_t handle;
    ESP_ERROR_CHECK(nvs_open("persistence", NVS_READONLY, &handle));
    char note[64];
    size_t size = sizeof(note);
    esp_err_t found = nvs_get_str(handle, "note", note, &size);
    nvs_close(handle);
    if (found == ESP_ERR_NVS_NOT_FOUND) return;
    ESP_ERROR_CHECK(found);
    printf("persistence: note=%s\n", note);
}

/* The lines of BOOTS.TXT once this boot's is appended; -1 without a card. The pins are those of
 * the esp32-devkitc profile's slot. */
static int log_boot(uint32_t boots) {
    sdmmc_host_t host = SDSPI_HOST_DEFAULT();
    const spi_bus_config_t bus = {
        .mosi_io_num = 15,
        .miso_io_num = 2,
        .sclk_io_num = 14,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = 4000,
    };
    if (spi_bus_initialize(host.slot, &bus, SDSPI_DEFAULT_DMA) != ESP_OK) return -1;
    sdspi_device_config_t slot = SDSPI_DEVICE_CONFIG_DEFAULT();
    slot.gpio_cs = 13;
    slot.host_id = host.slot;
    const esp_vfs_fat_sdmmc_mount_config_t config = {
        .format_if_mount_failed = false,
        .max_files = 1,
        .allocation_unit_size = 16 * 1024,
    };
    sdmmc_card_t *card;
    if (esp_vfs_fat_sdspi_mount(MOUNT, &host, &slot, &config, &card) != ESP_OK) {
        spi_bus_free(host.slot);
        return -1;
    }
    int lines = -1;
    FILE *file = fopen(BOOTS, "a");
    if (file != NULL) {
        fprintf(file, "boot %" PRIu32 "\n", boots);
        fclose(file);
        file = fopen(BOOTS, "r");
    }
    if (file != NULL) {
        lines = 0;
        for (int c; (c = fgetc(file)) != EOF;) lines += c == '\n';
        fclose(file);
    }
    esp_vfs_fat_sdcard_unmount(MOUNT, card);
    spi_bus_free(host.slot);
    return lines;
}

void app_main(void) {
    uint32_t boots = count_boot();
    uint32_t rtc = next(&rtc_magic, &rtc_count);
    uint32_t ram = next(&ram_magic, &ram_count);
    int sd = log_boot(boots);
    printf("persistence: boot=%" PRIu32 " reason=%d rom=%d rtc=%" PRIu32 " ram=%" PRIu32
           " sd=%d uptime_ms=%" PRId64 "\n",
           boots, esp_reset_reason(), (int)esp_rom_get_reset_reason(0), rtc, ram, sd,
           esp_timer_get_time() / 1000);
    print_note();
    if (boots == 1) {
        printf("persistence: restarting\n");
        fflush(stdout);
        esp_restart();
    }
    printf("persistence: done\n");
}
