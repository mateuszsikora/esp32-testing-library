/* console_repl with ESP-IDF's console on the USB-Serial/JTAG controller
 * (CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y in sdkconfig.defaults): the same REPL and prompt, read and
 * written through the controller's driver instead of UART0's. */
#include "esp_console.h"
#include "esp_err.h"

void app_main(void) {
    esp_console_repl_t *repl = NULL;
    esp_console_repl_config_t config = ESP_CONSOLE_REPL_CONFIG_DEFAULT();
    esp_console_dev_usb_serial_jtag_config_t usb = ESP_CONSOLE_DEV_USB_SERIAL_JTAG_CONFIG_DEFAULT();
    config.prompt = "console_repl>";
    ESP_ERROR_CHECK(esp_console_new_repl_usb_serial_jtag(&usb, &config, &repl));
    ESP_ERROR_CHECK(esp_console_start_repl(repl));
}
