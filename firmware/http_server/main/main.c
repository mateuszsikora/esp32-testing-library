/* A Wi-Fi station that serves HTTP on port 80 with ESP-IDF's esp_http_server, for a test that
 * opens the connection: `GET /status` answers JSON with the requests served and the uptime,
 * `POST /echo` parses a JSON body and answers it back. It joins `myssid` with `mypassword`, the
 * defaults of ESP-IDF's examples, over WPA2, joins again whenever it loses the access point, and
 * starts the server once, at its first address. A request that asks for `Connection: close` gets
 * it: the server closes the connection after the response. */
#include <stdio.h>
#include <string.h>
#include <strings.h>

#include "cJSON.h"
#include "esp_event.h"
#include "esp_http_server.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "nvs_flash.h"

#define SSID "myssid"
#define PASSWORD "mypassword"
#define MAX_BODY 2048

static httpd_handle_t server;
static int served;

static esp_err_t respond(httpd_req_t *req, const char *status, const char *json) {
    char connection[16] = "";
    bool close = httpd_req_get_hdr_value_str(req, "Connection", connection, sizeof connection) ==
                     ESP_OK &&
                 strcasecmp(connection, "close") == 0;
    served++;
    httpd_resp_set_status(req, status);
    httpd_resp_set_type(req, "application/json");
    if (close) httpd_resp_set_hdr(req, "Connection", "close");
    esp_err_t err = httpd_resp_sendstr(req, json);
    printf("http_server: %s %s -> %.3s\n", req->method == HTTP_GET ? "GET" : "POST", req->uri,
           status);
    if (close) httpd_sess_trigger_close(req->handle, httpd_req_to_sockfd(req));
    return err;
}

static esp_err_t status(httpd_req_t *req) {
    char json[96];
    snprintf(json, sizeof json, "{\"status\":\"ok\",\"requests\":%d,\"uptime_ms\":%lld}",
             served + 1, (long long)(esp_timer_get_time() / 1000));
    return respond(req, "200 OK", json);
}

static esp_err_t echo(httpd_req_t *req) {
    if (req->content_len > MAX_BODY) {
        return respond(req, "413 Content Too Large", "{\"error\":\"body over 2048 bytes\"}");
    }
    static char body[MAX_BODY + 1];
    size_t length = 0;
    while (length < req->content_len) {
        int got = httpd_req_recv(req, body + length, req->content_len - length);
        if (got == HTTPD_SOCK_ERR_TIMEOUT) continue;
        if (got <= 0) return ESP_FAIL;
        length += got;
    }
    body[length] = '\0';
    cJSON *parsed = cJSON_ParseWithLength(body, length);
    if (parsed == NULL) return respond(req, "400 Bad Request", "{\"error\":\"invalid JSON\"}");
    cJSON *answer = cJSON_CreateObject();
    cJSON_AddItemToObject(answer, "echo", parsed);
    cJSON_AddNumberToObject(answer, "bytes", (double)length);
    char *json = cJSON_PrintUnformatted(answer);
    esp_err_t err = respond(req, "200 OK", json);
    cJSON_free(json);
    cJSON_Delete(answer);
    return err;
}

static void start_server(void) {
    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    ESP_ERROR_CHECK(httpd_start(&server, &config));
    httpd_uri_t get = {.uri = "/status", .method = HTTP_GET, .handler = status};
    httpd_uri_t post = {.uri = "/echo", .method = HTTP_POST, .handler = echo};
    ESP_ERROR_CHECK(httpd_register_uri_handler(server, &get));
    ESP_ERROR_CHECK(httpd_register_uri_handler(server, &post));
    printf("http_server: listening on port %d\n", config.server_port);
}

static void on_event(void *arg, esp_event_base_t base, int32_t id, void *data) {
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        wifi_event_sta_disconnected_t *event = data;
        printf("http_server: Wi-Fi disconnected %d\n", event->reason);
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = data;
        printf("http_server: got address " IPSTR "\n", IP2STR(&event->ip_info.ip));
        if (server == NULL) start_server();
    }
}

void app_main(void) {
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_event, NULL));
    wifi_config_t config = {
        .sta = {.ssid = SSID, .password = PASSWORD, .threshold.authmode = WIFI_AUTH_WPA2_PSK},
    };
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &config));
    ESP_ERROR_CHECK(esp_wifi_start());
}
