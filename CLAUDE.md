# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Test Commands

```bash
pip install -e ".[dev]"        # Install in editable mode with dev deps
pytest                          # Run all tests
pytest tests/test_models.py -v  # Run a specific test file
pytest -k "test_w1c" -v         # Run tests matching a pattern
```

Tests use `pytest-asyncio` with `asyncio_mode = "auto"` — async test functions are detected automatically without explicit markers.

## CLI

The `autoemu` CLI (Click-based, entry point in `src/autoemu/main.py`) has these commands:

- `autoemu model PERIPHERAL` — LLM-driven 6-phase pipeline (extract → analyze → infer → connect → generate → validate)
- `autoemu builtin PERIPHERAL` — Generate from built-in templates (DMA1, DMA2, ETH, USB_OTG_FS, USB_OTG_HS, SUBGHZ), no LLM required
- `autoemu parse-svd FILE` — Parse an SVD file and display register maps
- `autoemu analyze FILE` — Analyze HAL/LL driver source for register access patterns
- `autoemu validate FILE` — Validate a peripheral model JSON
- `autoemu query PROMPT` — Free-form LLM query
- `autoemu batch` — Batch-model multiple peripherals

All LLM commands accept `-b/--backend claude|openai` to select the agent backend.

## Architecture

**Pipeline flow:** SVD/headers/drivers → Parsers → Pydantic models → Agent (6 phases) → QEMU C code generator → Validators

### Models (`src/autoemu/models/`)
Pydantic v2 models forming the core data layer. `Peripheral` is the top-level model containing a `RegisterBlock` (with `Register`/`BitField`), optional `StateMachine`, `InterruptModel`, and `DependencyGraph`. Register access semantics (W1C, RC_W1, W1S, etc.) are enforced in `Register.apply_write()`/`apply_read()`.

### Parsers (`src/autoemu/parsers/`)
Three input parsers, all returning model objects:
- `svd_parser` — lxml-based CMSIS-SVD XML parser → `dict[str, RegisterBlock]`
- `header_parser` — Regex-based CMSIS C header parser → base addresses, struct definitions, bit macros
- `driver_parser` — HAL/LL driver analyzer → `DriverAnalysis` (register accesses, ISR patterns, init sequences, DMA configs)

### Agent (`src/autoemu/agent/`)
Abstract backend pattern with `AgentBackend` base class (`backend.py`). Two implementations in `backends/`: `ClaudeBackend` (claude-agent-sdk) and `OpenAIAgentsBackend` (openai-agents). Factory: `create_backend("claude"|"openai")`.

`tools.py` defines 16 `ToolSpec` objects (backend-agnostic tool definitions with async handlers). The `orchestrator.py` runs the 6-phase pipeline, each phase sending a phase-specific prompt (`prompts.py`) with the full tool set to the agent backend. Events stream as `AgentEvent` (text, tool_call, complete, error).

### Generators (`src/autoemu/generators/`)
- `qemu_generator` — Produces `.h`, `.c`, `meson.build`, QTest harness, and model JSON per peripheral
- `test_generator` — Produces standalone C test harnesses for reset values, W1C behavior, RO protection, field isolation

### Built-in Peripherals (`src/autoemu/peripherals/`)
Pre-built `Peripheral` constructors for DMA, Ethernet, USB OTG, and Sub-GHz radio. Each returns a fully populated model without needing LLM inference.

### Validators (`src/autoemu/validators/`)
- `register_validator` — Structural checks (overlapping offsets/fields, duplicate names, access conflicts)
- `behavior_validator` — Cross-validates model against driver analysis (missing registers, ISR mismatches)
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
- Generated output goes to `output/` by default (git-ignored along with `qemu-9.2.4/`)
