#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_DIR="$ROOT_DIR/env"
SRC_DIR="$ENV_DIR/src"
STAMP_DIR="$ENV_DIR/.stamps"

LINUX_VERSION="6.12.28"
QEMU_VERSION="9.2.0"
BUILDROOT_VERSION="2024.02.10"

LINUX_URL="https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-${LINUX_VERSION}.tar.xz"
QEMU_URL="https://download.qemu.org/qemu-${QEMU_VERSION}.tar.xz"
BUILDROOT_URL="https://buildroot.org/downloads/buildroot-${BUILDROOT_VERSION}.tar.xz"

LINUX_SHA256="e8a099182562aecff781de72ce769461e706d97af42d740dff20eb450dd5771e"
QEMU_SHA256="f859f0bc65e1f533d040bbe8c92bcfecee5af2c921a6687c652fb44d089bd894"
BUILDROOT_SHA256="b193867d91ed468925a76828bd35ba64d8b4bd1ec238e35db8722fdd406926c2"

download_file() {
    local url="$1"
    local dest="$2"
    tmp_dest="${dest}.tmp"

    cleanup_tmp() {
        if [[ -f "$tmp_dest" ]]; then
            rm -f "$tmp_dest"
        fi
    }
    trap cleanup_tmp EXIT

    if command -v wget &>/dev/null; then
        wget -q -O "$tmp_dest" "$url"
    elif command -v curl &>/dev/null; then
        curl -sL -o "$tmp_dest" "$url"
    else
        echo "ERROR: wget or curl is required" >&2
        exit 1
    fi

    mv -f "$tmp_dest" "$dest"
    trap - EXIT
}

verify_checksum() {
    local file="$1"
    local expected="$2"
    local actual
    actual="$(sha256sum -- "$file" | awk '{print $1}')"
    if [[ "$actual" != "$expected" ]]; then
        echo "ERROR: SHA256 mismatch for $file" >&2
        echo "  expected: $expected" >&2
        echo "  actual:   $actual" >&2
        rm -f -- "$file"
        exit 1
    fi
}

setup_component() {
    local name="$1"
    local url="$2"
    local sha256="$3"
    local src_dir="$4"
    local stamp="$STAMP_DIR/download-${name}"

    if [[ -f "$stamp" ]]; then
        echo "[setup] ${name}: already downloaded (stamp exists)"
        return 0
    fi

    local tarball_name
    tarball_name="$(basename "$url")"
    local tarball_path="$SRC_DIR/$tarball_name"

    if [[ ! -f "$tarball_path" ]]; then
        echo "[setup] ${name}: downloading ${tarball_name}"
        download_file "$url" "$tarball_path"
    else
        echo "[setup] ${name}: tarball already present"
    fi

    echo "[setup] ${name}: verifying SHA256"
    verify_checksum "$tarball_path" "$sha256"

    if [[ ! -d "$src_dir" ]]; then
        echo "[setup] ${name}: extracting"
        tar -xf "$tarball_path" -C "$SRC_DIR"
    else
        echo "[setup] ${name}: source already extracted"
    fi

    if [[ ! -d "$src_dir" ]]; then
        echo "ERROR: ${name}: expected source directory ${src_dir} not found after extraction" >&2
        exit 1
    fi

    touch "$stamp"
    echo "[setup] ${name}: done"
}

main() {
    mkdir -p "$SRC_DIR" "$STAMP_DIR"

    setup_component "linux" "$LINUX_URL" "$LINUX_SHA256" "$SRC_DIR/linux-${LINUX_VERSION}"
    setup_component "qemu" "$QEMU_URL" "$QEMU_SHA256" "$SRC_DIR/qemu-${QEMU_VERSION}"
    setup_component "buildroot" "$BUILDROOT_URL" "$BUILDROOT_SHA256" "$SRC_DIR/buildroot-${BUILDROOT_VERSION}"

    echo "[setup] All sources ready in ${SRC_DIR}"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
