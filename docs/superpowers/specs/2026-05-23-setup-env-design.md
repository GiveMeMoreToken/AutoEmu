# Design: AutoEmu Build Environment (setup-env.sh / build-env.sh / run.sh)

## Summary

Provide three top-level shell scripts that download, compile, and run a minimal
Linux VM from upstream source code. The scripts live alongside the AutoEmu
project but ignore the existing `qemu/` tree. All source code is fetched into
`env/` and all build artifacts are kept under `env/` so the workspace stays
clean.

## Goals

- One command to download Linux + QEMU + Buildroot sources.
- One command to compile everything, resumable after failure.
- One command to launch a minimal VM.
- Support four architectures with a sensible default.

## Non-Goals

- Packaging or distribution of binaries.
- Cross-compilation from non-Linux hosts.
- Integration with AutoEmu's Python pipeline or TUI.

## Source Versions

| Component | Version | Reason |
|-----------|---------|--------|
| Linux | 6.12.28 | Current LTS, long support horizon |
| QEMU | 9.2.0 | Stable, good aarch64/riscv64 support |
| Buildroot | 2024.02.10 | LTS release with mature QEMU board configs |

## Architecture Support

| `ARCH` value | QEMU target | Linux defconfig | Buildroot defconfig | QEMU machine | QEMU CPU | Console | Drive IF | Root dev | Cross compiler |
|--------------|-------------|-----------------|---------------------|--------------|----------|---------|----------|----------|----------------|
| `x86_64` | `x86_64-softmmu` | `x86_64_defconfig` | `qemu_x86_64_defconfig` | `pc` | `qemu64` | `ttyS0` | `virtio` | `/dev/vda` | none (native) |
| `aarch64` (default) | `aarch64-softmmu` | `defconfig` (ARCH=arm64) | `qemu_aarch64_virt_defconfig` | `virt` | `cortex-a72` | `ttyAMA0` | `virtio` | `/dev/vda` | `aarch64-linux-gnu-` |
| `riscv64` | `riscv64-softmmu` | `defconfig` (ARCH=riscv) | `qemu_riscv64_virt_defconfig` | `virt` | `rv64` | `ttyS0` | `virtio` | `/dev/vda` | `riscv64-linux-gnu-` |
| `mipsel` | `mipsel-softmmu` | `malta_defconfig` (set `CONFIG_CPU_LITTLE_ENDIAN=y`) | `qemu_mips32r2el_malta_defconfig` | `malta` | `24Kf` | `ttyS0` | `ide` | `/dev/hda` | `mipsel-linux-gnu-` |

## File Layout

```text
env/
├── src/
│   ├── qemu/          # QEMU 9.2.0 source
│   ├── linux/         # Linux 6.12.28 LTS source
│   └── buildroot/     # Buildroot 2024.02.10 LTS source
├── build/
│   ├── qemu/          # Out-of-tree QEMU build
│   ├── linux/         # Out-of-tree Linux build
│   └── buildroot/     # Buildroot build/output
├── .stamps/           # Stamp files for resumability
└── output/
    ├── qemu-system-<arch>   # Symlink to compiled QEMU binary
    ├── kernel               # Compiled kernel image (standardized name)
    └── rootfs.ext4          # Generated root filesystem
```

Top-level scripts:

- `setup-env.sh` — downloads, verifies checksums, and extracts sources.
- `build-env.sh` — configures and compiles QEMU, Linux, then Buildroot.
- `run.sh` — launches the VM with the compiled artifacts.

## Component Details

### `setup-env.sh`

**Input environment:** none (architecture-agnostic download)

**Steps:**
1. Create `env/src/`, `env/build/`, `env/.stamps/`, `env/output/`.
2. For each component (Linux, QEMU, Buildroot):
   - Skip if `env/.stamps/download-<component>` exists.
   - Download the tarball into `env/src/` if not already present.
   - Verify an embedded SHA256 checksum.
   - Extract if the source directory does not exist.
   - Write the stamp file.

**Idempotency:** Re-running the script is a no-op after stamps are written.

### `build-env.sh`

**Input environment:**
- `ARCH` — target architecture (default `aarch64`)
- `JOBS` — parallel make jobs (default `$(nproc)`)
- `CLEAN` — if `1`, remove all stamps before building (force rebuild)

**Steps:**
1. Validate `ARCH` is one of the four supported values.
2. If `CLEAN=1`, delete `env/.stamps/build-*`.
3. **QEMU build:**
   - Skip if `.stamps/build-qemu` exists.
   - Run `configure` from `env/src/qemu/` with `--target-list=<arch>-softmmu`, `--disable-werror`, `--disable-docs`, and prefix `env/build/qemu/install/`.
   - Run `make -j$JOBS && make install`.
   - Symlink `env/build/qemu/install/bin/qemu-system-<arch>` to `env/output/qemu-system-<arch>`.
   - Write `.stamps/build-qemu`.
4. **Linux build:**
   - Skip if `.stamps/build-linux` exists.
   - Run `make O=env/build/linux ARCH=<linux_arch> <defconfig>`.
   - Run `make O=env/build/linux ARCH=<linux_arch> -j$JOBS`.
   - Copy the produced kernel image to `env/output/kernel`. Exact source paths per architecture:
     - x86_64: `env/build/linux/arch/x86/boot/bzImage`
     - aarch64: `env/build/linux/arch/arm64/boot/Image`
     - riscv64: `env/build/linux/arch/riscv/boot/Image`
     - mipsel: `env/build/linux/vmlinux.bin` (or `vmlinux` if ELF boot)
   - Write `.stamps/build-linux`.
5. **Buildroot build:**
   - Skip if `.stamps/build-buildroot` exists.
   - Run `make O=env/build/buildroot -C env/src/buildroot <defconfig>`.
   - Run `make O=env/build/buildroot -C env/src/buildroot -j$JOBS`.
   - Copy `env/build/buildroot/images/rootfs.ext4` to `env/output/rootfs.ext4`.
   - Write `.stamps/build-buildroot`.

### `run.sh`

**Input environment:**
- `ARCH` — target architecture (default `aarch64`)
- `MEMORY` — VM RAM (default `512M`)
- `EXTRA_QEMU_OPTS` — additional QEMU arguments

**Steps:**
1. Validate `ARCH` is supported.
2. Verify that `env/output/qemu-system-<arch>`, `env/output/kernel`, and `env/output/rootfs.ext4` exist.
3. Launch QEMU with architecture-specific flags (values taken from the Architecture Support table):
   - `-M <machine>`
   - `-m $MEMORY`
   - `-cpu <cpu>`
   - `-kernel env/output/kernel`
   - `-drive file=env/output/rootfs.ext4,format=raw,if=<drive_if>`
   - `-append "root=<root_dev> rw console=<console> nographic"`
   - `-nographic`
   - Optional: `-serial mon:stdio` for monitor multiplexing
   - Optional: auto-detect and add `-enable-kvm` when host CPU matches target (x86_64/aarch64)
4. Document how to exit: Ctrl+A then X.

## Data Flow

```text
setup-env.sh
  ├── linux-6.12.28.tar.xz  ──► env/src/linux/  ──► .stamps/download-linux
  ├── qemu-9.2.0.tar.xz     ──► env/src/qemu/   ──► .stamps/download-qemu
  └── buildroot-2024.02.10.tar.xz ──► env/src/buildroot/ ──► .stamps/download-buildroot

build-env.sh
  ├── qemu:   env/src/qemu/   ──► env/build/qemu/   ──► .stamps/build-qemu   ──► env/output/qemu-system-<arch>
  ├── linux:  env/src/linux/  ──► env/build/linux/  ──► .stamps/build-linux  ──► env/output/kernel
  └── buildroot: env/src/buildroot/ ──► env/build/buildroot/ ──► .stamps/build-buildroot ──► env/output/rootfs.ext4

run.sh
  └── env/output/* ──► qemu-system-<arch> CLI ──► VM console
```

## Error Handling

- **Missing dependencies:** Each script checks for required tools (`wget` or `curl`, `tar`, `make`, `gcc`, `bison`, `flex`). For non-native architectures (`aarch64`, `riscv64`, `mipsel`), it also checks that the corresponding `CROSS_COMPILE` toolchain (e.g. `aarch64-linux-gnu-gcc`) is present in `PATH`.
- **Partial build:** `build-env.sh` uses stamp files to resume from the last successful step.
- **Force rebuild:** Setting `CLEAN=1` removes build stamps so the next run rebuilds everything. This is required after changing source versions in the script, since stamps do not auto-invalidate on version bumps.
- **Architecture mismatch:** `run.sh` checks that output artifacts match the requested `ARCH` and exits with a clear hint if not.

## Testing Strategy

1. **Syntax check:** `bash -n setup-env.sh build-env.sh run.sh`
2. **Idempotency:** Run `setup-env.sh` twice; second run should be instantaneous.
3. **Resume:** Interrupt `build-env.sh` mid-build and re-run; it should skip completed steps.
4. **Golden path:** On a clean environment, run `setup-env.sh && build-env.sh && run.sh` and verify the VM boots to a shell prompt.

## Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| Buildroot/Linux compilation is slow | Stamp files make re-runs fast; `JOBS` defaults to all cores |
| Host missing build dependencies | Scripts check prerequisites before starting |
| QEMU configure fails on exotic hosts | `--disable-werror` and `--disable-docs` reduce host sensitivity |
| Architecture-specific QEMU CLI drift | Each `ARCH` has its own explicit flag block in `run.sh` |
| Buildroot userspace vs standalone Linux kernel version mismatch | Buildroot defconfigs are generally kernel-version-agnostic for userspace; known risk if drivers drift |
| Terminal state corrupted after `-nographic` QEMU exit | `run.sh` uses `-serial mon:stdio` and documents `Ctrl+A X`; optionally runs `stty sane` on exit |
