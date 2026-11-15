# CI

For whoever changes [`.github/workflows/ci.yml`](../.github/workflows/ci.yml), its called build
workflows (`build-firmware.yml`, `build-runner.yml`), or reads one of its
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

## Jobs

GitHub-hosted macOS runners have no Docker, so the validation firmware is built once, in the
pinned ESP-IDF and PlatformIO containers in eight parallel Linux jobs, and the runner crate once
per platform.
The test jobs then run only the runner and Python: they build nothing.

| Job | Runs on | What it does |
|---|---|---|
| `firmware` | `ubuntu-24.04` (x64) | Eight parallel groups (`idf-1` to `idf-4`, `examples-1` to `examples-3`, `platformio`) once per content of `firmware/`, `examples/`, the build workflow and the `Makefile`, the Arduino image with PlatformIO's core directory restored from the cache; `idf-1` fetches the ROM ELF once per revision of `scripts/fetch-rom-elf` |
| `runner` | `ubuntu-24.04` (x64); `macos-15` (ARM64) | `make build-runner test-runner`, once per content of `runner/` (its sources, its esp32sim pin, its toolchain), the build workflow and the `Makefile` |
| `test` | `ubuntu-24.04` (x64); `macos-15` (ARM64); Python 3.11, 3.13, 3.14; pairs `1/4 4/4` and `2/4 3/4` | Unpacks the images and the runner, restores the ROM ELF, runs wall-clock tests sequentially, then two existing pytest shards concurrently; builds nothing |
| `determinism` | `ubuntu-24.04` (x64); Python 3.11 | Unpacks the images and the runner as `test` does, and runs `make determinism`: the determinism tests twice, the two runs compared |
| `lint` | `ubuntu-24.04` (x64); Python 3.11 | `make lint`, clippy included |
| `docs` | `ubuntu-24.04` (x64); pull requests only | `make docs-check` against the pull request's base branch |

`test` and `determinism` wait for every `firmware` group and both `runner` jobs. `lint` and `docs` need nothing from the
other jobs. The `firmware`, `runner` and `test` matrices do not fail fast: one platform or Python version
failing does not cancel the others.

## Where the jobs run

All Linux jobs use the standard `ubuntu-24.04` image (x64). The `runner` and `test`
matrices always include `macos-15` (Apple Silicon, ARM64), including in private repositories.
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

Private runs consume the account's hosted allowance and may incur charges. Publishing the
repository needs no workflow switch: both platforms already run. Required checks / branch
protection can be configured separately after publication.

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
| `build/ci/runner.tar` | `runner-` and the hash of `runner/**`, `Makefile`, `.github/workflows/build-runner.yml`, then the runner's OS and architecture | `runner`, once per platform | Fails the job |
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

On a cache miss, it installs rustup if it is missing (with no default toolchain: rustup installs the
one `runner/rust-toolchain.toml` pins on the first build), runs `make build-runner test-runner`, and
packs `runner/target/release/esp32tl-runner` into `build/ci/runner.tar`, which keeps its executable
bit, then saves it and checks that the cache kept it, as `firmware` does. The hosted images have
rustup and native linkers; the fallback also supports an image without rustup.

The key covers all of `runner/`: the sources, the esp32sim pin in `Cargo.toml` and `Cargo.lock`,
the toolchain, the profiles, and whatever else cargo reads. A cold build takes under a minute, so
cargo's `runner/target/` is not cached. Timeout: 30 minutes.

## `test`

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
(including no tests collected) fails the job. Cancellation terminates their process groups.
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

`make lint` in full on Python 3.11, the oldest Python esp32tl supports and the one mypy and ruff
target. It installs rustup if missing, as `runner` does, because `runner` may have hit its cache
and skipped that step. The checkout removes `runner/target/`, so clippy checks esp32sim from
scratch, without a target-directory cache (the earlier self-hosted measurement was 8 s). Timeout: 15
minutes.

## `docs`

On pull requests only. It checks out the full history (`fetch-depth: 0`), because the check needs
the merge base with the pull request's base branch, sets up Python 3.11, and runs the
"Documentation contract" step:

```sh
make docs-check BASE="$BASE"    # env: BASE: origin/${{ github.base_ref }}
```

It builds nothing and needs nothing from the other jobs. What the check enforces, and why it is not
a pre-commit hook, is in [guides/documentation.md](guides/documentation.md#the-check). While the
repository is private, no job is a required check: GitHub's free plan offers no branch protection
for private repositories, so a red `docs` job does not stop a merge by itself.

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

`ci.yml` calls `build-firmware.yml` and `build-runner.yml` through `workflow_call`. The called
workflows own all build preparation, commands, packing and cache saving, and expose the same
base keys as before. Each key hashes its entire build workflow, the Makefile and source trees;
firmware still includes all `firmware/**` and `examples/**`, and the native runner all `runner/**`.
A change to test pairing, Python setup or the test wrapper does not invalidate build outputs.
A change to build commands, pins, packaging or the build matrix does invalidate them. No manual
cache version or stale-build fallback is used. The split incurs a one-time cache miss.
