/* nmea_reader: reads NMEA 0183 from a GNSS receiver on a UART and prints its position fixes on
 * the console, a validation image of esp32tl (plan M1). The UART comes from sdkconfig (Kconfig
 * menu "nmea_reader"); the defaults are the esp32-devkitc profile's GNSS route. It prints
 *
 *   fix acquired lat=52.229677 lon=21.012233     the first fix
 *   fix lat=52.229657 lon=21.012260              each later one
 *
 * in decimal degrees with six decimals, south and west negative. A fix is one epoch: a GGA and
 * an RMC with the same UTC time print one line. It sends nothing to the receiver. */
#include <stdio.h>
#include <string.h>

#include "driver/uart.h"
#include "nmea.h"
#include "sdkconfig.h"

#define PORT CONFIG_NMEA_READER_UART_PORT

/* Microdegrees as decimal degrees, "-34.603700". */
static void format_degrees(char *out, size_t size, int32_t e6) {
    uint32_t magnitude = e6 < 0 ? (uint32_t)-e6 : (uint32_t)e6;
    snprintf(out, size, "%s%lu.%06lu", e6 < 0 ? "-" : "", (unsigned long)(magnitude / 1000000),
             (unsigned long)(magnitude % 1000000));
}

static void print_fix(const char *what, const nmea_fix_t *fix) {
    char lat[16], lon[16];
    format_degrees(lat, sizeof lat, fix->lat_e6);
    format_degrees(lon, sizeof lon, fix->lon_e6);
    printf("%s lat=%s lon=%s\n", what, lat, lon);
    fflush(stdout);
}

void app_main(void) {
    const uart_config_t config = {
        .baud_rate = CONFIG_NMEA_READER_UART_BAUD,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    /* The receive buffer has to be larger than the 128-byte hardware FIFO; no transmit buffer. */
    ESP_ERROR_CHECK(uart_driver_install(PORT, 1024, 0, 0, NULL, 0));
    ESP_ERROR_CHECK(uart_param_config(PORT, &config));
    ESP_ERROR_CHECK(uart_set_pin(PORT, CONFIG_NMEA_READER_UART_TX_PIN,
                                 CONFIG_NMEA_READER_UART_RX_PIN, UART_PIN_NO_CHANGE,
                                 UART_PIN_NO_CHANGE));
    printf("nmea_reader: UART%d RX GPIO%d TX GPIO%d %d baud\n", PORT,
           CONFIG_NMEA_READER_UART_RX_PIN, CONFIG_NMEA_READER_UART_TX_PIN,
           CONFIG_NMEA_READER_UART_BAUD);
    fflush(stdout);

    /* A sentence runs from '$' to the line ending; a '$' starts over, and a sentence longer
     * than NMEA allows is dropped. The buffer holds its 80 characters, the line ending left
     * out, and the NUL. */
    char sentence[NMEA_SENTENCE_MAX - 1];
    size_t length = 0;
    bool in_sentence = false;
    nmea_fix_t fix;
    bool fixed = false;
    char last_time[sizeof fix.time] = "";
    for (;;) {
        uint8_t byte;
        if (uart_read_bytes(PORT, &byte, 1, portMAX_DELAY) != 1) continue;
        if (byte == '$') {
            sentence[0] = '$';
            length = 1;
            in_sentence = true;
        } else if (byte == '\r' || byte == '\n') {
            if (!in_sentence) continue;
            in_sentence = false;
            sentence[length] = '\0';
            if (!nmea_parse_fix(sentence, &fix) || strcmp(fix.time, last_time) == 0) continue;
            strcpy(last_time, fix.time);
            print_fix(fixed ? "fix" : "fix acquired", &fix);
            fixed = true;
        } else if (in_sentence) {
            if (length < NMEA_SENTENCE_MAX - 2) sentence[length++] = (char)byte;
            else in_sentence = false;
        }
    }
}
