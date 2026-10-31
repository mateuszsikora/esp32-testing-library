# Documentation decisions

How the repository keeps its documentation in step with the code, and the conventions for
contributors and agents that go with it.

## Every behaviour change lands its documentation, and CI enforces it

Status: decided (#54)

A commit or pull request that changes behaviour updates the documentation of that behaviour in the
same change. People and agents write the documentation by hand. `scripts/check-docs.py` checks only
that the documentation was not forgotten: each rule of its ownership map pairs a source pattern
with the documents that own it. The "Documentation contract" step in CI fails a pull request that
touches an owned path without its documentation.

**Rejected:** generating documentation from the code, because the documents that matter here (what
a test author sees, why the runner stops where it does, which messages appear) are not in
docstrings, and a generated page claims to be true whether it is or not.

## The documentation check is a Python script with no dependencies

Status: decided (#54)

`scripts/check-docs.py` and `scripts/decisions-index.py` use the standard library only and run with
the system `python3`, without `make setup`. Their tests are pytest tests that build temporary git
repositories, as `tests/test_firmware_build.py` does for `firmware/build.sh`.

**Rejected:** a bash script with a Node `decisions-index.mjs`, because the repository has no Node
and CI would need it only for this. In bash, macOS's bash 3.2, `set -euo pipefail` around every
`grep` that may print nothing, and computing GitHub's heading anchors would each be a way to fail
silently.

## CI is the gate, not a pre-commit hook

Status: decided (#54)

The check runs in CI on every pull request and by hand with `make docs-check`. It is not a
pre-commit hook. A failing `docs` job does not block a merge yet: while the repository is private,
on GitHub's free plan, branch protection and rulesets are not available (the API answers 403), so
no check can be made required. The job marks the pull request red, and whoever merges reads it.
Once the repository is public, `docs` becomes a required check of `main`, with the other jobs.

**Rejected:** a hook, because the check compares the whole branch with its merge base, not one
commit: it would block a code-only commit whose documentation comes in the next commit of the same
pull request, and the repository squashes pull requests anyway.

## The documentation job runs on pull requests only

Status: decided (#54)

The `docs` job runs on `pull_request` events, against `origin/<base branch>` of the pull request,
with the full history the merge base needs (`fetch-depth: 0`).

**Rejected:** running it on pushes to `main` too, because there the merge base with `main` is the
commit itself: the contract part would compare nothing with nothing and always pass. The integrity
part (links, anchors, the decisions index, the rules table) does not depend on the diff, and two
pull requests that pass on their own can break it together, one renaming a heading the other
links to. On `main` that is caught by `make test`'s
`tests/test_check_docs.py::test_this_repository_passes_its_integrity_checks`, which the `test`
jobs run on every push, not by this job.

## SKIP_DOCS_CHECK is ignored in CI

Status: decided (#54)

`SKIP_DOCS_CHECK=1` skips the check on a contributor's machine, for work in progress. When `CI` is
set, the check prints that the variable is for local work only and runs anyway.

**Rejected:** honouring it everywhere, because a variable set in a workflow file, or exported on
the self-hosted runner, would turn the gate off without anyone noticing.

## The map lists files by name only where a directory has two owners

Status: decided (#54)

A rule claims a whole directory when one document owns it (`firmware/`, `esp32tl/backends/`). Where
a directory has several owners, the rules name its files (`scripts/`). Under `runner/src/`, rule F6
carves the protocol files out instead, and under `esp32tl/`, rule F12 catches every path no other
rule owns, so a new file there is owned from its first line. F12 accepts only a new feature
document: an edit to an existing one would satisfy it while documenting nothing.

**Rejected:** listing every file by name, because a new file in a listed directory then matches no
rule and is not enforced at all.

## pyproject.toml accepts the documentation of any of its owners

Status: decided (#54)

`pyproject.toml` holds dependencies (development guide), pytest's configuration (testing guide)
and the entry points (plugin and CLI documents). Rule C7 passes when any of those documents changed;
review checks that it was the right one.

**Rejected:** reading the diff by TOML section, because a parser of the diff's hunks would be more
code than the rest of the check, for a file that changes a few times a month.

## Version-only bumps need no documentation, except for pins the documentation states

Status: decided (#54)

A file whose every changed line differs only in a version is left out before the rules run: the
`uses:` refs of a workflow, `rev:` in `.pre-commit-config.yaml`, and Python dependency versions in
`pyproject.toml`. A filter that fails fails the check.

**Rejected:** filtering every pin, because the documentation states the esp32sim revision, the Rust
toolchain, the ESP-IDF image and the ROM release: a bump of one of those changes what is true.

## Test files are left out, and the runner's unit tests live in tests.rs files

Status: decided (#54)

Test files describe behaviour rather than change it, so the check leaves them out by path. The
runner's unit tests moved from inline `mod tests { ... }` blocks into `runner/src/tests.rs` and
`runner/src/cli/tests.rs` so that their path says what they are.

**Rejected:** keeping the inline modules and accepting that a change to a Rust test asks for
documentation, because CI would then fail every test-only runner change.

## The README is an entry point; the documents in docs/ own the facts

Status: decided (#54)

`README.md` says what esp32tl is, how to run the tests and what a test looks like, and links to
`docs/`. Requirements, make targets, CI, the backend and the firmware build are documented once,
in `docs/`, where a rule of the map owns them.

**Rejected:** keeping the README as the owner of those facts, because the README is no rule's
document: two copies would drift, and the check could not say which one was stale.

## Format specifications stay at the top of docs/

Status: decided (#54)

`docs/protocol.md` stays where it is and owns the link code (rules F8 to F10), rather than becoming
`docs/features/link-protocol-technical.md`. It is a contract between two peers, not a feature of
one, and the plan and the issues link to it by that path.

**Rejected:** moving it under `docs/features/`, because every existing link to it would break for
no change in what it says.

## The recording format is a document at the top of docs/, with a rule of its own

Status: decided (#14)

`docs/recording-format.md` specifies the recording format and owns `esp32tl/recording.py` (rule
F14), as `docs/protocol.md` owns the link code: it is a format between the tools that write
recordings and the replay that reads them, not a feature of either. The replay,
`esp32tl/sources/replay.py`, has feature documents of its own, `docs/features/replay-*.md` (rule
F15), named by file: the models and the real source will be other files of `sources/`.

**Rejected:** a `docs/features/recordings-*.md` pair, because the format is a contract of several
writers and readers, which [the protocol's decision](#format-specifications-stay-at-the-top-of-docs)
keeps at the top of `docs/`; and one rule for all of `sources/`, because its other files will be
documented elsewhere.

## The decisions index is generated

Status: decided (#54)

`docs/decisions/README.md` is generated from the `##` headings of the decision files by
`scripts/decisions-index.py`, and the check fails when it is stale or when a decision file is
missing from the script's `DOMAINS`.

**Rejected:** an index kept by hand, because it goes stale, and a stale index answers "no such
decision" for one that exists.

## AGENTS.md is the agents' file, and CLAUDE.md links to it

Status: decided (#54)

`AGENTS.md` carries the instructions for every coding agent; `CLAUDE.md` is a symlink to it.
`.claude/rules/documentation.md` restates the contract for Claude Code when it edits a source file.

**Rejected:** a copy in `CLAUDE.md`, because two copies drift. The reasoning lives in `docs/` rather
than in `AGENTS.md`, because Codex stops reading `AGENTS.md` at 32 KiB without saying so.

## Contributor conventions live in CONTRIBUTING.md; the create-pr skill restates them

Status: decided (#44)

`CONTRIBUTING.md` holds the rules for branch names, commit messages, pull request titles and
descriptions. The `create-pr` skill restates the rules it checks, so they are at hand while it
checks a draft; `CONTRIBUTING.md` wins where the two disagree, and a change to a rule goes into
both.

**Rejected:** a skill that only points to `CONTRIBUTING.md`, because the agent would look up every
rule while checking a draft.

## Titles carry a type and an optional scope, with no catch-all scope

Status: decided (#44)

Commit subjects and pull request titles are `<type>(<scope>): <summary>`, or `<type>: <summary>`
when no scope fits. Descriptions keep the style of #2, #42 and #43; the squash commits on `main`
before #44 stay as they are.

**Rejected:** a `repo` scope for everything outside the package, runner, firmware, tools and plan,
because it named no part of the project and matched no label.

## The create-pr skill takes no arguments

Status: decided (#44)

The skill finds the issue, judges readiness and gathers context from the branch, the conversation
and the issue tracker.

**Rejected:** arguments, because they would be a second, possibly stale, source for the same
information.

## The squash settings stay as GitHub's defaults

Status: decided (#44)

The repository squashes with `COMMIT_OR_PR_TITLE` and `COMMIT_MESSAGES`. `CONTRIBUTING.md` and the
skill describe what those settings do.

**Rejected:** changing the settings to match the documents, because the documents can describe
the defaults exactly.
