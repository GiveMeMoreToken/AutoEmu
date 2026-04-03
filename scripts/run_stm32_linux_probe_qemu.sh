#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${script_dir}/common.sh"

repo_root="$(autoemu_repo_root)"
qemu_binary="$(ensure_qemu_binary "${repo_root}")"
linux_build_dir="${LINUX_BUILD_DIR:-${repo_root}/build/linux-out/stm32f4-probe}"
buildroot_build_dir="${BUILDROOT_BUILD_DIR:-${repo_root}/build/buildroot-out/stm32f4-probe}"
kernel_elf="${STM32_LINUX_KERNEL:-${linux_build_dir}/vmlinux}"
dtb_file="${STM32_LINUX_DTB:-${linux_build_dir}/autoemu-stm32f4-probe.dtb}"
rootfs_image="${STM32_LINUX_ROOTFS:-${buildroot_build_dir}/images/rootfs.cpio}"
qemu_log="${QEMU_LOG_FILE:-${linux_build_dir}/qemu-stm32f4-probe.log}"

require_commands tee

if [[ ! -f "${rootfs_image}" ]]; then
  "${repo_root}/scripts/build_stm32_linux_rootfs.sh" >/dev/null
fi

if [[ ! -f "${kernel_elf}" || ! -f "${dtb_file}" || "${rootfs_image}" -nt "${kernel_elf}" ]]; then
  STM32_LINUX_INITRAMFS="${rootfs_image}" \
    "${repo_root}/scripts/build_stm32_linux_probe_kernel.sh" >/dev/null
fi

mkdir -p "$(dirname "${qemu_log}")"

echo "Running ${kernel_elf} with ${dtb_file} on stm32f4-board" >&2
echo "Embedded initramfs source: ${rootfs_image}" >&2
echo "Saving guest console log to ${qemu_log}" >&2
"${qemu_binary}" \
  -M stm32f4-board \
  -kernel "${kernel_elf}" \
  -dtb "${dtb_file}" \
  -no-reboot \
  -nographic \
  -monitor none \
  -serial stdio | tee "${qemu_log}"
