# AutoEmu Agent Guide

## Project Purpose

AutoEmu is a framework for generating QEMU-compatible peripheral models from
target MCU/board and peripheral inputs. The deterministic local pipeline is the
primary implementation; agent backends are optional helpers for source collection
and model enrichment.

## Commands

```bash
pip install -e ".[dev]"
pytest
pytest -m integration
python -m compileall -q src tests
pyinstaller autoemu.spec --clean
```

The CLI entry point is `autoemu`, which launches the Textual TUI from
`src/autoemu/main.py`.

## Configuration

Create `.autoemu.toml` in the working directory, or use environment variables.
Valid agent backend names are:

- `claude-sdk`
- `codex-sdk`
- `anthropic-api`
- `openai-api`

Use `backend = "codex-sdk"` or `AUTOEMU_AGENT_BACKEND=codex-sdk` for the Codex
SDK backend. Do not use old aliases such as `claude`, `codex`, or `openai`.

## Architecture Boundaries

- `src/autoemu/agent/`: runtime, orchestrator, tool registry, backend adapters.
- `src/autoemu/fetchers/`: web discovery, download, cache, input resolution.
- `src/autoemu/parsers/`: SVD/header/driver parsing and register extraction.
- `src/autoemu/inference/`: state machine, interrupt, and dependency inference.
- `src/autoemu/generators/`: QEMU C/H, Meson, and QTest output.
- `src/autoemu/validators/`: register, behavior, compile, replay, and security checks.
- `src/autoemu/platforms/`: STM32, MIPS, and generic platform plugins.

Keep inference and data models generic. Platform plugins may describe input
formats and QEMU target conventions, but shared parsers, fetchers, inference,
and generators must not grow board-specific or GPU-family-specific branches.

## Source Acquisition Rules

- Fetch SVD/XML, register-map headers, driver sources, documentation, and
  DTS/DTSI device-tree files when available.
- For MMIO base address and region size, prefer explicit evidence from SVD
  `baseAddress` fields or DTS/DTSI `reg = <...>` entries.
- Header-only register maps often contain register offsets only. Do not treat
  offset-only headers as proof of a peripheral base address.
- Device-tree files should be treated as documentation inputs and consumed by
  generic MMIO inference.
- If fetched local data lacks `.dts` or `.dtsi` files under `data/<target>/docs/`,
  rerun fetch before judging base-address inference.

## Agent Rules

- Do not invent URLs, file contents, register maps, base addresses, or fetched
  artifacts. Summaries must match files actually written or reused.
- Hardware identity confirmation is metadata only. It may record names,
  architecture family, hardware generation, and evidence, but it must not add
  hardware-specific logic to the codebase.
- Agent backend failures must fall back to the deterministic local output
  whenever local inputs are sufficient.
- Generated QEMU code targets latest upstream QEMU. Use
  `device_class_set_legacy_reset()`, `OBJECT_DECLARE_SIMPLE_TYPE`,
  `MemoryRegionOps`, Meson, QTest, and bare VMSTATE field names.
- Include `hw/sysbus.h` for `SysBusDevice` access (QEMU 9.2 compatible).
- Include `hw/qdev-properties.h` for `DeviceClass` access (QEMU 9.2 compatible).

## 2026-05-20 Handoff Notes

- Logs `autoemu_20260520_203850.log` and `autoemu_20260520_223725.log` showed
  Hikey960/GPU runs with `base_address=0` or empty registers after agent
  failures.
- The useful generic fixes identified from those logs were:
  - macro-only register extraction for Linux-style register maps
  - related direct-prefix and indexed-family register inclusion
  - MMIO base/size inference from DTS/DTSI `reg` entries
  - fetch selection that preserves lower-scored device-tree documentation
  - timeout-tolerant fetch discovery that returns completed results
  - explicit fetch prompts for DTS/DTSI/device-tree sources
- Keep these fixes generic. Do not add special cases for Hikey960, Mali,
  Bifrost, Valhall, Panfrost, or any other specific hardware.

## Verification Expectations

- Run `pytest` before claiming code changes are complete.
- For MMIO inference changes, include a focused regression where a synthetic
  DTS/DTSI `reg = <...>` entry drives nonzero `base_address` and `address_size`.
- For fetch changes, include tests that ensure DTS/DTSI docs are requested,
  resolved from `docs/`, and not dropped by candidate selection.
