/* Deep sleep with a 5 s timer wake, counted across wakes in RTC memory. On a chip that wakes, the
 * console shows a new boot about every 5 s, with the wake cause and the slow clock it slept on. */
#include <stdio.h>
#include "esp_attr.h"
#include "esp_private/esp_clk.h" /* private IDF API, for the slow-clock calibration only; may move */
#include "esp_sleep.h"
#include "esp_timer.h"

static RTC_DATA_ATTR unsigned boots;

void app_main(void) {
    boots++;
    printf("DEEPSLEEP boot %u, wake cause %d, slow clock cal %lu, uptime %lld us\n", boots,
           (int)esp_sleep_get_wakeup_cause(), (unsigned long)esp_clk_slowclk_cal_get(),
           (long long)esp_timer_get_time());
    esp_sleep_enable_timer_wakeup(5 * 1000 * 1000);
    printf("DEEPSLEEP sleeping for 5 s\n");
    esp_deep_sleep_start();
}
