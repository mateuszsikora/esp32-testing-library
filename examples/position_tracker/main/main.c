/* GNSS positions over cellular PPP and MQTT/TLS. Configuration belongs in NVS;
 * UTC comes from NTP, while the reporting interval uses the monotonic clock. */
#include <assert.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "driver/uart.h"
#include "esp_event.h"
#include "esp_modem_api.h"
#include "esp_netif.h"
#include "esp_netif_sntp.h"
#include "esp_timer.h"
#include "freertos/event_groups.h"
#include "freertos/semphr.h"
#include "mqtt_client.h"
#include "nmea.h"
#include "nvs.h"
#include "nvs_flash.h"

#define REPORT_INTERVAL_US 10000000LL
#define FIX_MAX_AGE_US 2500000LL
#define PPP_READY BIT0
#define MQTT_READY BIT1

static EventGroupHandle_t events;
static SemaphoreHandle_t fix_lock;
static TaskHandle_t reporter;
static nmea_fix_t latest_fix;
static bool has_fix;
static int64_t fix_at;
/* MQTT retains pointers into this configuration for its lifetime. */
static char broker_host[254], ntp_host[254], apn[64];
static char *broker_ca;
static uint16_t broker_port;

static bool read_string(nvs_handle_t nvs, const char *key, char *out, size_t size) {
    return nvs_get_str(nvs, key, out, &size) == ESP_OK && out[0] != '\0';
}

static bool configuration(void) {
    nvs_handle_t nvs;
    if (nvs_flash_init() != ESP_OK || nvs_open("config", NVS_READONLY, &nvs) != ESP_OK) {
        return false;
    }
    size_t ca_size = 0;
    bool ok = read_string(nvs, "broker_host", broker_host, sizeof broker_host) &&
              read_string(nvs, "ntp_host", ntp_host, sizeof ntp_host) &&
              read_string(nvs, "apn", apn, sizeof apn) &&
              nvs_get_u16(nvs, "broker_port", &broker_port) == ESP_OK && broker_port != 0 &&
              nvs_get_str(nvs, "broker_ca", NULL, &ca_size) == ESP_OK &&
              ca_size > 1 && ca_size <= 8192;
    if (ok) {
        broker_ca = malloc(ca_size);
        ok = broker_ca && nvs_get_str(nvs, "broker_ca", broker_ca, &ca_size) == ESP_OK;
    }
    nvs_close(nvs);
    if (!ok) {
        free(broker_ca);
        broker_ca = NULL;
    }
    return ok;
}

static void receive_gnss(void *arg) {
    char sentence[NMEA_SENTENCE_MAX - 1];
    size_t length = 0;
    bool reading = false;
    for (;;) {
        uint8_t byte;
        if (uart_read_bytes(UART_NUM_2, &byte, 1, pdMS_TO_TICKS(100)) != 1) continue;
        if (byte == '$') {
            sentence[0] = '$';
            length = 1;
            reading = true;
        } else if (byte == '\r' || byte == '\n') {
            if (!reading) continue;
            reading = false;
            sentence[length] = '\0';
            if (length < 7 || sentence[6] != ',' ||
                (strncmp(sentence + 3, "GGA", 3) && strncmp(sentence + 3, "RMC", 3))) continue;
            nmea_fix_t fix;
            bool valid = nmea_parse_fix(sentence, &fix);
            xSemaphoreTake(fix_lock, portMAX_DELAY);
            has_fix = valid;
            if (valid) {
                latest_fix = fix;
                fix_at = esp_timer_get_time();
            }
            xSemaphoreGive(fix_lock);
        } else if (reading) {
            if (length < sizeof sentence - 1) sentence[length++] = (char)byte;
            else reading = false;
        }
    }
}

static void ip_event(void *arg, esp_event_base_t base, int32_t id, void *data) {
    if (id == IP_EVENT_PPP_GOT_IP) xEventGroupSetBits(events, PPP_READY);
    if (id == IP_EVENT_PPP_LOST_IP) xEventGroupClearBits(events, PPP_READY);
}

static void mqtt_event(void *arg, esp_event_base_t base, int32_t id, void *data) {
    if (id == MQTT_EVENT_CONNECTED) {
        xEventGroupSetBits(events, MQTT_READY);
        xTaskNotifyGive(reporter);  /* Publish immediately, also after an MQTT reconnect. */
    } else if (id == MQTT_EVENT_DISCONNECTED) {
        xEventGroupClearBits(events, MQTT_READY);
    }
}

static void publish_position(esp_mqtt_client_handle_t client) {
    xSemaphoreTake(fix_lock, portMAX_DELAY);
    nmea_fix_t fix = latest_fix;
    bool valid = has_fix && esp_timer_get_time() - fix_at < FIX_MAX_AGE_US;
    xSemaphoreGive(fix_lock);
    char position[96], report[192];
    if (valid) {
        snprintf(position, sizeof position, "\"fix\":true,\"lat\":%.6f,\"lon\":%.6f",
                 fix.lat_e6 / 1000000.0, fix.lon_e6 / 1000000.0);
    } else {
        snprintf(position, sizeof position, "\"fix\":false,\"lat\":null,\"lon\":null");
    }
    snprintf(report, sizeof report, "{%s,\"time\":%" PRId64 "}", position, (int64_t)time(NULL));
    /* QoS 0, no retained state or offline backlog. Each report describes this instant. */
    if (esp_mqtt_client_publish(client, "tracker/position", report, 0, 0, 0) < 0) {
        printf("position_tracker: publish failed\n");
    }
}

void app_main(void) {
    if (!configuration()) {
        printf("position_tracker: invalid or missing NVS configuration\n");
        return;
    }
    events = xEventGroupCreate();
    fix_lock = xSemaphoreCreateMutex();
    assert(events && fix_lock);
    reporter = xTaskGetCurrentTaskHandle();
    const uart_config_t gnss = {
        .baud_rate = 9600, .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE, .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE, .source_clk = UART_SCLK_DEFAULT,
    };
    ESP_ERROR_CHECK(uart_driver_install(UART_NUM_2, 2048, 0, 0, NULL, 0));
    ESP_ERROR_CHECK(uart_param_config(UART_NUM_2, &gnss));
    ESP_ERROR_CHECK(uart_set_pin(UART_NUM_2, 21, 22, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE));
    if (xTaskCreate(receive_gnss, "gnss", 3072, NULL, 5, NULL) != pdPASS) abort();

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, ESP_EVENT_ANY_ID, ip_event, NULL));
    esp_netif_config_t ppp = ESP_NETIF_DEFAULT_PPP();
    esp_netif_t *netif = esp_netif_new(&ppp);
    assert(netif);
    esp_modem_dte_config_t dte = ESP_MODEM_DTE_DEFAULT_CONFIG();
    dte.uart_config.tx_io_num = 26;
    dte.uart_config.rx_io_num = 27;
    dte.uart_config.rts_io_num = UART_PIN_NO_CHANGE;
    dte.uart_config.cts_io_num = UART_PIN_NO_CHANGE;
    esp_modem_dce_config_t dce_config = ESP_MODEM_DCE_DEFAULT_CONFIG(apn);
    esp_modem_dce_t *modem = esp_modem_new_dev(ESP_MODEM_DCE_SIM7600, &dte, &dce_config, netif);
    assert(modem);
    ESP_ERROR_CHECK(esp_modem_set_mode(modem, ESP_MODEM_MODE_DATA));
    xEventGroupWaitBits(events, PPP_READY, pdFALSE, pdTRUE, portMAX_DELAY);

    esp_sntp_config_t sntp = ESP_NETIF_SNTP_DEFAULT_CONFIG(ntp_host);
    ESP_ERROR_CHECK(esp_netif_sntp_init(&sntp));
    /* Never connect or report with an unsynchronized clock. */
    while (esp_netif_sntp_sync_wait(pdMS_TO_TICKS(10000)) != ESP_OK) {
        printf("position_tracker: waiting for NTP\n");
    }
    const esp_mqtt_client_config_t mqtt = {
        .broker.address.hostname = broker_host,
        .broker.address.port = broker_port,
        .broker.address.transport = MQTT_TRANSPORT_OVER_SSL,
        .broker.verification.certificate = broker_ca,
        .credentials.client_id = "position-tracker",
    };
    esp_mqtt_client_handle_t client = esp_mqtt_client_init(&mqtt);
    assert(client);
    ESP_ERROR_CHECK(esp_mqtt_client_register_event(client, ESP_EVENT_ANY_ID, mqtt_event, NULL));
    ESP_ERROR_CHECK(esp_mqtt_client_start(client));

    int64_t next_report = 0;
    for (;;) {
        int64_t remaining = next_report - esp_timer_get_time();
        TickType_t wait = remaining > 0 ? pdMS_TO_TICKS((remaining + 999) / 1000) : 0;
        if (ulTaskNotifyTake(pdTRUE, wait)) next_report = esp_timer_get_time();
        int64_t now = esp_timer_get_time();
        if (now < next_report) continue;
        if (xEventGroupGetBits(events) & MQTT_READY) publish_position(client);
        next_report = now + REPORT_INTERVAL_US;
    }
}
