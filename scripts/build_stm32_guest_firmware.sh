#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${script_dir}/common.sh"

repo_root="$(autoemu_repo_root)"
firmware_dir="${repo_root}/firmware/stm32f4_probe"
build_dir="${BUILD_DIR:-${repo_root}/build/guest-firmware}"
cube_commit="b77c8154573522d4f9d7d3600057e73456fec715"
default_cube_root="${repo_root}/build/third_party/STM32CubeF4-${cube_commit}"
cube_root="${STM32CUBEF4_ROOT:-${default_cube_root}}"
output="${build_dir}/stm32f4_probe.elf"
map_file="${build_dir}/stm32f4_probe.map"

require_commands clang git

ensure_cube_root() {
  if [[ -d "${cube_root}/Drivers/STM32F4xx_HAL_Driver/Inc" ]] && \
     [[ -d "${cube_root}/Drivers/CMSIS/Include" ]] && \
     [[ -d "${cube_root}/Drivers/CMSIS/Device/ST/STM32F4xx/Include" ]]; then
    return
  fi

  if [[ -n "${STM32CUBEF4_ROOT:-}" ]]; then
    echo "STM32CUBEF4_ROOT is set but does not point to a usable STM32CubeF4 tree: ${cube_root}" >&2
    exit 1
  fi

  mkdir -p "$(dirname "${cube_root}")"
  rm -rf "${cube_root}"

  echo "Fetching STM32CubeF4 ${cube_commit} into ${cube_root}" >&2
  git clone --filter=blob:none https://github.com/STMicroelectronics/STM32CubeF4.git "${cube_root}" >&2
  git -C "${cube_root}" checkout "${cube_commit}" >&2
  git -C "${cube_root}" submodule update --init \
    Drivers/STM32F4xx_HAL_Driver \
    Drivers/CMSIS \
    Drivers/CMSIS/Device/ST/STM32F4xx >&2
}

pick_source() {
  local preferred="$1"
  local fallback="$2"

  if [[ -f "${preferred}" ]]; then
    printf '%s\n' "${preferred}"
  else
    printf '%s\n' "${fallback}"
  fi
}

ensure_cube_root
mkdir -p "${build_dir}"

dma_source="$(pick_source \
  "${repo_root}/data/stm32/stm32f407vg/drivers/hal/stm32f4xx_hal_dma.c" \
  "${cube_root}/Drivers/STM32F4xx_HAL_Driver/Src/stm32f4xx_hal_dma.c")"
eth_source="$(pick_source \
  "${repo_root}/data/stm32/stm32f407vg/drivers/hal/stm32f4xx_hal_eth.c" \
  "${cube_root}/Drivers/STM32F4xx_HAL_Driver/Src/stm32f4xx_hal_eth.c")"
pcd_source="$(pick_source \
  "${repo_root}/data/stm32/stm32f407vg/drivers/hal/stm32f4xx_hal_pcd.c" \
  "${cube_root}/Drivers/STM32F4xx_HAL_Driver/Src/stm32f4xx_hal_pcd.c")"
ll_usb_source="$(pick_source \
  "${repo_root}/data/stm32/stm32f407vg/drivers/ll/stm32f4xx_ll_usb.c" \
  "${cube_root}/Drivers/STM32F4xx_HAL_Driver/Src/stm32f4xx_ll_usb.c")"

clang \
  --target=arm-none-eabi \
  -mcpu=cortex-m4 \
  -mthumb \
  -O1 \
  -ffreestanding \
  -fno-builtin \
  -fno-stack-protector \
  -ffunction-sections \
  -fdata-sections \
  -DSTM32F407xx \
  -DUSE_HAL_DRIVER \
  -I"${firmware_dir}" \
  -I"${cube_root}/Drivers/STM32F4xx_HAL_Driver/Inc" \
  -I"${cube_root}/Drivers/CMSIS/Include" \
  -I"${cube_root}/Drivers/CMSIS/Device/ST/STM32F4xx/Include" \
  -nostdlib \
  -fuse-ld=lld \
  -Wl,-T,"${firmware_dir}/linker.ld" \
  -Wl,-Map,"${map_file}" \
  -Wl,--gc-sections \
  "${firmware_dir}/startup.c" \
  "${firmware_dir}/semihost.c" \
  "${firmware_dir}/runtime.c" \
  "${firmware_dir}/hal_support.c" \
  "${firmware_dir}/main.c" \
  "${dma_source}" \
  "${eth_source}" \
  "${pcd_source}" \
  "${ll_usb_source}" \
  -o "${output}"

printf '%s\n' "${output}"
