# Examples

Small ESP-IDF applications tested through their wires with esp32tl. Each project has `main/`,
`sdkconfig.defaults` and readable `tests/`; all run in CI.

- [GPS alarm clock](gps_alarm_clock/): a destination in NVS, NMEA from a GNSS receiver, and a
  buzzer that sounds inside the radius. Start with [its tests](gps_alarm_clock/tests/test_alarm.py).
- [SD data logger](sd_data_logger/): one CSV row per GNSS fix, one file per date on an SPI SD
  card, with a storage-error output. Start with [its tests](sd_data_logger/tests/test_logger.py).

See the [examples guide](../docs/features/examples-user.md) for wiring, configuration and commands.

```sh
make setup
make examples
.venv/bin/pytest examples
```
