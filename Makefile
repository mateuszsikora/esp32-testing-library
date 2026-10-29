PYTHON ?= python3
VENV ?= .venv
BIN := $(VENV)/bin

.PHONY: setup build-runner firmware test lint hooks clean

# Python environment, then the runner. `make test` runs this first, so a clean
# checkout needs one command.
setup: $(VENV)/.installed build-runner

$(VENV)/.installed: pyproject.toml
	$(PYTHON) -m venv $(VENV)
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

clean:
	rm -rf $(VENV) build dist .mypy_cache .ruff_cache .pytest_cache
