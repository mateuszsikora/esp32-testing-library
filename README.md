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
  `.cache/`, unless an ESP-IDF install under `~/.espressif` (or `$IDF_TOOLS_PATH`) already
  has the same file; it also needs `curl` and `tar`.

Building the validation firmware (`make firmware`) also needs Docker.

## Make targets

| Target | What it does |
|---|---|
| `make setup` | Creates `.venv`, installs `esp32tl` with its dev tools, builds the runner, puts the ROM ELF in `.cache/` |
| `make build-runner` | Builds the runner crate only, into `runner/target/release/`; `RUNNER_PROFILE=dev` builds it without fat LTO, into `runner/target/debug/` |
| `make rom-elf` | Puts `esp32_rev300_rom.elf` of esp-rom-elfs `20241011` at `.cache/esp-rom-elfs/20241011/`, checked by SHA-256 |
| `make test-runner` | The runner's Rust unit tests, in `RUNNER_PROFILE` like `build-runner`; `RUNNER_PROFILE=dev` skips the fat-LTO link while iterating |
| `make firmware` | Builds the validation firmware in the pinned ESP-IDF container |
| `make test` | Runs `make setup`, then the runner's tests and the pytest suite |
| `make lint` | `ruff check`, `ruff format --check` and `mypy`; `cargo fmt --check` and `cargo clippy` on the runner |
| `make hooks` | Installs the pre-commit hooks (they run the tools from `.venv`) |
| `make clean` | Removes `.venv` and tool caches; keeps the runner build and `.cache/` |

After `make setup`, `.venv/bin/pytest` runs the tests directly.

`make` creates `.venv` with `python3`. To use another interpreter, pass it on every call or
export it, for example `export PYTHON=python3.11`: when `pyproject.toml` changes, the next
`make setup` recreates `.venv` from scratch with whatever `PYTHON` is then.

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
