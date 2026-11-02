/* Append dated GNSS fixes to an SPI SD card. GPIO18 stays high when storage is
 * unavailable; the receiver is still serviced, even with an empty card slot. */
#include <stdio.h>
#include <string.h>

#include "driver/gpio.h"
#include "driver/sdspi_host.h"
#include "driver/spi_common.h"
#include "driver/uart.h"
#include "esp_vfs_fat.h"
#include "nmea.h"
#include "sdmmc_cmd.h"

#define ERROR_PIN GPIO_NUM_18
#define GNSS UART_NUM_1
#define MOUNT "/sdcard"

static bool mount_card(void) {
    sdmmc_host_t host = SDSPI_HOST_DEFAULT();
    const spi_bus_config_t bus = {
        .mosi_io_num = 15,
        .miso_io_num = 2,
        .sclk_io_num = 14,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = 4000,
    };
    esp_err_t result = spi_bus_initialize(host.slot, &bus, SDSPI_DEFAULT_DMA);
    if (result == ESP_OK) {
        sdspi_device_config_t slot = SDSPI_DEVICE_CONFIG_DEFAULT();
        slot.gpio_cs = 13;
        slot.host_id = host.slot;
        const esp_vfs_fat_sdmmc_mount_config_t config = {
            .format_if_mount_failed = false,
            .max_files = 1,
            .allocation_unit_size = 16 * 1024,
        };
        sdmmc_card_t *card;
        result = esp_vfs_fat_sdspi_mount(MOUNT, &host, &slot, &config, &card);
        if (result != ESP_OK) spi_bus_free(host.slot);
    }
    if (result != ESP_OK) {
        printf("sd_data_logger: SD card unavailable (%s)\n", esp_err_to_name(result));
        return false;
    }
    return true;
}

/* RMC supplies both the date and position. GGA has no date and is ignored, so
 * a receiver's RMC/GGA pair produces one row. Years 00..99 mean 2000..2099. */
static bool dated_fix(const char *sentence, nmea_fix_t *fix, char date[9]) {
    if (strlen(sentence) < 7 || strncmp(sentence + 3, "RMC,", 4) ||
        !nmea_parse_fix(sentence, fix)) return false;

    const char *field = sentence;
    for (int i = 0; i < 9; i++) {
        field = strchr(field, ',');
        if (field == NULL) return false;
        field++;
    }
    if (strcspn(field, ",*") != 6) return false;
    for (int i = 0; i < 6; i++) {
        if (field[i] < '0' || field[i] > '9') return false;
    }
    int day = (field[0] - '0') * 10 + field[1] - '0';
    int month = (field[2] - '0') * 10 + field[3] - '0';
    int year = 2000 + (field[4] - '0') * 10 + field[5] - '0';
    const int days[] = {31, 28 + (year % 4 == 0), 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
    if (month < 1 || month > 12 || day < 1 || day > days[month - 1]) return false;

    const char *time = fix->time;
    if ((time[0] - '0') * 10 + time[1] - '0' > 23 ||
        (time[2] - '0') * 10 + time[3] - '0' > 59 ||
        (time[4] - '0') * 10 + time[5] - '0' > 59) return false;
    if (time[6] != '\0') {
        if (time[6] != '.' || time[7] == '\0') return false;
        for (const char *p = time + 7; *p; p++) {
            if (*p < '0' || *p > '9') return false;
        }
    }
    /* Only checked digits enter the 8.3 file name. */
    snprintf(date, 9, "20%c%c%c%c%c%c", field[4], field[5], field[2], field[3], field[0], field[1]);
    return true;
}

static bool append_fix(const char *date, const nmea_fix_t *fix) {
    char path[32];
    snprintf(path, sizeof path, MOUNT "/%s.CSV", date);
    FILE *file = fopen(path, "a");
    if (file == NULL) return false;
    bool ok = fprintf(file, "%s,%.6f,%.6f\n", fix->time,
                      fix->lat_e6 / 1000000.0, fix->lon_e6 / 1000000.0) > 0;
    /* Close each row: flush stdio and sync the FAT file before announcing it. */
    if (fclose(file) != 0) ok = false;
    return ok;
}

void app_main(void) {
    ESP_ERROR_CHECK(gpio_reset_pin(ERROR_PIN));
    ESP_ERROR_CHECK(gpio_set_level(ERROR_PIN, 1));
    ESP_ERROR_CHECK(gpio_set_direction(ERROR_PIN, GPIO_MODE_OUTPUT));
    const uart_config_t config = {
        .baud_rate = 9600,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    ESP_ERROR_CHECK(uart_driver_install(GNSS, 2048, 0, 0, NULL, 0));
    ESP_ERROR_CHECK(uart_param_config(GNSS, &config));
    ESP_ERROR_CHECK(uart_set_pin(GNSS, 4, 5, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE));
    bool available = mount_card();
    ESP_ERROR_CHECK(gpio_set_level(ERROR_PIN, !available));
    printf("sd_data_logger: ready\n");

    char sentence[NMEA_SENTENCE_MAX - 1];
    size_t length = 0;
    bool reading = false;
    for (;;) {
        uint8_t byte;
        if (uart_read_bytes(GNSS, &byte, 1, pdMS_TO_TICKS(100)) != 1) continue;
        if (byte == '$') {
            sentence[0] = '$';
            length = 1;
            reading = true;
        } else if (byte == '\r' || byte == '\n') {
            if (!reading) continue;
            reading = false;
            sentence[length] = '\0';
            nmea_fix_t fix;
            char date[9];
            if (!dated_fix(sentence, &fix, date)) continue;
            if (available && !append_fix(date, &fix)) {
                available = false;
                ESP_ERROR_CHECK(gpio_set_level(ERROR_PIN, 1));
                printf("sd_data_logger: SD card write failed\n");
            }
            printf("sd_data_logger: fix %s %s (%s)\n", date, fix.time,
                   available ? "saved" : "card unavailable");
        } else if (reading) {
            if (length < sizeof sentence - 1) sentence[length++] = (char)byte;
            else reading = false;
        }
    }
}
