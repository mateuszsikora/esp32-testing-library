# Flash contents: how it works

## Data flow

1. `Esp32Sim.nvs(namespaces, partition="nvs")` in `esp32tl/backends/esp32sim.py` refuses a
   powered board, then finds the one `partition-table.bin` in `IdfApp.flash_files`, which comes
   from the build's `flasher_args.json`. It reads the table only when NVS is requested.
2. `esp32tl.flash.partitions(table)` returns `Partition` records keyed by name. The backend
   resolves the requested name and requires type 1, subtype 2, flags 0.
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
   `_nvs` maps each partition name to `(offset, path)`; replacing a call replaces that mapping
   only after generation and writing succeed. No file from the build is modified.
5. At power-on `runner_command(extra_flash=tuple(self._nvs.values()))` adds these `--flash-at`
   arguments after all build files. The runner applies them in order. Teardown keeps the files;
   the runner does not write emulated flash changes back to them.

`set_up()` includes each NVS partition so the plugin's existing never-powered-on check covers
NVS too. `RunLog` records `board.nvs(partition='NAME')` at time zero, without keys or values.

## Schema

Partition records are 32-byte little-endian `<HBBII16sI`: magic `0x50aa`, type, subtype,
offset, size, 16-byte UTF-8 label (NUL terminated when shorter), flags. The reader accepts tables
with or without the MD5 record. That record is `eb eb`, fourteen `ff` bytes, and the MD5 digest
of all preceding entries. Everything after it must be erased (`ff`). Erased padding ends a table
without MD5. Invalid magic, truncated records, empty or duplicate names and invalid MD5 are
errors. Layout overlap/alignment validation remains the ESP-IDF build's responsibility.

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

`u8`, `u16`, `u32` and `i32` return immutable `Typed(type, value)` objects. Their constructors
validate integer type and range. `Namespaces` is a mapping from namespace strings to mappings
of key strings to `str | bytes | Typed`. The user-facing entry point is `board.nvs`; the reader,
value validator and image generator live in `esp32tl/flash.py`.

## Failure modes

The backend raises `BackendError`:

| Message | Cause |
|---|---|
| `board.nvs: write the flash before board.power_on()` | Runner already started |
| `board.nvs: the build must flash one partition-table.bin` | None or more than one matching build file |
| `board.nvs: cannot read PATH: ERROR` | File access or partition parsing failed |
| `the build has no partition 'NAME': it has NAMES` | Unknown name, with every available name in table order |
| `board.nvs: partition 'NAME' must be unencrypted, writable NVS` | Type/subtype is not data/NVS, or any flag is set |

Validation raises `TypeError` for unsupported values or non-integer constructor arguments,
`ValueError` for out-of-range integers, bad names, NUL in strings, invalid tables, bad partition
sizes and generator failures. Messages include:

- `NVS name NAME: use 1 to 15 UTF-8 bytes without NUL, CR or LF` (`NAME` is represented with `repr`).
- `NAMESPACE/KEY: an NVS string cannot contain NUL`.
- `NVS partition size must be a multiple of 4096 and at least 12288 bytes`.
- `NVS generator failed: ERROR` with the generator's exception message.
- `NVS generator wrote ACTUAL bytes, expected SIZE`.

Image file I/O errors propagate as `OSError`. No runner is started by `nvs()`.

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
`tests/test_nvs_reader.py` boots the actual `nvs_reader` firmware and verifies all six supported
types, Unicode and quotes, two configurations and the rejection after power-on.

## See also

- [User guide](flash-contents-user.md).
- [Backend](esp32sim-backend-technical.md).
- [Espressif's generator](https://github.com/espressif/esp-idf-nvs-partition-gen).
