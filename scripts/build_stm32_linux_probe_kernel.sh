#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${script_dir}/common.sh"

repo_root="$(autoemu_repo_root)"
build_root="$(autoemu_build_root "${repo_root}")"
linux_src_root="${LINUX_SOURCE_ROOT:-${build_root}/linux-src}"
linux_src="${LINUX_SRC:-${linux_src_root}/linux-6.17.0-rc1}"
build_dir="${LINUX_BUILD_DIR:-${repo_root}/build/linux-out/stm32f4-probe}"
buildroot_build_dir="${BUILDROOT_BUILD_DIR:-${repo_root}/build/buildroot-out/stm32f4-probe}"
dts_source="${STM32_LINUX_DTS:-${repo_root}/scripts/linux/autoemu-stm32f4-probe.dts}"
dtb_output="${build_dir}/autoemu-stm32f4-probe.dtb"
kernel_elf="${build_dir}/vmlinux"
linux_probe_overlay_dir="${repo_root}/scripts/linux/kernel"
linux_probe_dir="${linux_src}/drivers/misc/autoemu"
linux_misc_kconfig="${linux_src}/drivers/misc/Kconfig"
linux_misc_makefile="${linux_src}/drivers/misc/Makefile"
default_initramfs="${buildroot_build_dir}/images/rootfs.cpio"
initramfs_source="${STM32_LINUX_INITRAMFS:-}"

require_commands make cpp dtc realpath

if [[ ! -d "${linux_src}" ]]; then
    echo "Linux source tree not found at ${linux_src}" >&2
    exit 1
fi

if [[ -z "${initramfs_source}" && -f "${default_initramfs}" ]]; then
    initramfs_source="${default_initramfs}"
fi

if [[ -n "${initramfs_source}" ]]; then
    initramfs_source="$(realpath "${initramfs_source}")"
fi

mkdir -p "${build_dir}"

mkdir -p "${linux_probe_dir}"
ln -sf "${linux_probe_overlay_dir}/Kconfig" "${linux_probe_dir}/Kconfig"
ln -sf "${linux_probe_overlay_dir}/Makefile" "${linux_probe_dir}/Makefile"
ln -sf "${linux_probe_overlay_dir}/autoemu_virt_probe.c" \
    "${linux_probe_dir}/autoemu_virt_probe.c"

if ! grep -q 'source "drivers/misc/autoemu/Kconfig"' "${linux_misc_kconfig}"; then
    printf '\nsource "drivers/misc/autoemu/Kconfig"\n' >> "${linux_misc_kconfig}"
fi

if ! grep -q 'obj-$(CONFIG_AUTOEMU_VIRT_PROBES) += autoemu/' "${linux_misc_makefile}"; then
    printf '\nobj-$(CONFIG_AUTOEMU_VIRT_PROBES) += autoemu/\n' >> "${linux_misc_makefile}"
fi

make_args=(
    O="${build_dir}"
    ARCH=arm
    LLVM=1
    LLVM_IAS=1
    CC="${CC:-clang}"
    LD="${LD:-ld.lld}"
    AR="${AR:-llvm-ar-18}"
    NM="${NM:-llvm-nm-18}"
    OBJCOPY="${OBJCOPY:-llvm-objcopy-18}"
    OBJDUMP="${OBJDUMP:-llvm-objdump-18}"
    STRIP="${STRIP:-llvm-strip-18}"
    READELF="${READELF:-llvm-readelf-18}"
    HOSTCC="${HOSTCC:-clang}"
    HOSTCXX="${HOSTCXX:-clang++}"
)

make -C "${linux_src}" "${make_args[@]}" stm32_defconfig >/dev/null

"${linux_src}/scripts/config" --file "${build_dir}/.config" \
    -d XIP_KERNEL \
    -e CMDLINE_FORCE \
    -e BLK_DEV_INITRD \
    -e DMADEVICES \
    -e STM32_DMA \
    -e SERIAL_AMBA_PL011 \
    -e SERIAL_AMBA_PL011_CONSOLE \
    -e AUTOEMU_VIRT_PROBES \
    -e USB_SUPPORT \
    -e USB_GADGET \
    -e USB_DWC2 \
    -e USB_DWC2_PERIPHERAL \
    -e USB_ZERO \
    --set-val CPU_V7M_NUM_IRQ 96 \
    --set-val DRAM_BASE 0x20000000 \
    --set-val DRAM_SIZE 0x01000000 \
    --set-str CMDLINE "earlycon=pl011,0x40011000 console=ttyAMA0,115200 ignore_loglevel loglevel=8 initcall_debug panic=-1 lpj=1680000" \
    --set-str INITRAMFS_SOURCE "${initramfs_source}"

make -C "${linux_src}" "${make_args[@]}" olddefconfig >/dev/null
make -C "${linux_src}" "${make_args[@]}" -j"$(nproc)" vmlinux >/dev/null

cpp -nostdinc -undef -x assembler-with-cpp \
    -I "${linux_src}/arch/arm/boot/dts" \
    -I "${linux_src}/include" \
    "${dts_source}" \
    > "${build_dir}/autoemu-stm32f4-probe.dts.pp"

dtc -I dts -O dtb -o "${dtb_output}" "${build_dir}/autoemu-stm32f4-probe.dts.pp"

printf '%s\n' "${kernel_elf}"
