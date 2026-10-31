# esp32-testing-library
Test unmodified ESP32 firmware through its wires: an emulated or real board, with modelled, replayed or real peripherals

## Getting started

From a clean checkout, one command sets everything up and runs the tests:

```sh
make test
```

That one command needs:

- Python 3.11 or newer.
- Rust through [rustup](https://rustup.rs), to build the runner crate in `runner/`.
  `runner/rust-toolchain.toml` pins the toolchain (Rust 1.99.1), and rustup installs it on
  the first build.
- Network access to GitHub. The runner depends on
  [esp32sim](https://github.com/mikroverk/esp32sim) as a git dependency pinned by revision,
  because esp32sim is not on crates.io. `make setup` also downloads the ESP32 mask ROM ELF
  from [esp-rom-elfs](https://github.com/espressif/esp-rom-elfs) release `20241011` into
  `.cache/`, unless an ESP-IDF install under `$IDF_TOOLS_PATH`, or `~/.espressif` when it is
  unset, already has the same file. It checks the file with `sha256sum` or `shasum`, and
  downloads it with `curl` and `tar`.

The tests also read the validation firmware, which `make test` builds when it is missing, so the
first `make test` needs Docker as well: it pulls the ESP-IDF image and builds `hello_world`, a
couple of minutes. Tests marked `requires_docker` need Docker too; without a reachable Docker
daemon, pytest skips them and says why. The firmware build expects Docker Desktop or a rootful
Docker Engine: under rootless Docker or Podman the files it writes belong to another uid, and
SELinux in enforcing mode keeps the container from reading the repository.

## Make targets

| Target | What it does |
|---|---|
| `make setup` | Creates `.venv`, installs `esp32tl` with its dev tools, builds the runner, puts the ROM ELF in `.cache/` |
| `make build-runner` | Builds the runner crate only, into `runner/target/release/`; `RUNNER_PROFILE=dev` builds it without fat LTO, into `runner/target/debug/` |
| `make rom-elf` | Puts `esp32_rev300_rom.elf` of esp-rom-elfs `20241011` at `.cache/esp-rom-elfs/20241011/`, checked by SHA-256 |
| `make test-runner` | The runner's Rust unit tests, in `RUNNER_PROFILE` like `build-runner`; `RUNNER_PROFILE=dev` skips the fat-LTO link while iterating |
| `make firmware` | Builds the validation firmware in the pinned ESP-IDF container, into `firmware/<image>/build/` |
| `make test` | Runs `make setup`, builds the validation images that are missing, then runs the runner's tests and the pytest suite |
| `make lint` | `ruff check`, `ruff format --check` and `mypy`; `cargo fmt --check` and `cargo clippy` on the runner |
| `make hooks` | Installs the pre-commit hooks (they run the tools from `.venv`) |
| `make clean` | Removes `.venv` and tool caches; keeps the runner build and `.cache/` |

After `make setup` and `make firmware`, `.venv/bin/pytest` runs the tests directly.

`make` creates `.venv` with `python3`. To use another interpreter, pass it on every call or
export it, for example `export PYTHON=python3.11`: when `pyproject.toml` changes, the next
`make setup` recreates `.venv` from scratch with whatever `PYTHON` is then.

## Validation firmware

`firmware/build.sh` (or `make firmware`) builds every validation image for the classic ESP32 in
`espressif/idf:v5.3.6`, pinned by digest; `firmware/build.sh hello_world` builds one. For now the
only image is ESP-IDF's own `examples/get-started/hello_world`, unmodified. Each lands in
`firmware/<image>/build/`, the layout pytest-embedded's `IdfApp` reads, with the
`flasher_args.json` that gives the flash offsets. `FIRMWARE_OUT=DIR` puts the images of
`firmware/build.sh` under `DIR` instead; `make firmware` and `make test` always use `firmware/`.
The first build pulls the image, 7 GB once unpacked.

To iterate without the container, source the `export.sh` of a local ESP-IDF and run
`IDF_LOCAL=1 firmware/build.sh`. That IDF need not be v5.3.6, so goldens come from the container
build only. Each build directory says how it was built in `esp32tl-build.json`, with
`"container": null` for a local build. Switching between the two rebuilds from scratch. The tests
accept a local build with a warning, and fail on one from a container other than the pinned one.

## Writing a test

```python
import pytest

pytestmark = pytest.mark.app_path("../firmware/hello_world")


def test_hello_world(console):
    console.expect("Hello world!")
```

A test that uses `board`, or `console`, which needs it, runs once per backend, with the backend
in its id: `test_hello_world[esp32sim]`. `esp32sim`, the runner on the emulated clock, is the only
backend so far; `--esp32tl-backend NAME` limits the run to one. `console.expect` is
pytest-embedded's `dut.expect`, with its arguments and its 30 s default timeout. Once the runner
has exited, an expect that has not matched fails at once with pexpect's `EOF`, and the console
ends with `esp32tl: the runner exited with code N`. The `EOF` carries that code and the runner's
last output, which pytest-embedded's "Bytes in current buffer" no longer shows: pexpect empties its
buffer at EOF. A runner that exits with an error on its own fails the test at teardown, even when
its expects passed; the teardown says nothing when an expect has already reported the exit.
`board.app` is the `IdfApp` the runner flashes; `dut.app` is pytest-embedded's base `App`, because
the backend runs no service.

The `app_path` marker names the ESP-IDF app, relative to the test file's directory, which is the
default; the backend runs the build in its `build/` directory, every file of `flasher_args.json`
at its offset. pytest-embedded's `--embedded-services`, `--app-path` and `--build-dir` are refused
when a test uses `board`, because esp32tl sets them itself, and so is `--count` above 1: a test
has one board.

The runner and the ROM ELF are those `make setup` leaves in `runner/target/release/` and
`.cache/`. `ESP32TL_RUNNER` and `ESP32TL_ROM` name others, for example the `RUNNER_PROFILE=dev`
build in `runner/target/debug/`. Without an editable install (`make setup` makes one), only those
two find them. A run stops after 300 emulated seconds if the test has not ended it before.

## CI

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs on every pull request and on `main`.
GitHub-hosted macOS runners have no Docker, so the work is split:

| Job | Runs on | What it does |
|---|---|---|
| `firmware` | self-hosted Linux | `make firmware` and the ROM ELF, once per content of `firmware/`, the workflow and the `Makefile` |
| `runner` | self-hosted Linux; `macos-latest` only when the repository is public | `make build-runner test-runner`, once per revision of the runner's sources, its esp32sim pin and its toolchain |
| `test` | self-hosted Linux; `macos-latest` only when the repository is public; Python 3.11, 3.13, 3.14 | Unpacks the images, the runner and the ROM ELF (fetched once by `firmware`) and runs pytest; builds nothing |
| `lint` | self-hosted Linux; Python 3.11 | `make lint`, clippy included |

The build jobs hand their output to the test jobs through the Actions cache, as tarballs under
`build/ci/`, keyed by what they were built from, the workflow and the `Makefile` included; a key
already in the cache is not built again. An entry unused for 7 days is evicted, so re-running only
the test jobs of an older run can fail on a cache miss: re-run all jobs. A bad entry stays until
`gh cache delete <key>` removes it.
The `requires_docker` tests run only on the self-hosted Linux runner, which has Docker; on macOS
they skip. The self-hosted runner is one machine, so its jobs queue one after another.
While the repository is private, the macOS jobs do not run: minutes on GitHub-hosted runners are
limited, and macOS ones count ten times. The workflow reads the repository's visibility, so they
come back once it is public.

## Pre-commit hooks

`make hooks` installs the hooks for every worktree of the clone, because git keeps hooks in the
shared `.git` directory. The ruff and mypy hooks run the tools from the `.venv` of the worktree
you commit from, so run `make setup` in each worktree before its first commit.

The installed hook also remembers the `.venv` of the worktree where `make hooks` ran. If that
worktree is removed, run `make hooks` again from another one.

## Layout

| Path | Contents |
|---|---|
| `esp32tl/` | The Python package and its pytest plugin |
| `runner/` | `esp32tl-runner`, the Rust crate that runs esp32sim with the harness attached |
| `firmware/` | Firmware kept in this repository and the container build script |
| `recordings/` | Committed recordings used by the tests |
| `tests/` | The pytest suite |
| `docs/` | The implementation plan and format specifications |

See [docs/implementation-plan.md](https://github.com/mateuszsikora/esp32-testing-library/blob/main/docs/implementation-plan.md) for the design.
