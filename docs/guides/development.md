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
  builds when it is missing: it pulls the ESP-IDF image and builds `hello_world`, `nmea_reader`,
  `gpio_echo`, `sdspi`, `sd_remount`, `modem_at`, `modem_recovery`, `nvs_reader`, `pppos_client`, `mqtt_session`, `http_request`, `persistence`, `ota_state` and the projects in `examples/`, about 25 minutes on the CI machine, hp600g1, because it starts a container for each image. `make firmware examples` first takes about 13: one container for each family, whose images share their compiler cache. What the firmware build expects of Docker,
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
| `make setup` | Creates `.venv`, installs `esp32tl` editable with its `dev` extra, builds the runner and puts the ROM ELF in `.cache/` |
| `make build-runner` | Builds the runner crate only, `cargo build --locked` in `runner/`, into `runner/target/release/`; `RUNNER_PROFILE=dev` builds it without fat LTO, into `runner/target/debug/` |
| `make rom-elf` | Runs `scripts/fetch-rom-elf`: puts `esp32_rev300_rom.elf` of esp-rom-elfs `20241011` at `.cache/esp-rom-elfs/20241011/`, checked by SHA-256 |
| `make test-runner` | The runner's Rust unit tests, `cargo test --locked`, in `RUNNER_PROFILE` like `build-runner`; `RUNNER_PROFILE=dev` skips the fat-LTO link while iterating |
| `make firmware` | Builds every validation image in the pinned ESP-IDF container, into `firmware/<image>/build/` |
| `make examples` | Builds every project discovered at `examples/*/CMakeLists.txt` in the pinned ESP-IDF container, into `examples/<name>/build/` |
| `make test` | Runs `make setup` and `make test-runner`, builds the validation images that are missing (`FIRMWARE_IMAGES`: `hello_world`, `nmea_reader`, `gpio_echo`, `sdspi`, `sd_remount`, `modem_at`, `modem_recovery`, `nvs_reader`, `pppos_client`, `mqtt_session`, `http_request`, `persistence` and `ota_state`), builds missing examples, then runs `.venv/bin/pytest` on both suites |
| `make determinism` | Runs `tests/test_determinism.py` twice through `scripts/check-determinism.py` and compares the two runs ([testing.md](testing.md#determinism)); builds the `nmea_reader`, `pppos_client`, `mqtt_session` and `http_request` images when missing and `.venv` when it is missing or stale, but not the runner or the ROM ELF: run `make setup` first |
| `make lint` | `ruff check`, `ruff format --check` and `mypy`; `cargo fmt --check` and `cargo clippy --locked --all-targets -- -D warnings` on the runner |
| `make hooks` | Installs the pre-commit hooks ([Pre-commit hooks](#pre-commit-hooks)) |
| `make docs-check` | Checks that the changes on this branch update the documentation they need ([documentation.md](documentation.md#the-check)); `BASE=REF` (passed as `--base REF`) compares with the merge base with `REF` rather than with the default branch |
| `make clean` | Removes `.venv`, `build/`, `dist/` and the mypy, ruff and pytest caches; keeps the runner build in `runner/target/` and `.cache/` |

`make test` counts an image as missing when its build directory has no `esp32tl-built`, the
file `firmware/build.sh` writes when a build finishes. `make firmware` builds them all again.
Both always write into `firmware/`, whatever `FIRMWARE_OUT` the shell exports, because the tests
read the images there.

`make lint`, `make hooks` and `make setup` create `.venv` first when it is missing or stale.
mypy checks `esp32tl/`, `tests/` and the example tests under `examples/`.
`make lint` does not build the runner, but clippy checks esp32sim from scratch on a clean
`runner/target/`.

`make clean` keeps `runner/target/` because the runner's build fetches esp32sim from the network,
and `.cache/` because the ROM ELF comes from it too.

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

`make rom-elf`, part of `make setup`, runs `scripts/fetch-rom-elf`. It puts
`esp32_rev300_rom.elf` of [esp-rom-elfs](https://github.com/espressif/esp-rom-elfs) release
`20241011` at `.cache/esp-rom-elfs/20241011/esp32_rev300_rom.elf`, the path the backend reads by
default:

1. A file already there whose SHA-256 matches `ROM_SHA256` in the script is kept; the script then
   prints nothing.
2. Otherwise it looks in an ESP-IDF install, under `$IDF_TOOLS_PATH/tools/esp-rom-elfs/`, or
   `~/.espressif/tools/esp-rom-elfs/` when `IDF_TOOLS_PATH` is unset: release `20241011` first,
   then any other release. Newer releases ship the same ELF, so the checksum of the ELF itself is
   the pin. A matching file is copied, not linked, because an ESP-IDF upgrade may remove the release
   it came from. One that differs is passed over with
   `fetch-rom-elf: <file> differs from release 20241011's (SHA-256 <sha>), skipped`.
3. Without one, it prints
   `fetch-rom-elf: no esp32_rev300_rom.elf matching release 20241011 under <dir>; downloading <url>`,
   downloads `esp-rom-elfs-20241011.tar.gz` from the GitHub release with `curl`, and checks the
   tarball against `TARBALL_SHA256` and the extracted ELF against `ROM_SHA256`. The download gets 20 s
   to connect and 120 s for each of 4 tries, about 8 minutes at worst, so that a stalled one fails
   instead of holding a CI job until its limit.

The target is phony: the script checks the checksum on every run. It runs from any directory and
always writes the repository's `.cache/`. Without `sha256sum` or `shasum` it stops with
`fetch-rom-elf: needs sha256sum or shasum`.

`esp32tl/backends/esp32sim.py` names the same release and file in `ROM_RELEASE` and `ROM_ELF`;
`test_rom_is_the_one_make_setup_fetches` in `tests/test_pytest_plugin.py` fails when the two
disagree.

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
| `firmware/` | `build.sh`, which builds the validation firmware, and `nmea_reader/`, `gpio_echo/`, `sd_remount/`, `modem_at/`, `modem_recovery/`, `nvs_reader/`, `persistence/` and `ota_state/`, the images kept here; each image's build lands in `firmware/<image>/build/` |
| `examples/` | Runnable ESP-IDF projects and their tests; see [examples](../features/examples-user.md) |
| `recordings/` | Committed recordings for the tests: `gnss-generated-1.jsonl` and `gnss-generated-2.jsonl`, which `scripts/generate-gnss-recordings.py` writes ([validation-firmware-user.md](../features/validation-firmware-user.md#the-gnss-recordings)) |
| `tests/` | The pytest suite ([testing.md](testing.md)) |
| `tools/` | Stage 0 measurement firmware (`cryptobench`, `deepsleep`, ESP-IDF projects) and `benchtime.py`, which times their runs ([stage0-findings.md](../stage0-findings.md#reproducing)) |
| `scripts/` | `fetch-rom-elf` (`make rom-elf`), `venv-tool` (the pre-commit hooks), `check-docs.py` (`make docs-check`), `decisions-index.py` (the decisions index), `generate-gnss-recordings.py` (the GNSS recordings in `recordings/`), `check-determinism.py` (`make determinism`) |
| `docs/` | The implementation plan, the link protocol, the Stage 0 findings and the CI description; `docs/README.md` says what lives where |
| `docs/features/` | One user and one technical document per feature |
| `docs/decisions/` | Why things are the way they are, one file per domain, with a generated index |
| `docs/guides/` | How to work on the repository: this guide, [testing.md](testing.md), [documentation.md](documentation.md) |
| `.github/` | The CI workflow (`workflows/ci.yml`) and the pull request template |
| `.claude/` | Claude Code's rules and the `create-pr` skill shared with Codex through `.agents/skills/` |
| `.agents/skills/` | Codex's repository skills; `create-pr` links to the shared skill under `.claude/skills/` |
| `AGENTS.md` | The contract for every agent working here; `CLAUDE.md` is a symlink to it |

Git ignores what the builds leave behind: `.venv/`, `.cache/` (the ROM ELF), `runner/target/`,
`firmware/**/build/`, the sources `firmware/build.sh` fetches or copies
(`firmware/.esp-protocols-*/`, `firmware/.http_request-*/`), `tools/*/build*/` and `build/`.

## See also

- [testing.md](testing.md): running this repository's tests.
- [docs/cicd.md](../cicd.md): what CI runs, and where.
- [validation-firmware-user.md](../features/validation-firmware-user.md): building the validation
  firmware, in the container or with a local ESP-IDF.
- [CONTRIBUTING.md](../../CONTRIBUTING.md): branch names, commit messages, pull requests.


### Network dependencies and firmware

`cryptography>=42` is a direct runtime dependency: `esp32tl.net.tls.CertificateAuthority`
generates per-test certificates. TLS itself uses standard-library `ssl`/MemoryBIO; no external
broker or network daemon is needed. `make firmware` and `make test` include `pppos_client`, `modem_recovery`, `mqtt_session` and
`http_request`; `make determinism` requires `pppos_client`, `mqtt_session` and `http_request` as well as `nmea_reader`. The first build of `pppos_client` fetches the pinned
esp-protocols source archive, and `modem_recovery` and `mqtt_session` fetch the same archive into their own build directories, verified by SHA-256, under `firmware/.esp-protocols-<commit>/`
(or `$FIRMWARE_OUT`). The preparation script uses Python's tarfile `data` extraction filter; the pinned ESP-IDF
container supplies a Python with that filter.
See [validation firmware](../features/validation-firmware-technical.md#pinned-esp-protocols-source).
`http_request`'s first build copies ESP-IDF's example to `firmware/.http_request-<ESP-IDF version>/`
and builds the copy, because its component manager writes `dependencies.lock` into the project
([validation firmware](../features/validation-firmware-technical.md#http_request)).
