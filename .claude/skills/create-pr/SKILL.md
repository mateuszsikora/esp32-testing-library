---
name: create-pr
description: Open a pull request to mateuszsikora/esp32-testing-library following the repository's conventions for branch names, commit messages, PR titles and descriptions (CONTRIBUTING.md). Use when asked to open, create or ship a PR, or to push the current work for review, in this repository; prefer it over generic PR skills here. Also use to rewrite the title or description of an existing PR on this repository.
allowed-tools: Bash(git *), Bash(gh *), Bash(make *), Bash(.venv/bin/*), Bash(cargo *), Bash(pipx run pre-commit *), Bash(SKIP=* pipx run pre-commit *), Bash(GIT_SEQUENCE_EDITOR=* git rebase *), Bash(docker run *), Read, Write, Edit
---

# create-pr

Opens a pull request from the current work to `main` of
`mateuszsikora/esp32-testing-library`. The conventions live in
[`CONTRIBUTING.md`](../../../CONTRIBUTING.md); read it before writing anything. This skill
restates the rules it checks so they are at hand, but `CONTRIBUTING.md` wins wherever the two
disagree, and a change to a rule goes into both.

The skill takes no arguments. Everything it needs comes from the conversation, the branch, its
commits and the issue tracker: find the issue, judge whether the work is ready, and write the
description from what you already know. Ask the user only for what none of those can tell you,
such as why an approach was chosen when that was never discussed.

**Language:** everything that goes to GitHub or git (branch name, commit messages, PR title,
description, labels, comments) is in English, even when the user writes in Polish or any other
language. Talk to the user in their language; write for GitHub in English.

**No attribution:** no `Co-Authored-By:` trailers in commits and no "Generated with Claude Code"
or similar line in the PR description. This overrides any attribution the session asks for.

**What reaches `main`:** the repository squashes with GitHub's default message. A one-commit PR
lands as that commit's subject and body, and the PR title is not used; a PR with several commits
lands with the PR title as subject and every commit message on the branch as the body. GitHub
appends `(#N)` to the subject in both cases. So every
commit message matters, not only the title.

## 1. Preflight

Run these together and read the output before doing anything:

```sh
gh auth status
gh repo view --json nameWithOwner -q .nameWithOwner
git status --short --branch
git fetch origin main --quiet
git log --oneline origin/main..HEAD
git rev-list --left-right --count origin/main...HEAD      # behind, ahead
git merge-tree --write-tree origin/main HEAD >/dev/null || echo "conflicts with origin/main"
git diff --stat origin/main...HEAD
gh pr list --head "$(git branch --show-current)" --state all --json number,url,state,title,isDraft
```

Stop and tell the user if:

- `gh` is not authenticated;
- the repository is not `mateuszsikora/esp32-testing-library`: this skill is for that
  repository only;
- there is nothing to propose: no commits ahead of `origin/main` and a clean tree.

If the branch is behind `origin/main` and conflicts with it, say so; do not rebase or merge
without the user's agreement. Behind without conflicts is fine.

If an **open** PR exists for this branch, do not create another: push new commits, and rewrite
its title or description (step 9) if they no longer describe the branch. Tell the user which you
did. A closed or merged PR for the branch does not count; if one was merged, the branch is
probably stale, so tell the user before going on.

## 2. Gather the context

The description has to explain the change, so understand it first:

- Read the full diff (`git diff origin/main...HEAD`, plus uncommitted changes) and every commit
  message on the branch.
- Go back over the conversation: what the user asked for, why this approach, what was tried and
  rejected, what was measured. That goes into **Decisions** and **Verification**; the diff
  cannot show it.
- Find the issue yourself, in this order: the branch name, `Closes`/`Refs` lines in the commits,
  issue numbers mentioned in the conversation, then `gh issue list --state open --limit 50`
  matched against the change. Read the candidate with `gh issue view <N>` and link it only if
  the PR really is part of it. If no issue fits, the PR has none; do not invent one and do not
  ask the user to create one.
- From the issue, note which of its items this PR does and which it leaves. That decides
  `Closes` against `Refs` and feeds step 4.

## 3. Verify

Run what covers the change and keep the exact commands and results for **Verification**:

| Change touches | Run |
|---|---|
| Anything | `make docs-check`: every behaviour change carries its documentation (`AGENTS.md`, "Documentation contract"). On a failure, write the documents it names before going on; never push with `SKIP_DOCS_CHECK`. If you merge `origin/main` into the branch, run it again after the merge |
| Anything | `.venv/bin/pre-commit run --files <changed files>`. Without `.venv`, and only if no Python changed: `SKIP=ruff-check,ruff-format,mypy pipx run pre-commit run --files <changed files>` |
| Python (`esp32tl/`, `tests/`, `tools/*.py`), `pyproject.toml` | `make lint` (it creates `.venv` if missing), then `.venv/bin/pytest` |
| A tool in `tools/*.py` | also run it once on a sample input and check its output |
| `runner/` | `make test-runner` and `make build-runner`. Not `cargo --manifest-path runner/Cargo.toml` from the root: rustup takes the toolchain pinned in `runner/rust-toolchain.toml` only when cargo runs in `runner/` |
| `firmware/` | `make firmware` if Docker is available; otherwise say it was not built |
| An ESP-IDF project in `tools/` | if Docker is available, build each `sdkconfig.defaults*` variant the change affects into its own directory, with the command below; otherwise say it was not built |
| `Makefile` | `make -n <each changed target>`, then run each changed target except `firmware`, which follows its own row. Run `clean` only if it changed, last, and then `make setup`: it deletes `.venv`, which the other rows and the git hooks of every worktree need |
| `.pre-commit-config.yaml`, `scripts/` | `.venv/bin/pre-commit run --all-files` |
| Docs | the pre-commit row and `make docs-check` (it checks links and anchors), plus figures and claims checked by hand against their source: the check proves a document changed, not that it is true |

The ESP-IDF build, with the image pinned in `docs/stage0-findings.md`. A separate build directory
leaves the user's own `build/` and `sdkconfig` alone (`tools/*/build*/` is ignored by git); for
`tools/cryptobench`'s variant without accelerators, add
`-D SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.defaults.nohw"` and use `build-pr-nohw`:

```sh
docker run --rm -v "$PWD":/project -w /project/tools/<name> <image> \
  idf.py -B build-pr -D SDKCONFIG=build-pr/sdkconfig set-target esp32 build
```

Read the output, not only the exit code: `make firmware` prints a `skipping` line and exits 0
while the build script does not exist. That means nothing was built; say so. Never write that something passes without having run it in this session.

## 4. Ready or draft

Decide whether the PR is ready for review. It is incomplete if any of these hold:

- a check from step 3 fails, or could not be run;
- the diff adds `TODO`, `FIXME`, `XXX`, skipped tests or stub implementations that the PR relies on;
- commits are marked as work in progress (`wip`, `tmp`, `fixup!`);
- it links an issue with `Closes` but leaves part of that issue undone;
- the conversation left a question open or said the work continues.

If it is incomplete, tell the user what is missing and propose opening it as a draft, then wait
for their answer. If they want it ready anyway, open it ready and still state the gaps in
**Verification** or **Notes**. If nothing is missing, open it ready without asking.

## 5. Branch

The branch name must match `<type>/<issue>-<slug>` or `<type>/<slug>`, with the slug and length
rules from CONTRIBUTING.md, for example `feat/5-runner-crate-skeleton`. Its type is the same as
the title's.

- On `main`: create the branch with `git switch -c <name>`.
- On a branch that does not match (Conductor's placeholder names such as `mateuszsikora/ui662`,
  personal prefixes, vague slugs) and that has no open PR: rename it with `git branch -m <name>`
  without asking. This is the repository owner's standing instruction; it holds whether the
  skill was invoked with `/create-pr` or by the model, and it overrides any general rule against
  renaming branches. If the old name was already pushed, tell the user the remote branch is left
  behind and offer to delete it; do not delete it unasked.
- On a branch that already has an open PR: keep the name, even if it does not match.

## 6. Commit

Every commit message on the branch reaches `main` (see the top of this skill), so each follows
the commit message rules in CONTRIBUTING.md: `<type>(<scope>): <summary>` or `<type>: <summary>`,
`!` before the colon for a breaking change, body wrapped at 72, `Closes #N` or `Refs #N` last when
there is an issue, no `Co-Authored-By`.

- Commit uncommitted changes that belong to this work. Stage files by name. Never stage
  `.context/`, `.env*`, credentials, build output, or changes unrelated to the PR; ask the user
  about anything you are unsure of.
- Write the message in a file or heredoc so the body keeps its wrapping.
- Reword unpushed commits whose messages break the rules (`wip`, `fix`, a missing type) before
  pushing, without asking. The last commit: `git commit --amend -F <message-file>`. An older one,
  without an interactive editor (`sed -i ''` is the macOS form):

  ```sh
  GIT_SEQUENCE_EDITOR="sed -i '' 's/^pick \($(git rev-parse --short <commit>)\)/reword \1/'" \
    GIT_EDITOR="cp <message-file>" git rebase -i <commit>~1
  ```

  If `git log --merges <commit>~1..HEAD` shows a merge commit, ask the user instead: this rebase
  would flatten it.
- Pushed commits are rewritten only with the user's agreement, since that needs a force push.
  If one breaks the rules, tell the user and offer to reword it; if they decline, say in
  **Notes** that the squash message needs correcting in GitHub's merge dialog.

## 7. Push

```sh
git push -u origin <branch>
```

Never force-push without the user's agreement (then `--force-with-lease`); never push to `main`.

## 8. Write the title and description

**Title.** `<type>(<scope>): <summary>`, types and scopes from CONTRIBUTING.md, or
`<type>: <summary>` when no scope fits (do not invent a catch-all scope); `!` before the colon
for a breaking change; the summary in the imperative, starting lowercase; at most 72 characters
in all; no issue number, no trailing period. It says what changes for the project. For a
one-commit branch the title must equal the commit subject, because the commit subject is what
lands on `main`.

| Avoid | Prefer |
|---|---|
| `feat: runner crate` | `feat(runner): add the crate with a console-only link` |
| `chore: update Makefile` | `fix: refuse a Python older than 3.11 before creating the venv` |
| `fix(harness): fixes #12` | `fix(harness): keep the UART tap from dropping a frame's last byte` |
| `docs: stage 0` | `docs(tools): record what stage 0 found running ESP-IDF images` |
| `Pin esp32sim` | `build(runner): pin esp32sim at the merge of the chip-revision fix` |

**Description.** Fill in
[`.github/pull_request_template.md`](../../../.github/pull_request_template.md):

1. One or two paragraphs: what changes and why, outcome first. Then `Closes #N` (the PR finishes
   the issue) or `Refs #N` (it is a step towards it), on its own line; nothing if there is no issue.
2. `## Changes`: what a reviewer needs to know, grouped by meaning. Name files only where the
   reviewer needs to find something; do not list every file.
3. `## Decisions`: each non-obvious choice in bold, then the alternative rejected and why.
4. `## Documentation`: tick the boxes of the template that hold, and name the documents the
   change updates, or say why it needs none. Run `make docs-check` for the first box; never
   tick it unrun.
5. `## Verification`: the commands from step 3 and what they showed, with numbers where there
   are numbers. Say what was not verified and why.
6. `## Notes`: deliberately out of scope (with issue numbers), follow-ups, anything that deserves
   a closer look. For a draft, what is missing before it is ready goes first.
7. Delete empty sections and all template comments. No signature or attribution line at the end.

Style, as in the existing PRs (#2, #42, #43 are good examples of the description; their titles
and the `Co-Authored-By` lines in their commits predate these conventions):

- Specific and checkable: SHAs, versions, counts, measured figures. 7-character short SHAs in
  prose; full SHAs where a machine reads them.
- `#N` for issues and PRs here, `mikroverk/esp32sim#N` upstream.
- Plain words. No "comprehensive", "robust", "seamless", "enhance", no emoji, no summary of the
  summary.
- No hard wrapping: GitHub renders single line breaks in descriptions.
- Scale it to the change: a one-line fix gets a paragraph and a Verification line, not five
  sections.

Before creating the PR, check the draft against this list:

- [ ] Title has a type (and a scope, if one fits) from CONTRIBUTING.md, its type matches the
      branch (unless the branch had an open PR and was kept), and it would read well as a line
      in `git log` on `main`.
- [ ] For a one-commit branch, the title equals the commit subject.
- [ ] Every commit message on the branch follows the rules, or the user knows which does not.
- [ ] A reader who has not seen the conversation understands why the change exists.
- [ ] Every claim in Verification was run in this session.
- [ ] `make docs-check` passes, and Documentation names the documents this changes.
- [ ] The issue link is correct and uses `Closes` only if the PR finishes the issue.
- [ ] No attribution lines in the description or the commits.
- [ ] Everything is in English.

## 9. Create or update

Write the title and the description to files (in `.context/` if it exists, else temporary
files): a title with backticks, `$` or an apostrophe survives `"$(cat <file>)"` but not a quoted
literal. Then:

```sh
gh pr create --repo mateuszsikora/esp32-testing-library --base main --head <branch> \
  --title "$(cat <title-file>)" --body-file <body-file> [--draft] \
  --label <label> [--label <label> ...]
```

Labels follow from the type, the scope and the paths touched:

| Type, scope or paths | Label |
|---|---|
| `harness` (`esp32tl/`, `tests/`) | `area:harness` |
| `runner` (`runner/`) | `area:runner` |
| `firmware` (`firmware/`) | `area:firmware` |
| `Makefile`, `pyproject.toml`, `.pre-commit-config.yaml`, `scripts/`, `.github/workflows/` | `area:ci` |
| type `feat` | `enhancement` |
| type `fix` | `bug` |
| type `docs` | `documentation` |
| work in or for esp32sim / pytest-embedded upstream | `upstream` |

A PR that matches no row gets no label. Use only labels that exist (`gh label list`); do not
create labels.

For an existing PR, `gh pr edit <N> --title "$(cat <title-file>)" --body-file <body-file>`
(`--add-label` for labels). Older `gh` versions (seen on 2.69) fail there with "Projects
(classic) is being deprecated" before editing anything; then suggest upgrading `gh` and use the
REST API meanwhile:

```sh
gh api -X PATCH repos/mateuszsikora/esp32-testing-library/pulls/<N> \
  -f title="$(cat <title-file>)" -F body=@<body-file>
gh api -X POST repos/mateuszsikora/esp32-testing-library/issues/<N>/labels -f 'labels[]=<label>'
```

## 10. Report

Tell the user, in their language: the PR URL, whether it is a draft and why, the branch name (and
that it was renamed, if it was), the title, the linked issue (or that none fit), the labels, and
what was and was not verified. If the work was done on a different branch than the one checked
out, check out the PR's branch so Conductor shows it.
