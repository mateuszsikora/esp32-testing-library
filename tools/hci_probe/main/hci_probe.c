/* Asks the chip's own Bluetooth controller, through VHCI and with no host running, what it reports,
 * and prints every packet both ways in hex, "TX <hex>" and "RX <hex>", on the console. Built with
 * Bluedroid configured as ESP-IDF's BLE 4.2 examples are, and never started, so that the controller
 * runs as under those examples (`BLE_50:0`); the commands come in Bluedroid's start-up order where
 * it matters: Espressif's flow control of advertising reports right after `HCI_Reset`. The answers
 * esp32tl's guest controller reports on an LE-only chip came from an ESP32-S3 running this
 * (`esp32tl/bluetooth/guest.py`, `docs/stage0-findings.md`). */
#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "esp_bt.h"
#include "nvs_flash.h"

typedef struct { uint16_t len; uint8_t data[300]; } packet_t;
static QueueHandle_t rx;

static void can_send(void) {}
static int received(uint8_t *data, uint16_t len)
{
    packet_t p = { .len = len > sizeof(p.data) ? sizeof(p.data) : len };
    memcpy(p.data, data, p.len);
    xQueueSend(rx, &p, 0);
    return 0;
}
static const esp_vhci_host_callback_t callbacks = { can_send, received };

static void hex(const char *what, const uint8_t *data, int len)
{
    printf("%s ", what);
    for (int i = 0; i < len; i++) printf("%02x", data[i]);
    printf("\n");
}

/* Drains what the controller sent for `ms` milliseconds. */
static int drain(int ms)
{
    packet_t p;
    int n = 0;
    TickType_t until = xTaskGetTickCount() + pdMS_TO_TICKS(ms);
    while (xTaskGetTickCount() < until) {
        if (xQueueReceive(rx, &p, pdMS_TO_TICKS(10))) { hex("RX", p.data, p.len); n++; }
    }
    return n;
}

static void command(uint16_t op, const uint8_t *params, uint8_t len, int wait_ms)
{
    uint8_t packet[260] = { 0x01, op & 0xff, op >> 8, len };
    if (len != 0) {
        memcpy(packet + 4, params, len);
    }
    while (!esp_vhci_host_check_send_available()) vTaskDelay(1);
    hex("TX", packet, 4 + len);
    esp_vhci_host_send_packet(packet, 4 + len);
    drain(wait_ms);
}

void app_main(void)
{
    ESP_ERROR_CHECK(nvs_flash_init());
    rx = xQueueCreate(256, sizeof(packet_t));
    if (rx == NULL) {
        printf("hci_probe: no memory for the receive queue\n");
        return;
    }
    vTaskDelay(pdMS_TO_TICKS(3000));
    printf("hci_probe: start\n");
    esp_bt_controller_config_t config = BT_CONTROLLER_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_bt_controller_init(&config));
    ESP_ERROR_CHECK(esp_bt_controller_enable(ESP_BT_MODE_BLE));
    ESP_ERROR_CHECK(esp_vhci_host_register_callback(&callbacks));

    command(0x0c03, NULL, 0, 200);                       /* HCI_Reset */
    command(0x0c31, (uint8_t[]){ 1 }, 1, 100);           /* Set_Controller_To_Host_Flow_Control */
    command(0xfd09, (uint8_t[]){ 1, 2, 0, 3, 0 }, 5, 100); /* as Bluedroid: right after */
    command(0x1001, NULL, 0, 100);                       /* Read_Local_Version_Information */
    command(0x1002, NULL, 0, 100);                       /* Read_Local_Supported_Commands */
    command(0x1003, NULL, 0, 100);                       /* Read_Local_Supported_Features */
    command(0x1004, (uint8_t[]){ 0 }, 1, 100);           /* Read_Local_Extended_Features, page 0 */
    command(0x1004, (uint8_t[]){ 1 }, 1, 100);           /* page 1 */
    command(0x1005, NULL, 0, 100);                       /* Read_Buffer_Size */
    command(0x1009, NULL, 0, 100);                       /* Read_BD_ADDR */
    command(0x2002, NULL, 0, 100);                       /* LE_Read_Buffer_Size */
    command(0x2060, NULL, 0, 100);                       /* LE_Read_Buffer_Size v2 */
    command(0x2003, NULL, 0, 100);                       /* LE_Read_Local_Supported_Features */
    command(0x201c, NULL, 0, 100);                       /* LE_Read_Supported_States */
    command(0x200f, NULL, 0, 100);                       /* LE_Read_Filter_Accept_List_Size */
    command(0x202a, NULL, 0, 100);                       /* LE_Read_Resolving_List_Size */
    command(0x202f, NULL, 0, 100);                       /* LE_Read_Maximum_Data_Length */
    command(0x2023, NULL, 0, 100);                       /* LE_Read_Suggested_Default_Data_Length */
    command(0x2007, NULL, 0, 100);                       /* LE_Read_Advertising_Physical_Channel_Tx_Power */
    command(0x203a, NULL, 0, 100);                       /* LE_Read_Maximum_Advertising_Data_Length */
    command(0x203b, NULL, 0, 100);                       /* LE_Read_Number_Of_Supported_Advertising_Sets */
    command(0x204b, NULL, 0, 100);                       /* LE_Read_Transmit_Power */
    command(0x0401, (uint8_t[]){ 0x33, 0x8b, 0x9e, 8, 0 }, 5, 100); /* HCI_Inquiry: BR/EDR */
    command(0x0c0d, (uint8_t[]){ 0, 0, 0 }, 2, 100);     /* Read_Stored_Link_Key: BR/EDR */
    command(0x0c7f, NULL, 0, 100);                       /* Write_Extended_Page_Timeout: BR/EDR */

    /* Espressif's flow control of advertising reports: 2 credits, lost threshold 3, then a scan
     * of everything around for 3 s with no duplicate filter, then 2 credits back. */
    command(0x200b, (uint8_t[]){ 0, 0x10, 0, 0x10, 0, 0, 0 }, 7, 100);   /* passive scan */
    command(0x200c, (uint8_t[]){ 1, 0 }, 2, 3000);                       /* scan on */
    printf("hci_probe: give 2 credits back\n");
    command(0xfd0a, (uint8_t[]){ 2, 0 }, 2, 1000);
    command(0x200c, (uint8_t[]){ 0, 0 }, 2, 300);                        /* scan off */
    printf("hci_probe: done\n");
}
