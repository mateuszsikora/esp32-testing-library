# Examples

Small ESP-IDF applications tested through their wires with esp32tl. Each project has `main/`,
`sdkconfig.defaults` and readable `tests/`; all run in CI.

- [GPS alarm clock](gps_alarm_clock/): a destination in NVS, NMEA from a GNSS receiver, and a
  buzzer that sounds inside the radius. Start with [its tests](gps_alarm_clock/tests/test_alarm.py).

See the [examples guide](../docs/features/examples-user.md) for wiring, configuration and commands.

```sh
make setup
make examples
.venv/bin/pytest examples
```
