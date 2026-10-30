PYTHON ?= python3
# Fixed: the pre-commit hooks run the tools from .venv.
override VENV := .venv
BIN := $(VENV)/bin

.PHONY: setup build-runner firmware test lint hooks clean

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

# The runner crate (plan §4.2) builds against esp32sim pinned by revision.
build-runner:
	@if [ -f runner/Cargo.toml ]; then \
		cargo build --release --manifest-path runner/Cargo.toml; \
	else \
		echo "build-runner: runner/ has no crate yet, skipping"; \
	fi

# Validation firmware, built in the pinned ESP-IDF container (plan M0).
firmware:
	@if [ -x firmware/build.sh ]; then \
		firmware/build.sh; \
	else \
		echo "firmware: firmware/build.sh does not exist yet, skipping"; \
	fi

test: setup
	$(BIN)/pytest

lint: $(VENV)/.installed
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .
	$(BIN)/mypy

hooks: $(VENV)/.installed
	$(BIN)/pre-commit install

# Keeps runner/target/ and .cache/: a cold runner build takes minutes and the
# ROM ELFs come from the network.
clean:
	rm -rf $(VENV) build dist .mypy_cache .ruff_cache .pytest_cache
