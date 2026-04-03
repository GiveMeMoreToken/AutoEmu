# AutoEmu

AutoEmu is a harness-first agent framework that fetches STM32 source data, reconstructs peripheral behavior, and emits QEMU-compatible virtual peripherals.

## CLI

The public CLI surface is intentionally small:

- `fetch-data`
- `build-qemu-peripheral`

`fetch-data` prepares the input bundle for one target MCU and peripheral.
`build-qemu-peripheral` consumes that bundle and generates the peripheral model, QEMU code, tests, and validation output.

Both commands run through the same public agent runtime. By default, that runtime uses the local harness backend and deterministic tools. Set `AUTOEMU_AGENT_BACKEND=claude` or `AUTOEMU_AGENT_BACKEND=openai` if you want the same workflow executed through an external LLM agent backend.

## Install

Requires Python 3.11+.

```bash
pip install -e ".[dev]"
```

Optional runtime configuration:

```bash
export AUTOEMU_AGENT_BACKEND=harness  # default: harness
export AUTOEMU_AGENT_MODEL=gpt-5.4    # only used for external agent backends
export AUTOEMU_AGENT_MAX_BUDGET_USD=5
```

## Build the Binary

Build the standalone CLI first:

```bash
source ~/miniconda3/etc/profile.d/conda.sh && conda activate agent && pyinstaller autoemu.spec --clean
```

This produces:

```bash
./dist/autoemu
```

External source trees follow the `build/*-src` layout:

- `build/qemu-src/qemu-9.2.4`
- `build/linux-src/linux-6.17.0-rc1`
- `build/buildroot-src/buildroot-2025.05`

## Workflow

### 1. Fetch target data

Fetch the reference manual, datasheet, SVD/header inputs, and relevant HAL/LL/RTOS driver files for one STM32 target:

```bash
source ~/miniconda3/etc/profile.d/conda.sh && conda activate agent && ./dist/autoemu fetch-data \
  --target-mcu STM32F407VG \
  --target-peripheral ETH \
  --output data/stm32
```

This writes a target-scoped bundle under `data/stm32/` and a manifest under `data/stm32/manifests/`.

### 2. Build the virtual peripheral

Build the QEMU peripheral directly from the fetched data:

```bash
source ~/miniconda3/etc/profile.d/conda.sh && conda activate agent && ./dist/autoemu build-qemu-peripheral \
  --target-mcu STM32F407VG \
  --target-peripheral ETH \
  --data-dir data/stm32 \
  --output-dir output/eth
```

This runs the full modeling pipeline:

1. register extraction
2. state-machine inference
3. interrupt-model inference
4. dependency-graph inference
5. QEMU/test generation
6. consistency validation

## Output

The build command writes:

- `*_registers.json`
- `*_state_machine.json`
- `*_interrupt_model.json`
- `*_dependencies.json`
- `*_peripheral.json`
- `stm32_<peripheral>.c`
- `stm32_<peripheral>.h`
- `meson.build`
- `qtest_stm32_<peripheral>.c`
- `test_stm32_<peripheral>.c`
- `*_validation.json`

## Validation Harnesses

### Bare-metal guest firmware

Use the guest probe harness when you need full firmware execution against the generated STM32 board model instead of MMIO/qtest-only validation:

```bash
source ~/miniconda3/etc/profile.d/conda.sh && conda activate agent && ./scripts/build_stm32_guest_firmware.sh
```

This builds:

```bash
build/guest-firmware/stm32f4_probe.elf
```

Run it under the in-repo QEMU board model:

```bash
source ~/miniconda3/etc/profile.d/conda.sh && conda activate agent && ./scripts/run_stm32_guest_firmware.sh
```

If you already have an STM32CubeF4 checkout and want to reuse it instead of letting the build script fetch a pinned copy into `build/third_party/`, set `STM32CUBEF4_ROOT`:

```bash
source ~/miniconda3/etc/profile.d/conda.sh && conda activate agent && \
  STM32CUBEF4_ROOT=/tmp/STM32CubeF4-min ./scripts/run_stm32_guest_firmware.sh
```

The guest probe firmware exercises:

- `HAL_DMA_Init()` / `HAL_DMA_Start_IT()` with real IRQ delivery
- `HAL_ETH_Init()` / `HAL_ETH_Start()`
- `HAL_PCD_Init()` / `HAL_PCD_Start()`
- the SUBGHZ MMIO/IRQ path on the test board mapping

### Linux probe harness

Use the Linux probe harness when you want a minimal Buildroot userspace, automatic driver probing, and an interactive serial shell on `ttyAMA0`:

```bash
source ~/miniconda3/etc/profile.d/conda.sh && conda activate agent && ./scripts/run_stm32_linux_probe_qemu.sh
```

This workflow:

1. Builds a Buildroot 2025.05 rootfs if needed.
2. Embeds that rootfs into the Linux `vmlinux` as initramfs.
3. Boots the in-repo `stm32f4-board` model under QEMU.
4. Probes DMA, ETH, USB, and Radio, then leaves a BusyBox shell available on `ttyAMA0`.

## Notes

- The fetcher is target-based. It does not keep hardcoded board/peripheral bundles or source-tree peripheral templates in the CLI workflow.
- The build step resolves fetched inputs from the manifest and target data directory.
- At least one register source (`.svd` or CMSIS header) and one driver source must exist for the build to proceed.
