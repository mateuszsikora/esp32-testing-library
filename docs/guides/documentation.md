# Writing documentation

Every commit and pull request that changes behaviour lands its documentation in the same change.
A behaviour change that leaves `docs/` untouched is incomplete, and removing behaviour removes its
documentation too. Nothing generates the documentation except the decisions index: people and
agents write it by hand. `make docs-check` only makes sure it cannot be forgotten.

[`docs/README.md`](../README.md) says what lives where. This guide says how to write each kind of
document, how the check works and what it cannot do.

## The check

```sh
make docs-check                  # against main
make docs-check BASE=origin/rel  # against another base, for a release branch
```

`scripts/check-docs.py` compares the work in progress with the merge base of `HEAD` and the
default branch: `origin/main` if it exists, else `main`. `--base REF` (`BASE=` in make) names
another. With neither found, it exits 2 and says so; it never falls back to some other branch.

What counts as changed is the union of:

- the commits since the merge base;
- staged and unstaged changes;
- untracked files that git does not ignore, so a new document counts before it is added.

Renames count as a deletion and an addition (`--no-renames`), so deleting or moving an owned file
is a change to its old path too.

Test files are left out before any rule runs: `tests/test_*.py`, `*.test.*`, `*.spec.*`,
`runner/src/**/tests.rs` and `runner/tests/`. They describe behaviour rather than change it. The
runner's unit tests live in `tests.rs` files of their own for this reason, not in inline
`mod tests { ... }` blocks.

Files whose only change is a version bump are left out next: `uses: …@ref` in a workflow, `rev:`
in `.pre-commit-config.yaml`, the version of a dependency in `pyproject.toml`, and changes only
to `[project].version` in `pyproject.toml` or `[package].version` in `runner/Cargo.toml`. The
package-version filter preserves every other character and is scoped to the owning table; a
dependency pin, comment or another table's version is not covered by that exception. Pins the
documentation states are not filtered: the esp32sim revision, the Rust toolchain, the ESP-IDF image
and the ROM release. If this filter fails, the check fails with it; it never passes instead.

Each rule of the ownership map then says: if a changed path matches the source pattern and no
changed path matches the documentation pattern, the check fails with the rule's message. The map
is `RULES` in `scripts/check-docs.py`; [`docs/features/README.md`](../features/README.md) lists the
same rules, and the check fails when the two differ. [`AGENTS.md`](../../AGENTS.md) restates them
as "you changed X, update Y".

The check also fails when:

- a reference to `docs/decisions/` carries a line number (`ci.md:12`, `ci.md#L12`) instead of a
  heading anchor;
- a link or reference into `docs/` names a file that does not exist, or an anchor that is not a
  heading of that file;
- a `plan §N`, `plan §N.N` or `plan MN` reference in the code names no heading of
  [`docs/implementation-plan.md`](../implementation-plan.md);
- [`docs/decisions/README.md`](../decisions/README.md) is not what `scripts/decisions-index.py`
  generates, or a decision file exists that the script does not list;
- [`docs/invariants.md`](../invariants.md) gives one number to two invariants (`<N> numbers <count>
  invariants; numbers are never reused`), or has a list item not written `- **<N>.** `: an
  ordered-list marker, which GitHub renumbers (`<N>. is an ordered-list marker, which GitHub
  renumbers; write - **<N>.**`, and the same for `<N>)`), or any other bullet, which the count would
  miss (`"<the item's first 30 characters>" is a list item not written - **<N>.**`). A line that
  goes on with a rule's text, indented under its dash, may start with a number other than 1: GitHub
  reads it as text, and so does the check. Two pull requests that each take the next free number
  pass on their own. The second fails once it is checked against a base that holds the first: after
  the base branch is merged into it, or when CI runs on a new push to it, since CI checks out the
  pull request merged into its base as of that push; re-running a finished job does not take the new
  base. If both land anyway,
  `tests/test_check_docs.py::test_this_repository_passes_its_integrity_checks` fails on `main`.

It prints every failure as `✗ <message>` and exits 1.

`SKIP_DOCS_CHECK=1 make docs-check` skips it, for local work only, such as a commit you know is
followed by its documentation. CI never sets it, and the check ignores it when `CI` is set. CI runs
it as the "Documentation contract" step on every pull request, against the pull request's base
branch ([`docs/cicd.md`](../cicd.md)).

It is not a pre-commit hook: it looks at the whole branch, so a hook would block a code-only commit
whose documentation comes in the next commit of the same pull request.

## Kinds of document

### Feature, user side: `docs/features/<feature>-user.md`

What someone using esp32tl can do, and what they see. No implementation detail: a reader who never
opens the code can use the feature from this document alone.

```markdown
# <Feature>

<One paragraph: what it is for, in the user's terms.>

## <Task the user does>

<Steps, the commands or code they write, the options and their defaults.>

## <States or outcomes>

<What they see in each case. Messages quoted verbatim, in code spans.>

## See also
```

### Feature, technical side: `docs/features/<feature>-technical.md`

How it works, for whoever changes it. Sections in this order; leave out any that does not apply.

```markdown
# <Feature>: how it works

## Data flow
<Who calls whom, what goes where, in order. Real module, class and function names.>

## Schema
<File and message formats this feature reads or writes, field by field.>

## Interfaces
<Command lines, fixtures, hooks, public functions: names, arguments, defaults.>

## Configuration
<Every environment variable and option, its default, who reads it.>

## Failure modes
<Every error message and exit code, verbatim, with its cause.>

## Tests
<Which test files cover what, and what is not covered.>

## See also
```

### Invariants: `docs/invariants.md`

Numbered rules about the domain that a test enforces. Each names the test that breaks when the rule
does. A rule that code can break without any test failing is a note, not an invariant: put it in
the owning technical document instead. Numbers are never reused; a rule that no longer holds is
struck through with the reason, so references to it stay meaningful.

```markdown
- **<N>.** <The rule.>
  `<test file>::<test name>`,
  `<test file>::<test name>`
```

The number is bold text in a bulleted item, not an ordered-list marker: GitHub numbers an ordered
list from its first item onwards and ignores the numbers written on the others, so `119.` after
`42.` would show as 43. A new rule takes the next number after the highest one in the file, in the
section it belongs to, so numbers within a section need not be in order. Continuation lines are
indented by at least two spaces; the items written before this format keep the indentation they
had.

### Decisions: `docs/decisions/<domain>.md`

Why things are the way they are: alternatives that were rejected, and scope that was postponed. One
`##` heading per decision; the heading says what was decided, not the question.

```markdown
## <What was decided, as a statement>

Status: decided (#N)

<What was decided and why, in a paragraph or two.>

**Rejected:** <the alternative>, because <reason>.
```

`Status:` is the first line after the heading, and one of `decided`, `postponed` or `superseded`,
followed by the pull request or issue in brackets. A superseded decision links to the one that
replaced it, by anchor: `[…](runner.md#the-new-heading)`, never by line number, because lines move.

A decision that a newer one will replace, once the change it needs lands, keeps `decided` and gets a
note under its `Status:` line, in italics: "Decided in #N to give way to", a link to the new
decision by anchor, and "this is what the code does until that lands". The pull request that makes
the change sets the old decision to `superseded` and drops the note. A decision whose note names one
part only, such as one marker of two, says which.

The index, [`docs/decisions/README.md`](../decisions/README.md), is generated:

```sh
python3 scripts/decisions-index.py          # rewrite it
python3 scripts/decisions-index.py --check  # fail if it is stale (make docs-check runs this)
```

A new domain is a new file plus an entry in `DOMAINS` in `scripts/decisions-index.py`; the check
fails on a file the script does not list. Before deciding something, read the index: a stale or
missing entry answers "no such decision" for one that exists, which is why the index is generated
rather than kept by hand.

### Guides: `docs/guides/<topic>.md`

How to work on the repository (setting up, testing, writing documentation) or with esp32tl
(`writing-tests.md`, testing your own firmware). They follow no template beyond a title and an
opening paragraph that says who the guide is for.

## Style

The rules of [`CONTRIBUTING.md`](../../CONTRIBUTING.md) hold here too: plain and precise, measured
claims with their numbers, no "comprehensive", "robust", "seamless" or "enhance". In addition:

- Write what is true now. No roadmap: what is planned belongs in issues and in the
  [implementation plan](../implementation-plan.md).
- Real paths, real names, real defaults. Quote messages and command output verbatim.
- Refer to this repository's code by path and symbol (`esp32tl/backends/esp32sim.py`,
  `Esp32Sim.close`), not by line number. Lines in esp32sim are fine with the revision they hold at.
- Where you are not sure, write `TODO(owner): <what is unknown>` rather than guessing.
- Every document lives in one place. Link to it; do not restate it.

## Agent tooling

`AGENTS.md` carries the contract for every agent; `CLAUDE.md` is a symlink to it. It stays under
about 23 KiB, because Codex stops reading it at its `project_doc_max_bytes` (32 KiB) without saying
so: the reasoning goes into `docs/` and `AGENTS.md` links to it. `.claude/rules/documentation.md`
restates the table for Claude Code when it edits a source file; Codex does not automatically load
`.claude/rules/`, so `AGENTS.md` carries the same rules.

Skills that open, review or merge pull requests carry the contract. The shared
[`create-pr` skill](development.md#agent-skills) runs `make docs-check` before pushing and fills
the Documentation section of the description for both Claude Code and Codex. A skill added later
for any of those jobs does the same:

- **opening a pull request:** run `make docs-check` before pushing, and fill the Documentation
  section of the pull request template;
- **reviewing:** a behaviour change without its documentation is a Major finding. Also check that
  the documentation is now true, which the script cannot do;
- **merging or shipping:** run `make docs-check` after merging the base branch in, never only
  before: the merge changes what the branch changed.

A skill that opens issues carries the contract forward: the
[`triage-feedback` skill](development.md#agent-skills) ends every issue's plan with the documents
the table in `AGENTS.md` names for the files it will change, the decisions and invariants it
records, and the line "Implementation must include the relevant documentation in the same
change and pass `make docs-check` (see `AGENTS.md`)."

## Known limits

- The check proves coverage, not correctness. Review owns the question "is the documentation now
  true?".
- A new file in a part of the map that lists files by name (`scripts/`, the runner's top level,
  the protocol files) matches no rule, and nothing enforces its documentation. Whoever adds the
  file adds it to the map. Under `esp32tl/`, rule F12 catches such files; nothing does elsewhere.
- F12 asks for a new feature document, but cannot tell whether that document describes the path
  that changed, or whether the map gained a row for it: review checks both.
- A red `docs` job does not block a merge while the repository is private: GitHub's free plan has
  no branch protection for private repositories, so no check can be required.
- `pyproject.toml` has several owners (rule C7): the check accepts any of their documents, and
  review checks that it was the right one.
- Agent tooling (`.claude/**`, `.agents/**`, skills) restates normative files (`CONTRIBUTING.md`,
  this guide, `AGENTS.md`) and goes stale silently. When a skill and a normative file disagree,
  the skill is wrong.

### Example ownership

F24 owns `examples/` and requires `docs/features/examples-user.md` or
`docs/features/examples-technical.md` in the same change. Test-only edits under
`examples/<name>/tests/` (`test_*.py`, including nested directories) are exempt, as root test
files are. Assembly profiles under `esp32tl/boards/` still require their F13 documentation.
