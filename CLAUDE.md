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

Running `autoemu` launches an interactive TUI (Textual-based). The user provides only a **target board/MCU** and a **target peripheral** — everything else is auto-inferred.

The unified pipeline runs: **detect platform → fetch data → build QEMU model → validate**.

The entry point is `src/autoemu/main.py` which calls the TUI at `src/autoemu/tui/app.py`. The pipeline logic is in `src/autoemu/agent/runtime.py` (`AutoEmuAgentRuntime.run_pipeline()`).

Set `AUTOEMU_AGENT_BACKEND=claude` or `AUTOEMU_AGENT_BACKEND=openai` to route the workflow through an external LLM backend instead of the local harness.

## Architecture

**Public flow:** TUI → `run_pipeline()` in `runtime.py` → platform detection → fetcher → modeling pipeline → compile validation → results in TUI

### Models (`src/autoemu/models/`)
Pydantic v2 models forming the core data layer. `Peripheral` is the top-level model containing a `RegisterBlock` (with `Register`/`BitField`), optional `StateMachine`, `InterruptModel`, and `DependencyGraph`. Register access semantics (W1C, RC_W1, W1S, etc.) are enforced in `Register.apply_write()`/`apply_read()`.

### Parsers (`src/autoemu/parsers/`)
Three input parsers, all returning model objects:
- `svd_parser` — lxml-based CMSIS-SVD XML parser → `dict[str, RegisterBlock]`
- `header_parser` — Regex-based CMSIS C header parser → base addresses, struct definitions, bit macros
- `driver_parser` — HAL/LL driver analyzer → `DriverAnalysis` (register accesses, ISR patterns, init sequences, DMA configs)

### Agent (`src/autoemu/agent/`)
Abstract backend pattern with `AgentBackend` base class (`backend.py`). Two implementations in `backends/`: `ClaudeBackend` (claude-agent-sdk) and `OpenAIAgentsBackend` (openai-agents). Factory: `create_backend("claude"|"openai")`.

`tools.py` defines the backend-agnostic `ToolSpec` registry. `runtime.py` contains the unified `AutoEmuAgentRuntime.run_pipeline()` entry point. `orchestrator.py` runs the 6-phase prompt-driven pipeline when an external LLM backend is selected.

### Platforms (`src/autoemu/platforms/`)
Platform abstraction with registry (`get_platform()`, `detect_platform()`). Three plugins:
- `stm32` — STM32 family MCUs
- `mips` — MIPS SoCs (PDF, device tree, kernel driver parsers)
- `generic` — Any other MCU via web search

### Fetchers (`src/autoemu/fetchers/`)
- `stm32.py` — STM32 data fetcher with DuckDuckGo search + GitHub API + retry/backoff
- `generic.py` — Generic fetcher for any MCU using parallel web search and scoring
- `base.py` — `BaseFetcher` ABC with shared download/cache/SHA256 logic

### TUI (`src/autoemu/tui/`)
Textual-based interactive terminal UI. Single screen with MCU/peripheral inputs, pipeline phase indicators, and a scrolling log panel.

### Generators (`src/autoemu/generators/`)
- `qemu_generator` — Produces `.h`, `.c`, `meson.build`, QTest harness, and model JSON per peripheral
- `test_generator` — Produces standalone C test harnesses for reset values, W1C behavior, RO protection, field isolation
- `fuzz_generator` — Produces AFL/libFuzzer harnesses targeting MemoryRegionOps

### Validators (`src/autoemu/validators/`)
- `register_validator` — Structural checks (overlapping offsets/fields, duplicate names, access conflicts)
- `behavior_validator` — Cross-validates model against driver analysis (missing registers, ISR mismatches)
- `compile_validator` — `-fsyntax-only` compilation check and `meson.build` validation
- `security_validator` — DMA boundary, privilege escalation, interrupt safety checks
- `driver_replay` — Replays register write/read sequences against the model

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
- Shared utilities in `modeling_utils.py`: `snake_case()`, `upper_case()`, `normalize_name()`
