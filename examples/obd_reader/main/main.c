/* An OBD-II reader over Bluetooth Classic: the client of an ELM327-compatible adapter plugged
 * into a vehicle's diagnostic port. It searches for a device named OBDII, pairs with PIN 1234,
 * opens the adapter's Serial Port Profile channel and talks to it in the adapter's commands, one
 * line ending in a carriage return at a time, each answered by lines ending in the prompt '>':
 *
 *   ATZ    reset; the adapter names itself, "ELM327 v1.5"
 *   ATE0   echo off
 *   ATSP0  find the vehicle's protocol by itself
 *   0100   which mode 01 PIDs 01 to 20 the vehicle supports, again every 2 s until it answers
 *   01xx   then, once a second, each of rpm (0C), speed (0D) and coolant (05) it supports
 *
 * and prints what it read, as "obd_reader: rpm=850 speed=0 coolant=90". An adapter that sends no
 * prompt within 2 s is disconnected. A search that found nothing and a session that ended start
 * the next search at once. A failed pairing or connection, a device that is no ELM327 and a vehicle
 * with none of the three readings start it 5 s later. */
#include <inttypes.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_bt.h"
#include "esp_bt_main.h"
#include "esp_gap_bt_api.h"
#include "esp_spp_api.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/stream_buffer.h"
#include "freertos/task.h"
#include "nvs_flash.h"

#define ADAPTER_NAME "OBDII"
#define ADAPTER_PIN "1234"
/* One search, in units of 1.28 s: 5.12 s. */
#define SEARCH_LENGTH 4
#define ANSWER_TIMEOUT_MS 2000
#define RETRY_MS 2000
#define SEARCH_PAUSE_MS 5000
#define READ_PERIOD_MS 1000
#define ANSWER_MAX 256

typedef enum { IDLE, SEARCHING, CONNECTING, CONNECTED, PAUSED } state_t;

/* The GAP and SPP callbacks, which Bluedroid runs in its own task one at a time, keep these. The
 * reader task changes `state` only while it is PAUSED, when no callback does. */
static volatile state_t state = IDLE;
static volatile bool inquiring;
static esp_bd_addr_t adapter;
static uint32_t sessions;

/* What the reader task shares with the callbacks: the number of the session open now, 0 while none
 * is, and its handle. A session that closed and another that opened during one of the reader's
 * waits have different numbers, so the reader never takes the second for the first. */
static volatile uint32_t session;
static volatile uint32_t handle;
/* Set by the reader before it disconnects: the next search comes after a pause. */
static volatile bool pause_after_close;
static StreamBufferHandle_t received;
static EventGroupHandle_t events;
#define OPENED BIT0
#define PAUSE BIT1
/* Set when a session closes, after `session` has become 0: it wakes the reader's waits. */
#define CLOSED BIT2

static const struct {
    uint8_t pid;
    const char *name;
} READINGS[] = {{0x0C, "rpm"}, {0x0D, "speed"}, {0x05, "coolant"}};
#define READING_COUNT (sizeof(READINGS) / sizeof(READINGS[0]))

static void start_inquiry(void) {
    printf("obd_reader: searching for " ADAPTER_NAME "\n");
    esp_bt_gap_start_discovery(ESP_BT_INQ_MODE_GENERAL_INQUIRY, SEARCH_LENGTH, 0);
}

/* The next search starts now, or when the inquiry still under way has stopped. */
static void search(void) {
    state = SEARCHING;
    if (!inquiring) {
        start_inquiry();
    }
}

/* After a failed attempt: the reader task starts the next search after a pause. */
static void search_later(void) {
    state = PAUSED;
    xEventGroupSetBits(events, PAUSE);
}

static bool named_adapter(const esp_bt_gap_cb_param_t *param) {
    for (int i = 0; i < param->disc_res.num_prop; i++) {
        esp_bt_gap_dev_prop_t *prop = &param->disc_res.prop[i];
        if (prop->type != ESP_BT_GAP_DEV_PROP_EIR) {
            continue;
        }
        uint8_t length = 0;
        uint8_t *name =
            esp_bt_gap_resolve_eir_data(prop->val, ESP_BT_EIR_TYPE_CMPL_LOCAL_NAME, &length);
        if (name == NULL) {
            name = esp_bt_gap_resolve_eir_data(prop->val, ESP_BT_EIR_TYPE_SHORT_LOCAL_NAME,
                                               &length);
        }
        if (name != NULL && length == strlen(ADAPTER_NAME) &&
            memcmp(name, ADAPTER_NAME, length) == 0) {
            return true;
        }
    }
    return false;
}

static void gap_event(esp_bt_gap_cb_event_t event, esp_bt_gap_cb_param_t *param) {
    switch (event) {
    case ESP_BT_GAP_DISC_RES_EVT:
        if (state == SEARCHING && named_adapter(param)) {
            memcpy(adapter, param->disc_res.bda, ESP_BD_ADDR_LEN);
            const uint8_t *a = adapter;
            printf("obd_reader: found " ADAPTER_NAME " at %02x:%02x:%02x:%02x:%02x:%02x\n", a[0],
                   a[1], a[2], a[3], a[4], a[5]);
            state = CONNECTING;
            esp_bt_gap_cancel_discovery();
            esp_spp_start_discovery(adapter);
        }
        break;
    case ESP_BT_GAP_DISC_STATE_CHANGED_EVT:
        inquiring = param->disc_st_chg.state == ESP_BT_GAP_DISCOVERY_STARTED;
        if (!inquiring && state == SEARCHING) {
            start_inquiry();
        }
        break;
    case ESP_BT_GAP_PIN_REQ_EVT: {
        esp_bt_pin_code_t pin;
        memcpy(pin, ADAPTER_PIN, strlen(ADAPTER_PIN));
        esp_bt_gap_pin_reply(param->pin_req.bda, true, strlen(ADAPTER_PIN), pin);
        break;
    }
    case ESP_BT_GAP_AUTH_CMPL_EVT:
        if (param->auth_cmpl.stat == ESP_BT_STATUS_SUCCESS) {
            printf("obd_reader: paired with %s\n", (const char *)param->auth_cmpl.device_name);
        } else {
            printf("obd_reader: pairing failed (status %d)\n", param->auth_cmpl.stat);
        }
        break;
    default:
        break;
    }
}

static void spp_event(esp_spp_cb_event_t event, esp_spp_cb_param_t *param) {
    switch (event) {
    case ESP_SPP_INIT_EVT:
        if (param->init.status == ESP_SPP_SUCCESS) {
            esp_bt_gap_set_device_name("obd_reader");
            esp_bt_gap_set_scan_mode(ESP_BT_NON_CONNECTABLE, ESP_BT_NON_DISCOVERABLE);
            search();
        } else {
            printf("obd_reader: SPP failed to start (status %d)\n", param->init.status);
        }
        break;
    case ESP_SPP_DISCOVERY_COMP_EVT:
        if (state != CONNECTING) {
            break;
        }
        if (param->disc_comp.status == ESP_SPP_SUCCESS && param->disc_comp.scn_num > 0) {
            esp_spp_connect(ESP_SPP_SEC_AUTHENTICATE, ESP_SPP_ROLE_MASTER, param->disc_comp.scn[0],
                            adapter);
        } else {
            printf("obd_reader: " ADAPTER_NAME " offers no serial port\n");
            search_later();
        }
        break;
    case ESP_SPP_OPEN_EVT:
        if (state != CONNECTING) {
            break;
        }
        if (param->open.status == ESP_SPP_SUCCESS) {
            handle = param->open.handle;
            state = CONNECTED;
            sessions = sessions == UINT32_MAX ? 1 : sessions + 1;
            session = sessions;
            /* Whatever the last session left, this one ends with a search at once. */
            pause_after_close = false;
            printf("obd_reader: connected\n");
            xEventGroupSetBits(events, OPENED);
        } else {
            printf("obd_reader: could not connect (status %d)\n", param->open.status);
            search_later();
        }
        break;
    case ESP_SPP_CLOSE_EVT:
        if (state == CONNECTED) {
            session = 0;
            xEventGroupSetBits(events, CLOSED);
            printf("obd_reader: disconnected\n");
            if (pause_after_close) {
                pause_after_close = false;
                search_later();
            } else {
                search();
            }
        } else if (state == CONNECTING) {
            printf("obd_reader: could not connect\n");
            search_later();
        }
        break;
    case ESP_SPP_DATA_IND_EVT:
        if (state == CONNECTED) {
            xStreamBufferSend(received, param->data_ind.data, param->data_ind.len, 0);
        }
        break;
    default:
        break;
    }
}

/* Why the last ask() of an open session failed. */
static const char *failure;

/* Sends `command` and its carriage return in session `mine`, then reads up to the adapter's
 * prompt. What it answered goes into `answer`: its lines, apart from empty ones and the echo of the
 * command, joined with '\n'. False when the session closed, or when the adapter did not answer
 * within the time or at more length than `answer` holds, which `failure` then names. */
static bool ask(uint32_t mine, const char *command, char *answer) {
    static char line[32];
    static char raw[ANSWER_MAX];
    int length = snprintf(line, sizeof(line), "%s\r", command);
    xStreamBufferReset(received);
    if (session != mine) {
        return false;
    }
    if (esp_spp_write(handle, length, (uint8_t *)line) != ESP_OK) {
        failure = "could not be written to";
        return false;
    }

    size_t got = 0;
    TickType_t deadline = xTaskGetTickCount() + pdMS_TO_TICKS(ANSWER_TIMEOUT_MS);
    while (got == 0 || raw[got - 1] != '>') {
        TickType_t now = xTaskGetTickCount();
        if (session != mine) {
            return false;
        }
        if ((int32_t)(deadline - now) <= 0) {
            failure = "stopped answering";
            return false;
        }
        if (got == sizeof(raw) - 1) {
            failure = "sent an answer too long to read";
            return false;
        }
        /* In steps of 50 ms at most, to see a session that closed meanwhile. */
        TickType_t wait = deadline - now < pdMS_TO_TICKS(50) ? deadline - now : pdMS_TO_TICKS(50);
        got += xStreamBufferReceive(received, raw + got, sizeof(raw) - 1 - got, wait);
    }
    raw[got - 1] = '\0';

    answer[0] = '\0';
    char *next;
    for (char *piece = strtok_r(raw, "\r\n", &next); piece != NULL;
         piece = strtok_r(NULL, "\r\n", &next)) {
        if (strcmp(piece, command) == 0) {
            continue;
        }
        if (answer[0] != '\0') {
            strlcat(answer, "\n", ANSWER_MAX);
        }
        strlcat(answer, piece, ANSWER_MAX);
    }
    return true;
}

/* Waits `ms` in session `mine`; false as soon as the session has closed. A CLOSED left by an
 * earlier session wakes it too, and it waits on. */
static bool rest(uint32_t mine, uint32_t ms) {
    TickType_t deadline = xTaskGetTickCount() + pdMS_TO_TICKS(ms);
    for (;;) {
        TickType_t now = xTaskGetTickCount();
        if (session != mine) {
            return false;
        }
        if ((int32_t)(deadline - now) <= 0) {
            return true;
        }
        xEventGroupWaitBits(events, CLOSED, pdTRUE, pdFALSE, deadline - now);
    }
}

/* The data bytes of a mode 01 answer to `pid`, "41 0C 0D 48" with or without spaces, from the
 * first of its lines that is one, as after "SEARCHING..."; how many, or -1 when none is, as for
 * NO DATA. */
static int mode01_answer(const char *answer, uint8_t pid, uint8_t *data, size_t size) {
    for (const char *p = answer; *p != '\0'; p += *p == '\n') {
        uint8_t bytes[8];
        size_t count = 0;
        bool valid = true;
        for (; *p != '\0' && *p != '\n'; p++) {
            if (*p == ' ' || !valid) {
                continue;
            }
            char digits[3] = {p[0], p[1], '\0'};
            char *end;
            long value = strtol(digits, &end, 16);
            valid = p[1] != '\0' && end == digits + 2 && count < sizeof(bytes);
            if (valid) {
                bytes[count++] = (uint8_t)value;
                p++;
            }
        }
        if (valid && count >= 2 && bytes[0] == 0x41 && bytes[1] == pid && count - 2 <= size) {
            memcpy(data, bytes + 2, count - 2);
            return (int)(count - 2);
        }
    }
    return -1;
}

/* Asks for READINGS[reading] and appends what came back to `line`, as " rpm=850". */
static bool read_pid(uint32_t mine, size_t reading, char *line, size_t size) {
    static char answer[ANSWER_MAX];
    char command[5];
    uint8_t pid = READINGS[reading].pid;
    snprintf(command, sizeof(command), "01%02X", pid);
    if (!ask(mine, command, answer)) {
        return false;
    }
    uint8_t data[2];
    int length = mode01_answer(answer, pid, data, sizeof(data));
    char value[24];
    if (pid == 0x0C && length == 2) {
        snprintf(value, sizeof(value), " rpm=%d", (data[0] * 256 + data[1]) / 4);
    } else if (pid == 0x0D && length == 1) {
        snprintf(value, sizeof(value), " speed=%d", data[0]);
    } else if (pid == 0x05 && length == 1) {
        snprintf(value, sizeof(value), " coolant=%d", data[0] - 40);
    } else {
        snprintf(value, sizeof(value), " %s=?", READINGS[reading].name);
    }
    strlcat(line, value, size);
    return true;
}

/* How a session ended: the adapter or the session went away, or there is nothing to read. */
typedef enum { LOST, NOTHING_TO_READ } end_t;

/* Session `mine`, from its first command. */
static end_t talk(uint32_t mine) {
    static char answer[ANSWER_MAX];
    if (!ask(mine, "ATZ", answer)) {
        return LOST;
    }
    if (strncmp(answer, "ELM327", 6) != 0) {
        printf("obd_reader: not an ELM327 adapter: %.*s\n", (int)strcspn(answer, "\n"), answer);
        return NOTHING_TO_READ;
    }
    printf("obd_reader: adapter %s\n", answer);
    if (!ask(mine, "ATE0", answer) || !ask(mine, "ATSP0", answer)) {
        return LOST;
    }

    uint8_t supported[4];
    for (;;) {
        if (!ask(mine, "0100", answer)) {
            return LOST;
        }
        if (mode01_answer(answer, 0x00, supported, sizeof(supported)) == (int)sizeof(supported)) {
            break;
        }
        const char *last = strrchr(answer, '\n');
        printf("obd_reader: no answer from the vehicle: %s\n", last ? last + 1 : answer);
        if (!rest(mine, RETRY_MS)) {
            return LOST;
        }
    }

    bool reads[READING_COUNT];
    bool any = false;
    char line[96] = "obd_reader: the vehicle supports";
    for (size_t i = 0; i < READING_COUNT; i++) {
        /* PID n is bit 32 - n of the four bytes, the most significant first. */
        uint8_t n = READINGS[i].pid - 1;
        reads[i] = supported[n / 8] & (0x80 >> (n % 8));
        if (reads[i]) {
            strlcat(line, " ", sizeof(line));
            strlcat(line, READINGS[i].name, sizeof(line));
            any = true;
        }
    }
    if (!any) {
        printf("obd_reader: the vehicle supports none of rpm speed coolant\n");
        return NOTHING_TO_READ;
    }
    printf("%s\n", line);

    for (;;) {
        strlcpy(line, "obd_reader:", sizeof(line));
        for (size_t i = 0; i < READING_COUNT; i++) {
            if (reads[i] && !read_pid(mine, i, line, sizeof(line))) {
                return LOST;
            }
        }
        printf("%s\n", line);
        if (!rest(mine, READ_PERIOD_MS)) {
            return LOST;
        }
    }
}

static void reader_task(void *arg) {
    for (;;) {
        EventBits_t bits = xEventGroupWaitBits(events, OPENED | PAUSE, pdTRUE, pdFALSE,
                                               portMAX_DELAY);
        if (bits & PAUSE) {
            vTaskDelay(pdMS_TO_TICKS(SEARCH_PAUSE_MS));
            search();
            continue;
        }
        uint32_t mine = session;
        if (mine == 0) {
            continue;
        }
        end_t end = talk(mine);
        if (session != mine) {
            /* The adapter closed it; the callback has started the next search. */
            continue;
        }
        if (end == LOST) {
            printf("obd_reader: the adapter %s\n", failure);
        }
        pause_after_close = end == NOTHING_TO_READ;
        esp_spp_disconnect(handle);
        while (session == mine) {
            xEventGroupWaitBits(events, CLOSED, pdTRUE, pdFALSE, portMAX_DELAY);
        }
    }
}

void app_main(void) {
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);

    received = xStreamBufferCreate(ANSWER_MAX, 1);
    events = xEventGroupCreate();
    xTaskCreate(reader_task, "reader", 4096, NULL, 5, NULL);

    ESP_ERROR_CHECK(esp_bt_controller_mem_release(ESP_BT_MODE_BLE));
    esp_bt_controller_config_t controller = BT_CONTROLLER_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_bt_controller_init(&controller));
    ESP_ERROR_CHECK(esp_bt_controller_enable(ESP_BT_MODE_CLASSIC_BT));
    esp_bluedroid_config_t host = BT_BLUEDROID_INIT_CONFIG_DEFAULT();
    /* Adapters pair with a PIN, as Bluetooth 2.0 devices do: no Secure Simple Pairing. */
    host.ssp_en = false;
    ESP_ERROR_CHECK(esp_bluedroid_init_with_cfg(&host));
    ESP_ERROR_CHECK(esp_bluedroid_enable());

    ESP_ERROR_CHECK(esp_bt_gap_register_callback(gap_event));
    esp_bt_pin_code_t unused = {0};
    ESP_ERROR_CHECK(esp_bt_gap_set_pin(ESP_BT_PIN_TYPE_VARIABLE, 0, unused));
    ESP_ERROR_CHECK(esp_spp_register_callback(spp_event));
    esp_spp_cfg_t spp = {.mode = ESP_SPP_MODE_CB, .enable_l2cap_ertm = false, .tx_buffer_size = 0};
    ESP_ERROR_CHECK(esp_spp_enhanced_init(&spp));
    printf("obd_reader: ready\n");
}
