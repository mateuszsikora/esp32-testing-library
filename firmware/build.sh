#!/bin/sh
# Builds the validation firmware for the classic ESP32 (plan M0), each image
# into $FIRMWARE_OUT/<image>/build/, the layout pytest-embedded's IdfApp reads:
# flasher_args.json, which the backend turns into the runner's --flash-at
# offsets, beside the ELF, the binaries and config/sdkconfig.json.
# FIRMWARE_OUT is firmware/ by default, or examples/ with --examples.
#
#   firmware/build.sh [IMAGE...]     all validation images when none is named
#   firmware/build.sh --examples [NAME...]     all examples when none is named
#
# The build runs in ESP-IDF's container, pinned by digest. IDF_LOCAL=1 builds
# with the ESP-IDF of the calling shell instead (export.sh sourced), for
# iteration; goldens come from the container build only, because a local IDF
# need not be the pinned one (plan §7). Every build directory records how it
# was built in esp32tl-build.json, with "container": null for a local build.
#
# Builds are incremental. A build directory is removed first when its
# esp32tl-build.json is missing or, compared as text, differs from the
# record of the build about to start: another ESP-IDF version, container or
# project directory. ESP-IDF refuses some of these itself (another project
# directory, Python or toolchain, as a switch between a local and a
# container build brings), but would reuse a directory from a version or
# container that changes none of them and keep the old build's sdkconfig:
# the image would not be what a build from scratch makes.
# esp32tl-built marks a build that finished. flasher_args.json cannot: CMake
# writes it while configuring, before anything is compiled.
set -eu

IDF_VERSION=v5.3.6
CONTAINER=espressif/idf:$IDF_VERSION@sha256:cee52f0c4fdaf088992a7a739f5993154e95cf1d3ab61c24f5b40e6d56a3a070
IMAGES="hello_world nmea_reader gpio_echo sdspi modem_at nvs_reader pppos_client http_request persistence"
family=firmware
if [ "${1:-}" = --examples ]; then
	shift
	family=examples
	IMAGES=
	for project in "$(dirname "$0")"/../examples/*/CMakeLists.txt; do
		[ -f "$project" ] || continue
		name=${project%/CMakeLists.txt}
		IMAGES="${IMAGES:+$IMAGES }${name##*/}"
	done
	[ -n "$IMAGES" ] || { echo "firmware/build.sh: no examples found" >&2; exit 1; }
fi

die() {
	echo "firmware/build.sh: $*" >&2
	exit 1
}

# The ESP-IDF project an image is built from: one of ESP-IDF's examples, or
# one kept under firmware/ or examples/ (the working directory is the repository's root
# when this runs).
project_of() {
	if [ "$family" = examples ]; then
		echo "$PWD/examples/$1"
		return
	fi
	case $1 in
	hello_world) echo "$IDF_PATH/examples/get-started/hello_world" ;;
	sdspi) echo "$IDF_PATH/examples/storage/sd_card/sdspi" ;;
	http_request)
		# Copied, unchanged, into the output: its component manager writes
		# dependencies.lock into the project, and ESP-IDF's own examples are
		# read-only in the container. One copy per ESP-IDF version, made once.
		source=$out/.http_request-$idf_version
		if [ ! -d "$source" ]; then
			rm -rf "$source.partial"
			cp -Rp "$IDF_PATH/examples/protocols/http_request" "$source.partial"
			mv "$source.partial" "$source"
		fi
		echo "$source"
		;;
	nmea_reader) echo "$PWD/firmware/nmea_reader" ;;
	gpio_echo) echo "$PWD/firmware/gpio_echo" ;;
	modem_at) echo "$PWD/firmware/modem_at" ;;
	pppos_client) python3 firmware/pppos_client/prepare.py "$out" ;;
	nvs_reader) echo "$PWD/firmware/nvs_reader" ;;
	persistence) echo "$PWD/firmware/persistence" ;;
	esac
}

for image in "$@"; do
	case " $IMAGES " in
	*" $image "*) ;;
	*) die "unknown image '$image'; the images are: $IMAGES" ;;
	esac
done
[ $# -gt 0 ] || set -- $IMAGES

# What the build needs is checked before anything is written. Docker creates
# /.dockerenv in every container; without it, ESP32TL_CONTAINER was left set on
# the host, and a build that took it for the container's would record a
# container it never ran in.
if [ -n "${ESP32TL_CONTAINER:-}" ]; then
	[ -f /.dockerenv ] || die "ESP32TL_CONTAINER is set outside a container; unset it"
	container="\"$ESP32TL_CONTAINER\""
elif [ "${IDF_LOCAL:-}" = 1 ]; then
	command -v idf.py >/dev/null 2>&1 && [ -n "${IDF_PATH:-}" ] ||
		die "IDF_LOCAL=1 needs ESP-IDF in the environment: source \$IDF_PATH/export.sh first"
	container=null
else
	command -v docker >/dev/null 2>&1 ||
		die "needs Docker, or IDF_LOCAL=1 to build with a local ESP-IDF"
	docker info >/dev/null 2>&1 ||
		die "the Docker daemon is not reachable ('docker info' failed); is Docker running?"
	container=
fi

# Before changing directory, so that a relative FIRMWARE_OUT is the caller's.
out=${FIRMWARE_OUT:-$(dirname "$0")/../$family}
mkdir -p "$out"
out=$(cd "$out" && pwd -P)
cd "$(dirname "$0")/.."

# The host side: start the container, which runs this script again with
# ESP32TL_CONTAINER naming its image and ESP32TL_HOST_OUT the host's path of
# /out, for the messages.
if [ -z "$container" ]; then
	[ "$family" != examples ] || set -- --examples "$@"
	# The repository read-only, so a build cannot write into it except
	# through /out. hello_world writes nothing into its project directory; a
	# project under firmware/ that takes components from the component manager
	# writes dependencies.lock and managed_components/ into its own, and needs
	# that directory writable first. The host's uid owns what the build
	# writes; HOME=/tmp gives that uid, unknown to the image, a writable home.
	exec docker run --rm --init \
		--user "$(id -u):$(id -g)" \
		--env HOME=/tmp \
		--env ESP32TL_CONTAINER="$CONTAINER" \
		--env FIRMWARE_OUT=/out \
		--env ESP32TL_HOST_OUT="$out" \
		--volume "$PWD:/project:ro" \
		--volume "$out:/out" \
		--workdir /project \
		"$CONTAINER" firmware/build.sh "$@"
fi

idf_version=$(idf.py --version | sed -n 's/^ESP-IDF //p')
[ -n "$idf_version" ] || die "'idf.py --version' printed no ESP-IDF version"
if [ "$container" = null ]; then
	echo "firmware/build.sh: IDF_LOCAL=1, ESP-IDF $idf_version at $IDF_PATH; not for goldens, which come from the container ($IDF_VERSION)" >&2
fi

json_string() {
	printf '"%s"' "$(printf '%s' "$1" | sed 's/[\\"]/\\&/g')"
}

for image in "$@"; do
	project=$(project_of "$image")
	build=$out/$image/build
	shown=${ESP32TL_HOST_OUT:-$out}/$image/build
	provenance=$(printf '{"idf_version": %s, "container": %s, "project": %s}' \
		"$(json_string "$idf_version")" "$container" "$(json_string "$project")")
	if [ -d "$build" ] && [ "$(cat "$build/esp32tl-build.json" 2>/dev/null)" != "$provenance" ]; then
		echo "firmware/build.sh: $image: $shown was built another way, removing it"
		rm -rf "$build"
	fi
	mkdir -p "$build"
	rm -f "$build/esp32tl-built"
	printf '%s\n' "$provenance" >"$build/esp32tl-build.json"
	# sdkconfig in the build directory: the project directory may be ESP-IDF's
	# own examples, or read-only.
	if [ "$family" = firmware ] && [ "$image" = pppos_client ]; then
		idf.py -C "$project" -B "$build" -DSDKCONFIG="$build/sdkconfig" -DIDF_TARGET=esp32 \
			"-DSDKCONFIG_DEFAULTS=$project/sdkconfig.defaults;$PWD/firmware/pppos_client/sdkconfig.defaults" build
	else
		idf.py -C "$project" -B "$build" -DSDKCONFIG="$build/sdkconfig" -DIDF_TARGET=esp32 build
	fi
	[ -f "$build/flasher_args.json" ] || die "$image: the build wrote no flasher_args.json"
	: >"$build/esp32tl-built"
	echo "firmware/build.sh: $image built in $shown (ESP-IDF $idf_version)"
done
