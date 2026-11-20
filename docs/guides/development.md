# Working on esp32tl

For whoever works on this repository: what a machine needs, how to set it up, what each make
target does, and how the pre-commit hooks behave. Running the tests is
[testing.md](testing.md); CI is [docs/cicd.md](../cicd.md); writing documentation is
[documentation.md](documentation.md).

## Requirements

From a clean checkout, one command sets everything up and runs the tests:

```sh
make test
```

That one command needs:

- Python 3.11 or newer. `make` uses `python3` unless `PYTHON` names another interpreter
  ([Python and `.venv`](#python-and-venv)).
- Rust through [rustup](https://rustup.rs), to build the runner crate in `runner/`.
  `runner/rust-toolchain.toml` pins the toolchain, Rust 1.99.1 with clippy and rustfmt, and rustup
  installs it on the first build ([The Rust toolchain](#the-rust-toolchain)).
- Network access to GitHub. The runner depends on
  [esp32sim](https://github.com/mikroverk/esp32sim) as a git dependency pinned by revision in
  `runner/Cargo.toml`, because esp32sim is not on crates.io. `make setup` also fetches the ESP32
  mask ROM ELF ([The ROM ELF](#the-rom-elf)), with `sha256sum` or `shasum` to check it and `curl`
  and `tar` to download it.
- Docker, for the first `make test`. The tests read the validation firmware, which `make test`
  builds when it is missing: it pulls the ESP-IDF images (v5.3.6, and v5.5.4 and v6.1 for the
  ESP32-P4's and the ESP32-S31's images, about 8.5 GB each) and builds `hello_world`, `nmea_reader`,
  `gpio_echo`, `sdspi`, `sd_remount`, `modem_at`, `modem_recovery`, `nvs_reader`, `pppos_client`, `mqtt_session`, `http_request`, `http_server`, `persistence`, `ota_state`, `timer_wake`, `reset_reasons`, `deep_sleep`, `bt_spp_initiator`, `bt_spp_initiator_psram` and the projects in `examples/`, about 25 minutes on the CI machine, hp600g1, because it starts a container for each image. `make firmware examples` first takes about 13: one container for each family, whose images share their compiler cache. TODO(owner): both measured before `bt_spp_initiator` and `bt_spp_initiator_psram`, each of which compiles Bluedroid, the largest component of the images; measure again on hp600g1. It also builds `arduino_shell`, the Arduino-ESP32 image, in a pinned Python container with PlatformIO: its first build pulls that image, about 1 GB, and downloads PlatformIO Core, the platform, the framework and the toolchains into `.cache/platformio/`, 5.7 GB, which took 8m52s on an arm64 Mac; with those there, a build from scratch takes about 80 s. What the firmware build expects of Docker,
  and how to build without it, is in
  [validation-firmware-user.md](../features/validation-firmware-user.md).

- mtools (`mformat`, `mcopy`, `mdir`, `mtype`), for the SD card tests, which make and read FAT
  images with it: `brew install mtools` on macOS, `sudo apt-get install mtools` on Debian and
  Ubuntu. Without it those tests are skipped
  ([testing.md](testing.md#requires_docker-and-requires_mtools)).

After `make setup` and `make firmware examples`, `.venv/bin/pytest` runs the tests directly.

## Make targets

| Target | What it does |
|---|---|
| `make setup` | Creates `.venv`, installs `esp32tl` editable with its `dev` extra, builds the runner and puts the ROM ELFs in `.cache/` |
| `make build-runner` | Builds the runner crate only, `cargo build --locked` in `runner/`, into `runner/target/release/`; `RUNNER_PROFILE=dev` builds it without fat LTO, into `runner/target/debug/` |
| `make rom-elf` | Runs `scripts/fetch-rom-elf`: puts the ROM ELF of every chip the runner emulates, `esp32_rev300_rom.elf`, `esp32s3_rev0_rom.elf`, `esp32c3_rev3_rom.elf`, `esp32c6_rev0_rom.elf`, `esp32p4_rev300_rom.elf`, `esp32p4_rev0_rom.elf` and `esp32s31_rev0_rom.elf` of esp-rom-elfs `20260528`, at `.cache/esp-rom-elfs/20260528/`, each checked by SHA-256 |
| `make test-runner` | The runner's Rust unit tests, `cargo test --locked`, in `RUNNER_PROFILE` like `build-runner`; `RUNNER_PROFILE=dev` skips the fat-LTO link while iterating |
| `make firmware` | Builds every ESP-IDF validation image in the pinned ESP-IDF container, into `firmware/<image>/build/` (`firmware/build.sh`), then the images of the ESP32-S3, ESP32-C3, ESP32-C6, ESP32-P4 and ESP32-S31, into `firmware/<image>/build-<chip>/` (`firmware/build.sh --chips`; the P4's in the ESP-IDF v5.5.4 container and the S31's in the v6.1 one), then every PlatformIO image (`PLATFORMIO_IMAGES`: `arduino_shell`'s `esp32dev` and `esp32s3_usb` environments) in the pinned PlatformIO container, into `firmware/<image>/.pio/build/<env>/` (`firmware/build-platformio.sh`) |
| `make examples` | Builds every project discovered at `examples/*/CMakeLists.txt` in the pinned ESP-IDF container, into `examples/<name>/build/` |
| `make test` | Runs `make setup` and `make test-runner`, builds the validation images that are missing (`FIRMWARE_IMAGES`: `hello_world`, `nmea_reader`, `gpio_echo`, `sdspi`, `sd_remount`, `modem_at`, `modem_recovery`, `nvs_reader`, `pppos_client`, `mqtt_session`, `http_request`, `http_server`, `persistence`, `ota_state`, `timer_wake`, `reset_reasons`, `deep_sleep`, `bt_spp_initiator`, `bt_spp_initiator_psram` and `hello_world_8mb`), the chips' images that are missing (`CHIP_IMAGES`: `hello_world`, `console_repl`, `gpio_echo`, `nmea_reader`, `nvs_reader` and `http_request` for each of `esp32s3`, `esp32c3` and `esp32c6`, `hello_world_psram` for `esp32s3`, `gatt_server`, `gatt_client` and `gatt_security_server` for `esp32s3` and `esp32c3`, `hello_world_8mb` and `console_repl_usb` for each of the three, `hello_world`, `console_repl`, `console_repl_usb`, `gpio_echo`, `nvs_reader` and `hello_world_psram` for each of `esp32p4` and `esp32s31`, `hello_world_rev1` for `esp32p4`, and `reset_reasons` for each of the five) and the PlatformIO images that are missing (`PLATFORMIO_IMAGES`: `firmware/arduino_shell/.pio/build/esp32dev` and `firmware/arduino_shell/.pio/build/esp32s3_usb`), builds missing examples, then runs `.venv/bin/pytest` on both suites |
| `make determinism` | Runs `tests/test_determinism.py` twice through `scripts/check-determinism.py` and compares the two runs ([testing.md](testing.md#determinism)); builds the `nmea_reader`, `pppos_client`, `modem_recovery`, `mqtt_session`, `http_request`, `bt_spp_initiator` and `sd_remount` images, the PlatformIO `arduino_shell` image and the `position_tracker` example when missing and `.venv` when it is missing or stale, but not the runner or the ROM ELF: run `make setup` first. `test_sd_card_faults` needs mtools, and skips without it |
| `make lint` | `ruff check`, `ruff format --check` and `mypy`; `cargo fmt --check` and `cargo clippy --locked --all-targets -- -D warnings` on the runner |
| `make hooks` | Installs the pre-commit hooks ([Pre-commit hooks](#pre-commit-hooks)) |
| `make docs-check` | Checks that the changes on this branch update the documentation they need ([documentation.md](documentation.md#the-check)); `BASE=REF` (passed as `--base REF`) compares with the merge base with `REF` rather than with the default branch |
| `make clean` | Removes `.venv`, `build/`, `dist/` and the mypy, ruff and pytest caches; keeps the runner build in `runner/target/` and `.cache/` |

`make test` counts an image as missing when its build directory has no `esp32tl-built`, the
file `firmware/build.sh` and `firmware/build-platformio.sh` write when a build finishes: in
`firmware/<image>/build/`, `firmware/<image>/build-<chip>/` for a chip's image, or
`firmware/<image>/.pio/build/<env>/` for a PlatformIO image, `esp32dev` or `esp32s3_usb` for
`arduino_shell`; the `esp32s3_usb` build waits for the `esp32dev` one, even under `make -j`,
because the two share the project's `.pio/`.
`make firmware` builds them all again.
Both always write into `firmware/`, whatever `FIRMWARE_OUT` the shell exports, because the tests
read the images there.

`make lint`, `make hooks` and `make setup` create `.venv` first when it is missing or stale.
mypy checks `esp32tl/`, `tests/` and the example tests under `examples/`.
`make lint` does not build the runner, but clippy checks esp32sim from scratch on a clean
`runner/target/`.

`make clean` keeps `runner/target/` because the runner's build fetches esp32sim from the network,
and `.cache/` because the ROM ELF and PlatformIO's core directory, `.cache/platformio/`, come from it
too.

## Dependencies

`pyproject.toml` declares them; their versions are there, and nowhere in the documentation, so a
version-only bump needs no document ([documentation.md](documentation.md#the-check)).

| Package | Why | How it is pinned |
|---|---|---|
| `pytest` | The plugin is a pytest plugin, loaded through the `pytest11` entry point | Lower bound |
| `pytest-embedded` | `Dut`, `DuplicateStdoutPopen`, `MessageQueue`: the backend builds on them | Compatible release (`~=`) |
| `pytest-embedded-idf[serial]` | `IdfApp`, which reads a build's `flasher_args.json`. pytest-embedded declares no extras, so each service is a package of its own; the `serial` extra pulls `pytest-embedded-serial-esp` and, through it, `pytest-embedded-serial` | Compatible release (`~=`), the same as `pytest-embedded` |
| `pytest-timeout` | The wall-time timeout the plugin gives every test that uses `board`, which ends a run that hangs ([the test's timeout](../features/pytest-plugin-user.md#the-tests-timeout)); the plugin reads its settings through `pytest_timeout.get_env_settings` | Lower bound |
| `esp-idf-nvs-partition-gen` | Encodes NVS entries for `board.nvs()` through its Python API, without the CLI's interpretation of Wi-Fi keys ([flash contents](../features/flash-contents-user.md)) | Exact (`==`): API changes require checking entry contents and deterministic output |
| `pyserial` | The host serial port of a [real source](../features/real-source-technical.md); it comes through `pytest-embedded-serial` too, which also chooses its version | Lower bound |
| `bumble` | The Bluetooth stack of the peers in `esp32tl/bluetooth/`: their devices, and the virtual controller that `esp32tl/bluetooth/controller.py` extends ([bluetooth-peer-technical.md](../features/bluetooth-peer-technical.md)) | Exact (`==`): at 0.0.x any release may change the controller's internals that esp32tl overrides |

The `dev` extra, which `make setup` installs, adds the tools:

| Package | Why | How it is pinned |
|---|---|---|
| `ruff`, `mypy` | `make lint` and the hooks | Exact (`==`), so every worktree and CI lint alike |
| `types-pexpect` | pexpect's annotations, for mypy | Exact |
| `types-pyserial` | pyserial's annotations, for mypy | Exact |
| `pre-commit` | `make hooks` | Lower bound |
| `esptool` | Imported by the tests | Unpinned: `pytest-embedded-idf[serial]` brings it and chooses its version |

Packaging: `hatchling` builds the wheel, which holds `esp32tl/` only, with the `esp32tl` console
script (`esp32tl.cli:main`) and the `pytest11` entry point (`esp32tl.pytest_plugin`).

The PyPI metadata in `pyproject.toml` links to the repository, documentation index and issue
tracker. It identifies esp32tl as an alpha pytest plugin for embedded firmware testing on Linux
and macOS; the exact native runtime targets are in [runtime installation](../features/runtime-user.md).
`Requires-Python` remains `>=3.11`. Version classifiers list only the CI matrix's Python 3.11,
3.13 and 3.14, alongside Python 3 and Python 3 only. Keywords cover ESP32, esp32sim, pytest,
embedded firmware, simulation and testing. The author name comes from `LICENSE`, without an
email address. The SPDX license expression remains `MIT`, with `LICENSE` included and no
redundant license classifier. `README.md` is the distribution's Markdown description, so its
repository links must be absolute URLs that also work on PyPI.

For presentation changes, activate the virtual environment and install the packaging tools first;
`make setup` and the `dev` extra do not install them:

```sh
. .venv/bin/activate
python -m pip install build twine
```

Then build into an empty output directory with `python -m build --outdir DIR`
and run `python -m twine check --strict DIR/*`. Inspect the wheel's `.dist-info/METADATA` as well:
Twine checks description rendering, not link targets or installation instructions. The
[release workflow](../cicd.md#runtime-releases) publishes those verified distributions without
rebuilding them; package installation still needs explicit `esp32tl runtime prepare`.

## Python and `.venv`

`make` creates `.venv` with `python3`, after checking that it is 3.11 or newer:

```
esp32tl needs Python 3.11 or newer; run with PYTHON=python3.11 (or newer)
```

To use another interpreter, pass it on every call or export it, for example
`export PYTHON=python3.11`. When `pyproject.toml` changes, the next target that needs `.venv`
recreates it from scratch (`python -m venv --clear`) with whatever `PYTHON` is then, upgrades pip
and installs `-e '.[dev]'` again. `.venv/.installed` marks a finished install.

The path `.venv` is fixed: the pre-commit hooks run the tools from there.

## The Rust toolchain

`runner/rust-toolchain.toml` pins channel `1.99.1`, profile `minimal`, with `clippy` and
`rustfmt`. rustup reads that file from the directory cargo runs in, not through
`--manifest-path`, so every make target runs cargo in `runner/`. A toolchain that does not come
from rustup ignores the file; `rust-version` in `runner/Cargo.toml` states the real minimum, 1.95.

`RUNNER_PROFILE` (default `release`) selects the cargo profile of `make build-runner` and
`make test-runner`. `release` is what the tests run: fat LTO, one codegen unit. After a change to
the runner, a release build links everything again with fat LTO, about 10 s on an M2 Pro against
0.3 s in `dev`, which is optimised all the same because an unoptimised emulator is too slow to boot
an image. Both targets share the profile so that the tests reuse the build of esp32sim. The tests
use the release binary unless `ESP32TL_RUNNER` names another
([esp32sim-backend-user.md](../features/esp32sim-backend-user.md)).

cargo's own cache in `runner/target/` is the cache: esp32sim is built again only when its
revision in `runner/Cargo.toml` changes. `--locked` makes cargo refuse to change `Cargo.lock`.

## The ROM ELF

`make rom-elf`, part of `make setup`, runs `scripts/fetch-rom-elf`. It puts the mask ROM ELF of
every chip the runner emulates, from [esp-rom-elfs](https://github.com/espressif/esp-rom-elfs)
release `20260528`, at `.cache/esp-rom-elfs/20260528/`, the directory the backend reads by default:
`esp32_rev300_rom.elf` (the classic ESP32), `esp32s3_rev0_rom.elf`, `esp32c3_rev3_rom.elf`,
`esp32c6_rev0_rom.elf`, `esp32p4_rev300_rom.elf` and `esp32p4_rev0_rom.elf` (the ESP32-P4 rev ≥ 3
and rev < 3) and `esp32s31_rev0_rom.elf`. `ROMS` in the script pins each by its SHA-256, one line per ELF:

1. A file already there whose SHA-256 matches its line is kept; when every ELF is, the script
   prints nothing.
2. Each one still missing is looked for in an ESP-IDF install, under
   `$IDF_TOOLS_PATH/tools/esp-rom-elfs/`, or `~/.espressif/tools/esp-rom-elfs/` when
   `IDF_TOOLS_PATH` is unset: release `20260528` first, then any other release. Other releases ship
   the same ELFs where they ship them at all (`20241011` has all but `esp32p4_rev300_rom.elf` and
   `esp32s31_rev0_rom.elf`, byte for byte), so the checksum of each ELF is the pin. A matching file is copied, not linked,
   because an ESP-IDF upgrade may remove the release it came from:
   `fetch-rom-elf: <dest> copied from <file>`. One that differs is passed over with
   `fetch-rom-elf: <file> differs from release 20260528's (SHA-256 <sha>), skipped`.
3. When some are still missing, it prints
   `fetch-rom-elf: <elf> ... not under <dir>; downloading <url>`, downloads
   `esp-rom-elfs-20260528.tar.gz` from the GitHub release once with `curl`, checks the tarball
   against `TARBALL_SHA256` and each ELF it extracts against its line
   (`fetch-rom-elf: <dest> downloaded`). The download gets 20 s to connect and 120 s for each of 4
   tries, about 8 minutes at worst, so that a stalled one fails instead of holding a CI job until
   its limit.

The target is phony: the script checks the checksum on every run. It runs from any directory and
always writes the repository's `.cache/`. Without `sha256sum` or `shasum` it stops with
`fetch-rom-elf: needs sha256sum or shasum`.

`esp32tl/backends/esp32sim.py` names the same release in `ROM_RELEASE`, and `esp32tl/runtime.py`
the same ELFs and checksums in `ROMS`; `test_rom_is_the_one_make_setup_fetches` in
`tests/test_pytest_plugin.py` and `test_rom_pins_match_developer_setup` in `tests/test_runtime.py`
fail when they disagree.

## Pre-commit hooks

`make hooks` runs `pre-commit install` from `.venv`. It installs the hooks for every worktree of the
clone, because git keeps hooks in the shared `.git` directory.

| Hook | What it runs | On |
|---|---|---|
| `end-of-file-fixer`, `trailing-whitespace`, `check-toml`, `check-yaml` | [pre-commit-hooks](https://github.com/pre-commit/pre-commit-hooks), at the `rev` the config pins | Staged files; TOML and YAML files for the two checks |
| `ruff-check` | `ruff check --fix --force-exclude` from `.venv` | Staged Python files |
| `ruff-format` | `ruff format --force-exclude` from `.venv` | Staged Python files |
| `mypy` | `mypy` from `.venv`, over the files `[tool.mypy]` lists (`esp32tl`, `tests`) | Every commit, whatever it stages |
| `cargo-fmt` | `cargo fmt` in `runner/` | Commits that stage a `.rs` file under `runner/` |
| `cargo-clippy` | `cargo clippy --locked --all-targets -- -D warnings` in `runner/`, as `make lint` | Commits that stage a `.rs` file under `runner/`, `runner/Cargo.toml`, `runner/Cargo.lock` or `runner/rust-toolchain.toml` |

`end-of-file-fixer`, `trailing-whitespace`, `ruff-check`, `ruff-format` and `cargo-fmt` fix the
files they flag, and the commit stops so you can stage the fixes; `make lint` runs ruff and cargo
fmt in their checking forms. `cargo-clippy` fixes nothing: it stops the commit on a warning. It
takes 2 to 3 s the first time in a worktree, while it compiles esp32sim's crates to metadata (their
API, no code), and about 0.2 s after that. `make docs-check` is not a hook
([documentation.md](documentation.md#the-check) says why).

The ruff and mypy hooks run the tools through `scripts/venv-tool`, from the `.venv` of the
worktree you commit from, so their versions come from `pyproject.toml` only. Run `make setup` in
each worktree before its first commit; without it the hook fails with
`.venv/bin/ruff not found: run make setup in this worktree` (or `mypy` for that hook).

The installed hook also remembers the `.venv` of the worktree where `make hooks` ran, to start
pre-commit itself. If that worktree is removed, run `make hooks` again from another one.

## Agent skills

The repository's `create-pr` skill follows [CONTRIBUTING.md](../../CONTRIBUTING.md) when
opening a pull request or updating its title and description. In Codex, invoke `$create-pr`;
in Claude Code, invoke `/create-pr`. It takes no arguments and gathers context from the
conversation, branch and issue tracker.

Edit [`.claude/skills/create-pr/SKILL.md`](../../.claude/skills/create-pr/SKILL.md) when changing
the workflow. The [shared-skill decision](../decisions/docs.md#codex-discovers-create-pr-through-a-symlink-to-the-existing-skill)
describes how Codex discovers it. If the skill does not appear after the checkout changes,
restart Codex. The workflow needs authenticated `gh` access and network access to GitHub for
its preflight, push and PR operations; the session's permissions must allow those commands.

## Layout

| Path | Contents |
|---|---|
| `esp32tl/` | The Python package and its pytest plugin |
| `runner/` | `esp32tl-runner`, the Rust crate that runs esp32sim with the harness attached |
| `firmware/` | `build.sh`, which builds the ESP-IDF validation firmware, `build-platformio.sh`, which builds the PlatformIO images, `arduino_shell/`, the Arduino-ESP32 image whose builds land in `firmware/arduino_shell/.pio/build/esp32dev/` and, for the ESP32-S3 with its console on USB-Serial/JTAG, `.pio/build/esp32s3_usb/`, `console_repl_usb/`, and `nmea_reader/`, `gpio_echo/`, `sd_remount/`, `modem_at/`, `modem_recovery/`, `nvs_reader/`, `http_server/`, `persistence/`, `ota_state/`, `timer_wake/` and `reset_reasons/`, the images kept here; each image's build lands in `firmware/<image>/build/` |
| `examples/` | Runnable ESP-IDF projects and their tests; see [examples](../features/examples-user.md) |
| `recordings/` | Committed recordings for the tests: `gnss-generated-1.jsonl` and `gnss-generated-2.jsonl`, which `scripts/generate-gnss-recordings.py` writes ([validation-firmware-user.md](../features/validation-firmware-user.md#the-gnss-recordings)) |
| `tests/` | The pytest suite ([testing.md](testing.md)) |
| `tools/` | Stage 0 measurement firmware (`cryptobench`, `deepsleep`, ESP-IDF projects) and `benchtime.py`, which times their runs ([stage0-findings.md](../stage0-findings.md#reproducing)) |
| `scripts/` | `fetch-rom-elf` (`make rom-elf`), `venv-tool` (the pre-commit hooks), `check-docs.py` (`make docs-check`), `decisions-index.py` (the decisions index), `generate-gnss-recordings.py` (the GNSS recordings in `recordings/`), `check-determinism.py` (`make determinism`), `compare-backends.py` (the tests on both backends, [serial-backend-user.md](../features/serial-backend-user.md#comparing-the-two-backends)) |
| `docs/` | The implementation plan, the link protocol, the Stage 0 findings and the CI description; `docs/README.md` says what lives where |
| `docs/features/` | One user and one technical document per feature |
| `docs/decisions/` | Why things are the way they are, one file per domain, with a generated index |
| `docs/guides/` | How to work on the repository: this guide, [testing.md](testing.md), [documentation.md](documentation.md) |
| `.github/` | The CI workflow (`workflows/ci.yml`) and the pull request template |
| `.claude/` | Claude Code's rules and the `create-pr` skill shared with Codex through `.agents/skills/` |
| `.agents/skills/` | Codex's repository skills; `create-pr` links to the shared skill under `.claude/skills/` |
| `AGENTS.md` | The contract for every agent working here; `CLAUDE.md` is a symlink to it |

Git ignores what the builds leave behind: `.venv/`, `.cache/` (the ROM ELF and PlatformIO's core
directory), `runner/target/`, `firmware/**/build/`, `firmware/**/.pio/`, the sources
`firmware/build.sh` fetches or copies (`firmware/.esp-protocols-*/`, `firmware/.http_request-*/`),
`tools/*/build*/` and `build/`.

## See also

- [testing.md](testing.md): running this repository's tests.
- [docs/cicd.md](../cicd.md): what CI runs, and where.
- [validation-firmware-user.md](../features/validation-firmware-user.md): building the validation
  firmware, in the container or with a local ESP-IDF.
- [CONTRIBUTING.md](../../CONTRIBUTING.md): branch names, commit messages, pull requests.


### Network dependencies and firmware

`cryptography>=42` is a direct runtime dependency: `esp32tl.net.tls.CertificateAuthority`
generates per-test certificates, and the harness's TLS server takes its AEADs, ECDHE and RSA
signatures from it; no external broker or network daemon is needed. `make firmware` and `make test` include `pppos_client`, `modem_recovery`, `mqtt_session` and
`http_request`; `make determinism` requires `pppos_client`, `modem_recovery`, `mqtt_session`, `http_request`, `bt_spp_initiator` and `sd_remount` as well as `nmea_reader`, and the `position_tracker` example. The first build of `pppos_client` fetches the pinned
esp-protocols source archive, and `modem_recovery` and `mqtt_session` fetch the same archive into their own build directories, verified by SHA-256, under `firmware/.esp-protocols-<commit>/`
(or `$FIRMWARE_OUT`). The preparation script uses Python's tarfile `data` extraction filter; the pinned ESP-IDF
container supplies a Python with that filter.
See [validation firmware](../features/validation-firmware-technical.md#pinned-esp-protocols-source).
`http_request`'s first build copies ESP-IDF's example to `firmware/.http_request-<ESP-IDF version>/`
and builds the copy, because its component manager writes `dependencies.lock` into the project
([validation firmware](../features/validation-firmware-technical.md#http_request)).

`make firmware` and `make test` also build `console_repl`, the ESP-IDF UART0 command-line
validation image in `firmware/console_repl`, and, for the S3, C3 and C6, `console_repl_usb`, the
same REPL on the chip's USB-Serial/JTAG controller, in `firmware/console_repl_usb`.

The `dev` extra includes `pytest-xdist>=3.6` for the parallel-board subprocess regressions.
Consumers install `pytest-xdist` separately; esp32tl has no `xdist` extra.
