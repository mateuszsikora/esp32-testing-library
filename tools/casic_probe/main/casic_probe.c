/* Writes CASIC binary frames to the L76K GNSS receiver of a LilyGO T-A7670G (UART2, TX 21, RX 22,
 * 9600 8N1, the peripheral supply on GPIO 12, as LilyGO's ExternalGPS_A7670G_Only example) and
 * prints what it answers, with times in microseconds of esp_timer, on the console (UART0, 115200):
 *
 *   NMEA <t> <sentence>            each NMEA sentence the receiver sent, stamped at its first byte
 *   CASIC <first> <last> <ok> <hex> each CASIC frame it sent, `ok` whether its checksum holds
 *   RAW <t> <hex>                  bytes that are neither
 *   TX <case> <i> <start> <end> <hex>  a frame written, `end` once its last bit left the TX pin
 *   RESULT <case> <i> <answer> <after_end_us> <busy>  the first ACK-ACK or ACK-NAK naming the
 *                                  frame within WINDOW_MS, or `none`; `busy` whether the receiver
 *                                  was sending NMEA at the end of the write
 *
 * It answers what the specifications leave open for esp32tl's GNSS receiver model (issue #333):
 * what a frame with a bad checksum gets, how soon an answer comes after the frame, and whether
 * AID-INI gets one. It sends no command that saves the receiver's configuration: no CFG-CFG, no $PCAS00, no
 * CFG-RST. Power-cycle the board after a run to drop the assistance data it wrote. What it found
 * on an L76K is in l76k.txt and docs/stage0-findings.md. */
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include "driver/gpio.h"
#include "driver/uart.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"

#define GNSS UART_NUM_2
#define GNSS_TX 21
#define GNSS_RX 22
#define POWER_ON 12
#define BAUD 9600
#define WINDOW_MS 1500
#define MAX_PAYLOAD 2047

typedef struct {
    int64_t first, last;
    uint8_t cls, id;
    bool ok;
    uint8_t answered_cls, answered_id;
} answer_t;

static QueueHandle_t answers;
static volatile int64_t last_rx;    /* the time of the last byte the receiver sent */
static volatile int64_t last_nmea;  /* the time of the last byte of an NMEA sentence */
static volatile bool in_sentence;   /* between a `$` and its line ending */
/* The UTC time and date of the last RMC, and when it came. */
static volatile int rmc_hms = -1, rmc_dmy = -1;
static volatile int64_t rmc_at;

static uint32_t checksum(uint8_t cls, uint8_t id, const uint8_t *payload, uint16_t len)
{
    uint32_t sum = ((uint32_t)id << 24) + ((uint32_t)cls << 16) + len;
    for (int i = 0; i + 4 <= len; i += 4) {
        sum += (uint32_t)payload[i] | (uint32_t)payload[i + 1] << 8 |
               (uint32_t)payload[i + 2] << 16 | (uint32_t)payload[i + 3] << 24;
    }
    return sum;
}

/* The frame of `cls` and `id` carrying `payload`, its checksum XORed with `spoil`. */
static int frame(uint8_t *out, uint8_t cls, uint8_t id, const uint8_t *payload, uint16_t len,
                 uint32_t spoil)
{
    out[0] = 0xba;
    out[1] = 0xce;
    out[2] = len & 0xff;
    out[3] = len >> 8;
    out[4] = cls;
    out[5] = id;
    memcpy(out + 6, payload, len);
    uint32_t sum = checksum(cls, id, payload, len) ^ spoil;
    memcpy(out + 6 + len, &sum, 4);
    return 10 + len;
}

static void hex(const uint8_t *data, int len)
{
    for (int i = 0; i < len; i++) printf("%02x", data[i]);
}

static void sentence_done(const char *line, int64_t at)
{
    printf("NMEA %lld %s\n", at, line);
    /* $xxRMC,hhmmss.ss,A|V,lat,N,lon,E,speed,course,ddmmyy,... */
    if (strlen(line) > 6 && strncmp(line + 3, "RMC,", 4) == 0) {
        int field = 0, hms = -1, dmy = -1;
        for (const char *p = line; *p; p++) {
            if (*p != ',') continue;
            field++;
            if (field == 1 && p[1] >= '0' && p[1] <= '9') sscanf(p + 1, "%6d", &hms);
            if (field == 9 && p[1] >= '0' && p[1] <= '9') sscanf(p + 1, "%6d", &dmy);
        }
        if (hms >= 0 && dmy > 0) {
            rmc_hms = hms;
            rmc_dmy = dmy;
            rmc_at = at;
        }
    }
}

/* Bytes that are neither NMEA nor CASIC, printed once the buffer is full or something else
 * comes. */
static uint8_t raw[64];
static int nraw;
static int64_t raw_at;

static void raw_flush(void)
{
    if (nraw == 0) return;
    printf("RAW %lld ", raw_at);
    hex(raw, nraw);
    printf("\n");
    nraw = 0;
}

static void raw_add(uint8_t c, int64_t at)
{
    if (nraw == 0) raw_at = at;
    raw[nraw++] = c;
    if (nraw == sizeof(raw)) raw_flush();
}

/* Reads the receiver a byte at a time, each stamped as it comes, and splits NMEA from CASIC. */
static void reader(void *arg)
{
    static uint8_t buf[6 + MAX_PAYLOAD + 4];
    static char line[128];
    int n = 0, nline = 0;
    int64_t first = 0, line_at = 0;
    for (;;) {
        uint8_t c;
        if (uart_read_bytes(GNSS, &c, 1, portMAX_DELAY) != 1) continue;
        int64_t t = esp_timer_get_time();
        last_rx = t;
        if (n == 1) {
            if (c == 0xce) {
                buf[n++] = c;
                continue;
            }
            /* A 0xBA that starts no header; this byte may start one. */
            n = 0;
            raw_add(0xba, first);
        }
        if (n == 0 && c == 0xba) {
            first = t;
            buf[n++] = c;
            continue;
        }
        if (n >= 2) {
            buf[n++] = c;
            if (n >= 4) {
                uint16_t len = buf[2] | buf[3] << 8;
                if (len > MAX_PAYLOAD) {
                    printf("RAW %lld ", first);
                    hex(buf, n);
                    printf(" (length %u)\n", len);
                    n = 0;
                } else if (n == 10 + len) {
                    uint32_t sum;
                    memcpy(&sum, buf + 6 + len, 4);
                    answer_t a = {first, t, buf[4], buf[5], sum == checksum(buf[4], buf[5], buf + 6, len),
                                  len >= 2 ? buf[6] : 0, len >= 2 ? buf[7] : 0};
                    printf("CASIC %lld %lld %d ", first, t, a.ok);
                    hex(buf, n);
                    printf("\n");
                    xQueueSend(answers, &a, 0);
                    n = 0;
                }
            }
            continue;
        }
        if (c == '$') {
            raw_flush();
            in_sentence = true;
            nline = 0;
            line_at = t;
        }
        if (in_sentence) {
            last_nmea = t;
            if (c == '\r') continue;
            if (c == '\n' || nline == sizeof(line) - 1) {
                line[nline] = 0;
                in_sentence = false;
                sentence_done(line, line_at);
                continue;
            }
            line[nline++] = c;
            continue;
        }
        raw_add(c, t);
    }
}

/* Waits until the receiver has sent nothing for `idle_ms`: the gap after an epoch's burst. */
static void wait_idle(int idle_ms)
{
    while (esp_timer_get_time() - last_rx < idle_ms * 1000) vTaskDelay(pdMS_TO_TICKS(2));
}

/* Writes `data`, then reports the first ACK-ACK or ACK-NAK that names (`cls`, `id`) in the
 * window after its end, and every other CASIC frame that came. */
static void probe(const char *name, int i, const uint8_t *data, int len, uint8_t cls, uint8_t id)
{
    answer_t a;
    while (xQueueReceive(answers, &a, 0)) {}
    int64_t start = esp_timer_get_time();
    uart_write_bytes(GNSS, data, len);
    uart_wait_tx_done(GNSS, pdMS_TO_TICKS(1000));
    int64_t end = esp_timer_get_time();
    /* NMEA bytes only: a CASIC answer to the first of two frames comes while the second is
     * still being written. */
    bool busy = in_sentence || end - last_nmea < 2000;
    printf("TX %s %d %lld %lld ", name, i, start, end);
    hex(data, len);
    printf("\n");
    const char *answer = "none";
    int64_t after = -1;
    int64_t until = end + WINDOW_MS * 1000;
    while (esp_timer_get_time() < until) {
        if (!xQueueReceive(answers, &a, pdMS_TO_TICKS(10))) continue;
        if (a.cls != 0x05 || a.answered_cls != cls || a.answered_id != id || after >= 0) continue;
        answer = a.id == 0x01 ? "ack" : a.id == 0x00 ? "nak" : "ack-other";
        after = a.first - end;
    }
    printf("RESULT %s %d %s %lld %d\n", name, i, answer, after, busy);
}

/* Days from 1970-01-01 to the civil date (Howard Hinnant's days_from_civil). */
static int64_t days_from_civil(int y, int m, int d)
{
    y -= m <= 2;
    int era = (y >= 0 ? y : y - 399) / 400;
    int yoe = y - era * 400;
    int doy = (153 * (m + (m > 2 ? -3 : 9)) + 2) / 5 + d - 1;
    int doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    return (int64_t)era * 146097 + doe - 719468;
}

/* AID-INI (ZKW's CASIC specification, 2.14.1, 56 bytes), in latitude and longitude, no position;
 * with `timed`, the GPS week and time of week of the last RMC, carried on to now. */
static int aid_ini(uint8_t *out, bool timed, uint32_t spoil)
{
    struct __attribute__((packed)) {
        double lat, lon, alt, tow;
        float freq_bias, p_acc, t_acc, f_acc;
        uint32_t res;
        uint16_t wn;
        uint8_t time_source, flags;
    } p = {0};
    _Static_assert(sizeof(p) == 56, "AID-INI is 56 bytes");
    p.flags = 0x20; /* B5: latitude and longitude; nothing valid */
    if (timed) {
        int hms = rmc_hms, dmy = rmc_dmy;
        int64_t days = days_from_civil(2000 + dmy % 100, dmy / 100 % 100, dmy / 10000) -
                       days_from_civil(1980, 1, 6);
        double gps = days * 86400.0 + hms / 10000 * 3600 + hms / 100 % 100 * 60 + hms % 100 +
                     18 /* GPS - UTC leap seconds */ + (esp_timer_get_time() - rmc_at) / 1e6;
        p.wn = (uint16_t)(gps / 604800);
        p.tow = gps - p.wn * 604800.0;
        p.t_acc = 1.0f;
        p.flags |= 0x02; /* B1: time valid */
        printf("AID-INI time: week %u tow %.3f\n", p.wn, p.tow);
    }
    return frame(out, 0x0b, 0x01, (const uint8_t *)&p, sizeof(p), spoil);
}

void app_main(void)
{
    gpio_set_direction(POWER_ON, GPIO_MODE_OUTPUT);
    gpio_set_level(POWER_ON, 1);
    uart_config_t config = {
        .baud_rate = BAUD,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    ESP_ERROR_CHECK(uart_driver_install(GNSS, 4096, 0, 0, NULL, 0));
    ESP_ERROR_CHECK(uart_param_config(GNSS, &config));
    ESP_ERROR_CHECK(uart_set_pin(GNSS, GNSS_TX, GNSS_RX, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE));
    /* Every byte as it comes, for its time. */
    ESP_ERROR_CHECK(uart_set_rx_full_threshold(GNSS, 1));
    ESP_ERROR_CHECK(uart_set_rx_timeout(GNSS, 1));
    answers = xQueueCreate(32, sizeof(answer_t));
    xTaskCreatePinnedToCore(reader, "reader", 6144, NULL, 10, NULL, 1);

    printf("casic_probe: waiting 10 s for the receiver\n");
    vTaskDelay(pdMS_TO_TICKS(10000));
    if (last_rx == 0) printf("casic_probe: nothing from the receiver: check GPIO 12 and the wiring\n");

    /* The receiver's firmware: $PCAS06,0 is a query, answered with $GPTXT. */
    const char *version = "$PCAS06,0*1B\r\n";
    wait_idle(30);
    uart_write_bytes(GNSS, version, strlen(version));
    vTaskDelay(pdMS_TO_TICKS(1500));

    uint8_t buf[256];
    int len;
    static const uint8_t none[1];
    uint8_t eph[72] = {0};

    /* Quectel's L76K specification, 3.2.2: CFG-PRT query, answered with CFG-PRT and ACK-ACK. */
    for (int i = 0; i < 10; i++) {
        wait_idle(30);
        len = frame(buf, 0x06, 0x00, none, 0, 0);
        probe("cfg-prt-query-idle", i, buf, len, 0x06, 0x00);
    }
    /* The same at any phase of the epoch, NMEA on the line or not. */
    for (int i = 0; i < 10; i++) {
        vTaskDelay(pdMS_TO_TICKS(337 + 71 * i));
        len = frame(buf, 0x06, 0x00, none, 0, 0);
        probe("cfg-prt-query-any", i, buf, len, 0x06, 0x00);
    }
    /* A bad checksum. */
    for (int i = 0; i < 5; i++) {
        wait_idle(30);
        len = frame(buf, 0x06, 0x00, none, 0, 1);
        probe("cfg-prt-query-bad-checksum", i, buf, len, 0x06, 0x00);
    }
    /* A class and id the specification does not define. */
    for (int i = 0; i < 3; i++) {
        wait_idle(30);
        len = frame(buf, 0x06, 0x7f, none, 0, 0);
        probe("cfg-unknown-id", i, buf, len, 0x06, 0x7f);
    }
    /* Two queries in one write. */
    for (int i = 0; i < 3; i++) {
        wait_idle(30);
        len = frame(buf, 0x06, 0x00, none, 0, 0);
        len += frame(buf + len, 0x06, 0x00, none, 0, 0);
        probe("cfg-prt-query-twice", i, buf, len, 0x06, 0x00);
    }
    /* AID-INI with nothing valid. */
    for (int i = 0; i < 5; i++) {
        wait_idle(30);
        len = aid_ini(buf, false, 0);
        probe("aid-ini-empty", i, buf, len, 0x0b, 0x01);
    }
    /* AID-INI with the receiver's own time, once it has one. */
    for (int i = 0; i < 5; i++) {
        if (rmc_hms < 0) {
            printf("RESULT aid-ini-timed %d skipped: no RMC with a time and date yet\n", i);
            continue;
        }
        wait_idle(30);
        len = aid_ini(buf, true, 0);
        probe("aid-ini-timed", i, buf, len, 0x0b, 0x01);
    }
    /* AID-INI with a bad checksum. */
    for (int i = 0; i < 3; i++) {
        wait_idle(30);
        len = aid_ini(buf, false, 1);
        probe("aid-ini-bad-checksum", i, buf, len, 0x0b, 0x01);
    }
    /* MSG-GPSEPH, 72 bytes, all zero: satellite 0, not valid. */
    for (int i = 0; i < 3; i++) {
        wait_idle(30);
        len = frame(buf, 0x08, 0x07, eph, sizeof(eph), 0);
        probe("msg-gpseph-zero", i, buf, len, 0x08, 0x07);
    }
    printf("casic_probe: done; power-cycle the board to drop what it wrote\n");
    vTaskDelay(portMAX_DELAY);
}
