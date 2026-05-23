#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_DIR="$ROOT_DIR/env"
SRC_DIR="$ENV_DIR/src"
BUILD_DIR="$ENV_DIR/build"
STAMP_DIR="$ENV_DIR/.stamps"
OUTPUT_DIR="$ENV_DIR/output"

ARCH="${ARCH:-aarch64}"
JOBS="${JOBS:-$(nproc 2>/dev/null || echo 4)}"
CLEAN="${CLEAN:-0}"

# Prevent any interactive prompts during builds
export TERM=dumb

LINUX_VERSION="6.12.28"
QEMU_VERSION="9.2.0"
BUILDROOT_VERSION="2024.02.10"

LINUX_SRC="$SRC_DIR/linux-${LINUX_VERSION}"
QEMU_SRC="$SRC_DIR/qemu-${QEMU_VERSION}"
BUILDROOT_SRC="$SRC_DIR/buildroot-${BUILDROOT_VERSION}"

LINUX_BUILD="$BUILD_DIR/linux-$ARCH"
QEMU_BUILD="$BUILD_DIR/qemu-$ARCH"
QEMU_INSTALL="$QEMU_BUILD/install/"
BUILDROOT_BUILD="$BUILD_DIR/buildroot-$ARCH"

case "$ARCH" in
    x86_64)
        QEMU_TARGET="x86_64-softmmu"
        LINUX_ARCH="x86_64"
        LINUX_DEFCONFIG="x86_64_defconfig"
        BUILDROOT_DEFCONFIG="qemu_x86_64_defconfig"
        CROSS_COMPILE=""
        ;;
    aarch64)
        QEMU_TARGET="aarch64-softmmu"
        LINUX_ARCH="arm64"
        LINUX_DEFCONFIG="defconfig"
        BUILDROOT_DEFCONFIG="qemu_aarch64_virt_defconfig"
        CROSS_COMPILE="aarch64-linux-gnu-"
        ;;
    riscv64)
        QEMU_TARGET="riscv64-softmmu"
        LINUX_ARCH="riscv"
        LINUX_DEFCONFIG="defconfig"
        BUILDROOT_DEFCONFIG="qemu_riscv64_virt_defconfig"
        CROSS_COMPILE="riscv64-linux-gnu-"
        ;;
    mipsel)
        QEMU_TARGET="mipsel-softmmu"
        LINUX_ARCH="mips"
        LINUX_DEFCONFIG="malta_defconfig"
        BUILDROOT_DEFCONFIG="qemu_mips32r2el_malta_defconfig"
        CROSS_COMPILE="mipsel-linux-gnu-"
        ;;
    *)
        echo "ERROR: Unsupported ARCH='$ARCH'. Supported: x86_64, aarch64, riscv64, mipsel" >&2
        exit 1
        ;;
esac

prereq_check() {
    local missing=()
    for tool in make gcc bison flex python3; do
        if ! command -v "$tool" &>/dev/null; then
            missing+=("$tool")
        fi
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        echo "ERROR: Missing build dependencies: ${missing[*]}" >&2
        exit 1
    fi

    if [[ -n "$CROSS_COMPILE" ]]; then
        local cc="${CROSS_COMPILE}gcc"
        if ! command -v "$cc" &>/dev/null; then
            echo "ERROR: Cross-compiler '$cc' not found in PATH (required for ARCH=$ARCH)" >&2
            exit 1
        fi
    fi
}

build_qemu() {
    echo "[build] QEMU for $ARCH (target: $QEMU_TARGET)"
    mkdir -p "$QEMU_BUILD"
    (
        cd "$QEMU_BUILD"
        "$QEMU_SRC/configure" \
            --target-list="$QEMU_TARGET" \
            --disable-werror \
            --disable-docs \
            --disable-gtk \
            --disable-sdl \
            --prefix="$QEMU_INSTALL"
        make -j"$JOBS"
        make install
    )

    local qemu_bin="$QEMU_INSTALL/bin/qemu-system-$ARCH"
    if [[ ! -e "$qemu_bin" ]]; then
        echo "ERROR: QEMU binary not found after install: $qemu_bin" >&2
        exit 1
    fi

    mkdir -p "$OUTPUT_DIR"
    ln -sf "$qemu_bin" "$OUTPUT_DIR/qemu-system-$ARCH"
}

build_linux() {
    echo "[build] Linux for $ARCH (arch: $LINUX_ARCH, defconfig: $LINUX_DEFCONFIG)"
    mkdir -p "$LINUX_BUILD"

    make -C "$LINUX_SRC" O="$LINUX_BUILD" ARCH="$LINUX_ARCH" "${LINUX_DEFCONFIG}" < /dev/null

    local fragment="$ROOT_DIR/configs/linux-$ARCH.fragment"
    if [[ -f "$fragment" ]]; then
        echo "[build] Applying config fragment: $fragment"
        cat "$fragment" >> "$LINUX_BUILD/.config"
    fi

    if [[ "$ARCH" == "mipsel" ]]; then
        echo "CONFIG_CPU_LITTLE_ENDIAN=y" >> "$LINUX_BUILD/.config"
    fi

    make -C "$LINUX_SRC" O="$LINUX_BUILD" ARCH="$LINUX_ARCH" olddefconfig < /dev/null

    if [[ -n "$CROSS_COMPILE" ]]; then
        make -C "$LINUX_SRC" O="$LINUX_BUILD" ARCH="$LINUX_ARCH" CROSS_COMPILE="$CROSS_COMPILE" -j"$JOBS" < /dev/null
    else
        make -C "$LINUX_SRC" O="$LINUX_BUILD" ARCH="$LINUX_ARCH" -j"$JOBS" < /dev/null
    fi

    mkdir -p "$OUTPUT_DIR"
    local kernel_src
    case "$ARCH" in
        x86_64)
            kernel_src="$LINUX_BUILD/arch/x86/boot/bzImage"
            ;;
        aarch64)
            kernel_src="$LINUX_BUILD/arch/arm64/boot/Image"
            ;;
        riscv64)
            kernel_src="$LINUX_BUILD/arch/riscv/boot/Image"
            ;;
        mipsel)
            kernel_src="$LINUX_BUILD/vmlinux"
            ;;
    esac
    cp "$kernel_src" "$OUTPUT_DIR/kernel-$ARCH"
}

build_buildroot() {
    echo "[build] Buildroot for $ARCH (defconfig: $BUILDROOT_DEFCONFIG)"
    mkdir -p "$BUILDROOT_BUILD"

    make O="$BUILDROOT_BUILD" -C "$BUILDROOT_SRC" "$BUILDROOT_DEFCONFIG"
    make O="$BUILDROOT_BUILD" -C "$BUILDROOT_SRC" -j"$JOBS" < /dev/null

    mkdir -p "$OUTPUT_DIR"
    cp "$BUILDROOT_BUILD/images/rootfs.ext4" "$OUTPUT_DIR/rootfs-$ARCH.ext4"
}

main() {
    for src_dir in "$QEMU_SRC" "$LINUX_SRC" "$BUILDROOT_SRC"; do
        if [[ ! -d "$src_dir" ]]; then
            echo "ERROR: Source directory missing: $src_dir" >&2
            echo "       Run ./setup-env.sh first to fetch sources." >&2
            exit 1
        fi
    done

    prereq_check
    mkdir -p "$BUILD_DIR" "$STAMP_DIR" "$OUTPUT_DIR"

    if [[ "$CLEAN" == "1" ]]; then
        echo "[build] CLEAN=1: removing build stamps"
        rm -f "$STAMP_DIR"/build-*-"$ARCH"
    fi

    if [[ -f "$STAMP_DIR/build-qemu-$ARCH" ]]; then
        echo "[build] QEMU already built, skipping"
    else
        build_qemu
        touch "$STAMP_DIR/build-qemu-$ARCH"
    fi

    if [[ -f "$STAMP_DIR/build-linux-$ARCH" ]]; then
        echo "[build] Linux already built, skipping"
    else
        build_linux
        touch "$STAMP_DIR/build-linux-$ARCH"
    fi

    if [[ -f "$STAMP_DIR/build-buildroot-$ARCH" ]]; then
        echo "[build] Buildroot already built, skipping"
    else
        build_buildroot
        touch "$STAMP_DIR/build-buildroot-$ARCH"
    fi

    echo "[build] All builds complete for ARCH=$ARCH"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
