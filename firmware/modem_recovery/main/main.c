/* PPP through esp_modem on the LilyGO T-A7670G R2 wires: it notices a lost connection, recovers a
 * modem that answers nothing, and redials. */
#include <stdio.h>
#include <string.h>

#include "driver/gpio.h"
#include "esp_event.h"
#include "esp_modem_api.h"
#include "esp_netif.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"
#include "lwip/sockets.h"

#define PWRKEY GPIO_NUM_4
#define RESET GPIO_NUM_5
#define POWER_ENABLE GPIO_NUM_12
#define RI GPIO_NUM_33
#define GOT_IP BIT0
#define LOST_IP BIT1
/* Longer than lwIP's 6 s between a Terminate-Request and IP_EVENT_PPP_LOST_IP, so that a modem
 * that offers one is heard first. */
#define SILENT_PROBES 10

static EventGroupHandle_t events;
static esp_modem_dce_t *modem;

static void ip_event(void *arg, esp_event_base_t base, int32_t id, void *data) {
    if (id == IP_EVENT_PPP_GOT_IP) xEventGroupSetBits(events, GOT_IP);
    if (id == IP_EVENT_PPP_LOST_IP) {
        xEventGroupClearBits(events, GOT_IP);
        xEventGroupSetBits(events, LOST_IP);
    }
}

static void pulse(gpio_num_t line, int ms) {
    ESP_ERROR_CHECK(gpio_set_level(line, 1));
    vTaskDelay(pdMS_TO_TICKS(ms));
    ESP_ERROR_CHECK(gpio_set_level(line, 0));
}

/* An NTP request to the far end of the link, which is a timeout away from an answer. */
static bool answered(void) {
    const struct sockaddr_in server = {
        .sin_family = AF_INET, .sin_port = htons(123), .sin_addr.s_addr = inet_addr("10.64.0.1"),
    };
    const struct timeval timeout = {.tv_sec = 1};
    uint8_t packet[48] = {0x23};
    int fd = socket(AF_INET, SOCK_DGRAM, IPPROTO_IP);
    if (fd < 0) return false;
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof timeout);
    bool ok = sendto(fd, packet, sizeof packet, 0, (const struct sockaddr *)&server, sizeof server) ==
                  sizeof packet &&
              recv(fd, packet, sizeof packet, 0) == sizeof packet;
    close(fd);
    return ok;
}

static void wait_for_loss(void) {
    for (int silent = 0; silent < SILENT_PROBES;) {
        silent = answered() ? 0 : silent + 1;
        if (xEventGroupWaitBits(events, LOST_IP, pdTRUE, pdTRUE, pdMS_TO_TICKS(1000)) & LOST_IP) {
            puts("modem_recovery: lost ip");
            return;
        }
    }
    puts("modem_recovery: no answer");
}

static void recover(void) {
    printf("modem_recovery: hung RI=%d\n", gpio_get_level(RI));
    if (gpio_get_level(RI)) pulse(PWRKEY, 3000);
    vTaskDelay(pdMS_TO_TICKS(20));
    printf("modem_recovery: after PWRKEY RI=%d\n", gpio_get_level(RI));
    ESP_ERROR_CHECK(gpio_set_level(POWER_ENABLE, 0));
    vTaskDelay(pdMS_TO_TICKS(1000));
    ESP_ERROR_CHECK(gpio_set_level(POWER_ENABLE, 1));
    pulse(RESET, 2600);
    vTaskDelay(pdMS_TO_TICKS(10000));
    pulse(PWRKEY, 100);
    vTaskDelay(pdMS_TO_TICKS(20));
    printf("modem_recovery: recovered RI=%d\n", gpio_get_level(RI));
}

static bool dial(void) {
    xEventGroupClearBits(events, LOST_IP);
    if (esp_modem_set_mode(modem, ESP_MODEM_MODE_DATA) != ESP_OK) return false;
    return xEventGroupWaitBits(events, GOT_IP, pdFALSE, pdTRUE, pdMS_TO_TICKS(30000)) & GOT_IP;
}

void app_main(void) {
    const gpio_config_t outputs = {
        .pin_bit_mask = 1ULL << PWRKEY | 1ULL << RESET | 1ULL << POWER_ENABLE,
        .mode = GPIO_MODE_OUTPUT,
    };
    const gpio_config_t input = {.pin_bit_mask = 1ULL << RI, .mode = GPIO_MODE_INPUT};
    ESP_ERROR_CHECK(gpio_set_level(PWRKEY, 0));
    ESP_ERROR_CHECK(gpio_set_level(RESET, 0));
    ESP_ERROR_CHECK(gpio_set_level(POWER_ENABLE, 1));
    ESP_ERROR_CHECK(gpio_config(&outputs));
    ESP_ERROR_CHECK(gpio_config(&input));
    if (!gpio_get_level(RI)) pulse(PWRKEY, 100);

    events = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, ESP_EVENT_ANY_ID, ip_event, NULL));
    esp_netif_config_t ppp = ESP_NETIF_DEFAULT_PPP();
    esp_netif_t *netif = esp_netif_new(&ppp);
    esp_modem_dte_config_t dte = ESP_MODEM_DTE_DEFAULT_CONFIG();
    dte.uart_config.tx_io_num = 26;
    dte.uart_config.rx_io_num = 27;
    dte.uart_config.rts_io_num = UART_PIN_NO_CHANGE;
    dte.uart_config.cts_io_num = UART_PIN_NO_CHANGE;
    esp_modem_dce_config_t dce = ESP_MODEM_DCE_DEFAULT_CONFIG("internet");
    modem = esp_modem_new_dev(ESP_MODEM_DCE_SIM7600, &dte, &dce, netif);
    assert(modem);

    for (;;) {
        if (dial()) {
            puts("modem_recovery: connected");
            wait_for_loss();
            esp_modem_set_mode(modem, ESP_MODEM_MODE_COMMAND);
        }
        if (esp_modem_sync(modem) != ESP_OK) {
            recover();
            continue;
        }
        char registration[64] = "";
        esp_modem_at(modem, "AT+CEREG?", registration, 1000);
        printf("modem_recovery: command mode %s\n", registration);
    }
}
