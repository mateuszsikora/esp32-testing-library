# Installing the simulator runtime

esp32tl is available on [PyPI](https://pypi.org/project/esp32tl/), with prebuilt simulator
runtimes in [GitHub Releases](https://github.com/mateuszsikora/esp32-testing-library/releases).
To build from a checkout, use the [developer setup](../guides/development.md).

Install Python 3.11 or newer, then, from your own project directory:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install esp32tl
esp32tl runtime prepare
pytest tests/
```

Alternatively, download the `.whl` asset from a published
[GitHub release](https://github.com/mateuszsikora/esp32-testing-library/releases) and install it
with `python -m pip install` followed by the downloaded wheel's filename, then run
`esp32tl runtime prepare`. The runtime comes from the matching `v<installed package version>`
release. No published release is required to build or validate artifacts locally, but normal
runtime preparation requires that release to be published and accessible; draft assets are not
available anonymously. `publish-pypi.yml` uploads the same verified wheel and sdist after the
GitHub release is published.

Build your firmware beforehand and use `app_path` as shown in
[Writing tests](../guides/writing-tests.md). Neither installation nor pytest builds it.
SD card image operations also require mtools (`brew install mtools` on macOS,
`sudo apt-get install mtools` on Debian/Ubuntu).

The release matrix is Linux x86_64 with glibc (Ubuntu 22.04 or newer) and macOS arm64
(macOS 15 or newer). Each release must pass the wheel/firmware smoke test on both GitHub-hosted
runners in the [runtime release workflow](../cicd.md#runtime-releases) before publication.
Other architectures, Windows and musl Linux have no prebuilt runtime.

`esp32tl runtime prepare` explicitly downloads the version-matched runner and JSON manifest from
`mateuszsikora/esp32-testing-library` release `v<package version>`, and the ROM archive directly
from Espressif's pinned `20241011` release. Nothing downloads during import or pytest. The ROM
is not redistributed in our artifacts; this change makes no assertion of permission to
redistribute Espressif's ROM. Existing upstream download terms apply.

The cache is `$XDG_CACHE_HOME/esp32tl/<version>/<platform>`, defaulting to
`~/.cache/esp32tl/<version>/<platform>` also on macOS. XDG_CACHE_HOME must be absolute.
A valid installation is verified and reused without network access. Corruption is an error;
remove the named version/platform directory and rerun preparation to repair it. A failed download
never exposes a partial installation. Concurrent preparations accept only a verified winner.

An installation holds the runner and the ROM ELF of every chip it runs: `esp32_rev300_rom.elf`
(the classic ESP32), `esp32s3_rev0_rom.elf`, `esp32c3_rev3_rom.elf` and `esp32c6_rev0_rom.elf`,
each pinned by its SHA-256 in `esp32tl.runtime.ROMS`.

Lookup is per file: explicit `ESP32TL_RUNNER`, and for the ROM of the board's chip `ESP32TL_ROM`
(the classic ESP32) or `ESP32TL_ROM_ESP32S3`, `ESP32TL_ROM_ESP32C3`, `ESP32TL_ROM_ESP32C6`; then the
source checkout's `runner/target/release/esp32tl-runner` and
`.cache/esp-rom-elfs/20241011/<rom>` when `runner/Cargo.toml` exists next to the source package,
otherwise the user cache.
A missing checkout build is an error, not a fallback to a stale cached release; use `make setup`.
Explicit paths must exist and are verified too. Runner version and esp32sim revision must match
exactly, including for developer builds; an older runner without `--runtime-version` must be rebuilt.
Each ROM must match its pinned ELF checksum.
When the backend selects a file from the cache, it checks the manifest and that file only.
For example, `ESP32TL_RUNNER` plus a cached ROM does not require the cached runner to exist or
execute; `ESP32TL_ROM` plus a cached runner does not require the cached ROM. A bad selected file
or incompatible cache manifest still fails. `esp32tl runtime prepare` always checks every cached
file, the runner and each ROM, regardless of environment overrides, before reusing an
installation.

Errors distinguish `unsupported runtime platform`, `runtime missing`, `incompatible runner`,
`invalid runtime manifest`, `download failed`, and `invalid checksum or missing file`.
Preparation exits 2 on failure. No automatic update or downgrade occurs.

A `download failed` error names the URL and the error the download got. When that error is an
HTTP 404 for the release's runtime manifest, the first file preparation downloads, the message
adds what it may mean and the ways round it, as:

```text
esp32tl: download failed: https://github.com/mateuszsikora/esp32-testing-library/releases/download/v0.1.0/esp32tl-runner-macos-arm64.json: HTTP Error 404: Not Found; possible causes: release v0.1.0 is not published (yet), or it has no runtime for macos-arm64; alternatives: set ESP32TL_RUNNER and ESP32TL_ROM, or install esp32tl from a source checkout and run make setup there
```

The 404 says only that the file is not there, so the causes are possibilities, not a diagnosis.
Any other HTTP status, a network error, and a 404 for the runner or the ROM archive after the
manifest was found keep the message without the hint. Preparation never tries another version
or another release instead.

In consumer CI install esp32tl from PyPI (or a published release wheel) and run `esp32tl runtime prepare` before pytest. Cache
`~/.cache/esp32tl` keyed by package version and OS/architecture (or set XDG_CACHE_HOME explicitly).
The first run needs HTTPS access to GitHub and Espressif's GitHub release; later runs need neither
Rust nor network access. Cache contents must be trusted just like installed executables.

## Integrity and release maintenance

`scripts/package-runtime.py` checks the package version against Cargo.toml and the esp32sim pin,
queries the executable identity, and emits a raw executable plus JSON manifest with version,
platform, full esp32sim revision and SHA-256. Raw executables need no unpacking. The installer
checks both ROM archive and ELF hashes from `scripts/fetch-rom-elf`; keep those constants in sync.
For the ROMs it copies only the regular member of each ELF name in `ROMS`, with a bounded size;
archive paths and links are never extracted. Temporary files live beside the destination, then
one directory rename exposes the verified runtime atomically.

The release manifest is trusted through HTTPS from the repository release, not a separate
signature authority. SHA-256 detects corruption; it does not protect against a compromised
release publisher or local cache writer. No general package manager is introduced.

Release maintainers: [the release workflow](../cicd.md#runtime-releases) builds the Python
sdist/wheel once and tests that wheel on both native targets before creating a draft. Its
`verified-release` artifact contains all six assets plus SHA256SUMS; neither ROM nor firmware
is redistributed. A runner taken from that artifact by hand has lost its executable bit and needs
`chmod +x` before `ESP32TL_RUNNER` can name it
([checking an artifact by hand](../cicd.md#checking-a-release-artifact-by-hand));
`esp32tl runtime prepare` sets the mode itself. `scripts/check-release.py` checks the exact asset set and manifest identities.

The current esp32sim revision is `357b0e542dfe97d4c62ec4a15c01c4969164448c`. When changing it,
update `esp32tl/runtime.py`, `runner/Cargo.toml` and `runner/Cargo.lock` together.
`test_runtime_revision_matches_runner_pins` checks their agreement.
