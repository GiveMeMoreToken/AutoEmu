#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${script_dir}/common.sh"

repo_root="$(autoemu_repo_root)"
build_root="$(autoemu_build_root "${repo_root}")"
buildroot_version="${BUILDROOT_VERSION:-2025.05}"
buildroot_download_dir="${BUILDROOT_SOURCE_ROOT:-${build_root}/buildroot-src}"
buildroot_src="${BUILDROOT_SRC:-${buildroot_download_dir}/buildroot-${buildroot_version}}"
buildroot_archive="${buildroot_download_dir}/buildroot-${buildroot_version}.tar.xz"
buildroot_unpack_dir="${buildroot_download_dir}/buildroot-${buildroot_version}"
build_dir="${BUILDROOT_BUILD_DIR:-${repo_root}/build/buildroot-out/stm32f4-probe}"
download_dir="${BUILDROOT_DL_DIR:-${repo_root}/build/buildroot-dl}"
external_tree="${repo_root}/scripts/buildroot/external"
rootfs_image="${build_dir}/images/rootfs.cpio"

require_commands curl make tar

mkdir -p "${buildroot_download_dir}" "${download_dir}"

if [[ ! -d "${buildroot_src}" ]]; then
    if [[ ! -d "${buildroot_unpack_dir}" ]]; then
        if [[ -f "${buildroot_archive}" ]] && ! tar -tf "${buildroot_archive}" >/dev/null 2>&1; then
            rm -f "${buildroot_archive}"
        fi

        curl \
            -fL \
            --retry 5 \
            --continue-at - \
            "https://buildroot.org/downloads/buildroot-${buildroot_version}.tar.xz" \
            -o "${buildroot_archive}"
        tar -C "${buildroot_download_dir}" -xf "${buildroot_archive}"
    fi

    buildroot_src="${buildroot_unpack_dir}"
fi

if [[ ! -d "${buildroot_src}" ]]; then
    echo "Buildroot source tree not found at ${buildroot_src}" >&2
    exit 1
fi

make -C "${buildroot_src}" \
    O="${build_dir}" \
    BR2_DL_DIR="${download_dir}" \
    BR2_EXTERNAL="${external_tree}" \
    autoemu_stm32f4_probe_defconfig >/dev/null

make -C "${buildroot_src}" \
    O="${build_dir}" \
    BR2_DL_DIR="${download_dir}" \
    BR2_EXTERNAL="${external_tree}" \
    olddefconfig >/dev/null

make -C "${buildroot_src}" \
    O="${build_dir}" \
    BR2_DL_DIR="${download_dir}" \
    BR2_EXTERNAL="${external_tree}" \
    -j"$(nproc)" >/dev/null

printf '%s\n' "${rootfs_image}"
