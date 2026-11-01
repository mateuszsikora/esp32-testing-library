# Flash contents from a test

`board.nvs()` gives the firmware configuration before it boots. The test names a partition in
the build's own table and supplies namespaces and keys. esp32tl knows their NVS types, not what
those keys mean to a product.

## Supply NVS before power-on

```python
from esp32tl.flash import u16


def test_connects(board, console):
    board.nvs(
        {
            "config": {
                "broker_host": "broker.test",
                "broker_port": u16(8883),
                "broker_ca": b"certificate bytes",
            },
        }
    )
    board.power_on()
    console.expect_exact("connected")  # the firmware's own message
```

The app still comes from the [`app_path` marker](pytest-plugin-user.md). Its build must flash
`partition-table.bin`, as ESP-IDF builds do. `board.nvs(namespaces, partition="nvs")` resolves
`partition` by name in that table; use `partition="settings"` for another NVS partition.
No flash offset belongs in the test.

Each call supplies a **whole partition**, replacing any image supplied by the build. A later
successful call for the same partition replaces the earlier call, without merging keys. Calls
for different partitions are independent. An empty mapping creates empty NVS. The source build
is never edited; each run gets its own image beside its `dut.log`, retained after teardown.
Firmware changes to emulated flash are not saved back to that image.

## Choose the type the firmware reads

Import the integer constructors from `esp32tl.flash`:

| Python value | NVS type | Accepted values |
|---|---|---|
| `str` | string (`nvs_get_str`) | UTF-8, including multiline text; no NUL |
| `bytes` | binary blob (`nvs_get_blob`) | Arbitrary bytes |
| `u8(value)` | unsigned 8-bit integer | 0 to 255 |
| `u16(value)` | unsigned 16-bit integer | 0 to 65535 |
| `u32(value)` | unsigned 32-bit integer | 0 to 4294967295 |
| `i32(value)` | signed 32-bit integer | -2147483648 to 2147483647 |

A bare `int` is refused: NVS stores an integer's width, and the firmware must read that same
type. Booleans, floats, `None` and other objects are refused too. The constructors check their
range and accept only integers, excluding booleans.

Namespace and key names take 1 to 15 UTF-8 bytes, without NUL, CR or LF. Names are literal:
`sta.ssid` and `sta.pswd`, for example, store exactly the supplied values and add no other keys.
The partition must be unencrypted, writable NVS, at least 12288 bytes and a multiple of 4096 bytes.
The generator reports
values too large to fit. Blobs larger than 64 KiB are supported when they fit in the partition.
It is installed with esp32tl; no local ESP-IDF install is needed.

## Errors

- After power-on: `BackendError: board.nvs: write the flash before board.power_on()`.
- Missing partition: `BackendError: the build has no partition 'settings': it has nvs, phy_init, factory`
  (the available names come from that build).
- Wrong partition type or flags: `BackendError: board.nvs: partition 'factory' must be unencrypted, writable NVS`.
- Bare integer or unsupported value at `config/key`:
  `TypeError: config/key: use str, bytes, u8(), u16(), u32() or i32(); a bare int has no NVS width`.
- A generator failure raises `ValueError` with `NVS generator failed: ERROR` and the generator's
  exception message.

A failed validation or generation leaves a previous successful image for that partition intact.
A passing test that sets up NVS without powering on fails teardown, for example:
`esp32sim: the test set up the NVS partition 'nvs' and never called board.power_on(): it ran no firmware`.
The run report records the call and partition name, without the configuration values.

## See also

- [Flash contents: how it works](flash-contents-technical.md).
- [Validation firmware](validation-firmware-user.md#nvs_reader): the firmware used to test NVS.
- [The partition-name decision](../decisions/harness.md#flash-contents-besides-the-build-go-into-a-partition-of-the-builds-own-table-by-name).
