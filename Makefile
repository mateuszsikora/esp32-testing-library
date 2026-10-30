PYTHON ?= python3
# Fixed: the pre-commit hooks run the tools from .venv.
override VENV := .venv
BIN := $(VENV)/bin

.PHONY: setup build-runner rom-elf test-runner firmware test lint hooks clean

# Python environment, the runner and the ROM ELF. `make test` runs this first,
# so a clean checkout needs one command.
setup: $(VENV)/.installed build-runner rom-elf

# --clear: a venv left behind by a failed run (say, created with too old a
# Python) would otherwise keep its interpreter.
$(VENV)/.installed: pyproject.toml
	@$(PYTHON) -c 'import sys; sys.exit(sys.version_info < (3, 11))' || \
		{ echo "esp32tl needs Python 3.11 or newer; run with PYTHON=python3.11 (or newer)" >&2; exit 1; }
	$(PYTHON) -m venv --clear $(VENV)
	$(BIN)/python -m pip install --upgrade pip
	$(BIN)/python -m pip install -e '.[dev]'
	touch $@

# The ESP32 mask ROM ELF, release 20241011 of espressif/esp-rom-elfs (plan M0),
# from ~/.espressif when ESP-IDF has it, from GitHub otherwise. The script
# checks the checksum on every run, so this target stays phony.
rom-elf:
	scripts/fetch-rom-elf

# The runner crate (plan §4.2), against esp32sim pinned by revision in
# runner/Cargo.toml. cargo's own cache in runner/target/ is the cache: esp32sim
# is built again only when that revision changes. RUNNER_PROFILE=dev builds
# without fat LTO, for local iteration; the binary lands in target/debug/.
# Commands run in runner/, because rustup takes the toolchain from
# runner/rust-toolchain.toml only there, not through --manifest-path.
RUNNER_PROFILE ?= release
build-runner:
	cd runner && cargo build --locked --profile $(RUNNER_PROFILE)

# In the same profile as build-runner, so the tests reuse its build of
# esp32sim instead of making a second one (in release, the test binary still
# goes through fat LTO; that costs a link, not a rebuild).
test-runner:
	cd runner && cargo test --locked --profile $(RUNNER_PROFILE)

# Validation firmware, built in the pinned ESP-IDF container (plan M0) into
# firmware/<image>/build/. IDF_LOCAL=1 builds with the ESP-IDF of the calling
# shell instead, for iteration; goldens come from the container build only.
# An empty FIRMWARE_OUT is firmware/, whatever the calling shell exports:
# the tests read the images there.
firmware:
	FIRMWARE_OUT= firmware/build.sh

# The images the tests read, built only when missing; build.sh writes
# esp32tl-built when a build finishes. The CI test jobs receive the images
# from the firmware job, so they build nothing (and the macOS ones have no
# Docker). `make firmware` rebuilds them.
FIRMWARE_IMAGES := hello_world
firmware/%/build/esp32tl-built:
	FIRMWARE_OUT= firmware/build.sh $*

test: setup test-runner $(FIRMWARE_IMAGES:%=firmware/%/build/esp32tl-built)
	$(BIN)/pytest

lint: $(VENV)/.installed
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .
	$(BIN)/mypy
	cd runner && cargo fmt --check
	cd runner && cargo clippy --locked --all-targets -- -D warnings

hooks: $(VENV)/.installed
	$(BIN)/pre-commit install

# Keeps runner/target/ and .cache/: the runner's build fetches esp32sim and
# the ROM ELFs come from the network.
clean:
	rm -rf $(VENV) build dist .mypy_cache .ruff_cache .pytest_cache
