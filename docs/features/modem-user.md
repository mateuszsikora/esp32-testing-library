# Cellular modem

The `modem` fixture puts a cellular modem on the board profile's `uart.modem` route. A test
changes signal, registration and power at the board's current instant. The firmware talks AT and
uses its ordinary PWRKEY and RI wires. No network, host serial port or `pppd` is needed.

On the serial backend, the peripheral remains physical: model, replay and tap requests skip
at the call. Merely requesting `modem` on a profile with that route does not skip a console test.

## Set up the modem

Select the model with its first call before `board.power_on()`. Merely requesting `modem` leaves
its UART silent; `modem.replay(path)` selects a recording instead. The LilyGO profile has the
modem's route: UART1, TX GPIO 26, RX GPIO 27, 115200 baud, PWRKEY GPIO 4, RESET GPIO 5 and RI
GPIO 33; its DTR line is a named GPIO without a model effect
([control lines](board-profiles-user.md#lilygo-control-lines-and-simulation-limits)). The devkit
profile has no modem route.

```python
import pytest
from esp32tl.units import seconds


@pytest.mark.app_path("../firmware/modem_at")
@pytest.mark.board_profile("lilygo-t-a7670g-r2")
def test_signal(board):
    board.modem.signal(dbm=-73)
    board.modem.tap("modem.jsonl")
    board.power_on()
    board.console.expect_exact("modem_at: dialogue complete")
    board.modem.signal(dbm=-101)
    board.console.expect_exact("+CSQ: 6,99", timeout=seconds(2))
    board.modem.lose_registration()
    board.console.expect_exact("+CEREG: 0,0", timeout=seconds(2))
    board.modem.register()
    board.console.expect_exact("+CEREG: 0,1", timeout=seconds(2))
```

The initial state is powered, registered, -73 dBm (CSQ 20), echo off. `modem.power_off()` before
board power-on instead starts it off, for firmware that boots it with PWRKEY.

| Call or property | Meaning |
|---|---|
| `signal(dbm=...)` | Finite signal strength in dBm; CSQ is clamped to 0–31, in 2 dB steps from -113 dBm; BER is 99 |
| `lose_registration()` | Lose registration, end data mode silently; queries return registration status 0 |
| `drop_call(terminate=False)` | End a data call and stay registered: `NO CARRIER`, then command mode ([below](#a-dropped-data-call)) |
| `register()` | Restore coverage; status becomes 1 if powered and `CFUN` is 1 |
| `power_off()` | Switch off; the supply remains available to PWRKEY |
| `power_on()` | Supply and power on, resetting command mode and AT settings |
| `lose_power()` | Remove supply and switch off; PWRKEY cannot start it |
| `restore_power()` | Restore supply, leaving a powered-off modem off until PWRKEY or `power_on()` |
| `clock(wrong_by=seconds(0), zone=seconds(0))` | `AT+CCLK?` tells the world's time, off by `wrong_by`, in `zone` ([below](#clock-and-operator)) |
| `clock_not_set(reads=...)` | `AT+CCLK?` tells `reads`, running on from this call, with no zone; `reads` is required |
| `clock_set_by_network()` | The same as `clock()`: the world's time, zone `+00` |
| `operator(name="ESP32TL", numeric="26000", short=None, act=7)` | What `AT+COPS?` names; `short=None` is `name` |
| `hang(pwrkey=True)` | The modem stops answering ([below](#a-hung-modem-and-reset)); `pwrkey=False`: PWRKEY does nothing either |
| `powered`, `registered`, `data_mode`, `hung` | Current model state; reading does not select a model |
| `restarts` | Each time a run of the modem ended, as `ModemRestart(at, cause)`, oldest first ([below](#a-hung-modem-and-reset)) |
| `mode_changes` | Each dial and each return to command mode, as `ModeChange(at, mode, cause)`, oldest first ([below](#mode-changes-and-dials)) |
| `next_mode_change(mode=None)` | A [described condition](pytest-plugin-user.md#described-conditions) for the next of them, or the next into `mode`, `"command"` or `"data"` |
| `commands` | Every complete AT command while powered, as `ModemCommand(at, text, response, fallback)`; `at` is a `Duration` |

Registration changes affect the next query; there are no unsolicited registration notifications.
Losing registration or power in data mode ends that mode silently: this model sends no unsolicited
`NO CARRIER` for those losses, only for [`drop_call`](#a-dropped-data-call). Firmware waiting for a
disconnect indication may time out. With `modem.network()`, loss resets PPP and loses the TCP
connections the test opened; no queued reply survives a new dial. The connections the device opened
stay open at the network's end, and silent, as a broker on the internet sees a device that lost its
coverage or its power: the broker publishes a Will when the keepalive runs out, 1.5 times the
keepalive after the device's last packet, not at the loss, and a device back before then takes its
session over ([network](network-user.md#a-lost-link-and-the-will)). A dial attempted while
unregistered does return `NO CARRIER`.
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
| `AT+CGDCONT=...`, `AT+CTZU=...`, `AT+COPS=...` other than the format below | `OK`; settings are accepted without further simulation |
| `AT+COPS=3,0`, `=3,1`, `=3,2` | `OK`; the format of `AT+COPS?`: long name, short name, numeric code. `AT+COPS=3` with any other format gets `ERROR` |
| `AT+COPS?` | `+COPS: 0,<format>,"<operator>",<act>` if registered, otherwise `+COPS: 0`, then `OK` |
| `AT+CCLK?` | `+CCLK: "yy/MM/dd,hh:mm:ss±zz"`, then `OK` ([below](#clock-and-operator)) |
| `AT+CGMM`, `AT+CGMR`, `AT+CGSN`, `AT+CIMI` | Fixed model identity, then `OK` |
| `ATD*99#`, `ATD*99***1#`, `ATD*99##`, `ATDT*99#`, `AT+CGDATA=...` | `CONNECT` and data mode when registered; otherwise `NO CARRIER` |
| `+++` in data mode | Return to command mode with `OK`; may be split across UART chunks. A [mode change](#mode-changes-and-dials), not an entry of `commands` |
| Other `AT` commands, `AT+CCLK=...` among them | `OK`, marked `fallback=True` in history and `(fallback OK)` in the report |

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

## Clock and operator

```python
from datetime import UTC, datetime
from esp32tl.units import hours

modem.clock(wrong_by=hours(-1), zone=hours(2))  # +CCLK: "26/01/01,13:00:05+08"
modem.clock_not_set(reads=datetime(2000, 1, 1, tzinfo=UTC))  # +CCLK: "00/01/01,00:00:05"
modem.clock_set_by_network()  # +CCLK: "26/01/01,12:00:05+00"
modem.operator(name="Example Mobile", numeric="26001", short="EXM", act=7)
```

The examples read the clock 5 s after a `board.power_on()` at the default world time, 12:00:00
UTC on 2026-01-01. `AT+CCLK?` tells the world's time, the one NTP tells behind `modem.network()`,
at the instant of the command, plus `zone` and `wrong_by`, cut to the whole second. The zone is in
quarters of an hour, `+08` for `hours(2)`, and `+00`, never `-00`, for none. The model starts with
the world's time in zone `+00`.

`clock_not_set` gives a clock the network has not set: `AT+CCLK?` tells `reads`, a `datetime` with a
timezone, as of the call, plus the time since then, in `reads`' own timezone, with no zone in the
answer. No document gives the clock's value before the network sets it, so `reads` has no default.
The manuals ([A76XX V1.12](https://eicut.com/wp-content/uploads/2025/10/A76XX_Series_AT_Command_Manual_V1.12.pdf),
section 3.2.10; [SIM7500/SIM7600 V3.00](https://ws.dfrobot.com.cn/Ftbi-gQP6fuk5I1SigJuHrOtnrxq),
section 3.2.17) say only that the factory time zone is invalid and that
`+CCLK?` returns the zone when it is valid: leaving the zone out is an inference. Setting the
clock with `AT+CCLK=` stays a fallback: it changes nothing.

`AT+COPS=3,<format>` chooses what `AT+COPS?` names: 0 `name`, 1 `short`, 2 `numeric`. The default
numeric code, `26000`, matches the model's IMSI, `260000000000000`. The format goes back to 0 at a
power cycle, like echo; the clock and the operator survive one, like signal and coverage.

A call that cannot hold raises and, as the first call, selects no model:

- `modem.clock: zone is 20 minutes: a whole number of quarters of an hour from -24 to +24 hours`
  (`ValueError`); the zone's range, -96 to 96 quarters, is the A76XX manual's;
- `modem.clock: zone is 2, not a Duration` (`TypeError`), and the same for `wrong_by`;
- `modem.clock_not_set: reads is datetime.datetime(2000, 1, 1, 0, 0), not a datetime with a
  timezone` (`TypeError`);
- `modem.operator: numeric is '2600', not 5 or 6 digits` (`ValueError`);
- `modem.operator: name is '', not text for quotes` (`ValueError`), and the same for `short`, for
  anything but non-empty printable text without a `"`: a line ending in it would end the answer;
- `modem.operator: act is True, not a whole number` (`ValueError`).

A `wrong_by` or `reads` that takes the date beyond what `datetime` holds is not refused at the call:
the `AT+CCLK?` that reaches it ends the run, and the waiting call raises `RunEnded`, whose message
names it, for example `the link failed: uart.modem.receive raised OverflowError: date value out of
range`.

## A hung modem and RESET

```python
modem.hang(pwrkey=False)
assert modem.hung
```

A hung modem answers nothing and passes no data, sends no `NO CARRIER`, and keeps RI high. Data
mode ends for the model, as with any loss: `data_mode` reads false, and with `modem.network()` PPP
ends at the hang, and the device's TCP connections wait for their keepalive, as with any loss. The
AT commands the firmware
sends stay in `commands`, with an empty `response`, and the report shows them as `(no answer)`, but
for one stuck to PPP bytes, which [is lost whole](#a-dropped-data-call). An answer the model gave
before the hang is already on its way, and still arrives within its 1 ms.
`hang` raises `SourceError: modem.hang: the modem is off` on a modem that is off, `SourceError:
modem.hang: the modem is hung already` on a hung one, whose `pwrkey` choice stays until a restart,
and `TypeError` for a `pwrkey` that is not `True` or `False`.

`hang(pwrkey=True)` leaves PWRKEY working: an off pulse switches the modem off, and the next on
pulse starts it, no longer hung. `hang(pwrkey=False)` ignores PWRKEY until a restart. Any restart
ends the hang: a RESET pulse, an off pulse where PWRKEY works, `lose_power()`, `board.power_off()`,
`modem.power_off()` and `modem.power_on()`.

`modem.reset` is active high at the ESP32 pad, with a threshold of 2000 ms. SIMCom's
[Hardware Design V1.03](https://files.waveshare.com/wiki/A7670E-Cat-1-GNSS-HAT/A7672X_A7670X_Series_Hardware_Design_V1.03.pdf),
section 3.2.3 and table 12, gives RESET active low at the module, 2 s minimum and 2.5 s typical;
LilyGO drives it [`MODEM_RESET_LEVEL HIGH`](https://github.com/Xinyuan-LilyGO/LilyGO-T-A76XX/blob/63af963038e93c91baf39d89a3a736d6bc26ff13/examples/ATdebug/utilities.h#L99-L105)
for [2600 ms](https://github.com/Xinyuan-LilyGO/LilyGO-T-A76XX/blob/63af963038e93c91baf39d89a3a736d6bc26ff13/examples/ATdebug/ATdebug.ino#L79-L85).
The pulse is judged at its release, as PWRKEY's is, and restarts a running modem in command mode,
with echo, query modes and the operator format at their power-on values. A shorter pulse does
nothing, and RESET on a modem that is off does nothing. RI stays high through a RESET restart.

`lines.board.power_enable` (GPIO 12) stays an observed line: driving it has no effect on the
modem. A firmware's recovery can pulse it, and a test can check the pulses with `lines.events()`.
`firmware/modem_recovery` [validates](validation-firmware-user.md#modem_recovery) a procedure on
this model: PWRKEY power-off unless RI reads low; GPIO 12 low for 1 s and back up; RESET high for
2.6 s, then low; 10 s later a PWRKEY power-on. With `hang(pwrkey=True)` PWRKEY recovers the modem
and RESET finds it off. With `hang(pwrkey=False)` RESET recovers it and the later short pulse
changes nothing.

No source says what PWRKEY does to a hung modem, or what state a modem is in during a RESET pulse.
These are the model's assumptions: PWRKEY works unless the test says otherwise, and a modem goes on
as it was until the pulse's release.

`restarts` holds one `ModemRestart(at, cause)` for each time a run of the modem ended, at the instant it
did. `cause` is `"reset"` (a RESET pulse), `"pwrkey"` (an off pulse), `"supply"` (`lose_power()` or
the board's supply going) or `"test"` (`modem.power_off()`, or `modem.power_on()` on a running
modem). Starting a modem that is off is no restart, and nor is a call before the board's first
instant, which says how the modem starts. A PWRKEY recovery, off then on, is one entry.

## A dropped data call

```python
modem.drop_call()  # stays registered, sends NO CARRIER, leaves data mode
modem.drop_call(terminate=True)  # first an LCP Terminate-Request
```

`drop_call` ends a data call while the modem stays registered. It sends `NO CARRIER`, 1 ms after the
test's instant, and returns to command mode. With `modem.network()`, PPP ends at that instant, as
with any loss, the device's TCP connections waiting for their keepalive, and a new dial negotiates
again. `lose_registration()` stays silent.

`terminate=True` offers an LCP Terminate-Request ([RFC 1661](https://www.rfc-editor.org/rfc/rfc1661),
section 5.5) just before `NO CARRIER`. No vendor document says whether a carrier or SIMCom sends one,
and none says when a real modem sends `NO CARRIER` unprompted: both are the test's choice, the first
off by default. What the firmware sends after the call has ended, such as lwIP's Terminate-Ack, is
noise on the command line, which the next newline clears. A command sent before that newline is part
of the noise and lost whole: it gets no answer and is not in `commands`, on a hung modem or a healthy
one, and on a healthy one the next `AT` gets its `OK`. `esp_modem` writes that newline when `+++` gets
no answer as it leaves data mode, and `esp_modem_sync` alone does not. The [A76XX manual](https://eicut.com/wp-content/uploads/2025/10/A76XX_Series_AT_Command_Manual_V1.12.pdf),
section 1.5.2, requires the `AT` prefix at the beginning of each command line, says `<CR>` ends one,
and does not say what happens to bytes before the prefix. Losing the line, and a lone newline clearing
it, are the model's assumptions.

It raises `SourceError: modem.drop_call: the modem is not in data mode` with no call to drop, as on
a hung modem; `SourceError: modem.drop_call: terminate needs modem.network(): no PPP` when
`terminate=True` has no network; `SourceError: modem.drop_call: terminate needs LCP open: it is not
yet` when PPP has not opened LCP, right after `CONNECT`; and `TypeError` for a `terminate` that is not
`True` or `False`. A refused call leaves the data call as it was.

What the firmware sees, read in `esp_modem` modem-v1.4.1 and in lwIP and esp_netif of ESP-IDF v5.3.6,
and shown by `firmware/modem_recovery` on that pin:

- In data mode `Netif::receive` hands every byte to `pppos_input`, where text without a valid frame
  raises no event. `esp_modem` looks for `NO CARRIER` only while the firmware itself leaves data
  mode (`exit_data`, `set_command_mode`). So `NO CARRIER` alone goes unnoticed: what it adds is that
  registration stays and AT works again. The firmware learns from its own timeout, such as a TCP
  or MQTT one, or from LCP echo, which is off by default.
- A Terminate-Request is the prompt signal. lwIP answers Terminate-Ack, takes the link down and,
  after `FSM_DEFTIMEOUT` (6 s), ends with `PPPERR_CONNECT`, on which esp_netif posts
  `IP_EVENT_PPP_LOST_IP`.
- `+++` with no answer in command mode is survivable: after a 5 s timeout `esp_modem` writes `\n` and
  `AT`, which also clears PPP bytes left in the command line. The parser needed no change for it.
- Under esp32sim the modem's answers reach `esp_modem` one byte per read, and `exit_data` looks for `NO
  CARRIER` within each read, so there it never finds it. A read on a real UART can hold the whole
  text, and by the code `exit_data` would then skip `+++`; that has not been measured. As measured
  with `modem_recovery`, whose `exit_data` stops listening 14 s into leaving data mode: a call
  dropped before that leaves `+++` with no answer, so `\n` and `AT` follow 5 s later; one dropped
  later has `NO CARRIER` answer `+++`, and `AT` goes out alone. Either way every command is answered
  and nothing restarts the modem.

## Mode changes and dials

```python
from esp32tl.units import seconds

change = board.wait_until(modem.next_mode_change("data"), within=seconds(60))
assert change.cause == "dial"
modem.drop_call()
dials = [c for c in modem.mode_changes if c.cause in ("dial", "no carrier")]
```

`mode_changes` holds one `ModeChange(at, mode, cause)` for each dial the modem answered and each
return to command mode, oldest first. `at` is a `Duration` on the board's clock, `mode` is
`"command"` or `"data"`, and `cause` says why:

| `cause` | `mode` | What happened | `at` |
|---|---|---|---|
| `"dial"` | `"data"` | A dial ([above](#at-dialogue)) answered `CONNECT` | The dial command's, as in `commands` |
| `"no carrier"` | `"command"` | A dial answered `NO CARRIER`, while unregistered: the modem stays in command mode | The dial command's |
| `"escape"` | `"command"` | `+++` took effect | Without a network, the UART chunk with its third plus; with `modem.network()`, the instant its `OK` is sent, a second after it |
| `"drop"` | `"command"` | `drop_call()` | The test's instant |
| `"hang"` | `"command"` | `hang()` in data mode | The test's instant |
| `"registration"` | `"command"` | `lose_registration()` in data mode | The test's instant |
| `"restart"` | `"command"` | A run of the modem ended in data mode; after the board's first instant, `restarts` says what ended it, at the same instant | The restart's |

So a test counts the firmware's PPP attempts by counting the dials, each with its result. Doing it
from `commands` is fragile: ESP-IDF's `esp_modem` sync step sends `AT` up to three times before it
gives up, and while the modem is in data mode those `AT`s are PPP payload, not commands.

- Each entry but `"no carrier"` is a change: a hang, a lost registration or a restart in command mode adds none, and a
  modem starts in command mode, so starting it adds none either.
- A dial on a hung modem gets no answer and adds none; it stays in `commands` as `(no answer)`.
- An escape that does not take effect adds none: with `modem.network()`, pluses within the second
  of silence on either side are PPP payload. `+++` in command mode is ignored and adds none.
- `+++` is never in `commands`, which holds AT command lines: an escape has no terminator, and with
  a network its `OK` is due a second after its last plus. Its entry here is its record.
- `AT+CFUN=0` and `=4` add none: the model reads AT commands only in command mode, since `+++` ends
  the call and there is no online command mode, so functionality never ends a call.

`next_mode_change(mode=None)` is a [described condition](pytest-plugin-user.md#described-conditions)
for the first entry after the call, or the first into `mode`: an entry into the other mode does not
satisfy it, and a later one into `mode` still can. Arm it before what changes the mode. A dial
refused with `NO CARRIER` is an entry into command mode, so `next_mode_change("command")` armed in
command mode is satisfied by a refused dial as well as by the end of a call; to wait for the end of
a call, wait for the dial first, with `next_mode_change("data")`, then arm it. The boundary
is `mode_changes` at the call; the condition consumes nothing and returns the same entry each time
it is asked. Its description, which a timed-out wait names, is `the modem's next mode change` or
`the modem's next change to data mode`. A `mode` that is none of `"command"`, `"data"` and `None`
raises `ValueError: modem.next_mode_change: mode is 'Data', not 'command', 'data' or None`; with no
model selected, as for the properties, `SourceError: uart.modem has no modem model: select it
before board.power_on()`.

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

The modem is on the board's supply. `board.power_off()` switches it off as `lose_power()` does: RI
drops, and PPP with its TCP sessions ends, and no PWRKEY pulse starts it while the board is off.
After the `board.power_on()` that follows, the modem stays off until the firmware's PWRKEY pulse
starts it, as on the board, whatever it was at the first power-on; the test's `modem.power_on()`
starts it too. The board's supply and the test's `lose_power()` are apart: a modem whose supply the
test took stays without it through a power cycle of the board, until `restore_power()`, and a
`modem.power_on()` while the board is off starts it only once the board's power is back and
PWRKEY is pulsed. `board.reset()` resets the
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
