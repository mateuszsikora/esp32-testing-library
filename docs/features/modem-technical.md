# Cellular modem: how it works

## Data flow

`Esp32Sim._route` creates `interfaces.modem.Modem` for `uart.modem`, on the profile's wire.
`pytest_plugin.modem` returns that interface. Its first successful model call selects
`sources.model.modem.Modem`, after the backend's capability and before-power-on checks.
`Uart._give(prepare=...)` invokes the line attachment under its source-selection lock, after
checking for an existing source. A selection that loses to replay cannot claim RI. Refused model
parameters leave the route without a source. Replay, taps and byte logging use `Uart`.

The model accumulates command bytes through CR or LF, consuming the LF of CRLF once. It ignores
noncommands and consumes standalone `+++` in command mode, so firmware's initial escape cannot
leave a stale `OK` or prefix its next AT command. Completing a command stamps `ModemCommand` with the
incoming `uart_tx` chunk containing its terminator (the chunk's first-byte timestamp); replies
are stamped 1,000,000 ns later. A dial changes the parser in the same chunk, and a three-plus
escape changes it back, even across chunks. Command lines over `MAX_COMMAND = 4096` bytes are
discarded until a terminator. Echo defaults off; when enabled it preserves raw command bytes.
Only the history text decodes non-ASCII bytes with replacement. Registration test commands
(`AT+CREG=?` and its variants) list modes 0–2 without changing the selected mode.

`AT+CCLK?` is `Modem._cclk(t)`, with `t` the command's stamp. `world()`, which the interface passes
from the backend, is the world's time at power-on, the value NTP (`esp32tl.net.ntp`) adds the board's
clock to; `_cclk` adds `t`, `_wrong_by` and `_zone` quarters (`QUARTER_NS`) and floors to the
microsecond before `strftime` cuts it to the second. `f"{zone:+03d}"` writes the zone, so no zone is
`+00`. `_unset`, set by `clock_not_set`, is `(reads, now)`; while it is set the answer is `reads`
plus `t - now`, without a zone. `clock()` clears it. `AT+COPS=3[,...]` takes the part after the
first comma as `_format`, and anything but `0`, `1` or `2` gets `ERROR`, the format unchanged.
`_operator` holds the three names by format.

`supply(on)`, which the backend calls through `Uart.supply` at `board.power_off()` and at the
`power_on()` after it, sets `_board`, the board's supply, apart from `_supply`, the one the test
takes with `lose_power()` and gives back with `restore_power()` or `power_on()`. Off, it powers a
running modem down as `lose_power()` does; back, it starts nothing. A PWRKEY pulse starts the
modem only with both, and `power_on()` with `_board` off restores `_supply` and starts nothing.

The model keeps every `ModemCommand` under an RLock. `commands` returns a snapshot. With the modem
model, `Esp32Sim.report` appends the command section; `properties` adds all commands to the modem
route. Both check the route's source by type, so a model of the test's on `uart.modem`
(`Uart.model`) adds neither. The rolling wire log still keeps only its normal last entries. Raw
data-mode payloads are available as a snapshot through `modem_model().data_received`, the route's
`ModemModel`; it was `model()` until `Uart.model(source)` took that name.

`_mode_changes` holds every `ModeChange` under the same lock, and `mode_changes` returns it as a
tuple. `_command` appends a dial's entry, `("data", "dial")` or `("command", "no carrier")`, stamped
`t`, the same stamp as the dial's `ModemCommand`; a hung modem returns before it. Every end of data
mode goes through `_end_data(cause, at=None)`, which appends `("command", cause)` only when
`data_mode` was set, stamped `at`, or `now()` when the caller gives none (`hang`,
`lose_registration`). Its callers are the escape, at the chunk's `t` in `receive` without a
network and at `poll`'s `now` with one, `drop_call` (`"drop"`), `hang`, `lose_registration`
(`"registration"`) and `_set_power` (`"restart"`, at the instant it also stamps a `ModemRestart`
with, after the board's first instant; before it, `_set_power` stamps none). `AT+CFUN=0` and `=4`
called it too; the parser reads them only in command mode, where `data_mode` is already off and an
`Internet` already stopped by what ended the call, so they no longer do. A `+++` is never a `ModemCommand`: [the decision](../decisions/harness.md#an-escape-is-a-mode-change-not-a-modem-command).

## Schema

The model reads PWRKEY's `active`, `on_ms` and `off_ms` fields and RESET's `active` and `pulse_ms`,
the profile's `LineRoute`s it takes as `pwrkey` and `reset`, from the [profile schema](board-profiles-technical.md#pulse-timing-on-a-line). The
modem guide documents [PWRKEY's](modem-user.md#pwrkey-and-reporting) and
[RESET's](modem-user.md#a-hung-modem-and-reset) LilyGO thresholds and their hardware sources.
No protocol or recording-format version changes: UART bytes and GPIO input use version 1.

`ModemRestart` is frozen: `at: Duration`, `cause`: `"reset"`, `"pwrkey"`, `"supply"` or `"test"`.
`ModeChange` is frozen: `at: Duration`, `mode`: `"command"` or `"data"` (`Mode`), `cause`: `"dial"`,
`"escape"`, `"no carrier"`, `"drop"`, `"hang"`, `"registration"` or `"restart"` (`ModeCause`).
`ModemCommand` is frozen: `at: Duration`, `text: str`, `response: bytes`, `fallback: bool`. The JSON
report converts time to `t_ns` and the reply to `response` text, replacing non-ASCII bytes in
JSON. The `ModemCommand.response` and tap retain the original bytes. The modem identity replies
are `ESP32TL-MODEM`, `ESP32TL 1.0`, IMEI `350000000000000`, IMSI `260000000000000`. The default
operator is `ESP32TL`, numeric `26000`, access technology 7.

## Interfaces

`Lines.attach(owner, output, watch, latency)` reserves the model's output and observes firmware-driven
inputs: `watch` maps each line's name to its callback, `modem.pwrkey` to `Modem.driven`, and
`modem.reset`, when the profile names it, to `Modem.reset_driven`. It refuses an output already driven by the test or claimed by a model.
`Lines` remains the sole link interface owning GPIOs; it calls observers outside its lock.
`model_drive` queues RI through `Lines.inputs`. `quiet_until` bounds the next turn by the
earliest pending RI change; only changes due at that turn go to the runner. A newer power change
replaces the pending one on the same pin, so a test that removes power before an RI response
cancels that response. Model line events and `RunLog` entries (`<line> in`) are recorded only
when the change goes out. The model declares 1 ms latency for both its UART and its GPIO
reactions, bounding the runner's turn. Initial RI is sent in the first turn, after the board
pull. Model events use `by="model"`; events are returned sorted by time. The fixture keeps all
lines visible to the test, but direct drive/release of RI is refused.

PWRKEY starts timing on its active edge and compares the elapsed time on the inactive edge or
output disable. It takes the on/off threshold from the current power state and changes RI one
latency later. A lost supply prevents PWRKEY from restarting the modem. `Modem.reset_driven` times
RESET the same way against `pulse_ms`, and on a powered modem calls `_set_power(t + LATENCY_NS,
True, "reset")`. `_set_power` appends a `ModemRestart` when the modem was powered and `t` is after 0,
clears `hung` and sets `_pwrkey_works` again.

`hang` sets `hung` and `_pwrkey_works`, empties `_queued` and calls `_end_data`, so the parser is in command mode whatever the firmware sends next, and
`Internet.stop` ends PPP with no output, leaving the device's TCP connections half-open. In
`_command`, a hung modem appends
the `ModemCommand` with an empty response and returns nothing. PPP bytes the firmware goes on sending
are noise in the command line, as for any loss of data mode. `_command` sees that noise and an `AT`
stuck to it as one line that does not start with `AT`, and neither answers nor records it; clearing
`_line` in `_end_data` would not help, because the noise comes after it. Power transitions clear
partial commands, echo, registration query modes, the operator format, data mode and pulse state.
Signal, coverage, clock and operator survive a power cycle; functionality resets to 1.

The model's optional constructor `data: Source | None` accepts a data peer; `modem.network()`
installs `esp32tl.net.Internet` before power-on. The `broker` fixture selects it automatically.
In data mode `arrive` reaches an `Internet` before the test holds the turn
([the path](network-technical.md#the-path)), its payload reaches `data.receive`, and `poll`, `quiet_until` and `latency` delegate
to that source (latency remains at most 1 ms). With no peer, payload is retained and unanswered.
An `Internet` is reached by `arrive`, `poll` and `quiet_until` in command mode and with the modem
off too (`_polled`): its broker's keepalive ends the connections a lost call left half-open, and
nothing else would run that deadline; out of data mode its `poll` has nothing to send.
A network dial starts PPP after CONNECT; power and registration loss reset it,
clear queued replies and lose the test's TCP connections, leaving the device's half-open. Only
`drop_call` emits an unsolicited `NO CARRIER`:
registration and power loss stay silent. Network data mode guards `+++` with one second of silence before and after it;
`poll` completes the escape and `quiet_until` exposes its deadline. A broker deadline at the
escape's instant runs first, in `arrive`: a PUBACK due then is `"emitted"`, though ending data mode
drops its bytes before they reach the firmware, and a keepalive due then closes the connection as an
expiry. Other data sources retain the immediate escape recognizer. See
[network internals](network-technical.md).

`drop_call` runs in the test's thread, while the link is held. With `terminate`, it takes
`Internet.terminate(t)`'s output, an LCP Terminate-Request stamped one latency later, before
`_end_data` calls `Internet.stop`, which would clear it. It appends `NO CARRIER`, stamped the same,
to `_queued`, which `poll` returns ahead of anything else; the link polls in the same turn, right
after the hold returns, so `quiet_until` has no bound to give for it. `hang` and `_set_power` empty
`_queued`.

`Modem.next_mode_change(mode)` (the interface) reads `modem_model()` once, at the call, and returns
a `Condition` whose `find` is `FirstFrom(lambda: model.mode_changes, len(model.mode_changes),
wanted)`, as `board.next_boot` is built ([the decision](../decisions/harness.md#history-conditions-scan-from-where-their-last-poll-ended)).
`mode_changes` is copied at each poll, as `boots` is; it holds at most two entries per dial.

## Failure modes

- `uart.modem: the modem model needs pwrkey pulse timing and ri in the profile`: the model lacks
  its physical wiring or timing. Replays do not require those lines.
- `modem.ri is driven by the test already`: model selection conflicts with a test's RI drive.
- `modem.ri is driven by uart.modem`: direct test drive/release conflicts with the selected model.
- The refusals of `clock`, `clock_not_set` and `operator`, verbatim in
  [the guide](modem-user.md#clock-and-operator): they come from the model's method, which
  `_tell` calls on a new model before giving it to the route, so a refused first call selects none.
- `uart.modem: the modem model needs reset with pulse_ms in the profile, not on_ms and off_ms`: the
  model times RESET against one threshold, and the schema also lets a line have two. A `reset` with
  no timing is an observed line.
- `modem.drop_call: the modem is not in data mode`, `modem.drop_call: terminate needs
  modem.network(): no PPP`, `modem.drop_call: terminate needs LCP open: it is not yet` (`SourceError`), `modem.drop_call: terminate is 1, not True or False`
  (`TypeError`).
- `modem.next_mode_change: mode is 'Data', not 'command', 'data' or None` (`ValueError`).
- `modem.hang: the modem is off`, `modem.hang: the modem is hung already` (`SourceError`), `modem.hang: pwrkey is 1, not True or False`
  (`TypeError`).
- `uart.modem has its source already: one per test`: replay and model are mutually exclusive; a
  replay or a real source given after the modem model.
- `uart.modem replays a recording: it has no modem to describe; one source per test` (`has a real
  device`, `has a model of the test's` for the others): a model call, `network()` or a property
  read on a route with another source.
- `uart.modem has no modem model: select it before board.power_on()`: a property was read before
  any source was given.
- `uart.modem: the board is on already: give an interface its source before board.power_on()`
  (`the board has been on` after `board.power_off()`): first source selection was too late. Later calls on an existing model remain allowed.

## Tests

`tests/test_modem_model.py` checks every required command, fallbacks, ignored noise, chunking and
answer timestamps, the clock's time, zone and refusals, the operator's formats and refusals, a
hung modem, RESET's threshold and polarity, `restarts`, a dropped call on both paths, escape and data handoff, signal mapping, registration, power loss, profile
pulse timing, RI ownership, taps and unbounded command history, and a real `BoardLink` exchange.
It checks `mode_changes` for a dial with either result, an escape with and without a network and
one within the guard time, `drop_call`, `hang`, `lose_registration`, the test's power calls and a
PWRKEY and a RESET restart, and `next_mode_change`'s boundary, filter, description and refusals.
`test_a_broker_deadline_at_the_escapes_instant_runs_before_it` opens PPP and an MQTT connection
through the model's network, then puts a delayed PUBACK and a keepalive expiry each at the escape's
instant: `arrive` runs the deadline, and `poll` returns the escape's `OK` alone.
`test_the_keepalive_runs_out_with_the_call_ended` ends the call by lost registration, power off and
the escape, and finds the keepalive expiry in the modem's `quiet_until` and run by its `arrive`.
`tests/test_modem_fixture.py` runs `modem_at` on the LilyGO profile, checks physical PWRKEY and
RI, starts powered, off and after restored supply, changes signal/registration/power on the
test's clock, keeps the modem on through `board.reset()` and off after a power cycle until the
firmware's PWRKEY pulse (and `tests/test_modem_model.py` keeps the board's supply and the test's
`lose_power()` apart), reads the operator's code and a right, a wrong and an unset clock through
the firmware, compares two complete taps byte for byte, and checks missing-route and late-source
errors. `tests/test_modem_recovery.py` runs `firmware/modem_recovery` through `esp_modem` and PPP, and
recovers a hung modem with and without PWRKEY, also one that hangs while it dials, and redials after a dropped call on both paths. `tests/test_boards.py` checks timing validation. The validation firmware and tests have
no network dependency.

## See also

- [Using the modem](modem-user.md).
- [The lines interface](pytest-plugin-technical.md).
- [Harness decisions](../decisions/harness.md).
