# Build Configurations

This directory contains minimal configuration fragments to ensure each source code (Linux, QEMU, Buildroot) builds non-interactively and produces bootable artifacts for the supported QEMU architectures.

## Linux Kernel Fragments

Files: `linux-<arch>.fragment`

Applied after `make defconfig` in `build-env.sh`. Ensures essential drivers are enabled for QEMU boot:

- **Block device**: virtio-blk (PCI/MMI-O) or IDE (PIIX4)
- **Filesystem**: EXT4
- **Serial console**: 8250 UART or AMBA PL011
- **PCI bus support**

Without these, the kernel may compile but fail to mount the root filesystem or produce console output inside QEMU.

## QEMU / Buildroot

QEMU configuration is passed via `configure` flags in `build-env.sh` (see `--target-list`, `--disable-werror`, `--disable-docs`, `--disable-gtk`, `--disable-sdl`).

Buildroot uses upstream defconfigs (`qemu_<arch>_defconfig`) which are already non-interactive.

## Non-Interactive Build Safeguards

`build-env.sh` also sets:

- `export TERM=dumb` — prevents curses menus from opening
- `< /dev/null` on all `make` invocations — prevents stdin-based prompts
- `make olddefconfig` after fragment injection — resolves new config options with defaults instead of prompting
