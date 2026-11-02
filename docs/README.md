# Documentation

What lives where. Every behaviour change updates the document that owns it, in the same change
([`AGENTS.md`](../AGENTS.md#documentation-contract)); [the documentation
guide](guides/documentation.md) says how to write each kind.

| Path | What it holds |
|---|---|
| [`features/`](features/README.md) | One pair of documents per feature: `-user.md`, what you can do with it, and `-technical.md`, how it works. Its README holds the ownership map that `make docs-check` enforces |
| [`features/examples-user.md`](features/examples-user.md) | Runnable examples: build commands, GPS alarm wiring, NVS configuration and tests |
| [`guides/development.md`](guides/development.md) | Requirements, setup, make targets, the Rust toolchain, the ROM ELF, pre-commit hooks, the repository layout |
| [`guides/testing.md`](guides/testing.md) | This repository's own test suite: how to run it, its configuration, what each test file needs |
| [`guides/documentation.md`](guides/documentation.md) | How to write each kind of document, how the documentation check works and what it cannot catch |
| [`cicd.md`](cicd.md) | The CI workflow: its jobs, where they run, how builds reach the test jobs |
| [`protocol.md`](protocol.md) | The link protocol between the runner and the harness, version 1: the contract both peers build against |
| [`recording-format.md`](recording-format.md) | The recording format, version 1: the `.jsonl` files a replay reads and the tools that record write |
| [`invariants.md`](invariants.md) | Numbered domain rules, each enforced by a named test |
| [`decisions/`](decisions/README.md) | Why things are the way they are: rejected alternatives and postponed scope, one file per domain, with a generated index |
| [`implementation-plan.md`](implementation-plan.md) | The design and its milestones, from issue #1; the code cites it as "plan §N" and "plan MN" |
| [`stage0-findings.md`](stage0-findings.md) | What stage 0 measured running ESP-IDF images in esp32sim, and how to reproduce it with `tools/` |

The plan says what is to be built; the other documents say what is true now. Where they differ
about something that exists, the feature documents, `protocol.md` and `recording-format.md` hold.
