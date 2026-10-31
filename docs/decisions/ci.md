# CI decisions

Why `.github/workflows/ci.yml` runs its jobs where it does, how builds reach the test jobs, and
what each job runs.

## Every Linux job runs on the self-hosted runner hp600g1

Status: decided (#48)

The `firmware`, `runner`, `test` and `lint` jobs run on `[self-hosted, Linux, X64]`, the machine
hp600g1, even a job such as `lint` that needs nothing from it. The repository is private, so
minutes on GitHub-hosted runners are limited, and macOS minutes count ten times; every job that
can runs on hp600g1. The machine also keeps the 7 GB `espressif/idf` image between runs, so the
firmware build pulls nothing, and it has Docker, so the `requires_docker` tests run there. The cost
is the queue: hp600g1 is one machine, and its jobs run one after another. In the run of #50,
`lint` went before `firmware` and delayed `firmware` and the Linux test jobs by about a minute. The
header comment of `ci.yml` gives this reason, so the choice is not proposed again (#50).

**Rejected:** `ubuntu-latest` for jobs that need nothing from hp600g1, such as `lint`, because each
run would spend hosted minutes (about one per run for `lint`) that the private repository is short
of.

## The macOS jobs run only once the repository is public

Status: decided (#52)

The `runner` and `test` matrices take their platforms from
`fromJSON(github.event.repository.private && '["linux"]' || '["linux", "macos"]')`. While the
repository is private they are the only jobs that would use GitHub-hosted runners (`runner (macos)`
and three `test (macos, …)` per run), and macOS minutes count ten times against the limit. Making
the repository public turns them back on with no change to the workflow.

**Rejected:** deleting the macOS entries, because someone would have to remember to restore them
when the repository goes public; the condition does it by itself.

## The runs-on label is computed from the platform

Status: decided (#52)

`runs-on` is `matrix.platform == 'macos' && 'macos-latest' || fromJSON('["self-hosted", "Linux",
"X64"]')`, with no `include` entries.

**Rejected:** an `include` entry per platform, because an `include` for `macos` with `macos` missing
from the matrix matches no combination, so GitHub adds it as a job of its own: a macOS job with no
Python version.

## CI moves to GitHub-hosted runners before the repository goes public

Status: decided (#52)

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

## Each cache key hashes the whole workflow and the Makefile

Status: decided (#48)

The firmware key hashes `firmware/**`, the `Makefile` and `ci.yml`; the runner key hashes
`runner/**`, the `Makefile` and `ci.yml`. `firmware/build.sh` holds the IDF tag, the container
digest and each image's project, and `runner/` holds the esp32sim pin, the toolchain and the
profiles, so a change to any of them is a new key. Since a tarball saved under a key stays there,
the `firmware` job checks it before saving: every image must have `esp32tl-built`,
`esp32tl-build.json`, `flasher_args.json`, `config/sdkconfig.json`, `project_description.json`,
each flash image `flasher_args.json` names and the ELF `project_description.json` names
(`app_elf`). The tarball holds only what `IdfApp` reads: 15 MB for `hello_world` against 116 MB
for the whole build directory, about 1 MB once the cache compresses it. The cost of hashing the
whole workflow is that any change to `ci.yml` or the `Makefile`, a comment included, rebuilds the
firmware (about 3 min on hp600g1) and the runner. If that starts to hurt, the packing can move into
a script whose hash replaces the workflow's.

**Rejected:** keys over a hand-picked list of files, because a key that misses an input serves a
stale build, and for the runner skips its tests, without any sign of it.

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

## The ROM ELF is fetched once, by the firmware job

Status: decided (#48)

The `firmware` job runs `make rom-elf` under a cache entry keyed by the hash of
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

The `runner` job installs rustup only on a cache miss, so on hp600g1 `~/.cargo/bin` may not be on
`PATH` when `lint` runs. `lint` runs the same six-line step itself.

**Rejected:** a composite action shared by both jobs, because it is more than two copies of six
lines are worth.
