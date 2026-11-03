# Cellular modem

The `modem` fixture puts a cellular modem on the board profile's `uart.modem` route. A test
changes signal, registration and power at the board's current instant. The firmware talks AT and
uses its ordinary PWRKEY and RI wires. No network, host serial port or `pppd` is needed.

On the serial backend, the peripheral remains physical: model, replay and tap requests skip
at the call. Merely requesting `modem` on a profile with that route does not skip a console test.

## Set up the modem

Select the model with its first call before `board.power_on()`. Merely requesting `modem` leaves
its UART silent; `modem.replay(path)` selects a recording instead. The LilyGO profile has the
modem's route: UART1, TX GPIO 26, RX GPIO 27, 115200 baud, PWRKEY GPIO 4 and RI GPIO 33; its
DTR and reset lines are named GPIOs without a model effect
([control lines](board-profiles-user.md#lilygo-control-lines-and-simulation-limits)). The devkit
profile has no modem route.

```python
import pytest
from esp32tl.units import seconds


@pytest.mark.app_path("../firmware/modem_at")
@pytest.mark.board_profile("lilygo-t-a7670g-r2")
def test_signal(board, modem, console):
    modem.signal(dbm=-73)
    modem.tap("modem.jsonl")
    board.power_on()
    console.expect_exact("modem_at: dialogue complete")
    modem.signal(dbm=-101)
    console.expect_exact("+CSQ: 6,99", timeout=seconds(2))
    modem.lose_registration()
    console.expect_exact("+CEREG: 0,0", timeout=seconds(2))
    modem.register()
    console.expect_exact("+CEREG: 0,1", timeout=seconds(2))
```

The initial state is powered, registered, -73 dBm (CSQ 20), echo off. `modem.power_off()` before
board power-on instead starts it off, for firmware that boots it with PWRKEY.

| Call or property | Meaning |
|---|---|
| `signal(dbm=...)` | Finite signal strength in dBm; CSQ is clamped to 0–31, in 2 dB steps from -113 dBm; BER is 99 |
| `lose_registration()` | Lose registration, end data mode; queries return registration status 0 |
| `register()` | Restore coverage; status becomes 1 if powered and `CFUN` is 1 |
| `power_off()` | Switch off; the supply remains available to PWRKEY |
| `power_on()` | Supply and power on, resetting command mode and AT settings |
| `lose_power()` | Remove supply and switch off; PWRKEY cannot start it |
| `restore_power()` | Restore supply, leaving a powered-off modem off until PWRKEY or `power_on()` |
| `powered`, `registered`, `data_mode` | Current model state; reading does not select a model |
| `commands` | Every complete AT command while powered, as `Command(at, text, response, fallback)`; `at` is a `Duration` |

Registration changes affect the next query; there are no unsolicited registration notifications.
Losing registration or power in data mode ends that mode silently: this model sends no unsolicited
`NO CARRIER` for an established connection's loss. Firmware waiting for a disconnect indication
may time out. With `modem.network()`, loss resets PPP and closes TCP connections, publishing
MQTT Wills once; no old connection or queued reply survives a new dial. A dial attempted
while unregistered does return `NO CARRIER`.
Power loss silences the UART, clears any partial command and ends data mode. RI is 1 while
powered and 0 while off. `lines.events()` includes RI changes with `by="model"`; `lines.level`
still reads only the firmware's output. Driving or releasing RI directly after selecting a model
raises `SourceError: modem.ri is driven by uart.modem`.

## AT dialogue

Commands end at CR or LF, regardless of UART packet boundaries. They are case-insensitive but
must start with `AT`, without leading whitespace or noise. `+++` in command mode is ignored,
including an unterminated escape immediately followed by `AT`. Empty lines and other noncommands
produce no reply.

| Command | Answer or action |
|---|---|
| `AT`, `ATE0`, `ATE1` | `OK`; echo off/on for subsequent commands |
| `AT+CPIN?` | `+CPIN: READY`, then `OK` |
| `AT+CREG?`, `AT+CGREG?`, `AT+CEREG?` | `+<name>: <n>,<status>`, then `OK`; `n` defaults to 0, status is 1 registered or 0 unregistered |
| `AT+CREG=?`, `AT+CGREG=?`, `AT+CEREG=?` | `+<name>: (0-2)`, then `OK`; does not change the query mode |
| `AT+CREG=<n>`, `AT+CGREG=<n>`, `AT+CEREG=<n>` | Store query mode 0, 1 or 2; invalid modes get `ERROR` |
| `AT+CSQ` | `+CSQ: <rssi>,99`, then `OK` |
| `AT+CFUN=0`, `=1`, `=4` | Change functionality; 0 and 4 prevent registration, 1 permits it; other values get `ERROR` |
| `AT+CFUN?` | `+CFUN: <value>`, then `OK` |
| `AT+CGDCONT=...`, `AT+CTZU=...`, `AT+COPS=...` | `OK`; settings are accepted without further simulation |
| `AT+COPS?` | `+COPS: 0,0,"ESP32TL",7` if registered, otherwise `+COPS: 0`, then `OK` |
| `AT+CGMM`, `AT+CGMR`, `AT+CGSN`, `AT+CIMI` | Fixed model identity, then `OK` |
| `ATD*99#`, `ATD*99***1#`, `ATD*99##`, `ATDT*99#`, `AT+CGDATA=...` | `CONNECT` and data mode when registered; otherwise `NO CARRIER` |
| `+++` in data mode | Return to command mode with `OK`; may be split across UART chunks |
| Other `AT` commands | `OK`, marked `fallback=True` in history and `(fallback OK)` in the report |

The dial recognizer accepts `ATD` or `ATDT`, followed by `*99`, an optional suffix of digits,
asterisks and hashes, and a final `#`. `AT+CGDATA=` accepts any arguments without validating them.
Echo preserves the original command bytes; command history decodes non-ASCII bytes as U+FFFD.

Answers are stamped 1 ms after the `uart_tx` chunk containing the command terminator. This is the
chunk's first-byte timestamp, not a separately measured terminator timestamp. Without a selected
network, data mode records bytes without answering them and three consecutive pluses escape
immediately, even in payload. With `modem.network()` (also selected by the `broker` fixture),
PPP reaches the [simulated network](network-user.md). Its binary payload preserves pluses;
`+++` escapes only after one second of silence on each side. One or two trailing candidate
pluses wait for the next byte. The model does not validate APNs or emulate a
cellular network, SIM provisioning, boot delays or shutdown delays. An overlong command (more
than 4096 bytes) is discarded through its next terminator.

## PWRKEY and reporting

A completed active pulse toggles power if it lasts at least the profile's threshold for the
current power state. The LilyGO profile uses active-high pulses at the ESP32 pad, with model
thresholds of 50 ms to start and 2500 ms to stop. SIMCom's
[Hardware Design V1.03, tables 10–11](https://files.waveshare.com/wiki/A7670E-Cat-1-GNSS-HAT/A7672X_A7670X_Series_Hardware_Design_V1.03.pdf#page=30)
lists 50 ms as the **typical** power-on pulse and 2.5 s as the minimum power-off pulse. The model
uses the typical 50 ms value as its acceptance threshold; it does not claim that it is a
manufacturer-guaranteed minimum. The model enforces neither the minimum 2 s power-off to power-on
buffer (`Toff-on`) nor the typical 8 s UART startup time (`Ton(uart)`) from those tables.

LilyGO's [ATdebug sequence](https://github.com/Xinyuan-LilyGO/LilyGO-T-A76XX/blob/63af963038e93c91baf39d89a3a736d6bc26ff13/examples/ATdebug/ATdebug.ino#L102-L109)
uses active-high ESP32-pad pulses with [nominal delays of 100 and 3000 ms](https://github.com/Xinyuan-LilyGO/LilyGO-T-A76XX/blob/63af963038e93c91baf39d89a3a736d6bc26ff13/examples/ATdebug/utilities.h#L1063-L1065).
Those nominal delays are not model thresholds: a tick-based delay may finish almost one tick
earlier. Pulses of 99.9 ms and 2999.9 ms are therefore accepted without extending the firmware's
delay. Short pulses do nothing; disabling the output ends a pulse. RI follows 1 ms after the
ending edge; a test's direct power call changes it at the test's instant. A power change before a
pending RI response takes effect replaces that response.

The modem is on the board's supply. `board.power_off()` is `lose_power()` for it: it goes off,
RI drops, and PPP with its TCP sessions ends. The `board.power_on()` after it is
`restore_power()`: the supply is back, and the modem stays off until the firmware's PWRKEY pulse
starts it, as on the board, whatever it was at the first power-on. `board.reset()` resets the
ESP32 alone: the modem keeps its power and its state, so a firmware that checks RI at boot finds
it on ([resets and power cycles](pytest-plugin-user.md#resets-and-power-cycles)).

The report has a `modem AT commands:` section keeping every command and its time, even when the
rolling UART log has discarded its bytes. The `esp32sim_link` JSON property's
`routes["uart.modem"]["commands"]` contains `t_ns`, `command`, `response` and `fallback` for each.
`modem.tap(path)` records both directions with source `model:modem`.

Command history is unbounded. Its memory use, text report and JSON/JUnit property grow with every
command and reply; a modem queried eight times a second produces about 29,000 entries in one
emulated hour. Tests with long polling loops should account for this report size. No entries are
truncated, so initialization commands remain available after a failure. Sent RI levels also
appear in the rolling run log as `modem.ri in`; cancelled pending levels do not appear.

## See also

- [How the model works](modem-technical.md).
- [The pytest fixtures](pytest-plugin-user.md).
- [Board profiles](board-profiles-user.md).
