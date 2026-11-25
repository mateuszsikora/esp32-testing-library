---
name: triage-feedback
description: Triage a feedback report on esp32tl (a Markdown file written by a consumer, a person or an agent that tests its own firmware with esp32tl) and open one GitHub issue on mateuszsikora/esp32-testing-library for each point worth addressing that the code, the issues and the pull requests do not cover yet. Use when the user hands over such a report, or asks to review, verify or triage consumer feedback, in this repository.
allowed-tools: Bash(git fetch *), Bash(git rev-parse *), Bash(git status *), Bash(git grep *), Bash(git show *), Bash(git log *), Bash(gh *), Bash(docker run *), Bash(docker images *), Bash(docker image inspect *), Bash(grep *), Bash(ls *), Read, Write, Edit
---

# triage-feedback

Reads a feedback report on esp32tl, verifies every point against the repository, decides what is
worth doing here, and opens one self-contained issue for each point that nothing covers yet.
Invoking this skill is the user's consent to open those issues. It gives no consent for anything
else: no comment, edit or close on another issue, nothing upstream, no push and no pull request.

**Argument:** the path of the report, usually under `.context/attachments/`. Without one, take
the report attached to the conversation. If there is none, ask for it.

**Language:** issues are in English, whatever the report's language. Talk to the user in their
language.

**The goal is a universal, high-quality esp32tl.** Every point is judged by what it gives anyone
who tests firmware with esp32tl, on every chip, not by what the reporting project needs. The
reporting project is never named in an issue, and its needs never justify scope.

## 1. Read the report and the state of the repository

```sh
git fetch origin main --quiet
git rev-parse origin/main          # the commit every link in the issues points at
git rev-parse HEAD                 # compared with origin/main: the working tree is not what you cite
git status --short
gh issue list --repo mateuszsikora/esp32-testing-library --state all --limit 1000 \
  --json number,title,state,stateReason
gh pr list --repo mateuszsikora/esp32-testing-library --state all --limit 1000 \
  --json number,title,state,headRefName,mergedAt
gh release list --repo mateuszsikora/esp32-testing-library --limit 5
```

Read the whole report. Note the esp32tl version it was run on, and the framework and version
the reporter's firmware is built with (ESP-IDF, Arduino-ESP32 or PlatformIO, and which
release). Split the report into points, one per gap, request or finding. A point that mixes two
requests becomes two points. Points the report lists under "worked well" are not triaged. Read
them all the same, as they say what an issue must not break.

This run's drafts go in `.context/issues/<YYYY-MM-DD>-<slug of the report>/`; writing the
first draft there creates it. Drafts of earlier runs stay where they are. Never take one of them
for this run's.

## 2. Verify every point in the code

Take nothing in the report as true until it has been checked. For each point:

- **Find the code it is about, at `origin/main`, never in the working tree.** The workspace is
  often on another branch, or has uncommitted changes, and the permalinks of step 4 point at
  `origin/main`, so every quote and line number must come from there:

  ```sh
  git grep -n '<pattern>' origin/main -- <path>
  git show origin/main:<path> | grep -n '<pattern>'
  ```

  Read a file at `origin/main` with its line numbers, and cite its lines from that, with
  `git grep -n '' origin/main -- <path>`. For one range, filter by the numbers, for lines 120
  to 149: `git grep -n '' origin/main -- <path> | grep -E '^[^:]+:[^:]+:1[2-4][0-9]:'`.
  Look at the module, the function and its docstring, and the user documentation in
  `docs/features/`. Quote what you find with file and line.
- **Check whether it is already covered**:
  - in the code;
  - in an open or closed issue. Search the titles of step 1, and also search by the point's key
    words, which finds bodies too:
    `gh issue list --repo mateuszsikora/esp32-testing-library --state all --search "<words> in:title,body"`.
    Read each candidate with `gh issue view N`;
  - in a pull request, open or merged:
    `gh pr list --repo mateuszsikora/esp32-testing-library --state all --search "<words>"`,
    then `gh pr view N`.

  The result decides the point:
  - A point an open issue already covers gets no new issue; name that issue in the report.
  - An issue closed as `NOT_PLANNED`: read its closing comment. A refusal on the merits stands
    like a rejected decision. A closure that waits for something ("yet", "until") is a
    postponement, and the rule for postponed decisions below applies. Either way, a new issue
    names the closed one.
  - An issue closed as `DUPLICATE` points at the issue that really covers the point: follow it.
  - A point a merged pull request fixed after the version the reporter ran is already done. Say
    whether the fix is in a release (step 1's `gh release list`), or only on `main` until the
    next one.
- **Look the topic up in `docs/decisions/`** at `origin/main`, the index and the domain files:
  `git grep -n '<topic>' origin/main -- docs/decisions/`.
  - A decision that rejected what the report asks for stands, unless the report brings evidence
    that its premise is false. Then the issue amends that decision and says why.
  - A decision that postponed a generic gap "until a test needs it" is no reason to postpone
    again: if the gap is real and generic, it gets an issue now.
- **Find the cause.** "To investigate" in a report is an invitation, not a verdict: follow the
  data path through esp32tl (and the runner) and decide whether the harness, the firmware or
  the reporter's test is the likely cause.
- **Check third-party behaviour in the reporter's version.** esp32tl runs the consumer's
  firmware unmodified, built with their framework, so what lwIP, FatFs, esp-mqtt or an ESP-IDF
  driver does must be read in the version the report names:
  - When one of the containers `firmware/build.sh` pins has that version (v5.3.6 by default,
    v5.5.4 for the ESP32-P4, v6.1 for the ESP32-S31), use it. Use the digest from the script.
  - Otherwise use `espressif/idf:<the reporter's version>` without a digest. Say in the issue
    that it is not one of the pinned images.
  - Arduino-ESP32 and PlatformIO's `framework-espidf` are built on an ESP-IDF release
    their own release names. Find that version first, and apply the two rules above to it.
  - When no version can be found, use the pinned container for the reporter's chip. Say in
    `### Today` that the quote comes from that version, and that the report names none.

  ```sh
  docker images | grep espressif/idf     # a pinned image may show only its digest
  docker run --rm --entrypoint sh <image> -c 'grep -n "<symbol>" /opt/esp/idf/components/<path>'
  ```

  Cite what you find with the ESP-IDF version, file and line, and default Kconfig values with
  their line.
- **Do not build firmware or run the suite to triage.** Read code and sources. A finding that
  is not reproduced says so in its issue, and reproducing it becomes the issue's first task,
  with the image and steps to use, preferably an image in `firmware/` that needs no change.

## 3. Decide what is worth doing, and where

For each point, choose one:

- **An issue here**: a real gap or bug in esp32tl that anyone testing firmware could hit, with
  a design that fits the project's decisions.
- **Already covered**: an open issue or pull request does it, or the code already does and the
  report missed it. If the documentation already says it clearly, nothing changes. If it does
  not, the issue is a documentation one.
- **The consumer's job**: what the reporter's test or firmware should do, or what is easy in
  their tests and would only add surface to esp32tl. For example, a way of structuring their
  tests, or a value they can already compute from what esp32tl gives.
- **esp32sim's job**: the emulator, not the harness. Do not open an issue upstream or comment
  on the tracker #229. Report it to the user with the evidence, and propose an entry for #229,
  which needs the user's yes.
- **Dropped**: out of scope, too costly for what it gives, or against a decision (or a
  `NOT_PLANNED` issue) whose premise holds. Dropped points get no `docs/decisions/` entry and no
  reply to the reporter. Only the report to the user names them, with the reason.

Judge critically, in both directions:

- A report can blame the firmware for a bug that is ours, or ask for a feature when a smaller,
  more general one would serve better: propose that one.
- A request for a convenience over what the harness cannot know is wrong. One example is
  file-system events from a card that sees only blocks. The issue then offers what can be known
  correctly, and its decision rejects the rest with the reason.

When two accepted points share a design (the same kind of handler on two services), make them
separate issues that refer to each other and say what must stay consistent, unless one PR could
not sensibly do one without the other.

## 4. Write each issue

Draft each issue in this run's directory as two files: `<letter>-<slug>.title`, holding the
title on one line, and `<letter>-<slug>.md`, holding the body. Each issue must let someone
implement it after this conversation is gone, from its text alone:

- **Title**: one imperative sentence that says what changes, with the API name where there is
  one, no type prefix and no trailing period. Examples from the tracker: "Let a model change its
  UART line rate, learn the firmware's, and take frame errors" and "Record the modem's power
  changes, on as well as off".
- **Opening paragraph**: what a test cannot do today and why that matters to anyone testing
  firmware, without naming the reporter. Say what was seen, in general terms, if the issue is a
  bug.
- **`### Today`**: start with "All links are at <7-character SHA>.". Then list the facts with
  permalinks, `https://github.com/mateuszsikora/esp32-testing-library/blob/<full SHA>/<path>#L<a>-L<b>`.
  Quote docstrings, messages and decisions verbatim. Check every line range you cite against
  `git grep -n '' origin/main -- <path>` before you publish. Third-party sources are cited by
  path, line and version, as found in step 2.
- **`### What to do`**: numbered steps. Give names, signatures, messages quoted as they should
  read, defaults and limits. Name the files to change and the tests to write: unit tests without
  firmware, and, where it needs them, tests on the emulator with an image from `firmware/`, saying
  whether it needs a change. Values that need a decision ("suggested, to be confirmed in the
  decision") are marked as such. The last step is **Documentation**:
  - for each file the change touches, the documents the contract table in `AGENTS.md` names,
    with the section to change;
  - `docs/decisions/<domain>.md` for each choice and rejected alternative, followed by
    `python3 scripts/decisions-index.py`;
  - `docs/invariants.md` for the rules a test will enforce. Do not give invariant numbers, as
    other branches take them.
- **`### Acceptance criteria`** and the closing line. The criteria are checkboxes a reviewer can
  tick. Copy the end of the body as it stands here:

  ```markdown
  - [ ] `make docs-check` passes.

  Implementation must include the relevant documentation in the same change and pass `make docs-check` (see `AGENTS.md`).
  ```

Plain words, specific and checkable, no hard wrapping. Name the reporter's environment only
where it matters, for example "a consumer on 0.4.0".

## 5. Open the issues

Labels:

- The type:
  - `bug` for a bug;
  - `enhancement` for a feature;
  - `documentation` for an issue that is documentation only, or beside `enhancement` when the
    issue mostly changes what the documentation says;
  - `question` for an issue whose only task is to find something out, for example whether a
    finding reproduces or what the hardware does.
- The area, by where the work is:
  - `area:harness` for `esp32tl/` and `tests/`;
  - `area:runner` for `runner/`;
  - `area:firmware` for `firmware/`;
  - `area:ci`.

Only use labels that exist (`gh label list`).

```sh
gh issue create --repo mateuszsikora/esp32-testing-library \
  --title "<the title from the .title file>" \
  --label <type> --label <area> --body-file <run directory>/<letter>-<slug>.md
```

The title goes in double quotes, because titles often hold an apostrophe. Escape each `` ` ``,
`$`, `"` and `\` in it with a backslash, or the shell runs what is between backticks.

Open this run's issues one at a time, so that a later one can name an earlier one's number. If
two refer to each other, open both, then add the second number to the first with
`gh issue edit N --repo mateuszsikora/esp32-testing-library --body-file <file>`. That edits an
issue this run opened, so it needs no further consent.

## 6. Report

Tell the user, in their language:

- A table of the issues opened: number with link, the report's point, and one line on what each
  does. Mark the one to do first, if one is.
- The points not turned into issues, each with one line:
  - already covered, by what, and whether a fix is released or only on `main`;
  - the consumer's job, and how they can do it;
  - esp32sim's job, with the proposed #229 entry, waiting for the user's yes;
  - dropped, and why.
- What was not verified, for example a finding not reproduced, and which issue starts with
  reproducing it. Also name any third-party behaviour read in another version than the
  reporter's.
