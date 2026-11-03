# Features

What esp32tl does today, one feature per pair of documents: `-user.md` says what you can do with
it, `-technical.md` how it works. [The documentation guide](../guides/documentation.md) gives the
templates.

| Feature | User | Technical | Code |
|---|---|---|---|
| pytest plugin: the `board`, `console`, `gnss`, `modem`, `broker`, `lines`, `sd_card` and `wifi` fixtures, the `app_path` and `app_recording` markers, `--esp32tl-backend` | [user](pytest-plugin-user.md) | [technical](pytest-plugin-technical.md) | `esp32tl/pytest_plugin.py`, `esp32tl/interfaces/` |
| `esp32sim` backend: the runner under pytest-embedded, on the emulated clock | [user](esp32sim-backend-user.md) | [technical](esp32sim-backend-technical.md) | `esp32tl/backends/esp32sim.py`, `esp32tl/backends/clock.py` |
| `serial` backend: a real ESP32 through pytest-embedded, on the wall clock | [user](serial-backend-user.md) | [technical](serial-backend-technical.md) | `esp32tl/backends/serial.py`, `esp32tl/backends/__init__.py` |
| `esp32tl` command | [user](cli-user.md) | | `esp32tl/cli.py`, `esp32tl/__init__.py` |
| Runner: `esp32tl-runner`, esp32sim with the harness attached | [user](runner-user.md) | [technical](runner-technical.md) | `runner/src/`, `runner/Cargo.toml` |
| Runnable examples | [user](examples-user.md) | [technical](examples-technical.md) | `examples/` |
| Validation firmware and its container build | [user](validation-firmware-user.md) | [technical](validation-firmware-technical.md) | `firmware/` |
| Board profiles: `esp32-devkitc`, `gps-alarm-clock`, `lilygo-t-a7670g-r2`, `sd-data-logger`, the capability declaration, the `board_profile` marker | [user](board-profiles-user.md) | [technical](board-profiles-technical.md) | `esp32tl/boards/` |
| Link protocol between the runner and the harness: the runner's `--board` and the backend's `BoardLink` | | [protocol](../protocol.md) | `esp32tl/link/`, `runner/src/protocol.rs`, `runner/src/socket_board.rs` |
| Recording format: the `.jsonl` files a replay reads | | [format](../recording-format.md) | `esp32tl/recording.py` |
| Replay: a recording at the far end of an interface, `gnss.replay(path)` | [user](replay-user.md) | [technical](replay-technical.md) | `esp32tl/sources/replay.py` |
| Matching engine: AT, PMTK and UBX requests, recorded answers and timing | [user](matching-user.md) | [technical](matching-technical.md) | `esp32tl/matching.py` |
| Taps and the run report: `gnss.tap(path)`, the run on the board's clock in a failing test's report | [user](run-report-user.md) | [technical](run-report-technical.md) | `esp32tl/tap.py`, `esp32tl/report.py` |
| Models and units: the GNSS receiver a test describes, `gnss.fix(at=...)`, and quantities with their unit | [user](models-user.md) | [technical](models-technical.md) | `esp32tl/units.py`, `esp32tl/sources/model/` except `modem.py` |
| Bluetooth peer: a Classic device with SPP on Bumble's virtual link, answered by a model on the board's clock | [user](bluetooth-peer-user.md) | [technical](bluetooth-peer-technical.md) | `esp32tl/bluetooth/` |
| Cellular modem: AT dialogue, signal, registration, PWRKEY and RI | [user](modem-user.md) | [technical](modem-technical.md) | `esp32tl/sources/model/modem.py`, `esp32tl/interfaces/modem.py` |
| Flash contents: `board.nvs()` and typed integers, by partition name | [user](flash-contents-user.md) | [technical](flash-contents-technical.md) | `esp32tl/flash.py` |
| Simulated network: PPP or Ethernet with ARP and DHCP, IPv4, TCP/UDP, DNS, NTP, TLS, MQTT and HTTP | [user](network-user.md) | [technical](network-technical.md) | `esp32tl/net/` |

## Ownership map

Which document a change to a path must update. `make docs-check` enforces this table:
`RULES` in `scripts/check-docs.py` holds the same rules, and the check fails when the two differ.
Patterns are extended regular expressions on paths from the repository root; `\|` in a cell is
`|`. A rule fails when a changed path matches its source pattern and no changed path matches its
documentation pattern.

Before any rule runs, the check leaves out test files (`tests/test_*.py`, `*.test.*`, `*.spec.*`,
`runner/src/**/tests.rs`, `runner/tests/`) and files whose only change is a version bump of a
GitHub Action, a pre-commit hook or a Python dependency.
[The documentation guide](../guides/documentation.md#the-check) explains both.

| # | source path regex | doc path regex | message shown on failure |
|---|---|---|---|
| F1 | `^esp32tl/pytest_plugin\.py$` | `^docs/features/pytest-plugin-` | pytest plugin changed (fixtures, markers, options, parametrisation): update docs/features/pytest-plugin-*.md |
| F2 | `^esp32tl/interfaces/` | `^docs/features/pytest-plugin-` | Console interface changed: update docs/features/pytest-plugin-*.md |
| F3 | `^esp32tl/backends/(esp32sim\|clock)\.py$` | `^docs/features/esp32sim-backend-` | esp32sim backend changed (spawn, ROM and runner lookup, end of run, teardown): update docs/features/esp32sim-backend-*.md |
| F23 | `^esp32tl/backends/(serial\|__init__)\.py$` | `^docs/features/serial-backend-` | Serial backend or backend registry changed: update docs/features/serial-backend-*.md |
| F4 | `^esp32tl/cli\.py$` | `^docs/features/cli-user\.md$` | esp32tl command changed: update docs/features/cli-user.md |
| F5 | `^esp32tl/__init__\.py$` | `^docs/features/cli-user\.md$` | Package root changed (version shown by esp32tl --version): update docs/features/cli-user.md |
| F6 | `^runner/src/`, without the paths of F9 and F10 | `^docs/features/runner-` | Runner changed (flags, stop line, exit codes, run loop): update docs/features/runner-*.md |
| F7 | `^runner/Cargo\.toml$` | `^docs/features/runner-technical\.md$` | runner/Cargo.toml changed (esp32sim pin, profiles): update docs/features/runner-technical.md; a pin bump also re-checks the esp32sim lines docs/protocol.md cites |
| F8 | `^esp32tl/link/` | `^docs/protocol\.md$` | Link protocol changed: docs/protocol.md is the contract both peers build against; update it, and its version if the wire changes |
| F9 | `^runner/src/protocol\.rs$` | `^docs/protocol\.md$` | Link protocol changed in runner/src/protocol.rs: update docs/protocol.md, and its version if the wire changes |
| F10 | `^runner/src/socket_board\.rs$` | `^docs/protocol\.md$` | Link protocol changed in runner/src/socket_board.rs: update docs/protocol.md, and its version if the wire changes |
| F11 | `^firmware/` | `^docs/features/validation-firmware-` | Validation firmware or its build changed: update docs/features/validation-firmware-*.md |
| F12 | `^esp32tl/`, without the paths of F1 to F11, F13 to F23 and `esp32tl/py.typed` | `^docs/features/[a-z0-9-]+\.md$`, a new file only | This path has no owning doc yet: add its feature docs, a new docs/features/<feature>-user.md or -technical.md, and add it to the ownership map (scripts/check-docs.py, docs/features/README.md, AGENTS.md) |
| F13 | `^esp32tl/boards/` | `^docs/features/board-profiles-` | Board profiles or the capability declaration changed (schema, pins, sources, taps): update docs/features/board-profiles-*.md |
| F14 | `^esp32tl/recording\.py$` | `^docs/recording-format\.md$` | Recording format changed: docs/recording-format.md is its specification; update it, and its version if the format changes |
| F15 | `^esp32tl/sources/replay\.py$` | `^docs/features/replay-` | Replay changed (what it replays, when, what it refuses): update docs/features/replay-*.md |
| F16 | `^esp32tl/(tap\|report)\.py$` | `^docs/features/run-report-` | Taps or the run report changed (what a tap writes, what the report shows): update docs/features/run-report-*.md |
| F17 | `^esp32tl/(units\.py\|sources/model/(?!modem\.py$))` | `^docs/features/models-` | Models or units changed (quantities, the GNSS receiver, its sentences): update docs/features/models-*.md |
| F18 | `^esp32tl/bluetooth/` | `^docs/features/bluetooth-peer-` | Bluetooth peer changed (the air's clock, the controller, the SPP peer): update docs/features/bluetooth-peer-*.md |
| F19 | `^esp32tl/sources/model/modem\.py$` | `^docs/features/modem-` | Modem model changed (AT dialogue, power, registration): update docs/features/modem-*.md |
| F20 | `^esp32tl/flash\.py$` | `^docs/features/flash-contents-` | Flash contents changed (partition table, NVS types and generation): update docs/features/flash-contents-*.md |
| F21 | `^esp32tl/matching\.py$` | `^docs/features/matching-` | Matching engine changed (protocols, requests, timing): update docs/features/matching-*.md |
| F22 | `^esp32tl/net/` | `^docs/features/network-` | Simulated network changed: update docs/features/network-*.md |
| F24 | `^examples/` | `^docs/features/examples-` | Examples changed: update docs/features/examples-*.md |
| C1 | `^\.github/workflows/` | `^docs/cicd\.md$` | CI workflow changed: update docs/cicd.md |
| C2 | `^Makefile$` | `^docs/guides/development\.md$` | Makefile changed: update the make targets in docs/guides/development.md |
| C3 | `^\.pre-commit-config\.yaml$` | `^docs/guides/development\.md$` | Pre-commit hooks changed: update docs/guides/development.md |
| C4 | `^scripts/fetch-rom-elf$` | `^docs/guides/development\.md$` | ROM ELF setup changed: update docs/guides/development.md |
| C5 | `^scripts/venv-tool$` | `^docs/guides/development\.md$` | Hook runner changed: update docs/guides/development.md |
| C6 | `^runner/rust-toolchain\.toml$` | `^docs/guides/development\.md$` | Rust toolchain pin changed: update the requirements in docs/guides/development.md |
| C7 | `^pyproject\.toml$` | `^docs/(guides/development\.md\|guides/testing\.md\|features/pytest-plugin-\|features/cli-user\.md)` | pyproject.toml changed: dependencies and packaging go to docs/guides/development.md, [tool.pytest] to docs/guides/testing.md, entry points to the plugin or CLI doc |
| C8 | `^tests/conftest\.py$` | `^docs/guides/testing\.md$` | Test-suite configuration changed: update docs/guides/testing.md |
| C9 | `^scripts/check-docs\.py$` | `^(AGENTS\.md\|docs/guides/documentation\.md)$` | Docs check changed: update the table in AGENTS.md and/or docs/guides/documentation.md |
| C10 | `^scripts/decisions-index\.py$` | `^(AGENTS\.md\|docs/guides/documentation\.md)$` | Decisions index changed: update AGENTS.md and/or docs/guides/documentation.md |
| C11 | `^tools/` | `^docs/stage0-findings\.md$` | Benchmark tool changed: update Reproducing in docs/stage0-findings.md; re-measure if results change |
| C12 | `^scripts/generate-gnss-recordings\.py$` | `^docs/features/validation-firmware-` | GNSS recording generator changed: update docs/features/validation-firmware-*.md |
| C13 | `^scripts/check-determinism\.py$` | `^docs/guides/testing\.md$` | Determinism check changed: update docs/guides/testing.md |

Where one directory has several owners, the map lists its files by name: `scripts/` (C4, C5 for
the development guide, C9, C10 for the documentation guide, C12 for the validation firmware, C13
for the testing guide) and the runner's top level (F7 for `Cargo.toml`, C6 for the toolchain;
`Cargo.lock` is nobody's), and `esp32tl/sources/` (F15 for `replay.py`, F17 for `model/` except
`modem.py`, F19 for that modem; its other files are F12's until they have documents of their own).
`runner/src/` instead carves the protocol files out of F6, so that a new runner file is owned from
its first line. `pyproject.toml` has several owners in one file: C7 accepts any of their
documents, and review checks the choice.

Left out on purpose: test files, which describe behaviour rather than change it;
`runner/Cargo.lock`, whose pin that matters is in `Cargo.toml`; `recordings/`, test data (C12
owns the script that generates the GNSS ones); `esp32tl/py.typed`, `LICENSE` and `.gitignore`;
and the files that are documentation or rules themselves (`docs/`, `README.md`, `AGENTS.md`,
`CONTRIBUTING.md`, the pull request template, `.claude/`, `.agents/`). Build output is ignored by
git and never part of a change.
