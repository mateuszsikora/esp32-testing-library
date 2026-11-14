# Flash contents from a test

`board.nvs()` gives the firmware configuration before it boots, and `board.flash()` reads back what
the firmware has written. The test names a partition in the build's own table and supplies or reads
namespaces and keys. esp32tl knows their NVS types, not what those keys mean to a product.

## Supply NVS before power-on

```python
from esp32tl.flash import u16


def test_connects(board):
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
    board.console.expect_exact("connected")  # the firmware's own message
```

The app still comes from the [`app_path` marker](pytest-plugin-user.md). Its build must flash
`partition-table.bin`, as ESP-IDF builds do. `board.nvs(namespaces, partition="nvs")` resolves
`partition` by name in that table; use `partition="settings"` for another NVS partition.
No flash offset belongs in the test.

Each call supplies a **whole partition**, replacing any image supplied by the build. A later
successful call for the same partition replaces the earlier call, without merging keys. Calls
for different partitions are independent. An empty mapping creates empty NVS. The source build
is never edited; each run gets its own image beside its `dut.log`, retained after teardown.
Firmware changes to emulated flash are not saved back to that image:
[read them with `board.flash()`](#read-the-flash-of-a-run).

`board.nvs(...)` is supported on esp32sim only. On serial it skips with
`serial: NVS contents cannot be configured on this backend`, without generating or writing a
replacement partition.

## Choose the type the firmware reads

Import the integer constructors from `esp32tl.flash`:

| Python value | NVS type | Accepted values |
|---|---|---|
| `str` | string (`nvs_get_str`) | UTF-8, including multiline text; no NUL |
| `bytes` | binary blob (`nvs_get_blob`) | Arbitrary bytes |
| `u8(value)` | unsigned 8-bit integer | 0 to 255 |
| `i8(value)` | signed 8-bit integer | -128 to 127 |
| `u16(value)` | unsigned 16-bit integer | 0 to 65535 |
| `i16(value)` | signed 16-bit integer | -32768 to 32767 |
| `u32(value)` | unsigned 32-bit integer | 0 to 4294967295 |
| `i32(value)` | signed 32-bit integer | -2147483648 to 2147483647 |
| `u64(value)` | unsigned 64-bit integer | 0 to 18446744073709551615 |
| `i64(value)` | signed 64-bit integer | -9223372036854775808 to 9223372036854775807 |

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
  `TypeError: config/key: use str, bytes, u8(), i8(), u16(), i16(), u32(), i32(), u64() or i64(); a bare int has no NVS width`.
- A generator failure raises `ValueError` with `NVS generator failed: ERROR` and the generator's
  exception message.

A failed validation or generation leaves a previous successful image for that partition intact.
A passing test that sets up NVS without powering on fails teardown, for example:
`esp32sim: the test set up the NVS partition 'nvs' and never called board.power_on(): it ran no firmware`.
The run report records the call and partition name, without the configuration values.

## Read the flash of a run

`board.flash()` returns a `FlashSnapshot`, an immutable copy of the whole flash at this instant,
from which the test reads a partition by name, NVS by namespace and key, and the OTA state:

```python
from esp32tl.flash import u32


def test_counts_its_boots(board):
    board.power_on()
    board.console.expect_exact("ready")  # the firmware's own message
    snapshot = board.flash()
    assert snapshot.nvs()["persistence"]["boots"] == u32(2)
    settings = snapshot.partition("storage")  # the bytes of a partition in the build's table
    assert snapshot.ota().boot_partition == "ota_0"
```

A test can read it at any time from `board.power_on()` on, as often as it likes:

- **While the board stands still.** The board's time does not move, and neither does `board.now`:
  the runner writes its flash in the turn the test holds it at. At time 0, before the firmware's
  first instruction, the flash holds the build and the NVS the test supplied.
- **After `board.reset()` or `board.power_off()`.** The flash keeps what the firmware wrote through
  both, and reads the same with the supply off. `power_off()` comes into effect at the next wait,
  and a read in between sees the same flash.
- **At the end of a run.** After `RunEnded`, or with the runner gone for any reason short of a crash
  of its own, `board.flash()` reads what the run left, which covers what the firmware wrote after
  the test last looked. In a fixture's teardown it works until the `board` fixture's own teardown
  has run.

The firmware may be a little ahead of what the test has seen: the board stops at the end of a slice
of emulated time, up to a millisecond past the console line the test just read. So order a test on
what the firmware prints after it writes, not before.

`partition(name)` gives the bytes of a partition of the build's own table, whatever is in it.

`nvs(partition="nvs")` gives a new mapping each time, in the shape `board.nvs(...)` takes: strings as
`str`, blobs as `bytes`, integers as `Typed` with the type the firmware wrote them with, so
`u32(2)` is not `u8(2)`. A namespace with no key left reads as an empty mapping. What was erased is
gone. A power cut leaves a key written twice, or half-written, and `nvs` reads what `nvs_get_*`
would find on the next boot: the later write when it is whole, and never a string's or blob's data
as if it were entries. Two exceptions to `nvs_get_*`: NVS
keeps a key written with two types as two keys, and `nvs` reads the later one; and a string that is
not UTF-8 keeps its bytes as surrogates (`surrogateescape`) rather than failing the read of the
others.

`ota()` gives the two entries of the OTA data partition, in their order, and the app partition they
select now, as `esp_ota_get_boot_partition()` finds it:

| `OtaData` / `OtaEntry` field | What |
|---|---|
| `boot_partition` | The name of the app partition the entries select, as `esp_ota_get_boot_partition()` finds it: the slot of the highest valid `seq`; with no valid entry, the `factory` app, else the first OTA slot |
| `entries` | Two `OtaEntry`s, one for each sector of the partition |
| `seq` | `ota_seq`; `4294967295` when the sector is erased |
| `state` | `new`, `pending_verify`, `valid`, `invalid`, `aborted` or `undefined` (`esp_ota_img_states_t`); a value it does not know, in hex |
| `valid` | `bootloader_common_ota_select_valid`: its `seq` is set, its CRC holds, and its state is neither `invalid` nor `aborted` |

`boot_partition` is not always what boots next. With rollback on
(`CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE`), the bootloader marks an entry still `pending_verify` as
`aborted` when it starts, before it selects. So a reset before the new app has confirmed itself boots
the slot before it, though `ota()` named the new one just before that reset; read it again after the
boot to see the rollback. Likewise an entry `new` reads as `pending_verify` after the next boot.

The partition table comes from the build, and a firmware that rewrites its own table is not followed.
`board.flash()` is supported on esp32sim only. On serial it skips with
`serial: flash contents cannot be read on this backend`: esptool would put the chip in download mode
and break the run ([decision](../decisions/harness.md#a-real-board-does-not-read-its-flash-the-test-skips)).

### Errors when reading

- Before power-on: `BoardOff: board.flash: read the flash after board.power_on(): until then it holds the build and the test's NVS`.
- Missing partition: `ValueError: the build has no partition 'x': it has nvs, phy_init, factory`, as
  `board.nvs` says it. `ota()` on a build without one: `ValueError: the build has no OTA data partition: it has nvs, phy_init, factory`.
- `nvs()` on another kind of partition, or an encrypted one: `ValueError: board.flash: partition 'factory' is not unencrypted NVS`.
- A page of a newer NVS format than ESP-IDF v5.3.6 writes: `ValueError: NVS page at 0x1000: format 0xfd is newer than version 2`.
- The runner could not write the file: `BackendError: board.flash: the runner cannot write the flash: PATH: ERROR`.
- A runner that crashed and left no file, or a read after the `board` fixture's teardown:
  `BackendError: board.flash: the run left no flash to read: No such file or directory; its output is in LOG`.

Each read adds `board.flash()` to the run's report, without what it read.

## See also

- [Flash contents: how it works](flash-contents-technical.md).
- [Validation firmware](validation-firmware-user.md#ota_state): the firmware used to test OTA.
- [Validation firmware](validation-firmware-user.md#nvs_reader): the firmware used to test NVS.
- [The partition-name decision](../decisions/harness.md#flash-contents-besides-the-build-go-into-a-partition-of-the-builds-own-table-by-name).
