# Installing the simulator runtime

Install Python 3.11 or newer, then, from your own project directory:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install /path/to/esp32tl-0.1.0-py3-none-any.whl
esp32tl runtime prepare
pytest tests/
```

Use a wheel from the same GitHub release as the runtime. No published release is required to
build or validate the artifacts; downloading through the normal command requires the release to
be published and accessible. The repository is public; draft assets still require publication before anonymous access.
A separate `publish-pypi.yml` uploads the verified wheel and sdist after the GitHub release
is published. Build your firmware beforehand and use `app_path` as
shown in [Writing tests](../guides/writing-tests.md). Neither this command nor pytest builds it.

The release matrix is Linux x86_64 with glibc (Ubuntu 22.04 or newer) and macOS arm64
(macOS 15 or newer). Each release must pass the wheel/firmware smoke test on both GitHub-hosted
runners before publication. Other architectures, Windows and musl Linux have no prebuilt runtime.
Local macOS verification alone does not establish Linux support: the first hosted workflow run
is a release prerequisite.

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

Lookup is per file: explicit `ESP32TL_RUNNER` / `ESP32TL_ROM`, then the source checkout's
`runner/target/release/esp32tl-runner` and `.cache/esp-rom-elfs/20241011/esp32_rev300_rom.elf`
when `runner/Cargo.toml` exists next to the source package, otherwise the user cache.
A missing checkout build is an error, not a fallback to a stale cached release; use `make setup`.
Explicit paths must exist and are verified too. Runner version and esp32sim revision must match
exactly, including for developer builds; an older runner without `--runtime-version` must be rebuilt.
The ROM must match the existing pinned ELF checksum.
When the backend selects a file from the cache, it checks the manifest and that file only.
For example, `ESP32TL_RUNNER` plus a cached ROM does not require the cached runner to exist or
execute; `ESP32TL_ROM` plus a cached runner does not require the cached ROM. A bad selected file
or incompatible cache manifest still fails. `esp32tl runtime prepare` always checks both cached
files, regardless of environment overrides, before reusing an installation.

Errors distinguish `unsupported runtime platform`, `runtime missing`, `incompatible runner`,
`invalid runtime manifest`, `download failed`, and `invalid checksum or missing file`.
Preparation exits 2 on failure. No automatic update or downgrade occurs.

In consumer CI install the wheel and run `esp32tl runtime prepare` before pytest. Cache
`~/.cache/esp32tl` keyed by package version and OS/architecture (or set XDG_CACHE_HOME explicitly).
The first run needs HTTPS access to GitHub and Espressif's GitHub release; later runs need neither
Rust nor network access. Cache contents must be trusted just like installed executables.

## Integrity and release maintenance

`scripts/package-runtime.py` checks the package version against Cargo.toml and the esp32sim pin,
queries the executable identity, and emits a raw executable plus JSON manifest with version,
platform, full esp32sim revision and SHA-256. Raw executables need no unpacking. The installer
checks both ROM archive and ELF hashes from `scripts/fetch-rom-elf`; keep those constants in sync.
For the ROM it copies only the single regular `esp32_rev300_rom.elf` member, with a bounded size;
archive paths and links are never extracted. Temporary files live beside the destination, then
one directory rename exposes the verified runtime atomically.

The release manifest is trusted through HTTPS from the repository release, not a separate
signature authority. SHA-256 detects corruption; it does not protect against a compromised
release publisher or local cache writer. No general package manager is introduced.

Release maintainers: [the release workflow](../cicd.md#runtime-releases) builds the Python
sdist/wheel once and tests that wheel on both native targets before creating a draft. Its
`verified-release` artifact contains all six assets plus SHA256SUMS; neither ROM nor firmware
is redistributed. `scripts/check-release.py` checks the exact asset set and manifest identities.

After the first PyPI publication, `python -m pip install esp32tl==0.1.0` can replace the local
wheel installation command above. Runtime preparation is still explicit and still downloads
native binaries from the matching GitHub release, not from PyPI.
