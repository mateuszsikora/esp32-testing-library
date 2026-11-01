# Instructions for coding agents

esp32tl tests unmodified ESP32 firmware through its wires: a pytest plugin (`esp32tl/`) drives
the firmware running in esp32sim through the runner crate (`runner/`). `CLAUDE.md` is a symlink to
this file, so Claude Code and Codex read the same instructions. Keep it under about 23 KiB: Codex
stops reading at 32 KiB without saying so. Reasoning goes into `docs/`, linked from here.

- What lives where: [`docs/README.md`](docs/README.md).
- Setting up, make targets, hooks: [`docs/guides/development.md`](docs/guides/development.md).
- The test suite: [`docs/guides/testing.md`](docs/guides/testing.md).
- Branches, commits, pull requests, labels: [`CONTRIBUTING.md`](CONTRIBUTING.md). Everything
  that lands on GitHub is in English; commits and pull requests carry no `Co-Authored-By:` or
  "Generated with" lines.
- Before deciding something, look it up in [`docs/decisions/`](docs/decisions/README.md).

## Documentation contract

Every commit and pull request that changes behaviour lands its documentation in the same change; a
behaviour change that leaves `docs/` untouched is incomplete. Removing behaviour removes its
documentation too. Nothing writes the documentation for you: write what is now true, with real
paths, names, defaults and messages quoted verbatim, and `TODO(owner): …` where you are not sure.

| You changed | You must also update |
|---|---|
| `esp32tl/pytest_plugin.py`, `esp32tl/interfaces/` | `docs/features/pytest-plugin-user.md` and/or `-technical.md` (F1, F2) |
| `esp32tl/backends/` | `docs/features/esp32sim-backend-user.md` and/or `-technical.md` (F3) |
| `esp32tl/cli.py`, `esp32tl/__init__.py` | `docs/features/cli-user.md` (F4, F5) |
| `runner/src/`, except the protocol files | `docs/features/runner-user.md` and/or `-technical.md` (F6) |
| `runner/Cargo.toml` (esp32sim pin, profiles) | `docs/features/runner-technical.md`; a pin bump also re-checks what `docs/protocol.md` cites (F7) |
| `esp32tl/link/`, `runner/src/protocol.rs`, `runner/src/socket_board.rs` | `docs/protocol.md`, and its version if the wire changes (F8, F9, F10) |
| `firmware/` | `docs/features/validation-firmware-user.md` and/or `-technical.md` (F11) |
| `esp32tl/boards/` | `docs/features/board-profiles-user.md` and/or `-technical.md` (F13) |
| `esp32tl/recording.py` | `docs/recording-format.md`, and its version if the format changes (F14) |
| `esp32tl/sources/replay.py` | `docs/features/replay-user.md` and/or `-technical.md` (F15) |
| `esp32tl/tap.py`, `esp32tl/report.py` | `docs/features/run-report-user.md` and/or `-technical.md` (F16) |
| Any other path under `esp32tl/` (the rest of `sources/`, a new module) | A new `docs/features/<feature>-user.md` / `-technical.md` (an edit to an existing one does not count), and a row in the ownership map (F12) |
| `.github/workflows/` | `docs/cicd.md` (C1) |
| `Makefile`, `.pre-commit-config.yaml`, `scripts/fetch-rom-elf`, `scripts/venv-tool`, `runner/rust-toolchain.toml` | `docs/guides/development.md` (C2 to C6) |
| `pyproject.toml` | Dependencies and packaging: `docs/guides/development.md`; `[tool.pytest]`: `docs/guides/testing.md`; entry points: the plugin or CLI doc (C7) |
| `tests/conftest.py` | `docs/guides/testing.md` (C8) |
| `scripts/check-docs.py`, `scripts/decisions-index.py` | This table and/or `docs/guides/documentation.md` (C9, C10) |
| `tools/` | Reproducing in `docs/stage0-findings.md`; measure again if results change (C11) |
| `scripts/generate-gnss-recordings.py` | `docs/features/validation-firmware-user.md` and/or `-technical.md` (C12) |
| A decision: an alternative rejected, scope postponed | `docs/decisions/<domain>.md`, then `python3 scripts/decisions-index.py` |
| A domain rule that a test enforces | `docs/invariants.md` |

Test files, and version bumps of an Action, a pre-commit hook or a Python dependency, need no
documentation. The rule ids are those of the ownership map in
[`docs/features/README.md`](docs/features/README.md#ownership-map), which `scripts/check-docs.py`
enforces; a change to the map changes the script, that table and this one together.

Verify before finishing: `make docs-check`. It compares with `origin/main`, else `main`;
`BASE=REF` names another base. It prints `✗ <message> [<rule id>]` for each part of the contract
the branch breaks. `SKIP_DOCS_CHECK=1` is for local
work in progress only; CI ignores it and runs the "Documentation contract" step on every pull
request. [`docs/guides/documentation.md`](docs/guides/documentation.md) says how to write each kind
of document, how the check works and what it cannot catch.

**Reviewing.** A behaviour change without its documentation is a Major finding. The check proves
only that a document changed: also check that what it now says is true of the code.

**Skills.** Skills that open, review or merge pull requests carry this contract, and so does any
such skill added later: opening a pull request runs `make docs-check` before pushing and fills the
Documentation section of the pull request template; a review treats missing documentation as a
Major finding and checks that the documentation is true; merging runs `make docs-check` after the
base branch is merged in, never only before. When a skill and this file or a document in `docs/`
disagree, the skill is wrong.
