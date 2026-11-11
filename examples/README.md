# Examples

Small ESP-IDF applications tested through their wires with esp32tl. Each project has `main/`,
`sdkconfig.defaults` and readable `tests/`; all run in CI.

- [GPS alarm clock](gps_alarm_clock/): a destination in NVS, NMEA from a GNSS receiver, and a
  buzzer that sounds inside the radius. Start with [its tests](gps_alarm_clock/tests/test_alarm.py).
- [SD data logger](sd_data_logger/): one CSV row per GNSS fix, one file per date on an SPI SD
  card, with a storage-error output. Start with [its tests](sd_data_logger/tests/test_logger.py).

- [Position tracker](position_tracker/): GNSS positions and NTP time sent through a cellular
  modem to MQTT over TLS, with the broker CA in NVS. Start with
  [its tests](position_tracker/tests/test_tracker.py).
- [Periodic wake](periodic_wake/): deep sleep with a timer wake every 15 minutes, a count in
  RTC memory and a pulse on a pin at every boot; a day of wakes runs in seconds. Start with
  [its tests](periodic_wake/tests/test_periodic_wake.py).
- [OBD-II reader](obd_reader/): Bluetooth Classic SPP to an ELM327-compatible adapter, found by
  name and paired with a PIN, and the AT and OBD-II commands the vehicle's answers lead it to.
  Start with [its tests](obd_reader/tests/test_obd_reader.py).

See the [examples guide](../docs/features/examples-user.md) for wiring, configuration and commands.

```sh
make setup
make examples
.venv/bin/pytest examples
```
