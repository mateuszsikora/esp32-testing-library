# esp32-testing-library
Test unmodified ESP32 firmware through its wires: an emulated or real board, with modelled, replayed or real peripherals

## Getting started

From a clean checkout, one command sets everything up and runs the tests:

```sh
make test
```

That one command needs:

- Python 3.11 or newer.
- cargo (Rust), to build the runner crate in `runner/`.
- Network access to GitHub. The runner depends on
  [esp32sim](https://github.com/mikroverk/esp32sim) as a git dependency pinned by revision,
  because esp32sim is not on crates.io. A cold build of it takes minutes.

Building the validation firmware (`make firmware`) also needs Docker.

## Make targets

| Target | What it does |
|---|---|
| `make setup` | Creates `.venv`, installs `esp32tl` with its dev tools, builds the runner |
| `make build-runner` | Builds the runner crate only |
| `make firmware` | Builds the validation firmware in the pinned ESP-IDF container |
| `make test` | Runs `make setup`, then the pytest suite |
| `make lint` | `ruff check`, `ruff format --check` and `mypy` |
| `make hooks` | Installs the pre-commit hooks |

After `make setup`, `.venv/bin/pytest` runs the tests directly.

## Layout

| Path | Contents |
|---|---|
| `esp32tl/` | The Python package and its pytest plugin |
| `runner/` | `esp32tl-runner`, the Rust crate that runs esp32sim with the harness attached |
| `firmware/` | Firmware kept in this repository and the container build script |
| `recordings/` | Committed recordings used by the tests |
| `tests/` | The pytest suite |
| `docs/` | The implementation plan and format specifications |

See [docs/implementation-plan.md](docs/implementation-plan.md) for the design.
