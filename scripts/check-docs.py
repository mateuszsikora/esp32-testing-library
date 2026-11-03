#!/usr/bin/env python3
"""Fails when a change to an owned path does not update the documentation that owns it.

    python3 scripts/check-docs.py [--base REF] [-v]     (make docs-check [BASE=REF])

The changes are everything since the merge base of HEAD with REF, by default `origin/main`, else
`main`: the commits, staged and unstaged changes, and untracked files. Each rule in RULES says: if
a changed path matches `source` and no changed path matches `docs`, fail with `message`. The rules
are the ownership map; docs/features/README.md lists the same rules, AGENTS.md restates them, and
docs/guides/documentation.md says how the check works and what it cannot do.

Exit codes: 0 when every rule holds, 1 when one does not, 2 when there is no base to compare with.
`SKIP_DOCS_CHECK=1` skips the check outside CI. Standard library only, so it runs on any Python 3
without the repository's virtual environment.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import ModuleType

DEFAULT_BRANCH = "main"

Scope = Callable[[list[str]], list[str]]


@dataclass(frozen=True)
class Rule:
    id: str
    source: str
    docs: str
    message: str
    # Which changed paths the rule looks at; None is all of them. An empty list is "nothing this
    # rule owns changed", not "look at everything".
    scope: Scope | None = None
    # Whether only a new document counts: one added since the merge base, or untracked.
    new_docs: bool = False


def without(*patterns: str) -> Scope:
    """A carve-out: the changed paths that match none of `patterns`."""
    return lambda paths: [p for p in paths if not any(re.search(x, p) for x in patterns)]


# The runner's link-protocol files belong to docs/protocol.md (F9, F10), not to the runner's own
# documentation: the protocol is the contract with the other peer, and lives in one document.
PROTOCOL_FILES = r"^runner/src/(protocol|socket_board)\.rs$"

FEATURES = [
    Rule(
        "F1",
        r"^esp32tl/pytest_plugin\.py$",
        r"^docs/features/pytest-plugin-",
        "pytest plugin changed (fixtures, markers, options, parametrisation):"
        " update docs/features/pytest-plugin-*.md",
    ),
    # All of interfaces/ while console.py is its only file; split by file name once the UART and
    # lines interfaces come with documents of their own.
    Rule(
        "F2",
        r"^esp32tl/interfaces/",
        r"^docs/features/pytest-plugin-",
        "Console interface changed: update docs/features/pytest-plugin-*.md",
    ),
    # The registry and serial backend belong to F23; the held clock belongs to esp32sim.
    Rule(
        "F3",
        r"^esp32tl/backends/(esp32sim|clock)\.py$",
        r"^docs/features/esp32sim-backend-",
        "esp32sim backend changed (spawn, ROM and runner lookup, end of run, teardown):"
        " update docs/features/esp32sim-backend-*.md",
    ),
    Rule(
        "F23",
        r"^esp32tl/backends/(serial|__init__)\.py$",
        r"^docs/features/serial-backend-",
        "Serial backend or backend registry changed: update docs/features/serial-backend-*.md",
    ),
    Rule(
        "F4",
        r"^esp32tl/cli\.py$",
        r"^docs/features/cli-user\.md$",
        "esp32tl command changed: update docs/features/cli-user.md",
    ),
    # The package root holds only the version, which is what `esp32tl --version` shows.
    Rule(
        "F5",
        r"^esp32tl/__init__\.py$",
        r"^docs/features/cli-user\.md$",
        "Package root changed (version shown by esp32tl --version):"
        " update docs/features/cli-user.md",
    ),
    # A carve-out rather than a list of file names, so that a new file in runner/src/ is owned
    # from its first line.
    Rule(
        "F6",
        r"^runner/src/",
        r"^docs/features/runner-",
        "Runner changed (flags, stop line, exit codes, run loop): update docs/features/runner-*.md",
        without(PROTOCOL_FILES),
    ),
    Rule(
        "F7",
        r"^runner/Cargo\.toml$",
        r"^docs/features/runner-technical\.md$",
        "runner/Cargo.toml changed (esp32sim pin, profiles): update"
        " docs/features/runner-technical.md; a pin bump also re-checks the esp32sim lines"
        " docs/protocol.md cites",
    ),
    Rule(
        "F8",
        r"^esp32tl/link/",
        r"^docs/protocol\.md$",
        "Link protocol changed: docs/protocol.md is the contract both peers build against;"
        " update it, and its version if the wire changes",
    ),
    Rule(
        "F9",
        r"^runner/src/protocol\.rs$",
        r"^docs/protocol\.md$",
        "Link protocol changed in runner/src/protocol.rs: update docs/protocol.md,"
        " and its version if the wire changes",
    ),
    Rule(
        "F10",
        r"^runner/src/socket_board\.rs$",
        r"^docs/protocol\.md$",
        "Link protocol changed in runner/src/socket_board.rs: update docs/protocol.md,"
        " and its version if the wire changes",
    ),
    Rule(
        "F11",
        r"^firmware/",
        r"^docs/features/validation-firmware-",
        "Validation firmware or its build changed: update docs/features/validation-firmware-*.md",
    ),
    # Numbered after F12, which was there first; listed before it, so that F12 leaves it out.
    Rule(
        "F13",
        r"^esp32tl/boards/",
        r"^docs/features/board-profiles-",
        "Board profiles or the capability declaration changed (schema, pins, sources, taps):"
        " update docs/features/board-profiles-*.md",
    ),
    # The recording format is a format specification, at the top of docs/ as the protocol is.
    Rule(
        "F14",
        r"^esp32tl/recording\.py$",
        r"^docs/recording-format\.md$",
        "Recording format changed: docs/recording-format.md is its specification; update it, and"
        " its version if the format changes",
    ),
    # sources/ by file name: the models and the real source will have documents of their own.
    Rule(
        "F15",
        r"^esp32tl/sources/replay\.py$",
        r"^docs/features/replay-",
        "Replay changed (what it replays, when, what it refuses): update docs/features/replay-*.md",
    ),
    # Two modules, one feature: the tap writes what the run report shows.
    Rule(
        "F16",
        r"^esp32tl/(tap|report)\.py$",
        r"^docs/features/run-report-",
        "Taps or the run report changed (what a tap writes, what the report shows):"
        " update docs/features/run-report-*.md",
    ),
    # The modem has its own feature documents (F19); the other models share units (F17).
    Rule(
        "F17",
        r"^esp32tl/(units\.py|sources/model/(?!modem\.py$))",
        r"^docs/features/models-",
        "Models or units changed (quantities, the GNSS receiver, its sentences):"
        " update docs/features/models-*.md",
    ),
    Rule(
        "F18",
        r"^esp32tl/bluetooth/",
        r"^docs/features/bluetooth-peer-",
        "Bluetooth peer changed (the air's clock, the controller, the SPP peer):"
        " update docs/features/bluetooth-peer-*.md",
    ),
    Rule(
        "F19",
        r"^esp32tl/sources/model/modem\.py$",
        r"^docs/features/modem-",
        "Modem model changed (AT dialogue, power, registration): update docs/features/modem-*.md",
    ),
    Rule(
        "F20",
        r"^esp32tl/flash\.py$",
        r"^docs/features/flash-contents-",
        "Flash contents changed (partition table, NVS types and generation):"
        " update docs/features/flash-contents-*.md",
    ),
    Rule(
        "F21",
        r"^esp32tl/matching\.py$",
        r"^docs/features/matching-",
        "Matching engine changed (protocols, requests, timing): update docs/features/matching-*.md",
    ),
    Rule(
        "F22",
        r"^esp32tl/net/",
        r"^docs/features/network-",
        "Simulated network changed: update docs/features/network-*.md",
    ),
    Rule(
        "F24",
        r"^examples/",
        r"^docs/features/examples-",
        "Examples changed: update docs/features/examples-*.md",
    ),
]

# The catch-all for the package: the rest of sources/ and any new module are owned from their
# first line of code instead of matching no rule. py.typed is a marker file with no behaviour.
# Only a new feature document satisfies it: an edit to an existing one, a blank line in
# cli-user.md, would document nothing. Once the new feature has its row in the map, this rule no
# longer applies to its paths.
FEATURES.append(
    Rule(
        "F12",
        r"^esp32tl/",
        r"^docs/features/[a-z0-9-]+\.md$",
        "This path has no owning doc yet: add its feature docs, a new"
        " docs/features/<feature>-user.md or -technical.md, and add it to the ownership map"
        " (scripts/check-docs.py, docs/features/README.md, AGENTS.md)",
        without(*(rule.source for rule in FEATURES), r"^esp32tl/py\.typed$"),
        new_docs=True,
    )
)

CROSS_CUTTING = [
    Rule(
        "C1",
        r"^\.github/workflows/",
        r"^docs/cicd\.md$",
        "CI workflow changed: update docs/cicd.md",
    ),
    Rule(
        "C2",
        r"^Makefile$",
        r"^docs/guides/development\.md$",
        "Makefile changed: update the make targets in docs/guides/development.md",
    ),
    Rule(
        "C3",
        r"^\.pre-commit-config\.yaml$",
        r"^docs/guides/development\.md$",
        "Pre-commit hooks changed: update docs/guides/development.md",
    ),
    # scripts/ has four owners, the development guide (C4, C5), the documentation guide (C9,
    # C10), the validation firmware documents (C12) and the testing guide (C13), so its files are
    # listed by name. A new script matches no rule: add it here.
    Rule(
        "C4",
        r"^scripts/fetch-rom-elf$",
        r"^docs/guides/development\.md$",
        "ROM ELF setup changed: update docs/guides/development.md",
    ),
    Rule(
        "C5",
        r"^scripts/venv-tool$",
        r"^docs/guides/development\.md$",
        "Hook runner changed: update docs/guides/development.md",
    ),
    Rule(
        "C6",
        r"^runner/rust-toolchain\.toml$",
        r"^docs/guides/development\.md$",
        "Rust toolchain pin changed: update the requirements in docs/guides/development.md",
    ),
    # One file with several owners: the check accepts any of their documents, and review checks
    # that it was the right one.
    Rule(
        "C7",
        r"^pyproject\.toml$",
        r"^docs/(guides/development\.md|guides/testing\.md|features/pytest-plugin-|features/cli-user\.md)",
        "pyproject.toml changed: dependencies and packaging go to docs/guides/development.md,"
        " [tool.pytest] to docs/guides/testing.md, entry points to the plugin or CLI doc",
    ),
    Rule(
        "C8",
        r"^tests/conftest\.py$",
        r"^docs/guides/testing\.md$",
        "Test-suite configuration changed: update docs/guides/testing.md",
    ),
    # A change to RULES is a change to the map, which AGENTS.md restates; the integrity check
    # below compares RULES with docs/features/README.md, but not with AGENTS.md's prose.
    Rule(
        "C9",
        r"^scripts/check-docs\.py$",
        r"^(AGENTS\.md|docs/guides/documentation\.md)$",
        "Docs check changed: update the table in AGENTS.md and/or docs/guides/documentation.md",
    ),
    Rule(
        "C10",
        r"^scripts/decisions-index\.py$",
        r"^(AGENTS\.md|docs/guides/documentation\.md)$",
        "Decisions index changed: update AGENTS.md and/or docs/guides/documentation.md",
    ),
    Rule(
        "C11",
        r"^tools/",
        r"^docs/stage0-findings\.md$",
        "Benchmark tool changed: update Reproducing in docs/stage0-findings.md;"
        " re-measure if results change",
    ),
    Rule(
        "C12",
        r"^scripts/generate-gnss-recordings\.py$",
        r"^docs/features/validation-firmware-",
        "GNSS recording generator changed: update docs/features/validation-firmware-*.md",
    ),
    Rule(
        "C13",
        r"^scripts/check-determinism\.py$",
        r"^docs/guides/testing\.md$",
        "Determinism check changed: update docs/guides/testing.md",
    ),
]

RULES = FEATURES + CROSS_CUTTING

# Tests describe behaviour rather than change it, so they are left out before any rule runs. The
# runner keeps its unit tests in tests.rs files for this reason, not in inline `mod tests` blocks.
TEST_FILES = [
    r"^tests/test_[^/]*\.py$",
    r"^examples/[^/]+/tests/(.*/)?test_[^/]*\.py$",
    r"(^|/)[^/]*\.(test|spec)\.[^/]+$",
    r"^runner/src/(.+/)?tests\.rs$",
    r"^runner/tests/",
]

# Files whose only change is a version bump need no documentation, so that dependency updates do
# not ask for any: per file, the lines that may differ only in a version. Each pattern's first
# group is the line without its version. Only pins the documentation does not state are here; the
# esp32sim revision, the Rust toolchain, the ESP-IDF image and the ROM release are documented, and
# a bump of one of those asks for its documentation like any other change.
VERSION_LINES = {
    r"^\.github/workflows/[^/]+\.ya?ml$": re.compile(
        r"^(\s*(?:-\s+)?uses:\s*[^@\s]+@)\S+\s*(#.*)?$"
    ),
    r"^\.pre-commit-config\.yaml$": re.compile(r"^(\s*(?:-\s+)?rev:\s*)\S+\s*(#.*)?$"),
    r"^pyproject\.toml$": re.compile(
        r'^(\s*"[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[^\]]*\])?\s*(?:===|==|~=|>=|<=|!=|>|<))'
        r'[^",;]+(?="\s*,?\s*(?:#.*)?$)'
    ),
}

# Key documents whose `plan §N` and `plan MN` references in code must resolve.
PLAN = "docs/implementation-plan.md"
PLAN_REF = re.compile(r"\bplan ((?:§\d+(?:\.\d+)?|M\d+)(?:, (?:§\d+(?:\.\d+)?|M\d+))*)")
FEATURES_INDEX = "docs/features/README.md"
INVARIANTS = "docs/invariants.md"
TEXT_SUFFIXES = {".md", ".py", ".rs", ".sh", ".toml", ".yml", ".yaml", ".c", ".h", ".txt", ""}


class NoBase(Exception):
    pass


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout


def paths(root: Path, *args: str) -> set[str]:
    """The NUL-separated paths a git command prints: -z keeps unusual names unquoted."""
    return {p for p in git(root, *args, "-z").split("\0") if p}


def merge_base(root: Path, base: str | None) -> tuple[str, str]:
    """The merge base of HEAD with `base`, or with origin/main, else main; never another branch,
    which would compare against changes that are not this branch's."""
    candidates = [base] if base else [f"origin/{DEFAULT_BRANCH}", DEFAULT_BRANCH]
    for ref in candidates:
        try:
            git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        except subprocess.CalledProcessError:
            continue
        try:
            return ref, git(root, "merge-base", "HEAD", ref).strip()
        except subprocess.CalledProcessError as error:
            raise NoBase(f"HEAD and {ref} have no common ancestor") from error
    tried = " or ".join(candidates)
    raise NoBase(f"no {tried} to compare with: fetch it, or name the base with --base REF")


def added_paths(root: Path, mb: str) -> set[str]:
    """The files that did not exist at the merge base: added in a commit, staged or not, or
    untracked. With rename detection, unlike the changed set: a document moved with `git mv` is
    not a new one. A move git cannot see, an untracked copy of a deleted file, still counts."""
    return paths(root, "diff", "--name-only", "--find-renames", "--diff-filter=A", mb) | paths(
        root, "ls-files", "--others", "--exclude-standard"
    )


def changed_paths(root: Path, mb: str) -> set[str]:
    """Committed since the merge base, staged, unstaged, and untracked. --no-renames: a rename
    is the removal of its old path, which the old path's rule must see."""
    return (
        paths(root, "diff", "--name-only", "--no-renames", mb, "HEAD")
        | paths(root, "diff", "--name-only", "--no-renames", "HEAD")
        | paths(root, "diff", "--name-only", "--no-renames", "--cached")
        # A brand-new document is still untracked when the check runs locally.
        | paths(root, "ls-files", "--others", "--exclude-standard")
    )


def version_bump_only(root: Path, mb: str, path: str) -> bool:
    """Whether every line `path` changed since `mb` differs from a removed line only in a
    version that VERSION_LINES allows."""
    pattern = next((p for f, p in VERSION_LINES.items() if re.search(f, path)), None)
    if pattern is None:
        return False
    diff = git(root, "diff", "-U0", "--no-color", "--no-ext-diff", "--no-renames", mb, "--", path)
    removed: Counter[str] = Counter()
    added: Counter[str] = Counter()
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("@@"):
            in_hunk = True
        elif not in_hunk or line.startswith("\\"):
            continue
        elif line[:1] in "-+":
            versioned = pattern.match(line[1:])
            if versioned is None:
                return False
            (removed if line[0] == "-" else added)[versioned[1]] += 1
    # An untracked or deleted file has no lines on one side, so it is no bump.
    return bool(removed) and removed == added


def drop_version_bumps(root: Path, mb: str, changed: Iterable[str]) -> tuple[list[str], list[str]]:
    """The changed paths without version-only bumps, and the errors of the filter. A filter that
    fails keeps the path and fails the check: it never lets a change through unchecked."""
    kept, errors = [], []
    for path in sorted(changed):
        try:
            if version_bump_only(root, mb, path):
                continue
        except Exception as error:
            errors.append(
                f"The version-bump filter failed on {path} ({error!r}); the check does not pass"
                " without it"
            )
        kept.append(path)
    return kept, errors


def require(source: str, docs: str, message: str, scope: list[str], changed: list[str]) -> bool:
    """The core rule: true when a path of `scope` matches `source` and no changed path matches
    `docs`. The documentation is looked for in every change, not only in `scope`."""
    owned = any(re.search(source, p) for p in scope)
    return owned and not any(re.search(docs, p) for p in changed)


def contract(changed: list[str], files: list[str], added: list[str]) -> list[str]:
    """The message of every rule that `changed` breaks. `files` is the changed set before test
    files and version bumps were taken out, which is where documentation is looked for; `added`,
    the new files among them, for a rule that wants a new document."""
    failures = []
    for rule in RULES:
        scope = rule.scope(changed) if rule.scope else changed
        docs = added if rule.new_docs else files
        if require(rule.source, rule.docs, rule.message, scope, docs):
            failures.append(f"{rule.message} [{rule.id}]")
    return failures


def load_decisions_index() -> ModuleType:
    path = Path(__file__).with_name("decisions-index.py")
    spec = importlib.util.spec_from_file_location("decisions_index", path)
    assert spec is not None and spec.loader is not None, path
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def text_files(root: Path) -> dict[str, str]:
    """Every text file in the work tree that git tracks or would track, by path. Test files are
    left out: their fixtures hold broken links on purpose."""
    found = {}
    for path in sorted(paths(root, "ls-files", "--cached", "--others", "--exclude-standard")):
        full = root / path
        if any(re.search(t, path) for t in TEST_FILES):
            continue
        if PurePosixPath(path).suffix not in TEXT_SUFFIXES or not full.is_file():
            continue
        if full.is_symlink():
            continue
        data = full.read_bytes()
        if b"\0" not in data:
            found[path] = data.decode("utf-8", errors="replace")
    return found


def without_fences(text: str) -> str:
    """Markdown without its fenced code blocks."""
    return re.sub(r"(?ms)^[ \t]*(`{3,}|~{3,}).*?^[ \t]*\1[ \t]*$", "", text)


def prose(text: str) -> str:
    """Markdown without its code: links and references in an example are not links."""
    return re.sub(r"(`+).+?\1", "", without_fences(text))


def link_targets(path: str, text: str) -> list[tuple[str, str | None]]:
    """(repository path, anchor) of every relative link of a Markdown file, and of every
    reference to a `docs/` file with an anchor in any file, as code comments make."""
    targets: list[tuple[str, str | None]] = []
    if path.endswith(".md"):
        text = prose(text)
        for link in re.findall(r"\]\(<?([^)>\s]+)>?(?:\s+\"[^\"]*\")?\)", text):
            if re.match(r"[a-z][a-z0-9+.-]*:", link):
                continue  # https:, mailto:
            target, _, anchor = link.partition("#")
            if target:
                target = os.path.normpath(str(PurePosixPath(path).parent / target))
            if not target.startswith(".."):
                targets.append((target or path, anchor or None))
    for target, anchor in re.findall(r"(?<![\w/.])(docs/[\w./-]+\.md)#([\w-]+)", text):
        targets.append((target, anchor))
    # A root-relative link in a Markdown file is also a reference: list it once.
    return list(dict.fromkeys(targets))


def integrity(root: Path) -> list[str]:
    """What is wrong with the documentation itself, whatever changed."""
    index = load_decisions_index()
    files = text_files(root)
    plan = plan_sections(index, files.get(PLAN, ""))
    anchors_of: dict[str, set[str]] = {}
    failures = []
    for path, text in files.items():
        # A line number goes stale with the next edit above it; a heading anchor does not.
        scanned = prose(text) if path.endswith(".md") else text
        for ref in re.findall(r"decisions/[\w.-]+\.md(?::\d+|#L\d+)", scanned):
            failures.append(
                f"{path}: {ref} links a decision by line number; use its heading anchor"
            )
        for target, anchor in link_targets(path, text):
            if not (root / target).exists():
                failures.append(f"{path}: links to {target}, which does not exist")
            elif anchor and target.endswith(".md"):
                if target not in anchors_of:
                    anchors_of[target] = index.anchors(files.get(target, ""))
                if anchor not in anchors_of[target]:
                    failures.append(f"{path}: links to {target}#{anchor}, no heading there")
        # The code cites the plan by section ("plan §4.2", "plan M0"); a renumbered plan would
        # leave those pointing at the wrong section, or none.
        if not path.endswith(".md"):
            for refs in PLAN_REF.findall(text):
                for ref in refs.split(", "):
                    if ref not in plan:
                        failures.append(f"{path}: plan {ref} is no heading of {PLAN}")
    failures += index.check(root)
    failures += rules_table(files.get(FEATURES_INDEX))
    failures += invariant_numbers(files.get(INVARIANTS))
    # A link used three times in a file is one problem.
    return list(dict.fromkeys(failures))


def invariant_numbers(text: str | None) -> list[str]:
    """A number docs/invariants.md gives twice, or a list item not written `- **<N>.** `.

    Every item has to be in that one form to be counted: one written `2)` or `* **2.**` would slip
    past the count, and an ordered-list marker past GitHub's numbering too. Two pull requests that
    each take the next free number pass on their own and collide once both are merged, so this
    runs whatever changed."""
    if text is None:
        return []
    failures = []
    counts: Counter[str] = Counter()
    # Whether the line before is text of a rule: its item's first line, or a line indented under
    # the dash that starts like text (a letter, a digit, code, a bracket, a quote, `~~`). Anything
    # else may end the paragraph (a heading, HTML, a table, a quote, a break, a setext underline),
    # and so does a blank line or a line at the left margin: past it, a number is checked.
    in_rule = False
    # Inline code stays: an item's test names are in backticks, and the report quotes the item.
    for line in without_fences(text).splitlines():
        starts_item = re.match(r" {0,3}(?:[-*+]|\d+[.)])(?:\s|$)", line)
        # GitHub starts an ordered list inside a paragraph only at 1: in a rule's text,
        # "  13. A card" is text, not a marker.
        continues = in_rule and re.match(r"\s{2}", line)
        in_rule = bool(starts_item) or bool(
            in_rule and re.match(r"\s{2,}(?:[^\W_]|[`(\[\"'~])", line)
        )
        if not starts_item:
            continue
        if item := re.match(r"- \*\*(\d+)\.\*\* ", line):
            counts[item[1]] += 1
        elif ordered := re.match(r" {0,3}(\d+)([.)])", line):
            if continues and ordered[1] != "1":
                continue
            failures.append(
                f"{INVARIANTS}: {ordered[1]}{ordered[2]} is an ordered-list marker, which GitHub"
                f" renumbers; write - **{ordered[1]}.**"
            )
        else:
            failures.append(
                f'{INVARIANTS}: "{line.strip()[:30]}" is a list item not written - **<N>.**'
            )
    failures += [
        f"{INVARIANTS}: {n} numbers {count} invariants; numbers are never reused"
        for n, count in sorted(counts.items(), key=lambda item: int(item[0]))
        if count > 1
    ]
    return failures


def plan_sections(index: ModuleType, text: str) -> set[str]:
    """`§1`, `§4.2`, `M0`, ...: the numbered headings of the implementation plan."""
    found = set()
    for _, heading, _ in index.headings(text):
        numbered = re.match(r"(\d+(?:\.\d+)?)\.?\s", heading)
        milestone = re.match(r"(M\d+)\.\s", heading)
        if numbered:
            found.add(f"§{numbered[1]}")
        if milestone:
            found.add(milestone[1])
    return found


def rules_table(text: str | None) -> list[str]:
    """Differences between RULES and the table of docs/features/README.md, whose rows start with
    a rule's id and give its source and docs patterns as the first code span of a cell."""
    if text is None:
        return [f"{FEATURES_INDEX} is missing; it lists the ownership map"]
    listed = {}
    for line in text.splitlines():
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", line)[1:-1]]
        if len(cells) < 3 or not re.fullmatch(r"[FC]\d+", cells[0]):
            continue
        spans = [re.search(r"`([^`]+)`", c) for c in cells[1:3]]
        listed[cells[0]] = tuple(s[1].replace("\\|", "|") if s else "" for s in spans)
    failures = []
    for rule in RULES:
        if rule.id not in listed:
            failures.append(f"Rule {rule.id} of scripts/check-docs.py is not in {FEATURES_INDEX}")
        elif listed[rule.id] != (rule.source, rule.docs):
            failures.append(
                f"Rule {rule.id} differs between scripts/check-docs.py and {FEATURES_INDEX}"
            )
    for extra in sorted(listed.keys() - {rule.id for rule in RULES}):
        failures.append(f"Rule {extra} of {FEATURES_INDEX} is not in scripts/check-docs.py")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="check-docs.py", description=__doc__.splitlines()[0])
    parser.add_argument("--base", help=f"compare with this ref (default: origin/{DEFAULT_BRANCH})")
    parser.add_argument("-v", "--verbose", action="store_true", help="list what is checked")
    args = parser.parse_args(argv)

    if os.environ.get("SKIP_DOCS_CHECK") == "1":
        # GitHub sets CI=true; CI=false or CI=0 is no CI.
        if os.environ.get("CI", "").lower() in ("", "0", "false"):
            print("docs-check: skipped (SKIP_DOCS_CHECK=1); CI runs it anyway")
            return 0
        print("docs-check: SKIP_DOCS_CHECK is for local work only; CI checks anyway")

    try:
        root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").strip())
        ref, mb = merge_base(root, args.base)
    except NoBase as error:
        print(f"docs-check: {error}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as error:
        print(f"docs-check: {' '.join(error.cmd)} failed: {error.stderr.strip()}", file=sys.stderr)
        return 2

    files = sorted(changed_paths(root, mb))
    tests_left_out = [p for p in files if not any(re.search(t, p) for t in TEST_FILES)]
    changed, failures = drop_version_bumps(root, mb, tests_left_out)
    failures += contract(changed, files, sorted(added_paths(root, mb)))
    failures += integrity(root)

    if args.verbose:
        for path in files:
            mark = " " if path in changed else "-"
            print(f"  {mark} {path}")
    if failures:
        for failure in failures:
            print(f"✗ {failure}")
        print(
            "\nAGENTS.md (Documentation contract) says what to update for each path;"
            " docs/guides/documentation.md says how to write it."
        )
        return 1
    print(
        f"docs-check: {len(files)} changed files since {ref} ({mb[:7]}),"
        " each with the documentation it needs"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
