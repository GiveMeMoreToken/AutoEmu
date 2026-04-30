# AutoEmu

Automated QEMU peripheral model generator for microcontrollers.

AutoEmu takes an MCU name and peripheral identifier, fetches the relevant documentation and driver source, and produces production-ready QEMU v9.2.4-compatible C code — header, implementation, meson snippet, and QTest harness — along with a full validation report.

## How it works

The pipeline has six phases:

1. **Fetch** — Downloads SVD files, CMSIS headers, HAL/LL drivers, and datasheets from vendor repositories and the web.
2. **Extract** — Parses SVD XML and CMSIS C headers into a unified register block model.
3. **Analyze** — Regex-driven analysis of HAL/LL driver source to identify register access patterns, ISR handlers, init sequences, and DMA configurations.
4. **Infer** — Derives state machines, interrupt models (flag/enable/clear mappings, NVIC IRQ numbers), and cross-peripheral dependency graphs (RCC, DMA, GPIO, timers).
5. **Generate** — Emits QEMU C code (`MemoryRegionOps`, IRQ wiring, `VMState`, reset handler), a standalone C test harness, and an AFL/libFuzzer fuzz harness.
6. **Validate** — Runs structural, behavioral, compilation (`gcc -fsyntax-only`), security, and driver-replay validators, producing a `validation_report.json`.

The pipeline is **harness-first**: the deterministic parsing and inference path is always primary. Claude, Codex, and OpenAI backends are optional fallbacks for steps where heuristics are insufficient.

## Supported platforms

| Platform | Register source | Driver source |
|----------|----------------|---------------|
| STM32 (ARM Cortex-M) | CMSIS-SVD, CMSIS headers | STM32 HAL / LL drivers |
| MIPS | PDF register tables, Device Tree | Linux kernel drivers (`readl`/`writel`) |

## Installation

```bash
pip install -e .
```

For compilation validation, `gcc` or `clang` must be on `PATH`. QEMU v9.2.4 headers are used for include-path validation when present.

To build a standalone binary:

```bash
pip install -e ".[build]"
pyinstaller autoemu.spec --clean
```

## Usage

### Interactive TUI

```bash
autoemu
```

The terminal UI presents an input form for the target MCU and peripheral. Enter the target and click **Analyze** to run the full pipeline. Progress, phase status, and log output are displayed in real time. After each run the full log is saved to `autoemu_<timestamp>.log`.

### Python API

```python
from autoemu.pipeline import run_model_pipeline

result = run_model_pipeline(
    peripheral_name="USART1",
    output_dir="output",
    svd_path="path/to/device.svd",
    header_path="path/to/stm32f4xx.h",
    driver_paths=["path/to/stm32f4xx_hal_uart.c"],
    mcu_family="STM32F4",
)
```

To let AutoEmu fetch all inputs automatically:

```python
from autoemu.pipeline import run_target_model_pipeline

result = run_target_model_pipeline(
    target_mcu="STM32F407VG",
    target_peripheral="ETH",
    data_dir="data/stm32",
    output_dir="output",
)
```

## Configuration

Create `.autoemu.toml` in your working directory:

```toml
[agent]
backend = "harness"           # "harness" (default), "claude", "codex", or "openai"
model   = ""                  # LLM model override (optional)
max_budget_usd = 5.0          # Max spend per pipeline run

# Anthropic / Claude
anthropic_api_key  = ""
anthropic_base_url = ""

# OpenAI
openai_api_key  = ""
openai_base_url = ""
```

Environment variables override the config file:

| Variable | Purpose |
|----------|---------|
| `AUTOEMU_AGENT_BACKEND` | `harness`, `claude`, `codex`, or `openai` |
| `ANTHROPIC_API_KEY` | Anthropic / Claude-compatible API key |
| `ANTHROPIC_BASE_URL` | Anthropic / Claude-compatible endpoint |
| `OPENAI_API_KEY` | OpenAI-compatible API key |
| `OPENAI_BASE_URL` | OpenAI-compatible endpoint |

Backend selection:

| Backend | SDK / mode |
|---------|------------|
| `harness` | Local deterministic pipeline only |
| `claude` | `claude-agent-sdk` with Anthropic-style API key and base URL |
| `codex` | `codex-app-server-sdk` |
| `openai` | `openai-agents` with OpenAI-style API key and base URL |

See `.autoemu.toml.example` for an annotated template.

## Output

Each run writes to the specified output directory:

```
output/
├── stm32_eth.h                  # Device type, register offsets, bit-field macros
├── stm32_eth.c                  # MemoryRegionOps, IRQ wiring, VMState, reset handler
├── meson.build                  # meson build snippet
├── qtest_stm32_eth.c            # QEMU QTest harness
├── test_stm32_eth.c             # Standalone C test harness
├── stm32_eth_peripheral.json    # Full peripheral model (Pydantic-serialized)
└── validation_report.json       # Per-validator results and accuracy metrics
```

## Project structure

```
src/autoemu/
├── main.py                      # CLI entry point (Click → TUI)
├── pipeline.py                  # Top-level pipeline orchestrator
├── models/                      # Pydantic v2 data models
│   ├── peripheral.py            #   Peripheral, PeripheralType, ClockConfig
│   ├── register.py              #   Register, BitField, AccessType
│   ├── state_machine.py         #   StateMachine, State, Transition
│   ├── interrupt.py             #   InterruptModel, InterruptLine, FlagBehavior
│   └── dependency.py            #   DependencyGraph, DependencyEdge
├── parsers/                     # Input parsers (SVD, CMSIS headers, HAL/LL drivers)
├── fetchers/                    # Web/GitHub data fetching with cache
├── inference/                   # State machine, interrupt, and dependency inference
├── generators/                  # QEMU C, test harness, bundle, and fuzz generators
├── validators/                  # Register, behavior, compile, security, replay validators
├── platforms/                   # Platform plugins (STM32, MIPS)
├── agent/                       # Agent orchestration and LLM backends
│   ├── backend.py               #   AgentBackend ABC, ToolSpec, AgentEvent
│   ├── backends/claude_backend.py
│   ├── backends/codex_backend.py
│   ├── backends/openai_backend.py
│   ├── runtime.py               #   Config loading, unified run_pipeline()
│   ├── orchestrator.py          #   6-phase prompt-driven orchestrator
│   ├── prompts.py               #   System and phase-specific prompts
│   └── tools.py                 #   Backend-agnostic tool registry (15+ tools)
└── tui/                         # Textual terminal UI
    ├── app.py
    └── widgets.py
```

## Running tests

```bash
pytest                          # Unit tests
pytest -m integration           # End-to-end pipeline tests (requires network)
pytest tests/test_models.py -v  # Single module
pytest -k "test_w1c" -v         # Pattern filter
```

## Dependencies

| Package | Role |
|---------|------|
| `textual` | Terminal UI |
| `click` | CLI |
| `pydantic >= 2` | Data models |
| `lxml` | SVD / HTML parsing |
| `jinja2` | Code generation templates |
| `rich` | Terminal formatting |
| `pyyaml` | Config files |
| `claude-agent-sdk` | Claude LLM backend |
| `codex-app-server-sdk` | Codex backend |
| `openai-agents` | OpenAI Agents backend |
