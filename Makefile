PYTHON ?= python3
# Fixed: the pre-commit hooks run the tools from .venv.
override VENV := .venv
BIN := $(VENV)/bin

.PHONY: setup build-runner test-runner firmware test lint hooks clean

# Python environment, then the runner. `make test` runs this first, so a clean
# checkout needs one command.
setup: $(VENV)/.installed build-runner

# --clear: a venv left behind by a failed run (say, created with too old a
# Python) would otherwise keep its interpreter.
$(VENV)/.installed: pyproject.toml
	@$(PYTHON) -c 'import sys; sys.exit(sys.version_info < (3, 11))' || \
		{ echo "esp32tl needs Python 3.11 or newer; run with PYTHON=python3.11 (or newer)" >&2; exit 1; }
	$(PYTHON) -m venv --clear $(VENV)
	$(BIN)/python -m pip install --upgrade pip
	$(BIN)/python -m pip install -e '.[dev]'
	touch $@

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

# Validation firmware, built in the pinned ESP-IDF container (plan M0).
firmware:
	@if [ -x firmware/build.sh ]; then \
		firmware/build.sh; \
	else \
		echo "firmware: firmware/build.sh does not exist yet, skipping"; \
	fi

test: setup test-runner
	$(BIN)/pytest

lint: $(VENV)/.installed
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .
	$(BIN)/mypy
	cd runner && cargo fmt --check
	cd runner && cargo clippy --locked --all-targets -- -D warnings

hooks: $(VENV)/.installed
	$(BIN)/pre-commit install

# Keeps runner/target/ and .cache/: a cold runner build takes minutes and the
# ROM ELFs come from the network.
clean:
	rm -rf $(VENV) build dist .mypy_cache .ruff_cache .pytest_cache
