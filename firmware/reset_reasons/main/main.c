/* Each boot prints esp_reset_reason() by name, with the ROM's reset reason, then resets the chip
 * the next way from what that reason was: a power-on restarts with esp_restart, the restart
 * panics with abort, the panic panics as the task watchdog does, and that boot sleeps 100 ms in
 * deep sleep. The wake prints done and returns. The reason is the only state: no boot needs to
 * remember the one before.
 *
 * esp32sim models no timer-group watchdog, so no task watchdog fires there: the image does what
 * ESP-IDF's does when one fires with trigger_panic (esp_system/task_wdt/task_wdt.c,
 * task_wdt_timeout_abort), the hint ESP_RST_TASK_WDT and then the panic, which keeps a hint it
 * finds (esp_system/panic.c, esp_panic_handler). */
#include <stdio.h>

#include "esp_rom_sys.h"
#include "esp_sleep.h"
#include "esp_system.h"
#include "esp_private/system_internal.h"

#define SLEEP_US (100 * 1000)

/* esp_reset_reason_t of ESP-IDF v5.3 to v6.1, by name (esp_system.h). */
static const char *name(esp_reset_reason_t reason) {
    switch (reason) {
    case ESP_RST_UNKNOWN: return "ESP_RST_UNKNOWN";
    case ESP_RST_POWERON: return "ESP_RST_POWERON";
    case ESP_RST_EXT: return "ESP_RST_EXT";
    case ESP_RST_SW: return "ESP_RST_SW";
    case ESP_RST_PANIC: return "ESP_RST_PANIC";
    case ESP_RST_INT_WDT: return "ESP_RST_INT_WDT";
    case ESP_RST_TASK_WDT: return "ESP_RST_TASK_WDT";
    case ESP_RST_WDT: return "ESP_RST_WDT";
    case ESP_RST_DEEPSLEEP: return "ESP_RST_DEEPSLEEP";
    case ESP_RST_BROWNOUT: return "ESP_RST_BROWNOUT";
    case ESP_RST_SDIO: return "ESP_RST_SDIO";
    case ESP_RST_USB: return "ESP_RST_USB";
    case ESP_RST_JTAG: return "ESP_RST_JTAG";
    case ESP_RST_EFUSE: return "ESP_RST_EFUSE";
    case ESP_RST_PWR_GLITCH: return "ESP_RST_PWR_GLITCH";
    case ESP_RST_CPU_LOCKUP: return "ESP_RST_CPU_LOCKUP";
    }
    return "?";
}

void app_main(void) {
    esp_reset_reason_t reason = esp_reset_reason();
    printf("reset_reasons: reason=%s rom=%d\n", name(reason), (int)esp_rom_get_reset_reason(0));
    fflush(stdout);
    switch (reason) {
    case ESP_RST_POWERON:
        printf("reset_reasons: restarting\n");
        fflush(stdout);
        esp_restart();
        break;
    case ESP_RST_SW:
        printf("reset_reasons: panicking\n");
        fflush(stdout);
        abort();
        break;
    case ESP_RST_PANIC:
        printf("reset_reasons: panicking as the task watchdog\n");
        fflush(stdout);
        esp_reset_reason_set_hint(ESP_RST_TASK_WDT);
        abort();
        break;
    case ESP_RST_TASK_WDT:
        printf("reset_reasons: sleeping %d ms\n", SLEEP_US / 1000);
        fflush(stdout);
        esp_sleep_enable_timer_wakeup(SLEEP_US);
        esp_deep_sleep_start();
        break;
    default:
        printf("reset_reasons: done\n");
        break;
    }
}
