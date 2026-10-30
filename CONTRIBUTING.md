# Contributing

Everything that lands on GitHub (branch names, commit messages, pull request titles and
descriptions, review comments) is written in English, whatever language the work was discussed in.

## Types and scopes

Branch names, commit subjects and pull request titles share one type. Commit subjects and pull
request titles add a scope when the change is about one part of the project.

| Type | For |
|---|---|
| `feat` | New behaviour in the package, runner or firmware |
| `fix` | A bug fix |
| `docs` | Documentation and measurement records only |
| `test` | Tests only |
| `refactor` | Restructuring with no change in behaviour |
| `build` | Makefile, packaging, dependencies, pins, CI, hooks |
| `chore` | Anything else that changes no shipped behaviour |

| Scope | Covers |
|---|---|
| `harness` | `esp32tl/` and `tests/`: the pytest plugin, backends, link, sources |
| `runner` | `runner/`: the Rust crate and its esp32sim pin |
| `firmware` | `firmware/`: validation firmware and its build |
| `tools` | `tools/`: benchmarks and helpers outside the package |
| `plan` | `docs/implementation-plan*.md` and other design documents |

Choose the type by what the change does for the project, not by which files it touches: a
`Makefile` change that fixes a bug is `fix`, not `build`. When a change spans several scopes, use
the one it is about. When no scope fits (the Makefile, packaging, hooks, CI, contributor docs),
leave it out: `chore: ...`. A new scope is fine when it names a lasting part of the project; add
it to this table in the same pull request.

## Branch names

```
<type>/<issue>-<slug>
<type>/<slug>            when there is no issue
```

The slug is lowercase kebab-case, three to six words, and says what changes, not where:
`feat/5-runner-crate-skeleton`, `build/pin-esp32sim-past-chip-revision`,
`fix/12-uart-tap-drops-last-byte`. The whole name stays under 50 characters. No personal prefixes,
no placeholder names.

## Commit messages

```
build(runner): pin esp32sim at the merge of the chip-revision fix

mikroverk/esp32sim#322 was merged as 8c65215. The plan pinned 2e36224,
from before it, and expected to carry the fix on the patch branch ...

Refs #1
```

Subject, `<type>(<scope>): <summary>`, or `<type>: <summary>` when no scope fits:

- The summary is in the imperative mood and starts lowercase, except for proper names:
  "add", "pin", "record", not "added" or "adds".
- 72 characters at most including the prefix, ideally under 60. No trailing period.
- No issue number; the body carries it.
- Says what changes for the project, not which files were touched:
  `fix: refuse a Python older than 3.11 before creating the venv`, not `chore: update Makefile`.
- A change that breaks the public API or the recording format adds `!` before the colon,
  `feat(harness)!: ...`, and explains the break in the body.

Body (required unless the subject says everything):

- Wrapped at 72 columns, plain prose, separated from the subject by a blank line.
- Says why the change is needed and what it does differently; the diff already shows how.
- Names the alternative that was rejected when the choice is not obvious.
- When there is an issue, ends with `Closes #N` if the commit completes it, `Refs #N` otherwise.
- No `Co-Authored-By:`, `Signed-off-by:` or "Generated with" lines.

Follow-up commits on a branch (review fixes) follow the same rules and say what the review found,
not "address review comments".

## Pull request titles, and what reaches `main`

The repository merges by squashing, with GitHub's default squash message:

- A pull request with one commit lands as that commit's message, subject and body; the pull
  request title is not used. Keep the title equal to the commit subject.
- A pull request with several commits lands with the pull request title as the subject, and
  every commit message on the branch, in order, as the body.

GitHub appends `(#N)` to the subject in both cases. Either way, every commit message on the
branch ends up on `main`. Fix one that breaks these rules before the pull request is merged;
whoever merges can also correct the squash message in GitHub's merge dialog.

The title follows the commit subject rules above, with the same type as the branch name.

## Pull request descriptions

[`.github/pull_request_template.md`](.github/pull_request_template.md) is the template. In short:

- Open with one or two paragraphs: what changes and why, outcome first. The issue link
  (`Closes #N` / `Refs #N`) goes right after them.
- **Changes** lists what a reviewer needs to know, grouped by meaning, not a file-by-file inventory.
- **Decisions** records the choices a reviewer might question, with the rejected alternative.
- **Verification** says what was run and what it showed: commands and results, not "tested locally".
- **Notes** covers what is deliberately left out, follow-ups and anything worth a closer look.
- Empty sections are deleted, not left with "N/A".
- No signatures or attribution lines ("Generated with ...", "Co-Authored-By").
- Do not hard-wrap paragraphs: GitHub renders line breaks in descriptions literally.

A pull request that is not ready to merge (checks failing or not run, part of the issue still to
do, open questions) is opened as a draft, and the description says what is missing.

References: issues as `#N`, upstream as `mikroverk/esp32sim#N`, commits as 7-character short
SHAs in prose and full SHAs in anything read by machine.

Write plainly and precisely: measured claims with their numbers, no "comprehensive", "robust",
"seamless" or "enhance", no emoji. If an earlier version of the pull request claimed something
that turned out wrong, say so.

## Labels

A pull request gets the `area:*` labels for the parts it touches (`area:harness`, `area:runner`,
`area:firmware`, `area:ci`), plus `enhancement` for `feat`, `bug` for `fix`, `documentation` for
`docs`, and `upstream` for work in or with esp32sim or pytest-embedded. A pull request that matches
none of these gets no label.
