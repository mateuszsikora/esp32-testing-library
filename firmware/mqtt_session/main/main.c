/* esp-mqtt with CleanSession=0 over the modem's PPP: it subscribes on every connection and prints
 * what the broker tells it, Session Present and each message's DUP, for a test to read. */
#include <assert.h>
#include <stdio.h>

#include "esp_event.h"
#include "esp_modem_api.h"
#include "esp_netif.h"
#include "freertos/event_groups.h"
#include "mqtt_client.h"

#define PPP_READY BIT0
#define COMMANDS "session/commands"

static EventGroupHandle_t events;

static void ip_event(void *arg, esp_event_base_t base, int32_t id, void *data) {
    if (id == IP_EVENT_PPP_GOT_IP) xEventGroupSetBits(events, PPP_READY);
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
    }
}

void app_main(void) {
    events = xEventGroupCreate();
    assert(events);
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
    ESP_ERROR_CHECK(esp_mqtt_client_register_event(client, ESP_EVENT_ANY_ID, mqtt_event, NULL));
    ESP_ERROR_CHECK(esp_mqtt_client_start(client));
}
