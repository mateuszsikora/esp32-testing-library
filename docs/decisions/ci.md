# CI decisions

Why `.github/workflows/ci.yml` runs its jobs where it does, how builds reach the test jobs, and
what each job runs.

## Public fork runs require approval from all external contributors

Status: decided (15 November 2026)

After publication, the repository uses `all_external_contributors` for fork workflow
approval. GitHub-hosted VMs isolate contributor code from the maintainer's machines;
approval additionally limits unsolicited compute use by returning contributors. This
revisits the earlier rejection of universal fork approval recorded under the superseded
self-hosted migration decision. A previous merged contribution does not permanently grant
unattended CI execution. The read-only token and `pull_request` trigger remain unchanged.

Public `main` protection requires PRs and all 25 existing CI checks, bound to the GitHub
Actions app, with strict up-to-date checks and administrator enforcement. No approving
review is required, so a solo maintainer can merge their own PR after CI. Force pushes and
deletion are disabled, and review conversations must be resolved. Repository settings and
their verification date are recorded in [CI](../cicd.md#public-repository-settings). That was
the state when the repository went public: since then the required checks have grown to 30, and
strict checks are off since 18 November 2026
([below](#a-pull-request-need-not-be-up-to-date-with-main-to-merge)).

**Rejected:** approval only for first-time contributors, because one accepted contribution
would remove the approval gate for subsequent fork runs. Requiring one approving review
would prevent the solo maintainer from merging their own work without another reviewer.

## Every Linux job runs on the self-hosted runner hp600g1

Status: superseded (#48)

Replaced by [standard hosted images](#ci-uses-standard-hosted-images-on-both-platforms-before-publication).
The following records the earlier configuration.

The `firmware`, `runner`, `test`, `lint` and `docs` jobs run on `[self-hosted, Linux, X64]`, the
machine hp600g1, even jobs such as `lint` and `docs` that need nothing from it. The repository is
private, so minutes on GitHub-hosted runners are limited, and macOS minutes count ten times; every
job that can runs on hp600g1. The machine also keeps the 7 GB `espressif/idf` image between runs, so
the firmware build pulls nothing, and it has Docker, so the `requires_docker` tests run there. The
cost is the queue: hp600g1 is one machine, and its jobs run one after another. In the run of #50,
`lint` went before `firmware` and delayed `firmware` and the Linux test jobs by about a minute. The
header comment of `ci.yml` gives this reason, so the choice is not proposed again (#50).

**Rejected:** `ubuntu-latest` for jobs that need nothing from hp600g1, such as `lint`, because each
run would spend hosted minutes (about one per run for `lint`) that the private repository is short
of.

## The macOS jobs run only once the repository is public

Status: superseded (#52)

Replaced by [standard hosted images](#ci-uses-standard-hosted-images-on-both-platforms-before-publication).
The following records the earlier configuration.

The `runner` and `test` matrices take their platforms from
`fromJSON(github.event.repository.private && '["linux"]' || '["linux", "macos"]')`. While the
repository is private they are the only jobs that would use GitHub-hosted runners (`runner (macos)`
and three `test (macos, …)` per run), and macOS minutes count ten times against the limit. Making
the repository public turns them back on with no change to the workflow.

**Rejected:** deleting the macOS entries, because someone would have to remember to restore them
when the repository goes public; the condition does it by itself.

## The runs-on label is computed from the platform

Status: superseded (#52)

Replaced by [standard hosted images](#ci-uses-standard-hosted-images-on-both-platforms-before-publication).
The following records the earlier configuration.

`runs-on` is `matrix.platform == 'macos' && 'macos-latest' || fromJSON('["self-hosted", "Linux",
"X64"]')`, with no `include` entries.

**Rejected:** an `include` entry per platform, because an `include` for `macos` with `macos` missing
from the matrix matches no combination, so GitHub adds it as a job of its own: a macOS job with no
Python version.

## CI moves to GitHub-hosted runners before the repository goes public

Status: superseded (#52)

Replaced by [standard hosted images](#ci-uses-standard-hosted-images-on-both-platforms-before-publication).
The following records the earlier configuration.

The self-hosted runner is used only while the repository is private (#17 makes it public). So a
pull request from a fork never runs its code on hp600g1 or reaches its Docker daemon, and that
exposure is out of scope for the workflow as it stands. The reason for keeping `lint` and the other
Linux jobs on hp600g1, limited hosted minutes, goes away at the same time.

**Rejected:** restricting the self-hosted jobs to pull requests from this repository, or requiring
approval for every outside contributor's workflow run, as #48 proposed before #17, because no
fork pull request will run on hp600g1.

## Builds reach the test jobs through the Actions cache, not artifacts

Status: decided (#48)

The `firmware` and `runner` jobs save tarballs under `build/ci/` in the Actions cache, under a key
each publishes as a job output. The test jobs restore exactly that key with `fail-on-cache-miss`,
so a test job never runs against another revision's build. The key also keeps a build from running
twice: a build job whose key is already cached builds nothing. A tarball saved on Linux restores on
macOS, since both sides use zstd. An entry unused for 7 days is evicted: re-running only the test
jobs of an older run then fails on the miss, and re-running all jobs builds again. A bad entry
stays until `gh cache delete <key>` removes it.

**Rejected:** artifacts, as plan M0 and #8 asked, because the first run failed at every upload with
"Artifact storage quota has been hit": that quota belongs to the account, is shared by all its
private repositories and is full, while the cache has 10 GB per repository of its own.

## A build job fails when the cache did not keep its tarball

Status: decided (#190)

The `firmware` and `runner` jobs restore their key with `actions/cache/restore` and, on a miss, save
the tarball with `actions/cache/save` and look the key up again with `lookup-only` and
`fail-on-cache-miss`. A save that did not land then fails the build job, whose log shows why, and a
re-run of that job can save again. On `main` on 11 November 2026, most likely the `runner` job of
the run for #188 reserved its key in its post-job save and never finished the entry: that job's log
was lost and its test jobs never started. The `runner` job of the next run, for #189, could not
save under the key (`Failed to save: Unable to reserve cache with key runner-dde51e…-Linux-X64,
another job may be creating this cache.`), stayed green, and all 12 `test` jobs and `determinism`
failed on the miss, where the cause was not in their logs.

**Rejected:** `actions/cache` saving in its post step at the end of the job, as before, because a
failed save does not fail the step: a key it cannot reserve is a plain log line, not even a
warning, so the job stays green.

**Rejected:** checking with `gh cache list`, because `actions/cache/restore` with `lookup-only` asks
the same question with an action the job already uses, while `gh cache list` would also need
`actions: read` on a token that has `contents: read` only.

## Each cache key hashes the whole workflow and the Makefile

Status: superseded (#48)

Replaced by [paired tests and isolated build workflows](#test-shards-share-vms-and-build-workflows-own-their-cache-inputs).
The following records the earlier configuration.

The firmware key hashes `firmware/**`, the `Makefile` and `ci.yml`; the runner key hashes
`runner/**`, the `Makefile` and `ci.yml`. `firmware/build.sh` holds the IDF tag, the container
digest and each image's project, and `runner/` holds the esp32sim pin, the toolchain and the
profiles, so a change to any of them is a new key. Since a tarball saved under a key stays there,
the `firmware` job checks it before saving: every image must have `esp32tl-built`,
`esp32tl-build.json`, `flasher_args.json`, `config/sdkconfig.json`, `project_description.json`, each
flash image `flasher_args.json` names and the ELF `project_description.json` names (`app_elf`); a
PlatformIO image, the files of its own list
([below](#the-arduino-image-is-built-in-the-firmware-job)). The tarball holds only what the backend
reads: 15 MB for `hello_world` against 116 MB for the whole build directory, about 1 MB once the
cache compresses it. The cost of hashing the whole workflow is that any change to `ci.yml` or the
`Makefile`, a comment included, rebuilds the firmware (about 3 min on hp600g1 when this was decided,
about 13 min with 15 projects now) and the runner. If that starts to hurt, the packing can move into
a script whose hash replaces the workflow's.

**Rejected:** keys over a hand-picked list of files, because a key that misses an input serves a
stale build, and for the runner skips its tests, without any sign of it.

## The firmware job's timeout is 60 minutes

Status: decided (#145)

A full build of 14 projects took 24 to 25 minutes on hp600g1, about 100 s a project. Five times on
4 and 5 November 2026 the machine ran two to three times slower for part of a build, for a reason
the logs do not show, and the job hit its earlier limit of 30 minutes: on #123, #125, #138 and
twice on #133, with 9 to 12 of the projects done. At those speeds a full build of 15 projects took
up to about 52 minutes, so 60 covered it with 8 to spare. A timeout is only a cap: a cache hit takes
seconds, and a build that stops making progress holds the one machine for 30 minutes more.

60 stopped being enough when a whole build ran about 2.3 times slower than normal. No measured run
held that speed for a whole build, though single projects reached 3 times. Since
[images share their ccache hits](firmware.md#images-of-one-container-share-their-ccache-hits), a
full build takes 13m13s, which moves that to about 4.7 times. The limit was not lowered with it.

`test` kept its 20 minutes then, and reached that limit three times in such slow periods;
[its shards](#the-test-jobs-run-four-shards-of-the-suite) came later.

**Rejected:** 45 minutes, because it is under the estimate for the slow run it has to fit.

## The runner job caches the binary, not runner/target

Status: decided (#48)

The `runner` job runs `make build-runner test-runner` and caches only
`runner/target/release/esp32tl-runner`, in a tarball, which keeps the executable bit. Measured
cold, with esp32sim and the fat-LTO link included, `cargo build` took 44 s on hp600g1, 20 s on
`macos-latest` and 20 s on an M2 Pro. The runner key adds `runner.os` and `runner.arch`.

**Rejected:** caching `runner/target` keyed by the esp32sim revision, which #45 left for #8,
because moving hundreds of MB of target directory would cost more than the build it saves.

## The test jobs run pytest, not make test

Status: decided (#48)

A test job unpacks the firmware and runner tarballs, checks that the runner starts (`--help`), runs
`make .venv/.installed rom-elf` (`make setup` without `build-runner`) and then `.venv/bin/pytest`.
The runner's own tests already ran in the `runner` job.

**Rejected:** `make test`, because after a checkout cargo sees the runner's sources as newer than
the cached binary and links it again.

## The test jobs run four shards of the suite

Status: superseded (#146)

Replaced by [paired tests and isolated build workflows](#test-shards-share-vms-and-build-workflows-own-their-cache-inputs).
The following records the earlier configuration.

A `test` job ran the whole suite: about 11 minutes, 17 to 20 when hp600g1 was slow, and three times
over its limit of 20. A job cancelled there cost its 20 minutes and all of its work. Each Python now
runs four shards, `--shard=K/4` in `tests/conftest.py`, so a failed shard is re-run alone. Measured
at normal speed, a shard took 2m41s to 4m26s, about 1 minute of it setup. At the three times slower
seen in those periods the longest takes about 13 minutes, which 15 covers. The cost is setup: 12
jobs spend about 12 minutes on it on the one machine, where 3 spent 3. Several machines could run the
shards at once, later, with no change to them.

A test's shard is the hash of its node id, so a new test needs no list, and no file of measured
durations has to be kept up to date. The hash balances test counts, not time: pytest took about 105 s
in the shortest shard and 200 s in the longest. The longest, at 4m26s, reaches 15 minutes at about
3.4 times slower; a new slow test, or a renamed one that moves to another shard, lowers that.

**Rejected:** three shards, because they take about 15 minutes at that speed, with nothing to spare;
five, because 3 more jobs of setup buy little; splitting by file, because the 65 files differ too
much in length: hashed into four, their times were 55, 165, 228 and 218 s; `pytest-split`, which
balances better but needs that file of durations; a higher limit on one job, because it makes a
cancelled job cost more.

## The ROM ELF is fetched once, by the firmware job

Status: decided (#48)

The `idf` member of the `firmware` matrix runs `make rom-elf` under a cache entry keyed by the hash of
`scripts/fetch-rom-elf`, and the test jobs restore that entry. A miss there is no failure:
`make rom-elf` downloads the ELF then.

**Rejected:** each test job fetching the ELF, because the jobs started at the same time and raced
to save the same cache entry.

## Every commit on main gets its own concurrency group

Status: decided (#48)

The group is `ci-<sha>` on `main` and `ci-<ref>` elsewhere, with `cancel-in-progress` off on
`main`. A new push to a pull request cancels the run of the one before; no run on `main` is
cancelled while waiting.

**Rejected:** one shared group for `main`, because a group keeps at most one run waiting, so of
three quick merges the middle one was cancelled without running.

## Clippy runs in the lint job, with no cache

Status: decided (#50)

The `lint` job runs `make lint` in full: ruff check, ruff format, mypy, `cargo fmt --check` and
`cargo clippy --locked --all-targets -- -D warnings`. `actions/checkout` removes `runner/target/`,
so clippy checks esp32sim from scratch in the `dev` profile, producing metadata only. On hp600g1
that took 8.32 s, fetching esp32sim included (2.94 s on an M2 Pro with esp32sim already fetched).

**Rejected:** a cache key for clippy's target directory, or a separate cached clippy job, because
either would cost more than the 8 s it saves.

## The lint job runs Python 3.11

Status: decided (#50)

3.11 is the oldest Python esp32tl supports, and the version `[tool.mypy] python_version` and
ruff's `target-version` name. Both tools take the target from the configuration, so any supported
version would give the same result.

## The lint job carries its own copy of the rustup step

Status: decided (#50)

The `runner` job installs rustup only on a cache miss, and each hosted job has a fresh VM,
so `lint` must prepare its own toolchain. `lint` runs the same six-line step itself.

**Rejected:** a composite action shared by both jobs, because it is more than two copies of six
lines are worth.

## The determinism job runs tests of its own twice and compares what they leave

Status: decided (#22)

`make determinism` runs `tests/test_determinism.py` twice, each run with a fixed `--basetemp` and a
`--junitxml` of its own, and `scripts/check-determinism.py` compares the two. Each test replays a
recording, or describes a receiver to the GNSS model, taps the route into its `tmp_path`, ends the
run at a board time it chose, and writes the console there with the board's time of each line;
the fixed basetemp gives its `tmp_path` the same name in both runs. The `esp32sim_link` property
in the JUnit XML adds the runner's stats: where the run ended and its turns. Only the tap header's
`created`, the basetemp in a tap's path and pytest's `*current` symbolic links are left out. A
difference is printed in the log as a diff, not uploaded, since the account's artifact quota is
full. When the job was written, a test that waited 5 s in one run and 6 s in the other failed the
check on its console, its tap and the stats.

**Rejected:** diffing pytest-embedded's `dut.log`, because each of its lines starts with the
wall-clock time the log forwarder read it (`2026-11-01 11:52:20`), the runner's last line holds
the run's wall time (`end: stop from the peer at 21.100000 s (0.8 s wall, 0 resets, 33 turns)`),
and the backend does not wait for the forwarder at the end of the run, so its last lines may be
missing. Running the whole suite twice, because most of its tests use no board or run generated
suites through `pytester`, and its board tests write neither their console nor a tap. A test in
the suite that runs a generated suite twice through `pytester`, because it would run on every
Python of the `test` matrix and fail as one test among many; a job of its own is a check of its
own on the pull request, as plan section 6 has it.

## The determinism job runs on Linux and Python 3.11 only

Status: decided (#22)

The `determinism` job has no matrix: it runs on `ubuntu-24.04` with Python 3.11, as `lint` does. What it
checks is that one machine runs one test the same way twice. It restores the same tarballs as the
`test` jobs, with its own copy of their restore and unpack steps.

**Rejected:** the `test` matrix's platforms and Pythons, because every extra entry adds hosted usage and checks nothing more; comparing runs across machines, because nothing
claims that two machines give the same run.

## The Arduino image is built in the firmware job

Status: superseded (#193)

Replaced by [three parallel groups](#firmware-builds-run-in-three-parallel-groups).
The following records the earlier single-job configuration.

`make firmware` builds `arduino_shell` after the ESP-IDF images, so the `firmware` job builds it,
packs it into the same tarball under the same key and checks it before saving: `esp32tl-built`,
`esp32tl-build.json`, `firmware.bin`, `firmware.factory.bin`, `firmware.elf`, `bootloader.bin` and
`partitions.bin` in `.pio/build/esp32dev/`. The first run on hp600g1, for #203 on 15 November 2026,
had nothing cached, and the job took 33m35s. Its build step took 28m20s: 15m31s for the ESP-IDF
images, 6m57s for `arduino_shell` and 5m51s for the examples and the packing. Of the 6m57s, pio
reported 377 s for its run, the downloads of the platform, the framework and the toolchains
included; the rest went on pulling `python:3.13-bookworm` and installing PlatformIO Core. Saving the
core directory at the end of the job took 4m29s more. That leaves 26 minutes to the limit, so the
job's 60 minutes cover a cold run up to about 1.8 times slower. The next run, for 58637a3, restored
the `platformio-` entry: restoring its 1912 MB took 3m54s, `arduino_shell` built from scratch in 48
s (pio reported 44.9 s) without a download or an install, nothing was saved at the end, and the job
took 26m56s.

**Rejected:** a job of its own, because it would need its own key, tarball and restore and unpack
steps in `test` and `determinism`, and hp600g1 runs its jobs one after another, so the images would
not be ready sooner. Its own key would spare the ESP-IDF images a rebuild when only the sketch
changes; the firmware key hashes `firmware/**` as a whole, and so it does for every other image.

## PlatformIO's core directory is cached under the key of its pins

Status: decided (#193)

On a miss of the firmware key, the `firmware` job restores `.cache/platformio/`, without `dist/`
and `tools/`, which a build does not need
([firmware.md](firmware.md#platformios-core-directory-is-cacheplatformio)), under
`platformio-` and the SHA-256 of the lines that set the pins: `CONTAINER=` and
`PIOARDUINO_VERSION=` in `firmware/build-platformio.sh` and `platform =` in each
`firmware/*/platformio.ini`, then the runner's OS and architecture. `actions/cache` saves it at the
end of the job and nothing checks it, as for the ROM ELF: a miss only downloads the packages again.
The first entry, for Linux X64, was 2,005,264,223 bytes, about 2.0 GB of the 10 GB the
repository's cache has (1.4 GB for the same directory on an arm64 Mac); on 15 November 2026,
before it, the cache held 2.1 GB in 54 entries, the largest firmware tarballs
61 MB. A change of a pin leaves the old entry for 7 days beside the new one. Should the cache
pass 10 GB, GitHub evicts the entries used least recently, which can be firmware or runner
tarballs a test job then misses.

**Rejected:** `hashFiles` over `firmware/build-platformio.sh` and the `platformio.ini` files,
because a change to a comment or a build flag there would download every package again. A directory
outside the workspace on hp600g1, which would survive between runs with no cache, because CI moves
to GitHub-hosted runners before the repository is public
([above](#ci-moves-to-github-hosted-runners-before-the-repository-goes-public)), and nothing would
key it by the pins.


## CI uses standard hosted images on both platforms before publication

Status: decided (15 November 2026)

All Linux jobs move from hp600g1 to `ubuntu-24.04` (x64); native Rust builds and the full pytest
matrix also run on `macos-15` (ARM64), even while private. GitHub lists these labels for both
private and public repositories in its
[standard runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners),
checked on 15 November 2026. Versioned labels avoid silently changing OS generations with
`-latest`; each job also checks the actual architecture before restoring native binaries.
No larger runners are needed. Private hosted usage can cost money; publication changes no matrix.

Docker Engine comes with the Ubuntu image; the firmware cache-miss path and Linux test jobs
start its service and require `docker info` to succeed as the job user. Test jobs install mtools
through apt or Homebrew and require all five commands, including `mmd`. Firmware setup selects
Python explicitly. Existing rustup bootstrap and pinned toolchain remain, with the images' native
compilers. Cold firmware builds remove unused hosted .NET, Android and Haskell SDK directories
to make room for containers, PlatformIO packages and build trees. Disk use and timings still
need a real hosted run; no test, shard or timeout is weakened.

The cache transport stays: firmware and ROM are ESP32 data shared across hosts, while the runner
key already includes OS and architecture (`Linux-X64`, `macOS-ARM64`), as does PlatformIO's
tools cache. Producer and consumer paths match. The workflow hash changes both build keys,
so old tarballs cannot mask the migration. Exact restores and verification after save remain.

**Rejected:** switching to artifacts during this migration, because hosted runners can use the
existing cache transport and the earlier artifact quota failure gives no reason to add that
dependency now. Larger runners, because standard VMs provide the needed capabilities; cold-run
resource measurements remain to be collected. Keeping macOS disabled until publication, because
that would leave the full configuration untested before publication. Adding Intel macOS or ARM
Linux to the matrix is outside this migration's two-platform coverage.

TODO(owner): run the workflow with cold and warm caches on GitHub Actions, verify all 24 shards,
determinism, native binaries, cross-platform firmware/ROM restores and disk/time margins.
Local actionlint and documentation checks cannot establish those runtime results.


## Firmware builds run in three parallel groups

Status: superseded (15 November 2026)

Replaced by [sharded firmware families](#firmware-families-are-sharded-to-shorten-the-critical-path).

The first hosted run of #205 was cancelled after its firmware job had spent 14m39s without
reaching the tests. Rust builds, lint and documentation had finished in about a minute.
The former `make firmware examples` executes three independent builds in sequence. The
`firmware` matrix now runs `idf` (`firmware/build.sh`), `platformio`
(`firmware/build-platformio.sh`) and `examples` (`make examples`) on separate Ubuntu x64 VMs,
with `fail-fast: false`. Each ESP-IDF family retains its in-container ccache hits. The scripts
still own the full project lists; the workflow does not duplicate them.

Each member checks and saves its own `build/ci/firmware-<group>.tar` under the existing base key
plus `-<group>`, and verifies the save. The identical base key is the matrix job output; test
and determinism jobs restore all three exact keys, fail if any is missing, and unpack all three.
Only `platformio` restores/saves its core directory; only `idf` fetches/saves the ROM. Build
outputs do not overlap between the groups. No extra aggregation job or artifact storage is needed.

**Rejected:** one job per image in this PR, because it loses the compiler-cache reuse within
an ESP-IDF family and multiplies container pulls and setup. Running simultaneous builds on one
VM competes for its limited CPU and disk. Narrowing cache invalidation or adding persistent
ccache needs separate validation and is deferred with further work toward a 5–10 minute total CI.
This small change removes sequential phases; its actual speedup must be measured by Actions.


## Firmware families are sharded to shorten the critical path

Status: decided (15 November 2026)

Run 43724340877 passed but took 22m19s. ESP-IDF occupied 15m59s before tests could start;
examples took 9m29s and PlatformIO 6m50s. Their build steps took 14m48s, 8m32s and 3m42s.
The workflow now uses four ESP-IDF validation shards, three example shards, and one PlatformIO
job. `firmware/build.sh [--examples] --shard K/N` partitions the script's own image list by
position modulo N. The union is complete and disjoint, tested against both families, and no
copy of the image list is maintained in CI. Selected images share ccache within each shard.

Tarball keys and paths include the shard group. Both consumers use a local composite action
that restores all eight exact keys and unpacks them; a missing shard fails the job. Only
`idf-1` fetches/saves the ROM, and only `platformio` caches its tools. The full OS/Python/pytest
shard matrix and failure checks remain unchanged.

SDK cleanup in the baseline cost 48s to 2m20s. It now removes SDK directories one at a time,
only while less than 20 GiB is free, instead of deleting all four unconditionally. That budget
leaves headroom for a pinned container plus a shard's build trees; logs report disk space before
and after cleanup. Hosted timings still need measurement; 10 minutes is a target, not a claim.

**Rejected:** one job per image, because it repeats all compiler work and container downloads
for every image and queues more machines. Persistent ccache and narrower build keys can be
considered after measuring this change; both add cache validity/storage concerns. Shortening
timeouts or dropping Python/OS combinations cannot establish a faster successful full CI.


## Test shards share VMs and build workflows own their cache inputs

Status: decided (15 November 2026)

Run 43749863055 (#206) passed all 37 jobs in 20m52s. Its firmware critical path was six minutes,
but a test job started as late as 10m52s after firmware completed. Twelve Linux test jobs used
2257 job-seconds and twelve macOS jobs 2361 job-seconds, including their repeated setup. The
four unchanged pytest shards now run in pairs (1 with 4, 2 with 3): twelve test jobs instead of
24, still every test on Linux x64 and macOS ARM64 with Python 3.11, 3.13 and 3.14. Two pytest
processes share a VM, with distinct TMPDIR, basetemp, cache and logs. Either failing fails the
pair, and both finish and report; no retry or error suppression is added. Cancellation terminates
both process groups. Pairing the previously shortest shard 1 with shard 4 avoids pairing two
of the longest shards. Real hosted timing and pass/skip counts decide whether this trade pays off.

`build-firmware.yml` and `build-runner.yml` now own their complete build job definitions and
expose cache keys through reusable-workflow outputs. The key hashes that build workflow,
Makefile and source trees; edits to test scheduling in `ci.yml` no longer force an unrelated
six-minute firmware build. Build inputs, pins and packaging still invalidate their outputs,
and exact restore/save verification remains. Migration changes the hash once.

**Rejected:** dropping OS/Python combinations or tests; that would change coverage. Four pytest
processes per VM would put more contention on standard runners; two is a bounded first step.
Manually versioned cache keys or hashing selected source files could silently miss build inputs.
Keeping the whole orchestration workflow in the build key rebuilds firmware when only test
scheduling changes, making optimization iterations unnecessarily slow.


The first paired run exposed `AF_UNIX path too long` in Python 3.11's multiprocessing manager
on macOS. Inheriting the hosted VM's long TMPDIR and appending wrapper/manager directories caused
fixture setup to fail. Pair roots now use a short prefix directly under `/tmp`; a real-manager
regression checks a deliberately long inherited TMPDIR. Test assertions and timeouts are unchanged.


Run 43755552026 exposed one additional host-lag skip in the paced byte-stamp test on macOS
Python 3.13. CI now runs `tests/test_real_source.py` sequentially for both shard selections before
starting the pair, and excludes that file only from the parallel phase. It still runs every test
exactly once on every OS/Python combination. Relaxing timing bounds, retrying until green, or
accepting reduced coverage would hide the cost of contention; isolating the wall-clock checks
costs a few serial seconds without giving up parallelism for the rest of the suite.


Review of #208 found that waiting only for each pytest leader after `SIGTERM` left descendants
alive when the leader exited first. Cleanup now monitors the process groups themselves: send
`SIGTERM` even after a leader exits, wait a shared five seconds for the groups to disappear,
then send `SIGKILL` to any remaining groups. Missing groups are harmless. Waiting only for the
leader or omitting groups of reaped leaders is insufficient because tests deliberately create
runners that ignore or block `SIGTERM`. Regressions cover both leader states at cancellation.


## Each platform waits only for its own native runner

Status: decided (15 November 2026)

Run 43760305678 passed in 16m36s with every build cache hit. Firmware completed 67 seconds after
creation, and the Linux native build completed at 35 seconds. The macOS native build queued
until second 259, then took seven seconds. The shared runner matrix held back Linux tests and
determinism until both native builds finished, even though their inputs were already ready.

`ci.yml` now calls `build-runner.yml` separately as `runner-linux` and `runner-macos`, passing
`platform`. Each build starts immediately and exposes its own key. `test-linux` and determinism
need firmware and `runner-linux`; `test-macos` needs firmware and `runner-macos`. Both test calls
share `test-platform.yml`, which owns the unchanged Python and shard-pair matrix and test setup.
Queued or failed builds on the other platform do not block a platform's tests. Both platforms
remain mandatory, with the same labels, architectures, assertions and failure propagation.

The entire native build workflow remains in its cache hash, so this edit causes one native
rebuild per architecture. Firmware keys and exact tarball restores do not change. Test workflow
edits remain outside both build keys. Every pytest invocation in `test-platform.yml` reports
`--durations=30` for later balancing decisions; determinism keeps its existing pytest arguments
without duration reporting. This change does not rebalance or remove tests. Existing decisions
to pair shards, isolate wall-clock tests, and run every commit on main remain in effect.

**Rejected:** duplicating all test steps for each platform, which would let dependency setup or
assertions drift. Removing the build dependency and hoping its cache exists would introduce a
race. A platform matrix on the reusable build call still gates dependants on the entire matrix;
separate calls give each consumer the dependency it actually needs. No new cache transport,
compiler cache, or main-branch cancellation policy is introduced in this change.

## Retain six macOS test jobs after measuring five

Status: decided (16 November 2026)

Keep two independent shard-pair jobs per Python version on macOS, as on Linux. The experiment
at `da54942` combined macOS Python 3.11's two pairs on one VM, sequentially, to avoid the sixth
VM's queue. It preserved every test, both platforms, all three Python versions, two concurrent
pytest processes per pair, serial wall-clock tests and failure propagation. Builds and their
cache keys were unchanged. The experiment did not land on main.

The [hosted measurements](../cicd.md#five-macos-job-measurement) used warm caches throughout:
six-job control 43769968112 finished in 8m36s, five-job experiment 43771329988 in 8m14s,
and repeated six-job control 43772184650 in 7m24s. The first control overlapped other repository
CI; the experiment and repeated control did not. All runs retained 2320 passes and the same
one hardware-only skip per OS/Python combination. Determinism and lint passed; manual dispatch
skipped the PR-only documentation job, which was checked locally.

**Rejected:** retaining the five-VM layout. Avoiding the queue made the combined job the critical
path at 7m50s. A 22-second improvement over the first control did not survive the second control:
the experiment was then 50 seconds slower and used 1507 macOS test job-seconds versus 1472.
There is no demonstrated speed or machine-time gain to justify the extra matrix exception,
sequential pair and required-check migration. These few runs do not establish a statistical
performance claim; retain the simpler existing layout unless new measurements justify a change.
Branch protection keeps all 25 checks. No test timeout, assertion, skip policy, cache transport
or main-branch cancellation policy changes as a result of this experiment.

## Balance the existing pair with one measured assignment

Status: decided (16 November 2026)

For four shards, assign
`tests/test_http_request.py::test_fetches_from_a_real_service_through_a_bridge[esp32sim]`
to shard 1 instead of its hashed shard 4. All other node IDs and other shard counts retain
SHA-256 modulo N. Keep the exception in `FOUR_SHARD_ASSIGNMENTS` in `tests/conftest.py`,
identical for all OS/Python combinations and independent of partial collection or order.
A regression fails if the named test disappears. Renaming it requires reviewing the assignment.

Hosted runs 43769968112, 43771329988 and 43772184650 measured this call at a median 39.73 s
across nine macOS results. In the last control, shard 1 took 103.09, 113.74 and 112.55 s
on Python 3.11, 3.13 and 3.14; shard 4 took 175.51, 212.72 and 191.91 s respectively.
Moving this one test reduces the 72-99 s imbalance without another VM, another
pytest process or a different pairing. This is an estimate from old timings, not a measured
whole-workflow improvement. Hosted validation must retain coverage and compare total elapsed
time including queues. Test assertions, timeouts, failure handling and build caches stay intact.

**Rejected:** a full duration database and runtime greedy assignment for this small adjustment.
Those add maintenance and risk different assignments for partial collections or platform-specific
collection. Renaming a test to manipulate its hash obscures the scheduling decision. Moving more
tests before measuring this one risks reversing the imbalance. Shortening the bridge test or
changing its wall-clock behavior would alter what is tested and is outside this change.

## Release runtime builds use GitHub-hosted runners

Status: decided

The separate runtime release workflow builds firmware on Ubuntu and executes native wheel
acceptance tests on Ubuntu 22.04 x86_64 and macOS 15 arm64, including for private repositories.
It uses artifacts and creates only draft releases on version tags. The main CI uses its own hosted
build matrix and is unchanged by the release workflow. Manual dispatch is the unpublished
verification path; real release URL access remains a first-publication check.

The matrix uses macOS 15 because [GitHub retired macOS 14 images on November 2, 2026](https://github.com/actions/runner-images/blob/main/README.md).

## One Python distribution is tested with both release runners

Status: decided

Build the sdist and its wheel once, then install that exact wheel in both native smoke tests.
Per-platform wheel builds were rejected because they can produce two different files with the
same filename and make artifact merging select one arbitrarily. Verify the exact asset set and
runner manifest checksums before creating a tag-triggered draft. Direct PRs and manual dispatch of Runtime release validate
without creating releases; automatic GitHub release publication remains out of scope. A draft
lets the maintainer review the release before its URLs become available to runtime preparation.

Use Ubuntu 22.04 for the release executable to preserve glibc 2.35 compatibility, even though
main CI runs on Ubuntu 24.04. Both release targets must pass native firmware smoke tests.

## PyPI publishes the distributions of the published GitHub release

Status: decided

A separate `publish-pypi.yml` runs on `release: published`. Publishing the reviewed draft is the
maintainer's release action: runtime downloads become available before the package reaches PyPI.
Download and verify the full release asset set, tag and existing SHA256SUMS in a read-only job;
pass only wheel and sdist to a separate job with OIDC permission and environment `pypi`.
Use PyPI Trusted Publishing instead of a stored API token. Do not rebuild distributions at this
stage: the exact wheel tested on both native targets must be the one users install from PyPI.

Rejected: publishing to PyPI on tag push (the runtime release would still be a draft), rebuilding
the wheel at upload time (different bytes from the tested artifact), and skipping existing PyPI
files on retries (can conceal an incomplete or conflicting publication). No actual publication
is needed to test the checksum/tag rejection logic locally; OIDC is checked on the first upload.

## Balance the second pair with the no-peer inquiry test

Status: decided (16 November 2026)

For four shards, move
`tests/test_bt_spp_initiator.py::test_with_no_peer_the_inquiry_runs_its_length_and_opens_nothing[esp32sim-nobody]`
from its hashed shard 2 to shard 3. Keep the HTTP bridge assignment and the same mechanism:
exact node IDs shared across both platforms and all Python versions, with hash fallback for
all other tests and counts. The partial-collection regression now checks each assignment
separately, so adding an exception also checks that its node exists in its target shard.

Run [43813352186](https://github.com/mateuszsikora/esp32-testing-library/actions/runs/43813352186)
passed all 25 jobs after #216. On macOS, shard 2 took 227.80, 178.72 and 185.88 seconds versus
202.12, 156.05 and 153.07 seconds for shard 3, in Python version order 3.11, 3.13, 3.14.
Linux's gaps were smaller: 154.04 vs 147.37, 159.86 vs 150.28 and 170.08 vs 154.55 seconds.
The no-peer inquiry call had a median of 7.64 seconds on macOS and 4.55 seconds on Linux.
Moving it reduces the estimated pair duration on both platforms; it does not establish a
whole-workflow gain. The baseline took 10m22s including queues, with the last test VM starting
at 5m55s. Hosted validation must separate job execution from queue time.

**Rejected:** moving approximately 13 seconds of macOS work by also moving the SD reset test.
Those two tests cost about 9-10 seconds on Linux, exceeding its smallest measured gap of
6.67 seconds and potentially making that pair slower. Start with the Bluetooth case alone.
Do not shorten the inquiry, change firmware, drop test variants, alter the serial real-source
phase or add platform-dependent assignment tables. Workflow definitions, required checks,
cache keys and test failure handling remain unchanged.

## Prepare versions in a PR and build releases after merge

Status: decided (16 November 2026)

A manual workflow takes an explicit increasing stable version, updates only the Python package,
runner and runner lockfile metadata, and creates a normal PR. After a pure version bump reaches
main, a separate workflow tags that exact commit and calls the native release workflow. Publication
of its draft remains the final manual action before PyPI Trusted Publishing. This preserves a
single version history on main and works with existing branch protection.

Rejected: version commits existing only under tags (main would retain old metadata), direct bot
pushes to main (bypass the ordinary review/check path), and deriving versions from commit messages
(adds release-policy machinery). The tag uses GITHUB_TOKEN, whose pushes do not start new push
workflows, so the caller explicitly reuses Runtime release. A stored PAT or GitHub App credential
solely to trigger a tag-push event adds setup and privilege without benefit. Direct manual runs of
Runtime release remain verification-only; the explicit reusable input is a distinct release path.

## Verify exact version diffs before using lightweight CI

Status: decided (16 November 2026)

Recognize only the exact three metadata replacements, with matching increasing stable versions and
unchanged file modes. Any other change runs full CI, regardless of branch name or labels. Check
package metadata and documentation on the short path, and run both native release smoke tests
after merge before creating a draft. A classification failure fails the required lint check.

Keep the 25 required contexts and matrix names; native build/test jobs run a minimal Ubuntu step
on version-only changes. This spends short Linux jobs to preserve existing protection without
allocating macOS VMs or doing compilation. Rejected: skipping whole reusable matrix jobs (their
expanded required contexts may be missing), replacing required checks as part of this release
feature, and writing synthetic successful statuses. No whole-workflow speed claim is made without
hosted measurements. The main CI's normal test matrix and firmware assertions remain intact.

The documentation checker exempts only changes to the owning package-version field in Python
and Cargo metadata. The previous dependency-only exemption did not cover release versions.
Other fields, comments and dependency pins still require their usual documentation.

## The chips' images build in three groups of their own

Status: decided (#236)

The firmware matrix has `chips-1` to `chips-3`, each running `firmware/build.sh --chips --shard
K/3` and packing `firmware/*/build-*`; the test jobs restore and unpack them with the other
groups. The shards follow `--shard`'s round robin like the other families, so adding an image
needs no change to the workflow.

**Rejected:** putting the entries into the `idf` shards, which would lengthen every classic group;
a group per chip, which would hard-code the chip list in the workflow, though its container's
ccache would hit more, every image of a group being for one target.

## A pull request need not be up to date with main to merge

Status: decided (18 November 2026)

Branch protection on `main` no longer requires strict status checks. With them, a pull request
without conflicts still could not merge once another landed before it: it had to merge `main` in
and run the whole CI again for every pull request merged ahead of it. The squash merge applies
the branch's diff to the `main` it lands on, and a conflict still blocks it.

**Rejected:** keeping strict checks, which made several ready pull requests on one day each wait
for a full CI run after every merge. What it would catch, two pull requests that merge cleanly
but break together, is caught by CI on `main` after the merge, where it is fixed forward.
