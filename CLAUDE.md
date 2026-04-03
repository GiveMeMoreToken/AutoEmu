# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Test Commands

```bash
source ~/miniconda3/etc/profile.d/conda.sh && conda activate agent
pip install -e ".[dev]"         # Install in editable mode with dev deps
pytest                          # Run all tests
pytest tests/test_models.py -v  # Run a specific test file
pytest -k "test_w1c" -v         # Run tests matching a pattern
pyinstaller autoemu.spec --clean  # Build the CLI binary at dist/autoemu
```

Tests use `pytest-asyncio` with `asyncio_mode = "auto"` — async test functions are detected automatically without explicit markers.

## CLI

The `autoemu` CLI (Click-based, entry point in `src/autoemu/main.py`) has these commands:

- `autoemu fetch-data --target-mcu MCU --target-peripheral PERIPH` — Fetch the input bundle for one target.
- `autoemu build-qemu-peripheral --target-mcu MCU --target-peripheral PERIPH` — Run the full modeling pipeline from fetched data and emit QEMU-ready artifacts.

Both commands flow through `src/autoemu/agent/runtime.py`. The default backend is the local harness runtime. Set `AUTOEMU_AGENT_BACKEND=claude` or `AUTOEMU_AGENT_BACKEND=openai` to route the same workflow through an external agent backend.

The binary-first workflow is:
1. Build `dist/autoemu` with PyInstaller.
2. Run `fetch-data`.
3. Run `build-qemu-peripheral`.

## Architecture

**Public flow:** CLI → agent runtime (`runtime.py`) → harness backend or LLM orchestrator → deterministic tools/pipeline → QEMU generator → validators

### Models (`src/autoemu/models/`)
Pydantic v2 models forming the core data layer. `Peripheral` is the top-level model containing a `RegisterBlock` (with `Register`/`BitField`), optional `StateMachine`, `InterruptModel`, and `DependencyGraph`. Register access semantics (W1C, RC_W1, W1S, etc.) are enforced in `Register.apply_write()`/`apply_read()`.

### Parsers (`src/autoemu/parsers/`)
Three input parsers, all returning model objects:
- `svd_parser` — lxml-based CMSIS-SVD XML parser → `dict[str, RegisterBlock]`
- `header_parser` — Regex-based CMSIS C header parser → base addresses, struct definitions, bit macros
- `driver_parser` — HAL/LL driver analyzer → `DriverAnalysis` (register accesses, ISR patterns, init sequences, DMA configs)

### Agent (`src/autoemu/agent/`)
Abstract backend pattern with `AgentBackend` base class (`backend.py`). Two implementations in `backends/`: `ClaudeBackend` (claude-agent-sdk) and `OpenAIAgentsBackend` (openai-agents). Factory: `create_backend("claude"|"openai")`.

`tools.py` defines the backend-agnostic `ToolSpec` registry. `runtime.py` is the harness-first entrypoint used by the CLI. `orchestrator.py` runs the 6-phase prompt-driven pipeline when an external LLM backend is selected. Events stream as `AgentEvent` (text, tool_call, complete, error).

The STM32 fetch flow also runs through the agent backend. It uses the same tool registry plus repository-local constraints from `AGENTS.md`.

### Generators (`src/autoemu/generators/`)
- `qemu_generator` — Produces `.h`, `.c`, `meson.build`, QTest harness, and model JSON per peripheral
- `test_generator` — Produces standalone C test harnesses for reset values, W1C behavior, RO protection, field isolation

### Validators (`src/autoemu/validators/`)
- `register_validator` — Structural checks (overlapping offsets/fields, duplicate names, access conflicts)
- `behavior_validator` — Cross-validates model against driver analysis (missing registers, ISR mismatches)
- `driver_replay` — Replays register write/read sequences against the model

## Probe Harnesses

- `scripts/run_stm32_guest_firmware.sh` builds and runs the bare-metal probe firmware.
- `scripts/run_stm32_linux_probe_qemu.sh` builds the Buildroot rootfs and Linux kernel as needed, then boots to a BusyBox shell on `ttyAMA0`.

## QEMU Code Generation Constraints

All generated C code targets **QEMU v9.2.4** specifically. Key API choices:
- `device_class_set_legacy_reset()` — not the deprecated `dc->reset`
- `OBJECT_DECLARE_SIMPLE_TYPE` for type declarations
- `MemoryRegionOps` for register read/write handlers
- Bare field names in `VMSTATE` macros (not `s->field`)
- Must include `hw/qdev-properties.h` for DeviceClass

## Key Conventions

- Python 3.11+ required (uses modern type syntax)
- All agent/tool operations are async
- Pydantic v2 API throughout (`model_validate()`, `model_dump()`, not v1 `.dict()`/`.parse_obj()`)
- Tool handlers return `dict[str, Any]` with a consistent shape (success/error keys)
- Generated output goes to `output/` by default. External source trees live under `build/*-src/`.
