#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_DIR="$ROOT_DIR/env"
OUTPUT_DIR="$ENV_DIR/output"

ARCH="${ARCH:-aarch64}"
MEMORY="${MEMORY:-512M}"
EXTRA_QEMU_OPTS="${EXTRA_QEMU_OPTS:-}"

HOST_ARCH="$(uname -m)"
case "$HOST_ARCH" in
    x86_64|amd64) HOST_ARCH="x86_64" ;;
    aarch64|arm64) HOST_ARCH="aarch64" ;;
    riscv64) HOST_ARCH="riscv64" ;;
esac

case "$ARCH" in
    x86_64)
        QEMU_MACHINE="pc"
        QEMU_CPU="qemu64"
        CONSOLE="ttyS0"
        DRIVE_IF="virtio"
        ROOT_DEV="/dev/vda"
        ;;
    aarch64)
        QEMU_MACHINE="virt"
        QEMU_CPU="cortex-a72"
        CONSOLE="ttyAMA0"
        DRIVE_IF="virtio"
        ROOT_DEV="/dev/vda"
        ;;
    riscv64)
        QEMU_MACHINE="virt"
        QEMU_CPU="rv64"
        CONSOLE="ttyS0"
        DRIVE_IF="virtio"
        ROOT_DEV="/dev/vda"
        ;;
    mipsel)
        QEMU_MACHINE="malta"
        QEMU_CPU="24Kf"
        CONSOLE="ttyS0"
        DRIVE_IF="ide"
        ROOT_DEV="/dev/hda"
        ;;
    *)
        echo "ERROR: Unsupported ARCH='$ARCH'. Supported: x86_64, aarch64, riscv64, mipsel" >&2
        exit 1
        ;;
esac

QEMU_BIN="$OUTPUT_DIR/qemu-system-$ARCH"
KERNEL="$OUTPUT_DIR/kernel"
ROOTFS="$OUTPUT_DIR/rootfs.ext4"

for f in "$QEMU_BIN" "$KERNEL" "$ROOTFS"; do
    if [[ ! -e "$f" ]]; then
        echo "ERROR: Missing artifact: $f" >&2
        echo "Run: ./build-env.sh ARCH=$ARCH" >&2
        exit 1
    fi
done

QEMU_ARGS=(
    -M "$QEMU_MACHINE"
    -m "$MEMORY"
    -cpu "$QEMU_CPU"
    -kernel "$KERNEL"
    -drive "file=$ROOTFS,format=raw,if=$DRIVE_IF"
    -append "root=$ROOT_DEV rw console=$CONSOLE nographic"
    -nographic
)

if [[ "$ARCH" == "$HOST_ARCH" && -e /dev/kvm ]]; then
    echo "[run] KVM detected, enabling acceleration"
    QEMU_ARGS+=(-enable-kvm)
fi

if [[ -n "$EXTRA_QEMU_OPTS" ]]; then
    read -ra EXTRA_ARGS <<< "$EXTRA_QEMU_OPTS"
    QEMU_ARGS+=("${EXTRA_ARGS[@]}")
fi

echo "[run] Launching qemu-system-${ARCH} (${MEMORY} RAM, machine: ${QEMU_MACHINE})"
echo "[run] Kernel: ${KERNEL}"
echo "[run] Rootfs: ${ROOTFS}"
echo "[run] Press Ctrl+A then X to quit QEMU"
echo ""

exec "$QEMU_BIN" "${QEMU_ARGS[@]}"
