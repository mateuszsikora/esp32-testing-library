# Firmware decisions

Why the validation firmware in `firmware/` and the ESP32 mask ROM ELF are built, fetched and
checked the way they are.

## Validation images are built in the pinned ESP-IDF container

Status: decided (#47)

`firmware/build.sh` builds each image in `espressif/idf:v5.3.6`, pinned by the digest stage 0
recorded (`sha256:cee52f0…`, a multi-arch index, so arm64 Macs run it natively). That is the
ESP-IDF stage 0 measured, not whatever a developer has installed. `IDF_LOCAL=1` builds with the
ESP-IDF of the calling shell instead, for iteration; it need not be v5.3.6, so goldens come from
the container build only. Every build directory records how it was built in `esp32tl-build.json`
(`idf_version`, `container`, `null` for a local build, and `project`). A build directory with a
different record is removed before building, because CMake refuses a cache configured for another
project directory.

## A local build passes with a warning, a missing or foreign one fails

Status: decided (#47)

`tests/test_firmware_images.py` fails with `run make firmware` when `firmware/hello_world/build`
holds no finished build, or one from a container other than the one `build.sh` pins now (read
from the script). A build with `"container": null` passes with a `UserWarning`.

**Rejected:** skipping the test, because a skip would go unnoticed in CI; and failing a local
build, because that is what `IDF_LOCAL=1` is for.

## Images land in `firmware/<image>/build/`

Status: decided (#47)

`IdfApp(app_path)` reads `app_path/build/`, so a test passes the image's directory as its app,
`firmware/hello_world`, and needs no build directory of its own. `make firmware` and `make test`
always pass an empty `FIRMWARE_OUT`, which means `firmware/`, whatever the calling shell exports.

## The example is built in place from ESP-IDF's examples directory

Status: decided (#47)

`hello_world` is built from `$IDF_PATH/examples/get-started/hello_world`, unmodified, with
`-DSDKCONFIG` in the build directory. A fixed source path keeps incremental builds working: 4.8 s
against 1:47 for a clean build. Neither the container's v5.3.6 (a root-owned tree, built as uid
501) nor a local v5.5.5 writes into the example directory.

**Rejected:** copying the example out of `$IDF_PATH` before building, as stage 0 did, because a new
source path each time breaks incremental builds.

## A separate marker file marks a finished build

Status: decided (#47)

`build.sh` removes `esp32tl-built` before `idf.py` runs and writes it after a successful build.
`make test` depends on `firmware/<image>/build/esp32tl-built`, so it builds the images that are
missing or unfinished and nothing else.

**Rejected:** `flasher_args.json` as the sign of a finished build, because CMake writes it while
configuring, so an interrupted or failed build would count as built. Also rejected: writing
`esp32tl-build.json` only on success, because a failed compile would then lose the incremental
build.

## pytest never builds firmware

Status: decided (#47)

`make test` and `make firmware` build the images; the test suite only reads them. CI builds them
once, in the `firmware` job
([ci.md](ci.md#builds-reach-the-test-jobs-through-the-actions-cache-not-artifacts)).
`build.sh` itself is tested against a fake `docker` and a fake `idf.py` that record their
arguments, in milliseconds and without Docker (`tests/test_firmware_build.py`).

**Rejected:** a `requires_docker` test that built `hello_world` from scratch, because it added two
to four minutes to every `make test` with Docker and would have built the firmware a second time
in the CI test jobs on Linux. What it checked beyond the build running is covered by the fakes and
by the provenance check on the real image.

## The firmware build supports Docker Desktop and rootful Docker Engine only

Status: decided (#47)

The container runs as the host's uid (`HOME=/tmp` for that uid, unknown to the image), with the
repository mounted read-only and the output directory as `/out`, so nothing is written outside the
output directory and the host's uid owns what is. Under rootless Docker or Podman the output
belongs to another uid, and SELinux in enforcing mode would need `:z` on the mounts. The README
says so. An image that takes components from the component manager writes `dependencies.lock` and
`managed_components/` into its project directory, which the read-only mount refuses; the comment
at the mount in `build.sh` says so.

## The ROM ELF is pinned by its checksum, not by its release directory

Status: decided (#46)

`scripts/fetch-rom-elf` accepts `esp32_rev300_rom.elf` from any release under
`${IDF_TOOLS_PATH:-~/.espressif}/tools/esp-rom-elfs/*/` (`20241011` first) whose SHA-256 is the
pinned `920b7063…ca31`, and names each copy it skips on stderr. `docs/stage0-findings.md` recorded
the same hash for the copy in `20260528`, so a newer install with the same bytes counts. With no
match it downloads `esp-rom-elfs-20241011.tar.gz`.

**Rejected:** accepting only the `20241011` directory, because it would download a byte-identical
file next to an install that already has it.

## The ROM ELF is copied into .cache/esp-rom-elfs/20241011/

Status: decided (#46)

The script copies the file (857,500 bytes) to `.cache/esp-rom-elfs/20241011/esp32_rev300_rom.elf`,
from any working directory. The backend gets one path whether or not ESP-IDF is installed, and an
ESP-IDF upgrade that removes the old release cannot break it. The release directory stays in the
path, so `ESP32SIM_ROM_DIR=.cache/esp-rom-elfs/20241011` also works for runs by hand. `make clean`
keeps `.cache/`.

**Rejected:** a symlink, or using the file in place under the ESP-IDF tools path, because an
ESP-IDF upgrade can remove the release it points to, and a checkout without ESP-IDF would have no
path at all.

## The ROM ELF is checked on every make setup

Status: decided (#46)

`rom-elf` is a phony target, and the script hashes the file in place every time; that takes a few
milliseconds, and the script prints nothing when the file is good.

**Rejected:** a stamp file, because it would not notice a truncated or replaced ELF.

## The ROM checksums are pinned in the script

Status: decided (#46)

`fetch-rom-elf` pins both the ELF's SHA-256 and the tarball's (`921f0001…e9a9`, the value the
release's own `esp-rom-elfs-20241011-checksum.sha256` lists), and checks the tarball and the
extracted ELF against them.

**Rejected:** fetching the release's checksum file at run time, because that would only prove the
download matches whatever GitHub serves now.
