---
paths:
  - "esp32tl/**"
  - "runner/**"
  - "firmware/**"
  - "tools/**"
  - "scripts/**"
  - ".github/workflows/**"
  - "tests/conftest.py"
  - "Makefile"
  - "pyproject.toml"
  - ".pre-commit-config.yaml"
---

# Documentation contract

A behaviour change lands its documentation in the same change; removing behaviour removes its
documentation. This restates the table in `AGENTS.md`, which Codex reads too and which wins where
the two differ. How to write each document: `docs/guides/documentation.md`.

- `esp32tl/pytest_plugin.py`, `esp32tl/interfaces/` → `docs/features/pytest-plugin-*.md`
- `esp32tl/backends/` → `docs/features/esp32sim-backend-*.md`
- `esp32tl/cli.py`, `esp32tl/__init__.py` → `docs/features/cli-user.md`
- `runner/src/` (not the protocol files) → `docs/features/runner-*.md`
- `runner/Cargo.toml` → `docs/features/runner-technical.md`
- `esp32tl/link/`, `runner/src/protocol.rs`, `runner/src/socket_board.rs` → `docs/protocol.md`
- `firmware/` → `docs/features/validation-firmware-*.md`
- `esp32tl/boards/` → `docs/features/board-profiles-*.md`
- `esp32tl/recording.py` → `docs/recording-format.md`
- `esp32tl/sources/replay.py` → `docs/features/replay-*.md`
- `esp32tl/tap.py`, `esp32tl/report.py` → `docs/features/run-report-*.md`
- any other `esp32tl/` path → a new `docs/features/<feature>-*.md` and a row in the ownership map
- `.github/workflows/` → `docs/cicd.md`
- `Makefile`, `.pre-commit-config.yaml`, `scripts/fetch-rom-elf`, `scripts/venv-tool`,
  `runner/rust-toolchain.toml` → `docs/guides/development.md`
- `pyproject.toml` → development guide, testing guide, or the plugin or CLI doc, by section
- `tests/conftest.py` → `docs/guides/testing.md`
- `scripts/check-docs.py`, `scripts/decisions-index.py` → `AGENTS.md` and/or
  `docs/guides/documentation.md`; a change to the rules also changes the table in
  `docs/features/README.md`, which the check compares with the script
- `tools/` → Reproducing in `docs/stage0-findings.md`
- `scripts/generate-gnss-recordings.py` → `docs/features/validation-firmware-*.md`
- a rejected alternative or postponed scope → `docs/decisions/<domain>.md`, then
  `python3 scripts/decisions-index.py`
- a domain rule that a test enforces → `docs/invariants.md`

Test files and version-only bumps need nothing.

Verify before finishing: `make docs-check`
