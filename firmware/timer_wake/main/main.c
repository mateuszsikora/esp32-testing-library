/* Deep sleep with a timer wake, twice: a boot count in RTC slow memory, which a deep sleep keeps,
 * printed with the ROM's reset reason and the wake cause, then 2 s of deep sleep. The third boot
 * prints done and returns. */
#include <stdio.h>

#include "esp_attr.h"
#include "esp_rom_sys.h"
#include "esp_sleep.h"

#define BOOTS 3
#define SLEEP_US (2 * 1000 * 1000)

static RTC_DATA_ATTR unsigned boots;

void app_main(void) {
    boots++;
    printf("timer_wake: boot=%u rom=%d wake=%d\n", boots, (int)esp_rom_get_reset_reason(0),
           (int)esp_sleep_get_wakeup_cause());
    if (boots >= BOOTS) {
        printf("timer_wake: done\n");
        return;
    }
    printf("timer_wake: sleeping %d ms\n", SLEEP_US / 1000);
    esp_sleep_enable_timer_wakeup(SLEEP_US);
    esp_deep_sleep_start();
}
