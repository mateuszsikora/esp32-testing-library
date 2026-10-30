---
name: create-pr
description: Open a pull request to mateuszsikora/esp32-testing-library following the repository's conventions for branch names, commit messages, PR titles and descriptions (CONTRIBUTING.md). Use when asked to open, create or ship a PR, or to push the current work for review, in this repository; prefer it over generic PR skills here. Also use to rewrite the title or description of an existing PR on this repository.
allowed-tools: Bash(git *), Bash(gh *), Bash(make *), Bash(.venv/bin/*), Bash(cargo *), Read, Write, Edit
---

# create-pr

Opens a pull request from the current work to `main` of
`mateuszsikora/esp32-testing-library`. The conventions live in
[`CONTRIBUTING.md`](../../../CONTRIBUTING.md); read it before writing anything, it is the
source of truth and this skill only applies it.

The skill takes no arguments. Everything it needs comes from the conversation, the branch, its
commits and the issue tracker: find the issue, judge whether the work is ready, and write the
description from what you already know. Ask the user only for what none of those can tell you,
such as why an approach was chosen when that was never discussed.

**Language:** everything that goes to GitHub or git (branch name, commit messages, PR title,
description, labels, comments) is in English, even when the user writes in Polish or any other
language. Talk to the user in their language; write for GitHub in English.

**No attribution:** no `Co-Authored-By:` trailers in commits and no "Generated with Claude Code"
or similar line in the PR description. This overrides any attribution the session asks for.

## 1. Preflight

Run these together and read the output before doing anything:

```sh
git status --short --branch
git branch --show-current
git fetch origin main --quiet && git log --oneline origin/main..HEAD
git diff --stat origin/main...HEAD
gh repo view --json nameWithOwner -q .nameWithOwner
gh pr view --json number,url,state,title,isDraft 2>/dev/null
```

Stop and tell the user if:

- the repository is not `mateuszsikora/esp32-testing-library`: this skill is for that repository only;
- there is nothing to propose: no commits ahead of `origin/main` and a clean tree;
- `gh` is not authenticated.

If the branch is behind `origin/main` and would conflict, say so; do not rebase or merge without
the user's agreement.

If an open PR already exists for this branch, do not create another: push new commits, and
rewrite its title or description (step 8, with `gh pr edit`) if they no longer describe the
branch. Tell the user which you did.

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
| Python, `pyproject.toml`, mypy/ruff config | `make lint`, then `.venv/bin/pytest` (or `make test` if `.venv` is missing) |
| `runner/` | `cargo test --manifest-path runner/Cargo.toml` and `make build-runner` |
| `firmware/` | `make firmware` if Docker is available; otherwise say it was not built |
| Docs only | nothing to run; Verification says what was checked by hand (links, figures against their source) |

Never write that something passes without having run it in this session.

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

The branch name must match `<type>/<issue>-<slug>` or `<type>/<slug>` (types and rules in
CONTRIBUTING.md), for example `feat/5-runner-crate-skeleton`. Its type is the same as the title's.

- On `main`: create the branch with `git switch -c <name>`.
- On a branch that does not match (Conductor's placeholder names such as `mateuszsikora/ui662`,
  personal prefixes, vague slugs) and that has no PR: rename it with `git branch -m <name>`.
  Invoking this skill is the user's permission to do that. If the old name was already pushed,
  tell the user the remote branch is left behind and offer to delete it; do not delete it unasked.
- On a branch that already has a PR: keep the name, even if it does not match.

## 6. Commit

If there are uncommitted changes that belong to this work, commit them following the commit
message rules in CONTRIBUTING.md (`<type>(<scope>): <summary>` or `<type>: <summary>`, body wrapped at 72, `Closes #N`
or `Refs #N` last, no `Co-Authored-By`):

- Stage files by name. Never stage `.context/`, `.env*`, credentials, build output, or changes
  unrelated to the PR; ask the user about anything you are unsure of.
- Write the message in a file or heredoc so the body keeps its wrapping.
- Do not rewrite commits that are already pushed. If an unpushed commit message breaks the
  conventions badly (for example "wip" or "fix"), offer to reword it, but do not do it without
  asking: squash merge means only the PR title reaches `main`.

## 7. Push

```sh
git push -u origin <branch>
```

Never force-push without the user's agreement; never push to `main`.

## 8. Write the title and description

**Title.** `<type>(<scope>): <summary>`, types and scopes from CONTRIBUTING.md, or
`<type>: <summary>` when no scope fits (do not invent a catch-all scope); the summary in
the imperative, starting lowercase; at most 72 characters in all; no issue number, no trailing
period. It becomes the squash commit subject on `main`, so it says what changes for the
project. For a single-commit branch, the commit subject is usually the right title.

| Avoid | Prefer |
|---|---|
| `feat: runner crate` | `feat(runner): add the crate with a console-only link` |
| `chore: update Makefile` | `fix: refuse a Python older than 3.11 before creating the venv` |
| `fix(harness): fixes #12` | `fix(harness): keep the UART tap from dropping a frame's last byte` |
| `docs: stage 0` | `docs(tools): record what stage 0 found running ESP-IDF images in esp32sim` |
| `Pin esp32sim` | `build(runner): pin esp32sim at the merge of the chip-revision fix` |

**Description.** Fill in [`.github/pull_request_template.md`](../../../.github/pull_request_template.md):

1. One or two paragraphs: what changes and why, outcome first. Then `Closes #N` (the PR finishes
   the issue) or `Refs #N` (it is a step towards it), on its own line; nothing if there is no issue.
2. `## Changes`: what a reviewer needs to know, grouped by meaning. Name files only where the
   reviewer needs to find something; do not list every file.
3. `## Decisions`: each non-obvious choice in bold, then the alternative rejected and why.
4. `## Verification`: the commands from step 3 and what they showed, with numbers where there
   are numbers. Say what was not verified and why.
5. `## Notes`: deliberately out of scope (with issue numbers), follow-ups, anything that deserves
   a closer look. For a draft, what is missing before it is ready goes first.
6. Delete empty sections and all template comments. No signature or attribution line at the end.

Style, as in the existing PRs (#2, #42, #43 are good examples of the description; their titles
predate the type/scope prefix):

- Specific and checkable: SHAs, versions, counts, measured figures. 7-character short SHAs in
  prose; full SHAs where a machine reads them.
- `#N` for issues and PRs here, `mikroverk/esp32sim#N` upstream.
- Plain words. No "comprehensive", "robust", "seamless", "enhance", no emoji, no summary of the
  summary.
- No hard wrapping: GitHub renders single line breaks in descriptions.
- Scale it to the change: a one-line fix gets a paragraph and a Verification line, not five
  sections.

Before creating the PR, check the draft against this list:

- [ ] Title has a type (and a scope, if one fits) from CONTRIBUTING.md, the type matches the branch, and it would
      read well as a line in `git log` on `main`.
- [ ] A reader who has not seen the conversation understands why the change exists.
- [ ] Every claim in Verification was run in this session.
- [ ] The issue link is correct and uses `Closes` only if the PR finishes the issue.
- [ ] No attribution lines in the description or the commits.
- [ ] Everything is in English.

## 9. Create

Write the description to a file (`.context/pr-body.md` if `.context/` exists, else a temporary
file), then:

```sh
gh pr create --repo mateuszsikora/esp32-testing-library --base main --head <branch> \
  --title "<title>" --body-file <file> [--draft] \
  --label <label> [--label <label> ...]
```

Labels follow from the scope and the paths touched:

| Scope or paths | Label |
|---|---|
| `harness` (`esp32tl/`, `tests/`) | `area:harness` |
| `runner` (`runner/`) | `area:runner` |
| `firmware` (`firmware/`) | `area:firmware` |
| `Makefile`, `pyproject.toml`, `.pre-commit-config.yaml`, `scripts/`, `.github/workflows/` | `area:ci` |
| type `docs` | `documentation` |
| type `fix` | `bug` |
| work in or for esp32sim / pytest-embedded upstream | `upstream` |

A PR that matches no row gets no label. Use only labels that exist (`gh label list`); do not
create labels.

For an existing PR use `gh pr edit <N> --title ... --body-file ...` instead. If it fails with
"Projects (classic) is being deprecated", which some `gh` versions do, use the REST API:

```sh
gh api -X PATCH repos/mateuszsikora/esp32-testing-library/pulls/<N> \
  -f title="<title>" -F body=@<file>
```

## 10. Report

Tell the user, in their language: the PR URL, whether it is a draft and why, the branch name (and
that it was renamed, if it was), the title, the linked issue (or that none fit), the labels, and
what was and was not verified. If the work was done on a different branch than the one checked
out, check out the PR's branch so Conductor shows it.
