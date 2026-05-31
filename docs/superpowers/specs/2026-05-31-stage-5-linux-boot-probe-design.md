# Stage 5 Linux Boot Probe Design

## Goal

Stage 5 must validate generated AutoEmu peripherals by booting a real guest
Linux kernel under QEMU and checking the guest console logs for driver probe
results. A compile-only QEMU object rebuild is not sufficient.

## Current Behavior

`autoemu_20260531_110042.log` shows phase 5 copying `hikey960_gpu.c` and
`hikey960_gpu.h` into `env/src/qemu-9.2.0`, then running a targeted `ninja`
rebuild in `env/build/qemu-aarch64`. The phase reports only the rebuild result.
It does not launch `qemu-system-aarch64`, does not boot Linux, and does not
verify the real driver probe path.

A manual QEMU run exposes the missing coverage:

```text
qemu-system-aarch64: -device hikey960-gpu: 'hikey960-gpu' is not a valid device model name
```

The generated object can compile while the runnable QEMU binary still cannot
instantiate the device. Stage 5 must catch this class of integration failure.

## Design

`src/autoemu/validators/qemu_probe_validator.py` remains the phase-5 entry
point. `run_qemu_probe()` will keep the existing soft-fail contract, but its
successful path becomes:

1. Resolve the QEMU build directory.
2. Resolve the matching QEMU source tree.
3. Apply generated C/H files into QEMU.
4. Apply the generated machine patch when present.
5. Enable the generated QEMU `CONFIG_*` symbol in the target build config.
6. Reconfigure or refresh the build graph as needed.
7. Rebuild the runnable `qemu-system-<arch>` binary.
8. Boot a guest Linux kernel with the generated device present.
9. Capture serial console output.
10. Verify driver probe success or failure from console logs.

The validator will prefer the existing local environment layout:

- QEMU build: `env/build/qemu-<arch>`
- QEMU binary: `env/build/qemu-<arch>/qemu-system-<arch>`, falling back to
  `env/output/qemu-system-<arch>` when present.
- Kernel: `env/output/kernel-<arch>`
- Rootfs: `env/output/rootfs-<arch>.ext4`

Environment overrides allow custom boot assets without changing code:

- `AUTOEMU_QEMU_BUILD_DIR`
- `AUTOEMU_QEMU_BIN`
- `AUTOEMU_LINUX_KERNEL`
- `AUTOEMU_LINUX_ROOTFS`
- `AUTOEMU_QEMU_MACHINE`
- `AUTOEMU_QEMU_CPU`
- `AUTOEMU_QEMU_MEMORY`
- `AUTOEMU_QEMU_EXTRA_ARGS`
- `AUTOEMU_PROBE_TIMEOUT`

## Architecture

The implementation will keep compile, boot, and log analysis as separate helper
functions inside `qemu_probe_validator.py`.

`_resolve_boot_assets()` maps the build directory and detected architecture to
the QEMU binary, kernel, rootfs, machine, CPU, console, root device, and drive
interface. It returns a structured error when required assets are missing.

`_enable_generated_config()` extracts the `CONFIG_*` symbol from generated
`meson.build`, checks whether it is already enabled in
`<arch>-softmmu-config-devices.mak`, and, when possible, appends the symbol to
the QEMU source `configs/devices/<arch>-softmmu/default.mak` before refreshing
the build graph. If automatic enablement is not possible, stage 5 soft-fails
with a message explaining which symbol and file need attention.

`_run_guest_linux_probe()` launches QEMU with `-nographic`, the resolved kernel
and rootfs, and a bounded timeout. It captures stdout and stderr, terminates the
guest after the timeout or after a decisive probe signal, and returns the full
tail of the serial log.

`_analyze_probe_log()` classifies the boot log using generated-device and
driver evidence:

- Success when the log contains target driver/probe tokens and no matching
  failure token.
- Failure when the log contains probe failure patterns such as
  `probe failed`, `error`, `failed to probe`, `deferred probe timeout`, or
  kernel oops/panic text near a target token.
- Inconclusive when Linux booted but no target driver/probe token appears.
- Boot failure when no Linux boot marker appears before timeout.

For the HiKey960 GPU case, expected target tokens include the generated QEMU
device type `hikey960-gpu`, the peripheral name `gpu`, and fetched driver stems
such as `panfrost`.

## Data Flow

`AutoEmuAgentRuntime._do_test()` continues to call `run_qemu_probe()`.

`run_qemu_probe()` returns the existing keys and adds:

- `qemu_cmd`: exact command used for the Linux boot probe.
- `boot_log`: tail of guest/QEMU output.
- `probe_lines`: log lines that matched target probe tokens.
- `probe_status`: one of `matched`, `failed`, `inconclusive`,
  `boot_failed`, or `skipped`.
- `boot_assets`: resolved paths for QEMU, kernel, and rootfs.

Pipeline success remains controlled by phases 1-4. Phase 5 remains soft-fail so
missing boot assets, failed rebuilds, failed QEMU startup, guest boot timeout,
or driver probe failure do not mark the whole pipeline failed. They are reported
in `result.probe_result` and progress logs.

## Error Handling

Every external dependency failure must be explicit:

- Missing QEMU build directory: skipped.
- Missing `build.ninja`: skipped.
- Missing generated C/H files: skipped.
- Missing `ninja`: skipped.
- Generated source not present in QEMU build graph: skipped with the generated
  source names.
- Failed generated source rebuild: soft-fail with the build log tail.
- Missing QEMU binary, kernel, or rootfs: skipped with exact missing paths.
- QEMU process timeout: soft-fail with the console tail.
- Linux panic or driver probe failure: soft-fail with matched log lines.
- Linux booted but no driver probe log found: soft-fail as inconclusive.

The validator must never invent a successful probe. Success requires observed
guest output.

## Testing

Focused regression tests will cover:

- `run_qemu_probe()` runs a QEMU boot command after a successful QEMU rebuild.
- Missing QEMU boot assets produce a soft skip with exact paths.
- A QEMU startup error such as an invalid device model produces `probe_status`
  `boot_failed` and `success == False`.
- A guest log containing a driver success/probe token yields success and
  records `probe_lines`.
- A guest log containing a probe failure token near the target driver yields a
  soft-fail.
- The old phase-5 soft-fail behavior remains intact when the QEMU build
  environment is missing or `ninja` fails.

Full test verification for code changes remains:

```bash
pytest
python -m compileall -q src tests
```
