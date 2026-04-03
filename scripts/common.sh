#!/usr/bin/env bash

autoemu_repo_root() {
    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    cd "${script_dir}/.." && pwd
}

autoemu_build_root() {
    local repo_root
    repo_root="${1:-$(autoemu_repo_root)}"
    printf '%s\n' "${repo_root}/build"
}

autoemu_qemu_src_root() {
    local repo_root build_root
    repo_root="${1:-$(autoemu_repo_root)}"
    build_root="$(autoemu_build_root "${repo_root}")"
    printf '%s\n' "${QEMU_SOURCE_ROOT:-${build_root}/qemu-src}"
}

autoemu_qemu_src_dir() {
    local repo_root qemu_src_root
    repo_root="${1:-$(autoemu_repo_root)}"
    qemu_src_root="$(autoemu_qemu_src_root "${repo_root}")"
    printf '%s\n' "${QEMU_SRC:-${qemu_src_root}/qemu-9.2.4}"
}

require_commands() {
    local missing=()
    local tool
    for tool in "$@"; do
        if ! command -v "${tool}" >/dev/null 2>&1; then
            missing+=("${tool}")
        fi
    done

    if [[ ${#missing[@]} -gt 0 ]]; then
        printf 'Missing required command(s): %s\n' "${missing[*]}" >&2
        return 1
    fi
}

resolve_qemu_build_dir() {
    local repo_root qemu_src_dir
    repo_root="${1:-$(autoemu_repo_root)}"
    qemu_src_dir="$(autoemu_qemu_src_dir "${repo_root}")"

    if [[ -n "${QEMU_BUILD_DIR:-}" ]]; then
        printf '%s\n' "${QEMU_BUILD_DIR}"
        return
    fi
    if [[ -n "${QEMU_BINARY:-}" ]]; then
        dirname "${QEMU_BINARY}"
        return
    fi

    printf '%s\n' "${qemu_src_dir}/build"
}

resolve_qemu_binary() {
    local repo_root qemu_build_dir
    repo_root="${1:-$(autoemu_repo_root)}"
    qemu_build_dir="$(resolve_qemu_build_dir "${repo_root}")"

    if [[ -n "${QEMU_BINARY:-}" ]]; then
        printf '%s\n' "${QEMU_BINARY}"
        return
    fi

    printf '%s\n' "${qemu_build_dir}/qemu-system-arm"
}

ensure_qemu_binary() {
    local repo_root qemu_build_dir qemu_binary
    repo_root="${1:-$(autoemu_repo_root)}"
    qemu_build_dir="$(resolve_qemu_build_dir "${repo_root}")"
    qemu_binary="$(resolve_qemu_binary "${repo_root}")"

    require_commands ninja

    if [[ -x "${qemu_binary}" && -z "${QEMU_BUILD_DIR:-}" ]]; then
        printf '%s\n' "${qemu_binary}"
        return
    fi

    if [[ ! -d "${qemu_build_dir}" ]]; then
        printf 'QEMU build directory not found at %s\n' "${qemu_build_dir}" >&2
        printf 'Set QEMU_BUILD_DIR or QEMU_BINARY if your build lives elsewhere.\n' >&2
        return 1
    fi

    echo "Building qemu-system-arm in ${qemu_build_dir}" >&2
    ninja -C "${qemu_build_dir}" qemu-system-arm >&2

    if [[ ! -x "${qemu_binary}" ]]; then
        printf 'qemu-system-arm was not produced at %s\n' "${qemu_binary}" >&2
        return 1
    fi

    printf '%s\n' "${qemu_binary}"
}
