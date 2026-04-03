#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${script_dir}/common.sh"

repo_root="$(autoemu_repo_root)"
qemu_binary="$(ensure_qemu_binary "${repo_root}")"

firmware_elf="$("${repo_root}/scripts/build_stm32_guest_firmware.sh")"

echo "Running ${firmware_elf} on stm32f4-board" >&2
"${qemu_binary}" \
  -M stm32f4-board \
  -kernel "${firmware_elf}" \
  -semihosting-config enable=on,target=native \
  -no-reboot \
  -nographic \
  -monitor none \
  -serial none
