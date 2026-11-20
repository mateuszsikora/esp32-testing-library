# Flash contents: how it works

## Data flow

1. `Esp32Sim.nvs(namespaces, partition="nvs", *, merge=False)` in `esp32tl/backends/esp32sim.py`
   refuses a powered board with `BoardOff`, and once the runner has started asks
   `HeldClock.check`, which raises `RunEnded` after the end of the run. It then reads the partition
   table where the build flashes it, `Build.table()`
   ([builds](builds-technical.md#data-flow)): up to `0xC00` bytes at `Build.partition_table`, from
   the last of the build's flash files that holds that offset. For an ESP-IDF build the offset is
   that of the `partition-table` entry of `flasher_args.json`, and the file `partition-table.bin`;
   for a PlatformIO build it is `0x8000`, in `partitions.bin` or in `firmware.factory.bin`. It
   reads the table only when NVS is requested.
2. `esp32tl.flash.partitions(table)` returns `Partition` records keyed by name. The backend
   resolves the requested name and requires type 1, subtype 2, flags 0.
   With `merge`, `_partition_now(entry)` gives the partition's bytes now, and `flash.nvs_values`
   reads them: before the first power-on, `0xff` over the partition's range with every flash file of
   the build, then every image in `_nvs`, copied over it where it overlaps, as the runner's
   `--flash-at` order would leave it; after it, the slice of the runner's flash that `_flash_now`
   reads through a `flash_dump`, as `flash()` does. `flash.nvs_types` first lists the keys that
   hold values of two types, which NVS keeps apart and `nvs_values` reads as the later one: one
   the test's mapping does not name is a `BackendError`, before anything is written. The test's
   namespaces are merged into those values key by key (`dict.update` per namespace), and the
   result goes to step 3 in place of the test's mapping.
3. `flash.nvs_image(namespaces, size)` opens an NVS writer on `io.BytesIO` through
   `generator.nvs_open(output, size - 4096, generator.Page.VERSION2)`. The size excludes the
   final reserved page, which the writer adds on close. `_name` validates each namespace and
   key; `_value` selects the encoding and validates each value. `NVS.write_namespace` and
   `NVS.write_entry` receive the names and values directly. Strings use `string`, bytes use
   `binary`, and typed integers keep their declared encoding and integer value. There is no
   CSV or temporary file, so CSV field limits do not restrict large blobs.
   These class methods encode entries without interpreting key names. The CLI and the
   module-level `write_entry` wrapper are not called: they special-case Wi-Fi names and add
   unrequested entries, even outside a Wi-Fi namespace. The dependency is pinned exactly
   because this adapter uses its Python API and a version-specific key adapter (below).
   `InputError` and `InsufficientSizeError` become `ValueError` with the original error as cause.
   Success must produce exactly the partition's size; this check also detects malformed output.
4. The backend writes the image beside `dut.logfile` as `nvs-<offset in lowercase hex>.bin`,
   for example `nvs-9000.bin`. The offset avoids interpreting the partition label as a path.
   Before the first power-on, `_nvs` maps each partition name to `(offset, path)`; replacing a call
   replaces that mapping only after generation and writing succeed. No file from the build is
   modified.
5. At power-on `runner_command(extra_flash=tuple(self._nvs.values()))` adds these `--flash-at`
   arguments after all build files. The runner applies them in order. Teardown keeps the
   files; the runner does not write emulated flash changes back to them.
6. Once the runner has started, the board off, `HeldClock.write_flash(offset, path)` instead: as
   `dump_flash` does for a read (below), it waits for a held turn and hands `FlashWrite(offset,
   path)` to the link's thread through `_hold_turn`. `BoardLink._held` sends the turn's board inputs
   so far, the `power` of a `power_off()` in the same turn among them, then the `flash_write`, and
   remembers the turn and the answer it waits for in `_asking`. The runner writes the file into its
   flash and answers `flash_write_ack` ([protocol
   5.25](../protocol.md#525-flash_write-and-flash_write_ack-ext-278)), which
   `BoardLink._flash_answered` gives to `HeldClock.answered`. An `error` in it is a `BackendError`;
   no answer, the run having ended meanwhile, is `RunEnded` from `check`. The image is
   `nvs-<offset>-t<now>.bin`, `now` the held clock's in nanoseconds, with `-2`, `-3` and on before
   `.bin` for a later write in the same instant, so that the file of step 4, which the runner's
   `--flash-at` names, keeps what the run started with, and each write's image stays. `_nvs` is
   not touched: the runner read its `--flash-at` files at its start.

`set_up()` includes each NVS partition of `_nvs` so the plugin's existing never-powered-on check
covers NVS too. `RunLog` records `board.nvs(partition='NAME')`, with `, merge=True` when given, at
time zero before the first power-on and at the held clock's `now` after it, without keys or
values.

Reading goes the other way:

1. At the first `power_on`, `runner_command(flash_out=...)` passes `--flash-out` with `flash.bin`
   beside the link's socket, in `listen()`'s temporary directory, which `_close_link` removes.
2. `Esp32Sim.flash()` refuses a board not started, reads the build's table through
   `_partition_table`, and calls `HeldClock.dump_flash()` in the test's thread.
3. `dump_flash` waits for a held turn and sets `_asked` to `FlashDump()`. `_hold_turn`, on the
   link's thread, wakes and returns it from `turn`; `BoardLink._held` sends the turn's board inputs
   so far, then it, instead of ending the turn, and remembers the turn, and that it waits for
   `flash_dump_ack`, in `_asking`. So the dump of a merge in the turn of a `power_off()` comes after
   its `power`, and on the ESP32-S31 the runner first takes in what the guest wrote since its last
   reset ([protocol 5.21](../protocol.md#521-flash_dump-and-flash_dump_ack-ext-131)).
4. The runner writes the file and answers `flash_dump_ack` ([protocol
   5.21](../protocol.md#521-flash_dump-and-flash_dump_ack-ext-131)). `BoardLink._flash_answered`
   calls `HeldClock.answered(ack)`, which hands the answer to the test and holds the same turn on,
   and `self.turns` and `HeldClock._turns` stay as they were. What the interfaces have to send goes
   out with the turn's `wait`, as without the dump.
5. `flash()`, through `_flash_now`, reads the file into memory and removes it, so that a run whose
   runner dies later reports no file rather than an older one. Once the run has ended, `dump_flash`
   returns `None` and asks nothing: the file is what the runner wrote before its `stats`, and it
   stays for the next read. The waiter thread ends the clock only after the runner has exited. The
   one file from a turn that can be left then is that of a runner killed between its rename and the
   harness reading the answer, and it is the flash the run left: no `wait` went out, so the guest
   has not run since. `link.stats` cannot tell the two apart, because the end of a run with the peer
   gone writes the file and sends no `stats`.
6. `FlashSnapshot(contents, table)` keeps the bytes, as many as the runner's flash has: the
   `--flash-mb` the backend passed
   ([esp32sim-backend-technical.md](esp32sim-backend-technical.md#data-flow)),
   4 MiB without it, 16 MiB at most. Its `repr` leaves them out, or a failed assertion would print
   them all. `nvs_values` and `OtaEntry` are computed on each call.

## Schema

Partition records are 32-byte little-endian `<HBBII16sI`: magic `0x50aa`, type, subtype,
offset, size, 16-byte UTF-8 label (NUL terminated when shorter), flags. The reader accepts tables
with or without the MD5 record. That record is `eb eb`, fourteen `ff` bytes, and the MD5 digest
of all preceding entries. Everything after it must be erased (`ff`). Erased padding ends a table
without MD5. Invalid magic, truncated records, empty or duplicate names and invalid MD5 are
errors. Layout overlap/alignment validation remains the ESP-IDF build's responsibility.

### What the NVS reader reads

`nvs_values` follows ESP-IDF v5.3.6's own loader (`components/nvs_flash/src/nvs_pagemanager.cpp`,
`nvs_page.cpp`, `nvs_storage.cpp`), not the generator: a page is 4096 bytes, a 32-byte header (`<IIB`:
state, sequence number, version, with a CRC at 28), a 32-byte state bitmap and 126 entries of 32 bytes.

- `_nvs_pages` keeps pages in state `ACTIVE`, `FULL` or `FREEING` whose header CRC holds, and sorts
  them by sequence number: NVS reuses sectors, so the place in the partition says nothing. With a
  page in `FREEING` the active page after it is a partial copy of it, which `PageManager::load`
  drops. A version byte below `0xfe` raises, as `Page::load` refuses it.
- `_nvs_items` keeps an entry the bitmap marks written (two bits per entry, `0b10`) whose CRC holds
  and whose header holds (`_header_holds`, `Item::checkHeaderConsistency` but for the blob index's
  largest size): an integer or a blob's index has a span of 1, an index has chunk `0xff` and a blob's
  chunk any other, and a string or chunk has the span its size needs, within the page. A header that
  does not hold, and a type NVS does not have, are one entry of nothing. A string or a blob's chunk
  also needs its whole span written and its data's CRC to hold; without them the item is gone and
  the reader still passes over its span, as `Page::eraseEntryAndSpan` erases it, so that its data is
  never read as entries. A power cut before the bitmap was marked, or an erase, leaves none.
- `nvs_values` takes the namespaces (namespace index 0, type `u8`) from every page first, since a
  key may sit on an earlier page than the namespace after a page is reclaimed; an item in a namespace
  nothing names is left out. Items then apply in order, so the later of two writes of a key wins,
  which is also what ESP-IDF's duplicate check leaves.
- A blob is its index (`0x48`: size, chunk count, chunk start, with a version bit, `0x80`, in the
  start) with its chunks (`0x42`) found by namespace, key and chunk index; an index whose chunks are
  not all there, or do not add up to its size, is nothing, as
  `Storage::eraseMismatchedBlobIndexes` has it, and an earlier index holds. The version 1 blob
  (`0x41`) reads as a string's span does, as bytes.
- Keys and strings decode with `surrogateescape`, so one that is not UTF-8 costs the test nothing
  else. A string stops at its first NUL.

The CRCs are `zlib.crc32(data, 0xFFFFFFFF)`, which is `esp_rom_crc32_le(0xffffffff, ...)`. While it
was written this reader was checked once against ESP-IDF's `nvs_partition_tool/nvs_parser.py`, on a
6-page partition that a firmware had rotated through sequence numbers 180 to 183 in physical pages 5,
1, 2 and 3: the two agreed on every key. It is not a dependency; the rules above are tested with
pages written by hand.

### What `ota()` reads

The OTA data partition (type 1, subtype 0) holds one `esp_ota_select_entry_t` at the start of each of
its two sectors (`<I20sII`: `ota_seq`, `seq_label`, `ota_state`, `crc`; `esp_flash_partitions.h`).
`crc` covers `ota_seq` alone. `OtaEntry.valid` is `bootloader_common_ota_select_valid`.
`boot_partition` is `esp_ota_get_boot_partition`, not the bootloader's choice at its next start: the
highest `seq` among the valid entries gives the slot `(seq - 1) % count`, on 32 bits, `count` being
`esp_ota_get_app_partition_count`, the subtypes `0x10 + slot` from `ota_0` to the first the table does
not have; the partition is the one of subtype `0x10 + slot`. The bootloader first turns an entry in
`pending_verify` into `aborted` with rollback on (`bootloader_utility_get_selected_boot_partition`),
which `tests/test_ota_state.py` pins with a power cycle. With no slot or none valid,
`find_default_boot_partition`, the factory app, the first OTA slot, then the test app. It
does not read `seq_label`, which ESP-IDF does not use either.

Values reach the generator unchanged: binary values are not hex-encoded, strings undergo
no newline translation or comment filtering. CRLF, lines beginning with `#` and multiline
certificates are preserved. Every namespace and key name is literal, including `sta.ssid`,
`sta.pswd`, `ap.ssid`, `ap.passwd` and `ap.authmode`; no related entries are added.

`_NvsKey` is a private `str` subclass used only at the generator boundary. Its text and UTF-8
encoding stay unchanged, but `len(key)` is the UTF-8 byte count. Generator 0.3.0's
`Page.write_primitive_data` and `Page.write_varlen_data` assign `key.encode()` into
`entry_struct[8:8 + len(key)]`. Counting Unicode characters there expands the 32-byte header
and can corrupt neighbouring entries or extend a page. The adapter makes both slice lengths
agree, for namespace names as well as keys. It does not patch the installed module or alter
how the caller's strings behave. Regression tests check exact bytes and CRCs across pages.

## Interfaces

`u8`, `i8`, `u16`, `i16`, `u32`, `i32`, `u64` and `i64` return immutable `Typed(type, value)`
objects. Their constructors validate integer type and range. `Value` is `str | bytes | Typed`, and
`Namespaces` is a mapping from namespace strings to mappings of key strings to `Value`. The
user-facing entry points are `board.nvs` and `board.flash`; the table reader, value validator, image
generator, `nvs_values`, `FlashSnapshot`, `OtaData` and `OtaEntry` live in `esp32tl/flash.py`.

## Failure modes

The backend raises `BackendError`:

| Message | Cause |
|---|---|
| `board.nvs: the board is on: call board.power_off() first, and write the flash while it is off` (`BoardOff`) | The supply is on |
| `board.nvs: the runner cannot write the flash: ERROR` | `flash_write_ack` with an `error` |
| `board.nvs: the runner cannot dump the flash to merge over it: ERROR` | With `merge` between boots, `flash_dump_ack` with an `error` |
| `board.nvs: the run left no flash to read: STRERROR; its output is in LOG` | With `merge` after the run's first power-on, no flash file to read |
| `board.nvs: cannot read FILE: STRERROR` | With `merge` before power-on, a flash file of the build or an earlier image cannot be read |
| `board.nvs: NS/KEY holds a TYPE and a TYPE in partition 'NAME', which a merge cannot keep both of: give NS/KEY a value in the test's keys, or write without merge` | With `merge`, `flash.nvs_types` finds a key of two types the test's mapping does not name; nothing is written |
| `board.nvs: BUILD/flasher_args.json names no partition table` | An ESP-IDF build without a `partition-table` entry |
| `board.nvs: no file of BUILD is flashed at OFFSET` | None of the build's flash files holds the table's offset |
| `board.nvs: cannot read the partition table of BUILD at OFFSET: ERROR` | File access or partition parsing failed; `OFFSET` as `0x8000` |
| `the build has no partition 'NAME': it has NAMES` | Unknown name, with every available name in table order |
| `board.nvs: partition 'NAME' must be unencrypted, writable NVS` | Type/subtype is not data/NVS, or any flag is set |

Validation raises `TypeError` for unsupported values or non-integer constructor arguments,
`ValueError` for out-of-range integers, bad names, NUL in strings, invalid tables, bad partition
sizes and generator failures. Messages include:

- `NVS name NAME: use 1 to 15 UTF-8 bytes without NUL, CR or LF` (`NAME` is represented with `repr`).
- `NAMESPACE/KEY: an NVS string cannot contain NUL`.
- `NAMESPACE/KEY: an NVS string is written as UTF-8, and this one holds bytes that are not (surrogates): give the key a value of its own`:
  a `str` that does not encode as UTF-8, as `nvs_values` reads a string that is not; the generator
  appends the NUL to the `str` and encodes it strictly, so it cannot write such bytes.
- `NVS partition size must be a multiple of 4096 and at least 12288 bytes`.
- `NVS generator failed: ERROR` with the generator's exception message.
- `NVS generator wrote ACTUAL bytes, expected SIZE`.

Image file I/O errors propagate as `OSError`. No runner is started by `nvs()`.

Reading adds:

| Raised | Message | Cause |
|---|---|---|
| `BoardOff` | `board.flash: read the flash after board.power_on(): until then it holds the build and the test's NVS` | Runner not started |
| `BackendError` | `board.flash: BUILD/flasher_args.json names no partition table`, `board.flash: no file of BUILD is flashed at OFFSET`, `board.flash: cannot read the partition table of BUILD at OFFSET: ERROR` | As for `board.nvs` |
| `BackendError` | `board.flash: the runner cannot write the flash: ERROR` | `flash_dump_ack` with an `error` |
| `BackendError` | `board.flash: the run left no flash to read: ERROR; its output is in LOG` | No file: a runner that crashed (exit 101), that never connected, or a read after `close` |
| `ValueError` | `the build has no partition 'NAME': it has NAMES` | `partition` or `nvs` with an unknown name |
| `ValueError` | `the build has no OTA data partition: it has NAMES` | `ota()` without one |
| `ValueError` | `board.flash: partition 'NAME' is not unencrypted NVS` | `nvs` on another type or subtype, or with the encrypted flag; a read-only one reads |
| `ValueError` | `board.flash: partition 'NAME' ends past the flash, of N bytes` | A table that reaches past the runner's flash, whose size is the board profile's `flash_mb` or the build's image headers' |
| `ValueError` | `NVS page at OFFSET: format VERSION is newer than version 2` | A page version byte below `0xfe` |
| `ValueError` | `board.flash: the build has no app partition: it has NAMES` | `ota()` on a table with no app, with no valid entry |

NVS that holds rubbish is not an error: it reads as what the rules above leave, as it would at
`nvs_flash_init`. They raise `ValueError`, not `BackendError`, because `FlashSnapshot` lives in
`esp32tl/flash.py`, below the backends.

## Tests

`tests/test_flash.py` checks tables with/without MD5, flags, malformed input, the binary
representation of each value type, all integer boundaries, unsupported values, name limits, actual generator
output and failure, deterministic image bytes, non-default partition offsets, override order,
repeated calls, lifecycle errors and preservation of the previous image on generation failure.
`test_nvs_names_do_not_change_values_or_add_keys` reads the generated binary's entry headers
and string payloads independently of the generator. It checks the exact values and complete
ordered entry list across two namespaces, as well as repeatable images, for Wi-Fi-looking
keys and namespace names. These checks include CRLF, Unicode and comment-looking lines.
`test_large_blob_fits_without_a_csv_field_limit` reconstructs blobs of 65535, 65536, 65537 and
70000 bytes from their NVS chunks and verifies the blob index. The UTF-8 boundary tests cover
primitive keys before, at and after the final page slot, up to 15-byte names, a namespace in
the final slot, and Unicode string/blob keys on the following page. The independent
`nvs_entries` reader verifies page, entry and string/blob payload CRCs for these tests.
`test_generator_output_must_match_partition_size` retains the final output-size guard.
`tests/test_pytest_plugin.py::test_nvs_without_power_on_fails_teardown` covers the fixture guard.
`tests/test_nvs_reader.py` boots the actual `nvs_reader` firmware and verifies all ten supported
types, Unicode and quotes, two configurations and the rejection after power-on.
`tests/test_platformio_build.py::test_board_nvs_and_board_flash_find_the_table_of_either_build`
reads the table of a PlatformIO build from `partitions.bin` and from `firmware.factory.bin`, and
`test_nvs_reader_built_by_platformio_runs_as_it_is` boots `nvs_reader` from both kinds of
PlatformIO build with the test's NVS and reads it back with `board.flash()`.

`tests/test_flash.py` also reads NVS: `test_a_round_trip_through_the_generator_reads_what_went_in`
for all eight integer types, `test_a_blob_of_many_chunks_on_many_pages_round_trips`, and, on pages
written by hand, the state bitmap (`test_the_state_bitmap_decides_what_is_there`), page order
(`test_pages_are_read_by_sequence_number_not_by_their_place_in_the_partition`), repeated entries,
broken CRCs, inconsistent headers, spans and blobs, the data of a broken item never read as entries,
and the page being freed. The `test_ota_*` tests read OTA data written
by hand. `tests/test_flash_snapshot.py` boots `persistence` and reads `boots` after a reset, after a
power-off and after `RunEnded`, with `board.now` and the link's turns unmoved.
`tests/test_ota_state.py` boots `ota_state` and compares `ota()` with what the firmware prints at each
step, and through a power cycle before the app confirms itself. `tests/test_clock.py` and `tests/test_boardlink.py` test the exchange without an emulator.

## See also

- [User guide](flash-contents-user.md).
- [Backend](esp32sim-backend-technical.md).
- [Espressif's generator](https://github.com/espressif/esp-idf-nvs-partition-gen).
