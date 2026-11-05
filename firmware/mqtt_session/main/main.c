/* esp-mqtt with CleanSession=0 over the modem's PPP: it subscribes on every connection and prints
 * what the broker tells it, Session Present and each message's DUP, for a test to read. Told
 * "backlog N" on its commands topic, it publishes N QoS 1 reports, each waiting for the last one's
 * PUBACK, so that the time they take follows the network's round trip. */
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_event.h"
#include "esp_modem_api.h"
#include "esp_netif.h"
#include "freertos/event_groups.h"
#include "freertos/queue.h"
#include "mqtt_client.h"

#define PPP_READY BIT0
#define COMMANDS "session/commands"
#define REPORTS "session/reports"

static EventGroupHandle_t events;
static QueueHandle_t backlogs;
static TaskHandle_t reporter;

static void ip_event(void *arg, esp_event_base_t base, int32_t id, void *data) {
    if (id == IP_EVENT_PPP_GOT_IP) xEventGroupSetBits(events, PPP_READY);
}

static void command(const char *data, int length, bool dup) {
    char text[16];
    if (dup || length >= (int)sizeof text) return;
    memcpy(text, data, length);
    text[length] = '\0';
    if (strncmp(text, "backlog ", 8) == 0) {
        int count = atoi(text + 8);
        xQueueSend(backlogs, &count, 0);
    }
}

static void report_backlogs(void *arg) {
    esp_mqtt_client_handle_t client = arg;
    int count;
    for (;;) {
        xQueueReceive(backlogs, &count, portMAX_DELAY);
        bool failed = false;
        for (int n = 1; n <= count && !failed; n++) {
            char report[32];
            int length = snprintf(report, sizeof report, "report %d", n);
            int id = esp_mqtt_client_publish(client, REPORTS, report, length, 1, 0);
            if (id < 0) {
                printf("mqtt_session: publish failed: %d\n", id);
                failed = true;
                continue;
            }
            uint32_t acknowledged;
            do {
                xTaskNotifyWait(0, 0, &acknowledged, portMAX_DELAY);
            } while ((int)acknowledged != id);
            printf("mqtt_session: published %d of %d\n", n, count);
        }
        if (!failed) printf("mqtt_session: backlog done\n");
    }
}

static void mqtt_event(void *arg, esp_event_base_t base, int32_t id, void *data) {
    esp_mqtt_event_handle_t event = data;
    if (id == MQTT_EVENT_CONNECTED) {
        printf("mqtt_session: connected session_present=%d\n", event->session_present);
        esp_mqtt_client_subscribe(event->client, COMMANDS, 1);
    } else if (id == MQTT_EVENT_DISCONNECTED) {
        printf("mqtt_session: disconnected\n");
    } else if (id == MQTT_EVENT_DATA) {
        printf("mqtt_session: data dup=%d qos=%d id=%d topic=%.*s payload=%.*s\n", event->dup,
               event->qos, event->msg_id, event->topic_len, event->topic, event->data_len,
               event->data);
        command(event->data, event->data_len, event->dup);
    } else if (id == MQTT_EVENT_PUBLISHED) {
        xTaskNotify(reporter, event->msg_id, eSetValueWithOverwrite);
    }
}

void app_main(void) {
    events = xEventGroupCreate();
    backlogs = xQueueCreate(4, sizeof(int));
    assert(events && backlogs);
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
    esp_modem_dce_config_t dce_config = ESP_MODEM_DCE_DEFAULT_CONFIG("internet");
    esp_modem_dce_t *modem = esp_modem_new_dev(ESP_MODEM_DCE_SIM7600, &dte, &dce_config, netif);
    assert(modem);
    ESP_ERROR_CHECK(esp_modem_set_mode(modem, ESP_MODEM_MODE_DATA));
    xEventGroupWaitBits(events, PPP_READY, pdFALSE, pdTRUE, portMAX_DELAY);

    const esp_mqtt_client_config_t mqtt = {
        .broker.address.uri = "mqtt://broker.test:1883",
        .credentials.client_id = "session-client",
        .session.disable_clean_session = true,
        .network.reconnect_timeout_ms = 1000,
    };
    esp_mqtt_client_handle_t client = esp_mqtt_client_init(&mqtt);
    assert(client);
    if (xTaskCreate(report_backlogs, "backlog", 4096, client, 5, &reporter) != pdPASS) abort();
    ESP_ERROR_CHECK(esp_mqtt_client_register_event(client, ESP_EVENT_ANY_ID, mqtt_event, NULL));
    ESP_ERROR_CHECK(esp_mqtt_client_start(client));
}
