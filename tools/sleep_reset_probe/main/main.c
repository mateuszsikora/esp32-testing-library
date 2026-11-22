#include <inttypes.h>
#include <stdio.h>

#include "esp_attr.h"
#include "esp_err.h"
#include "esp_rom_sys.h"
#include "esp_sleep.h"
#include "hal/wdt_hal.h"
#include "soc/rtc_cntl_reg.h"
#include "soc/soc.h"

static RTC_NOINIT_ATTR unsigned boots;

void app_main(void)
{
    uint32_t state = REG_READ(RTC_CNTL_STATE0_REG);
    uint32_t power = REG_READ(RTC_CNTL_DIG_PWC_REG);
    int reset = esp_rom_get_reset_reason(0);
    if (reset == RESET_REASON_CHIP_POWER_ON) { // POWERON_RESET, including the board's EN button
        boots = 0;
    }
    ++boots;
    printf("PROBE boot=%u reset=%d wake=%d STATE0=0x%08" PRIx32
           " SLEEP_EN=%d DIG_PWC=0x%08" PRIx32 " DG_WRAP_PD_EN=%d\n",
           boots, reset, (int)esp_sleep_get_wakeup_cause(), state,
           !!(state & RTC_CNTL_SLEEP_EN), power, !!(power & RTC_CNTL_DG_WRAP_PD_EN));
    if (boots != 1 && boots != 2) {
        return;
    }

    wdt_hal_context_t wdt;
    wdt_hal_init(&wdt, WDT_RWDT, 0, false); // leaves the watchdog off for the control
    ESP_ERROR_CHECK(esp_sleep_enable_timer_wakeup(boots == 1 ? 10000000 : 1000000));
    if (boots == 1) {
        wdt_hal_write_protect_disable(&wdt);
        // The HAL pauses the RWDT in sleep by default. Let it count slow-clock ticks.
        REG_CLR_BIT(RTC_CNTL_WDTCONFIG0_REG, RTC_CNTL_WDT_PAUSE_IN_SLP);
#if CONFIG_PROBE_RESET_RTC
        const wdt_stage_action_t action = WDT_STAGE_ACTION_RESET_RTC;
#else
        const wdt_stage_action_t action = WDT_STAGE_ACTION_RESET_SYSTEM;
#endif
        wdt_hal_config_stage(&wdt, WDT_STAGE0, 150000, action);
        wdt_hal_enable(&wdt);
        wdt_hal_write_protect_enable(&wdt);
    }
    esp_deep_sleep_start();
}
