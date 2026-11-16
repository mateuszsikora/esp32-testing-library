# CI

For whoever changes [`.github/workflows/ci.yml`](../.github/workflows/ci.yml), its called
workflows (`build-firmware.yml`, `build-runner.yml`, `test-platform.yml`), or reads one of its
runs: what each job does, where it runs, and how the builds reach the test jobs. Why it is built
this way, and what was rejected, is in [decisions/ci.md](decisions/ci.md). Running the same things
locally is [guides/development.md](guides/development.md) and
[guides/testing.md](guides/testing.md).

## When it runs

The workflow runs on every pull request, on every push to `main`, and by hand
(`workflow_dispatch`). Its token has `contents: read` and no other permission.

A new push to a pull request cancels the run for the push before. On `main` every commit gets its
own concurrency group and its own run, and nothing is cancelled: a shared group would keep at most
one run waiting and cancel the others.

## Public repository settings

Verified and configured through the GitHub API on 15 November 2026. These are repository
settings, not settings applied by the workflow files:

- Fork workflow approval is `all_external_contributors`: a maintainer must approve runs
  requiring approval from external contributors, including returning contributors. Before
  approving, inspect the proposed workflow and the code it executes. See
  [GitHub's fork approval policy](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions-settings-for-a-repository).
- CI uses `pull_request`, not `pull_request_target`. Fork PRs receive a read-only token and
  no repository secrets. All jobs use GitHub-hosted VMs; the repository has no registered
  self-hosted runners. Do not register a machine with private network access for public CI.
- The default workflow permission is `read`. On 16 November 2026, the owner enabled
  **Allow GitHub Actions to create and approve pull requests** for the release-preparation
  workflow; that workflow only creates PRs, never approves or merges them.
  Each CI workflow also declares `contents: read`. The repository allows all actions;
  action tags are not required to be pinned to full commit SHAs.
- Secret scanning, secret scanning push protection and Dependabot vulnerability alerts
  are enabled. Dependabot automatic security-update PRs remain disabled.
- `main` requires a pull request, zero approving reviews, resolved review conversations,
  and all 25 CI checks from the GitHub Actions app (app ID `15368`). These are the eight
  `firmware / firmware (...)` checks, `runner-linux / runner`, `runner-macos / runner`,
  six `test-linux / test (...)` and six `test-macos / test (...)` checks, `determinism`,
  `lint` and `docs`, matching the job matrix below. The 14 native-build and test context
  names were updated for the separate platform calls; all 25 checks remain required.
  Strict status checks require the branch to be up to date with `main`. Protection applies
  to administrators too; force pushes and branch deletion are disabled. When renaming jobs
  or changing matrix entries, update the required check names in branch protection as well.
  GitHub accepts `success`, `skipped` and `neutral` check conclusions, so maintainers must
  still review workflow changes that could skip checks.

Fork PRs can restore base-branch build caches. Their new cache entries are scoped to
`refs/pull/<number>/merge`; `main` and other PRs cannot restore them. Never put secrets in
caches. See [GitHub's cache scope rules](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching).

The audit found no repository Actions secrets and no secrets in the `pypi` environment.
At audit time that environment had no deployment protection rules or branch restrictions.
The publishing workflow added here uses it only after a GitHub release is published, with
OIDC permission confined to the upload job; ordinary CI does not use it.
This audit checked configuration; it did not execute a test PR from an external fork.

## Jobs

GitHub-hosted macOS runners have no Docker, so the validation firmware is built once, in the
pinned ESP-IDF and PlatformIO containers in eight parallel Linux jobs, and the runner crate once
per platform.
The test jobs then run only the runner and Python: they build nothing. The table describes full
CI; [version-only changes](#version-only-ci) use short Ubuntu jobs with the same context names.

| Job | Runs on | What it does |
|---|---|---|
| `change` | `ubuntu-24.04` (x64); Python 3.11 | Checks exact version-only diffs before selecting full or lightweight CI |
| `firmware` | `ubuntu-24.04` (x64) | Eight parallel groups (`idf-1` to `idf-4`, `examples-1` to `examples-3`, `platformio`) once per content of `firmware/`, `examples/`, the build workflow and the `Makefile`, the Arduino image with PlatformIO's core directory restored from the cache; `idf-1` fetches the ROM ELF once per revision of `scripts/fetch-rom-elf` |
| `runner-linux`, `runner-macos` | `ubuntu-24.04` (x64); `macos-15` (ARM64) | `make build-runner test-runner`, once per content of `runner/` (its sources, its esp32sim pin, its toolchain), the build workflow and the `Makefile` |
| `test-linux`, `test-macos` | `ubuntu-24.04` (x64); `macos-15` (ARM64); Python 3.11, 3.13, 3.14; pairs `1/4 4/4` and `2/4 3/4` | Unpacks the images and the runner, restores the ROM ELF, runs wall-clock tests sequentially, then two existing pytest shards concurrently; builds nothing |
| `determinism` | `ubuntu-24.04` (x64); Python 3.11 | Unpacks the images and the runner as `test` does, and runs `make determinism`: the determinism tests twice, the two runs compared |
| `lint` | `ubuntu-24.04` (x64); Python 3.11 | `make lint`, clippy included |
| `docs` | `ubuntu-24.04` (x64); pull requests and manual runs | `make docs-check` against the pull request's base branch |

`test-linux` and `determinism` need only `firmware` and `runner-linux`; `test-macos` needs only
`firmware` and `runner-macos`. The two native builds start independently of firmware and each
other. A queued or failed macOS build does not block Linux tests or determinism, and the reverse
holds for macOS tests. `lint` and `docs` have no build dependencies; `lint` waits for change classification. The firmware and test matrices
use `fail-fast: false`; one platform or Python version failing does not cancel another.

Both native build calls use `build-runner.yml` with a required `platform` input. Both test calls
use `test-platform.yml` with that platform and the exact firmware, ROM ELF and native-runner keys
from their dependencies. The reusable workflow's `test` job expands only Python versions and
shard pairs. The sections below use `runner` and `test` for these shared job definitions.

## Where the jobs run

All Linux jobs use the standard `ubuntu-24.04` image (x64). The native build and test
calls in full CI include `macos-15` (Apple Silicon, ARM64), including in private repositories.
These labels and architectures are listed for both repository visibilities in
[GitHub's standard runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
(checked 15 November 2026). No larger runners are used. Each job checks `RUNNER_OS`,
`RUNNER_ARCH` and `uname -m` before using caches. There are two native runner builds and
12 pytest jobs: two platforms × three Python versions × two shard pairs. All four shards
still run once on each OS/Python combination. A pair uses two processes on one VM.

Hosted jobs use fresh VMs. The
[Ubuntu image inventory](https://github.com/actions/runner-images/blob/main/images/ubuntu/Ubuntu2404-Readme.md)
supplies Docker Engine, make and the native compiler; the
[macOS image inventory](https://github.com/actions/runner-images/blob/main/images/macos/macos-15-arm64-Readme.md)
supplies Xcode command-line tools, make and Homebrew. Python is selected with
`actions/setup-python`; rustup installs the pinned toolchain when needed.
The Linux `firmware` job on a cache miss and every Linux `test` job run
`sudo systemctl start docker` and `docker info` as the job user. An unavailable daemon fails
the job before pytest. Currently only generated marker tests use `requires_docker`; this also
keeps the daemon available for future container tests. macOS has no Docker; the existing
`requires_docker` marker still skips tests requiring it there.
Every `test` job installs mtools (apt on Linux, Homebrew on macOS) and checks all five
commands: `mformat`, `mcopy`, `mdir`, `mtype`, `mmd`.

On a cache miss the firmware job reports free space, then removes unused SDKs only while less
than 20 GiB is free: Android (`/usr/local/lib/android`), .NET (`/usr/share/dotnet`), then Haskell
(`/opt/ghc`, `/usr/local/.ghcup`), in that order. It stops as soon as the threshold is met and
reports space again with `df -h .`. The pinned containers,
PlatformIO packages and all project build trees must fit on a standard hosted disk; Docker
images are pulled afresh. TODO(owner): measure peak disk use and cold-build duration on the
hosted VM; the existing 60-minute firmware and 15-minute test limits remain unchanged.

Private runs consume the account's hosted allowance and may incur charges. The repository is
now public; publication needed no workflow switch because both platforms already ran.
Branch protection is configured as described above.

## How the builds reach the test jobs

The build jobs hand their output to the test jobs through the Actions cache, not as artifacts:
artifacts count against the account's storage quota, which is full, while the cache has 10 GB per
repository of its own.

Each build job saves its output under a key it publishes as a job output, and the test jobs
restore that exact key or fail (`fail-on-cache-miss`). The key is also what keeps a build from
running twice: a job whose key is already cached builds nothing. The firmware and runner keys hash
the build workflow and the `Makefile` as well as the sources, so a change to how a build is made or packed
is a new key too.

On a miss, a build job saves its tarball in the step "Save the tarball" (`actions/cache/save`)
right after building it, then checks that the cache kept it: the step "Check that the cache kept
the tarball" looks the key up (`lookup-only`) and fails the build job on a miss with `Failed to
restore cache entry. Exiting as fail-on-cache-miss is set. Input key: <key>`. A failed save does not
fail its step: `Failed to save: Unable to reserve cache with key <key>, another job may be creating
this cache.` is a plain log line, not even a warning, and other failures are a warning or an error
annotation. Without the check the build job would stay green and every test job would fail on the
miss instead. On a hit, a build job saves and checks nothing. The ROM ELF entry and PlatformIO's
core directory are saved by `actions/cache` at the end of `firmware (idf-1)` and `firmware (platformio)`, respectively, and not checked, since
a miss only downloads them again.

| Cache entry | Key | Saved by | A miss in `test` |
|---|---|---|---|
| `build/ci/firmware-<group>.tar` | `firmware-` and the hash of `firmware/**`, `examples/**`, `Makefile`, `.github/workflows/build-firmware.yml`, then the group suffix (`-idf-1` to `-idf-4`, `-examples-1` to `-examples-3`, `-platformio`) | Matching `firmware` matrix member | Fails the job if any group is missing |
| `build/ci/runner.tar` | `runner-` and the hash of `runner/**`, `Makefile`, `.github/workflows/build-runner.yml`, then the runner's OS and architecture | `runner-linux` or `runner-macos` | Fails the job |
| `.cache/esp-rom-elfs` | `rom-elf-` and the hash of `scripts/fetch-rom-elf` | `firmware (idf-1)` | No failure: `make rom-elf` downloads the ELF |
| `.cache/platformio`, without `dist/` and `tools/` | `platformio-` and the SHA-256 of the lines `CONTAINER=` and `PIOARDUINO_VERSION=` of `firmware/build-platformio.sh` and `platform =` of each `firmware/*/platformio.ini`, then the runner's OS and architecture | `firmware (platformio)`: restored when the firmware key misses, saved when the `platformio-` key missed too | Not restored there; a miss in `firmware` downloads PlatformIO Core, the platform, the framework and the toolchains again |

Firmware and ROM contain ESP32 data, not host executables, so Linux and macOS use the same
entries. Both hosted systems use zstd for Actions cache compression. Native runner entries end in
`Linux-X64` or `macOS-ARM64`, matching their consumers, including Linux determinism.
PlatformIO's tools stay in their `Linux-X64` cache and are never restored on macOS.
The build workflow hash invalidates firmware and runner tarballs for this migration. Each firmware
group has a separate path and key suffix; the matrix publishes the same base key regardless of
which group finishes last. Both consumers restore and unpack all eight explicit group paths using `.github/actions/restore-firmware/action.yml`.
Exact restores and post-save verification stay unchanged; changing to artifacts is unnecessary.

A cache entry unused for 7 days is evicted. A test job re-run on its own after that fails on the
cache miss: re-run all jobs instead, which builds again. A bad tarball saved under a key stays there
until it is deleted:

```sh
gh cache delete <key>
```

When "Check that the cache kept the tarball" fails, re-run the build job. A second job that built
the same key at the same time and was still uploading it has finished its upload by then, and the
re-run hits its entry. If the re-run fails the same way, the key is blocked: an entry under it was
reserved and never finished. `gh cache list` does not show such an entry, so `gh cache delete`
cannot remove it; a change to the affected build workflow makes a new key. TODO(owner): whether a blocked key frees
itself, and after how long.

## `firmware`

Only `firmware (platformio)`, on a cache miss, restores PlatformIO's core directory, `.cache/platformio`, under its own
key (the step "PlatformIO's core directory"), which `firmware/build-platformio.sh` mounts into its
container: PlatformIO Core, pioarduino's platform, the Arduino-ESP32 framework and the toolchains,
about 2.0 GB compressed on hp600g1. `dist/`, the downloaded archives, and `tools/`, a second copy of the
toolchains, are left out: a build needs neither
([decisions/firmware.md](decisions/firmware.md#platformios-core-directory-is-cacheplatformio)).
The key changes only with those pins, so a change to the sketch reuses the entry
([decisions/ci.md](decisions/ci.md#platformios-core-directory-is-cached-under-the-key-of-its-pins)).

The matrix runs these independent commands on separate standard VMs:

| Group | Build command | Tarball |
|---|---|---|
| `idf-K`, K=1..4 | `firmware/build.sh --shard K/4` | `build/ci/firmware-idf-K.tar` |
| `platformio` | `firmware/build-platformio.sh` | `build/ci/firmware-platformio.tar` |
| `examples-K`, K=1..3 | `firmware/build.sh --examples --shard K/3` | `build/ci/firmware-examples-K.tar` |

`FIRMWARE_OUT` is empty, preserving the default repository paths. Each shard keeps
its shared in-container ccache across its selected images. All projects still build; no image list is copied
into the workflow. The groups can overlap instead of adding their durations, subject to hosted
capacity. This does not yet establish a 5- or 10-minute total CI duration.

Each group packs only what the backend reads. Of an ESP-IDF build, what pytest-embedded's `IdfApp` reads: the files at the top of each
`firmware/<image>/build/` and `examples/<name>/build/` (`flasher_args.json`, the ELF, the binaries,
`esp32tl-build.json`, `esp32tl-built`), `config/sdkconfig.json`, and the `.bin` and `.elf` files of
`bootloader/` and `partition_table/`. Of a PlatformIO build, `firmware/<image>/.pio/build/esp32dev/`,
the `.bin` files, `firmware.elf`, `esp32tl-build.json` and `esp32tl-built` at its top. The object
files and PlatformIO's `firmware.map` stay behind.

Before the tarball is written, and so before it is cached for good, a Python step checks every
image for the files the tests need: `esp32tl-built`, `esp32tl-build.json`, `flasher_args.json`,
`config/sdkconfig.json`, every flash image `flasher_args.json` names, `project_description.json`
and the ELF it names; of a PlatformIO build, `esp32tl-built`, `esp32tl-build.json`, `firmware.bin`,
`firmware.factory.bin`, `firmware.elf`, `bootloader.bin` and `partitions.bin`. Each missing
file is reported as `::error::<path> is missing from the tarball` and fails the job. The job then
lists the tarball (`tar -tvf`), hit or miss, and on a miss saves it and checks that the cache kept
it ([above](#how-the-builds-reach-the-test-jobs)).

`firmware/build.sh` holds the ESP-IDF tag, the container digest and the project each image is built
from, and `firmware/build-platformio.sh` and each `platformio.ini` the PlatformIO pins, so a change to
any of them, or to a project kept under `firmware/` or `examples/`, is a new key. The Arduino image
builds independently in the `platformio` group
([decision](decisions/ci.md#firmware-families-are-sharded-to-shorten-the-critical-path)).

Only the `idf-1` group restores `.cache/esp-rom-elfs` and runs `make rom-elf`, so the ROM ELF is fetched once
there rather than by all the test jobs at the same time. Timeout: 60 minutes per group.

## `runner`

The shared job in `build-runner.yml` builds only the platform supplied by its caller; it has no
platform matrix. Its output is consumed only by that platform's tests (and Linux determinism).

On a cache miss, it installs rustup if it is missing (with no default toolchain: rustup installs the
one `runner/rust-toolchain.toml` pins on the first build), runs `make build-runner test-runner`, and
packs `runner/target/release/esp32tl-runner` into `build/ci/runner.tar`, which keeps its executable
bit, then saves it and checks that the cache kept it, as `firmware` does. The hosted images have
rustup and native linkers; the fallback also supports an image without rustup.

The key covers all of `runner/`: the sources, the esp32sim pin in `Cargo.toml` and `Cargo.lock`,
the toolchain, the profiles, and whatever else cargo reads. A cold build takes under a minute, so
cargo's `runner/target/` is not cached. Timeout: 30 minutes.

## `test`

The four-shard selection uses SHA-256 node-ID hashing with one measured exception in
`tests/conftest.py`: the HTTP bridge test runs in shard 1 instead of 4. Its approximately
40 seconds of work moves within the same `1/4 4/4` VM pair on both platforms and all Python
versions. Job names, required checks, cache keys and the serial real-source phase are unchanged.
See [the balancing decision](decisions/ci.md#balance-the-existing-pair-with-one-measured-assignment).

The shared job in `test-platform.yml` runs six matrix entries per platform: three Python
versions and two shard pairs. There are still twelve test VMs in the complete workflow.

For each platform, Python version and shard pair: sets up Python with pip's cache keyed on
`pyproject.toml`, restores eight firmware tarballs, the native runner tarball and the ROM ELF, unpacks the tarballs (the images land
in `firmware/<image>/build/`, `firmware/<image>/.pio/build/esp32dev/` and `examples/<name>/build/`,
where the tests read them), and runs `esp32tl-runner --help` to check that the binary starts. It
then runs `make PYTHON=<that Python> .venv/.installed rom-elf`, which is `make setup` without
`build-runner`. Before `.venv/bin/python .github/scripts/test-pair.py K/4 J/4` the `mtools` step makes sure `mformat`,
`mcopy`, `mdir` and `mtype` are on `PATH`, for the SD card tests, which skip without them: on macOS
it installs mtools with Homebrew; on Linux it runs `sudo apt-get update` and
`sudo apt-get install -y mtools`. The check includes `mmd`; a missing command fails the job.
Before the parallel phase, each job runs `tests/test_real_source.py` once for each of its two
shard selections, sequentially. This keeps wall-clock measurements free of contention from the
other pytest process. The parallel phase passes `--ignore=tests/test_real_source.py`, so every
test still runs exactly once across the four shards. The serial phase uses normal pytest exit
codes and adds no retries or relaxed assertions.

The wrapper starts two `python -m pytest --shard=K/4` processes concurrently, pairing 1 with 4
and 2 with 3. Each has a separate `TMPDIR`, `--basetemp`, pytest cache and captured log. Both
logs and exit codes are printed, including when either shard fails; any nonzero exit code
(including no tests collected) fails the job. Cleanup sends `SIGTERM` to both process groups, including groups whose pytest leader has
already exited. It waits up to five seconds for the groups to disappear, then sends `SIGKILL`
to any that remain, so descendants ignoring `SIGTERM` do not survive cancellation.
Both serial pytest invocations and both parallel shards receive `--durations=30`. Each prints
its 30 slowest setup, call or teardown phases in the job log. These are wall-clock measurements
under that VM's load, not CPU profiles; use repeated runs before changing shard assignments.
No test selection, timeout or pass/fail rule changes with this reporting flag.
"Re-run failed jobs" re-runs the pair. Timeout stays 15 minutes; no pytest timeout is relaxed.

Paired pytest temporary roots are created under `/tmp` with a short `e32-pair-` prefix,
independently of the host's `TMPDIR`. This keeps multiprocessing's Unix socket names below
macOS path limits. A regression test starts real multiprocessing managers with a long inherited
host temporary path; shortening paths does not skip or alter the tests.

## `determinism`

Two runs of one recording must be identical (plan, section 6). The job restores the firmware and
runner tarballs and the ROM ELF under the keys `test` uses, and fails on a miss of any tarball as
`test` does; it unpacks them, checks that the runner starts (`esp32tl-runner --help`), runs
`make PYTHON=<Python 3.11> .venv/.installed rom-elf`, then `make determinism`, which runs
`tests/test_determinism.py` twice and compares the consoles, the taps and the runner's stats of the
two runs. Each difference is printed in the job's log as a `✗` line, with a diff where both runs
have the file or the test; there is no artifact, since the account's artifact quota is full. What
the check compares, and what it leaves out, is in
[guides/testing.md](guides/testing.md#determinism).

It runs on Linux and one Python only: what it checks is that one machine runs one test the same
way twice, not that two machines agree. Timeout: 10 minutes.

## `lint`

Except for [version-only changes](#version-only-ci), `make lint` runs in full on Python 3.11, the oldest Python esp32tl supports and the one mypy and ruff
target. It installs rustup if missing, as `runner` does, because `runner` may have hit its cache
and skipped that step. The checkout removes `runner/target/`, so clippy checks esp32sim from
scratch, without a target-directory cache (the earlier self-hosted measurement was 8 s). Timeout: 15
minutes.

## `docs`

On pull requests and manual runs. It checks out the full history (`fetch-depth: 0`), because the check needs
the merge base with the pull request's base branch (`origin/main` for manual runs), sets up Python 3.11, and runs the
"Documentation contract" step:

```sh
make docs-check BASE="$BASE"    # env: BASE: origin/${{ github.base_ref || 'main' }}
```

It builds nothing and needs nothing from the other jobs. What the check enforces, and why it is not
a pre-commit hook, is in [guides/documentation.md](guides/documentation.md#the-check).
The public repository requires this check on pull requests through `main` branch protection.

## See also

- [decisions/ci.md](decisions/ci.md): why CI is built this way.
- [guides/development.md](guides/development.md): the make targets the jobs run.
- [guides/testing.md](guides/testing.md): the test suite the `test` jobs run.
- [implementation-plan.md](implementation-plan.md): M0 and the test strategy CI implements.

## Examples

Every `examples/*/CMakeLists.txt` project is built in exactly one `firmware (examples-K)` job and packed into
`build/ci/firmware-examples-K.tar`. `examples/**` participates in its cache key. pytest's default collection
includes `examples/`, so every `test` matrix member runs their tests using the restored images.
Adding a project and `tests/test_*.py` requires no new CI job or explicit example list.


## Hosted baseline

[The successful run of #205](https://github.com/mateuszsikora/esp32-testing-library/actions/runs/43724340877)
took 22m19s from creation to completion. Firmware jobs took 15m59s (ESP-IDF), 9m29s
(examples), and 6m50s (PlatformIO). Their build-and-pack steps took 14m48s, 8m32s and 3m42s;
unconditional SDK deletion took 1m00s, 48s and 2m20s. All 24 pytest jobs and determinism passed.
The longest pytest job took 5m34s, including 4m37s in pytest. These are measurements of the
three-group baseline, not of the eight-group optimization. Cold and warm end-to-end timings
must be measured separately; hosted queue time counts toward the 10-minute target too.


## Cache invalidation boundaries

`ci.yml` calls `build-firmware.yml`, `build-runner.yml` and `test-platform.yml` through
`workflow_call`. The two build workflows own all build preparation, commands, packing and cache
saving, and expose the same base keys as before. Each key hashes its entire build workflow, the Makefile and source trees;
firmware still includes all `firmware/**` and `examples/**`, and the native runner all `runner/**`.
A change to `test-platform.yml`, test pairing, Python setup or the test wrapper does not
invalidate build outputs.
A change to build commands, pins, packaging or the build workflow does invalidate them. No manual
cache version or stale-build fallback is used. The split incurs a one-time cache miss.


Splitting the native build matrix into two calls changes `build-runner.yml`, causing one native
cache miss per platform. The firmware build workflow, cache keys, tarball transport and
architecture suffixes are unchanged. The test workflow receives the matching platform's base
key and appends `runner.os` and `runner.arch`, preserving exact architecture-specific restores.

The final pre-split PR run [43760305678](https://github.com/mateuszsikora/esp32-testing-library/actions/runs/43760305678)
passed in 16m36s despite hitting all build caches. Firmware was ready at 1m07s, but the macOS
native build started at 4m19s and ran for seven seconds; all Linux tests consequently started
around 4m28s. Separate dependencies remove that cross-platform barrier. They do not remove
macOS scheduling delays or establish a new whole-workflow time before a hosted measurement.

## Five-macOS-job measurement

CI retains six test jobs per platform. An experiment at `da54942` combined both macOS
Python 3.11 pairs on one VM, running `1/4 4/4` before `2/4 3/4`, to avoid allocating a sixth
macOS test VM. The other OS/Python combinations and all build inputs were unchanged.
The experiment was rejected because it did not demonstrate a whole-workflow speedup.

These manual GitHub Actions runs used the same firmware and native-runner cache keys, with
all build caches hit. Elapsed time is from workflow creation to the last job's completion,
including queues. Job-seconds sum the macOS test jobs' execution times, including setup;
they are not billed minutes or a price estimate.

| Layout and run | Commit | Whole CI | Last macOS test job started | macOS test job-seconds |
|---|---|---|---|---|
| Six jobs, [first control](https://github.com/mateuszsikora/esp32-testing-library/actions/runs/43769968112) | `b79fe18` | 8m36s | 4m37s | 1657 |
| Five jobs, [experiment](https://github.com/mateuszsikora/esp32-testing-library/actions/runs/43771329988) | `da54942` | 8m14s | 25s | 1507 |
| Six jobs, [second control](https://github.com/mateuszsikora/esp32-testing-library/actions/runs/43772184650) | `b79fe18` | 7m24s | 3m23s | 1472 |

The first control overlapped other repository CI runs. The experiment and second control
ran sequentially, without other repository CI runs active. Five jobs avoided the queue, but
the combined job took 7m50s and determined completion. It was 22 seconds faster than the first
control and 50 seconds slower than the second. The second control also used fewer macOS
job-seconds. This small sample does not establish typical performance; it gives no evidence
that serializing another pair reliably improves the elapsed-time target.

All three runs passed the same 2320 tests and skipped the same one hardware-only GNSS test
on each of Linux x64 and macOS ARM64 with Python 3.11, 3.13 and 3.14. Determinism and lint
passed. The `docs` job is skipped on `workflow_dispatch`; `make docs-check` was run locally.
These are warm-cache measurements, not fresh firmware-build timings or a five-minute guarantee.
The six-job matrix, test commands, cache strategy and 25 required check contexts remain unchanged.
See [the decision](decisions/ci.md#retain-six-macos-test-jobs-after-measuring-five).

## Prepare a version pull request

Run **Prepare release** (`prepare-release.yml`) from Actions on `main`, with `version` such as
`0.1.1` (no `v` prefix). The workflow accepts only an increasing `MAJOR.MINOR.PATCH` version,
with no leading zeros or prerelease suffix. It uses the selected main commit and changes exactly
three values: `[project].version` in `pyproject.toml`, `[package].version` in `runner/Cargo.toml`,
and the `esp32tl-runner` package version in `runner/Cargo.lock`. All existing versions must agree.
It does not choose a version from commit messages or change dependency versions.

The workflow creates `build/release-VERSION` and opens a PR with a `build: prepare release VERSION`
title. Merge that PR after its checks pass. Normal branch protection and the requirement to be
up to date still apply. If other changes are added to the PR, full CI runs and the automatic
release trigger will not accept the mixed change.

Repository setup: **Settings → Actions → General → Workflow permissions → Allow GitHub Actions
to create and approve pull requests** must be enabled for `GITHUB_TOKEN` to open the PR. This
option was enabled with the owner's approval on 16 November 2026; the default token permission
remains `read`. The workflow creates PRs; it does not approve or merge them. Its write permissions are limited to
contents, pull requests and Actions. It explicitly dispatches `ci.yml` on the PR branch because
pushes with `GITHUB_TOKEN` do not start push CI. This also provides CI without relying on approval
of an automatically created PR's event-triggered workflow runs.

CLI equivalent:

```sh
gh workflow run prepare-release.yml --ref main -f version=0.1.1
```

The preparation job has a five-minute timeout and is serialized. Existing release branches are
never force-pushed. If creating the PR or dispatching CI fails after the branch push, use the
existing branch to open the PR or run `gh workflow run ci.yml --ref build/release-0.1.1`; do not
rerun preparation expecting it to overwrite that branch.

## Version-only CI

`.github/scripts/release-version.py classify BASE HEAD` checks the actual git trees. The fast
path requires exactly the three version files, unchanged regular-file modes, matching versions
in both trees, an increasing stable version, and byte-for-byte equality with the expected three
version replacements. Labels, branch names and PR titles do not grant a shortcut. Changes to
code, dependencies, comments, file modes, or any fourth file use full CI. Git errors fail the
classification job; the required `lint` job runs with `always()` and fails if classification failed.

CI compares PRs with their base SHA, pushes with the previous main SHA, and manual runs with
`origin/main`. For an exact version bump, `lint` builds wheel/sdist and checks metadata with
`twine check --strict`; `docs` still runs the documentation contract on PRs and manual runs.
The eight firmware jobs, two runner jobs and twelve test jobs retain their required context names,
but run a short explanatory step on Ubuntu without downloads, cache restores, native builds or
pytest. Determinism is skipped. No macOS VM is allocated for this path and all 25 existing required
checks remain configured. This avoids a branch-protection migration and does not fabricate statuses
with the Checks API. A scheduling overhead remains; no fixed elapsed-time improvement is claimed
until measured on GitHub.

The Runtime release workflow also recognizes version-only PRs: it builds and checks Python
metadata but skips native builds and firmware. Direct manual verification and releases after merge
still run the full native release matrix. The helper's regression tests use real local git commits
and ensure mixed changes, unknown refs, inconsistent versions and version decreases cannot use the
shortcut (`tests/test_release_version.py`).

## Release a merged version

`release-on-merge.yml` runs when a push to `main` touches version files. It checks the complete
before/after diff with the same classifier. Only a pure increasing version bump creates `vVERSION`
on the exact merged commit. This applies to manually authored version-only PRs too. A mixed change
creates no tag; separate the version bump from implementation changes. No write is made to `main`.

The tag push is not forced; an existing tag at another commit is rejected. A retry with the same
commit is safe. The workflow then calls `runtime-release.yml` directly with `release_tag`, using
the tagged sources for every build and validation step. This is necessary because a tag pushed
with `GITHUB_TOKEN` does not trigger another push workflow. A reusable call needs no extra PAT or
GitHub App secret. See [GitHub's token behavior](https://docs.github.com/en/actions/concepts/security/github_token)
and [reusable workflow permissions](https://docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations).

The classification and tag jobs each have a five-minute timeout (classification is read-only;
only the tag job can write contents); the reusable release call permits its draft job to write contents too. Runs are
serialized per merged SHA. If native tests fail after tagging, rerun failed jobs in **Release merged
version**. Do not move the tag to fix source code: merge a new fix and prepare a higher version.
An existing draft is not overwritten; inspect it before retrying a failed draft creation.

## Runtime releases

`.github/workflows/runtime-release.yml` builds distribution assets independently of the main CI.
It runs on relevant pull requests, manual dispatch and `v*` tags, using only standard hosted
runners. A tag must equal `v` plus the Python package version, and Cargo's package version must
match, before packaging. Draft creation requires either a `push` of a `v*` tag or an explicit reusable call
from **Release merged version**. Direct PR and manual runs of **Runtime release** never create
releases, including manual dispatch against an existing version tag.
This workflow does not upload to PyPI; the separate publishing workflow below does.

Jobs and their outputs:

- `change` (Ubuntu 24.04, Python 3.11): recognizes exact version-only PRs; all other events
  use the full release pipeline.
- `package` (Ubuntu 24.04, Python 3.11): `python -m build` builds an sdist and then a wheel from
  that sdist; `twine check --strict` validates metadata. The one wheel is used on both platforms.
- `firmware` (Ubuntu 24.04): checks Docker and builds only `hello_world`, using the pinned builder.
- `runtime` (Ubuntu 22.04 x86_64 and macOS 15 arm64): checks native architecture, builds the
  pinned Rust runner, runs its tests, packages the executable and compatibility manifest, then
  installs the shared wheel into a clean venv outside the checkout. The smoke test prepares the
  runtime over real HTTP and boots the supplied firmware twice with network connections blocked
  and Rust tools absent from PATH. Ubuntu 22.04 deliberately preserves the glibc 2.35 baseline,
  while the main CI uses Ubuntu 24.04.
- `assemble` (Ubuntu 24.04): downloads the wheel/sdist and both runners. `scripts/check-release.py`
  rejects missing or extra assets and wrong version, esp32sim revision, target or executable
  checksum, then writes SHA256SUMS for exactly those six files. It uploads `verified-release`.
- `draft` (Ubuntu 24.04, version-tag pushes or merged-version calls): verifies SHA256SUMS and creates the draft
  GitHub release with generated release notes. Only this job has `contents: write`; it does not replace an existing
  release or publish the draft.

Actions artifacts have a seven-day retention period; missing files fail their upload. Concurrency
is serialized by ref without cancelling a running release build. Limits are 10 minutes for
package/assemble/draft, 30 for firmware and 25 per runtime job. ROM and firmware never enter the
release asset set. This workflow intentionally uses artifacts, separate from the main CI's cache
transport; artifact storage quota must permit its uploads.

Before the first release:

1. Merge the workflow and runtime implementation, then run **Runtime release** with
   `workflow_dispatch` on main. Inspect both native smoke tests and download `verified-release`.
2. Run **Prepare release** on main with the desired new version and merge its version-only PR
   after the fast checks pass. This tags the merged commit, runs full native release validation,
   and creates a draft containing the wheel, sdist, two executables, two JSON manifests and
   SHA256SUMS. Use a new version for every release; never replace published runtime assets.
3. Review the draft's notes, hashes and target identities, then click **Publish release**.
   A direct version-tag push remains supported (for example `git tag v0.1.0` and
   `git push origin v0.1.0` for the version already in the repository). This also runs full
   validation and creates only a draft.
4. Install that release's wheel in a clean environment, run `esp32tl runtime prepare` against
   the real GitHub release URLs, and boot the firmware again offline. Anonymous downloading
   requires a published release; a draft is insufficient even though this repository is public.

Publishing the GitHub release triggers the PyPI workflow below, so configure its Trusted
Publisher before clicking Publish. The release workflow files must be included in the tagged commit.
The workflows are validated locally with actionlint; hosted runs and PyPI's OIDC exchange still
require the workflows on GitHub and are not implied by a successful local smoke test.

## PyPI Trusted Publishing

`.github/workflows/publish-pypi.yml` listens only to `release: published` for tags starting with
`v`. Creating a draft, pushing a tag, opening a PR or editing release notes does not upload to
PyPI. A maintainer's Publish release action starts this workflow after runtime assets are public.

The `verify` job checks out the release tag with read-only permissions and no persisted Git
credentials, downloads all assets from that GitHub release, and runs
`python scripts/check-release.py release --verify --tag "$TAG"`. This validates the exact file
set, version, runner identities and checksums, then compares all six computed checksums with the
published SHA256SUMS without rewriting it. A missing checksum file or changed wheel/sdist fails
before upload. Only the verified wheel and sdist go into the `pypi-distributions` Actions artifact
(retained for seven days); neither runners nor manifests go to PyPI. Nothing is rebuilt.

The separate `publish` job downloads those two files and uses
`pypa/gh-action-pypi-publish@release/v1`. It has `id-token: write`, uses environment `pypi`, and
runs no checkout or build. No API token, username or password is configured. Both jobs use
Ubuntu 24.04 with ten-minute timeouts; runs are serialized per release tag without cancelling
an active upload. OIDC authentication and the first upload cannot be validated by local tests.

Configure the GitHub repository environment `pypi`, then add a GitHub Trusted Publisher under
[PyPI account Publishing](https://pypi.org/manage/account/publishing/) for a new project:

| Field | Value |
|---|---|
| Project name | `esp32tl` |
| Owner | `mateuszsikora` |
| Repository | `esp32-testing-library` |
| Workflow filename | `publish-pypi.yml` |
| Environment | `pypi` |

For an existing project, use its Publishing settings instead. A pending publisher creates the
project on first upload; it does not reserve the name. See [PyPI's setup guide](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)
and [OIDC publishing guide](https://docs.pypi.org/trusted-publishers/using-a-publisher/).
After the first successful upload, verify `python -m pip install esp32tl==0.1.0` in a clean
venv and `esp32tl runtime prepare` against the published GitHub runtime assets.

A failed upload leaves the GitHub release published. Fix the cause before retrying the failed
job. The workflow deliberately does not enable `skip-existing`: a partial upload or an already
published version requires inspection, and PyPI files cannot be silently replaced.
