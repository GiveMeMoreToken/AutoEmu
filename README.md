# AutoEmu

AutoEmu is a harness-first agent framework that automatically generates QEMU-compatible virtual peripherals for microcontrollers. It fetches STM32 source data (SVD files, CMSIS headers, HAL/LL drivers), reconstructs peripheral behavior through deterministic analysis, and emits production-ready QEMU v9.2.4 C code with validation harnesses.

## Features

### Data Fetching (`fetch-data`)
- **Automated source collection** via DuckDuckGo search and GitHub API for SVD, CMSIS headers, HAL/LL/RTOS drivers, and reference manuals
- **Retry with exponential backoff** on all HTTP requests for reliability
- **Local cache with SHA256 validation** — skips re-downloading unchanged artifacts
- **`--offline` mode** — works entirely from cached data without network access
- **Graceful degradation** — continues pipeline when optional inputs (SVD, LL driver, reference manual) are unavailable
- **Manifest-based tracking** — every fetch produces a JSON manifest under `data/<platform>/manifests/`

### Register Parsing
- **SVD Parser** — lxml-based CMSIS-SVD XML parser with full `derivedFrom` chain resolution (including transitive chains), cluster/array expansion, and non-standard field width handling
- **Header Parser** — Regex-based CMSIS C header parser extracting `typedef struct` register layouts, `#define` base addresses, and `_Pos`/`_Msk` bit field macros
- **Driver Parser** — HAL/LL driver code analyzer extracting register access patterns, ISR flag logic, init sequences, DMA configurations, and state hints
- **Register Extractor** — Merges SVD and header sources into unified register blocks with accumulated warnings (never aborts on partial input)

### Behavioral Inference
- **State Machine Inference** — Automatically constructs FSM models from driver analysis: init/enable/disable transitions, transfer states, ISR-driven completion/error events. Falls back to trivial single-state model for peripherals without clear FSM patterns.
- **Interrupt Model Inference** — Maps interrupt flags to status/enable registers, infers clear mechanisms (W1C, read-clear, software-clear), resolves NVIC IRQ numbers, and builds event-to-flag maps from ISR patterns.
- **Dependency Graph Inference** — Detects cross-peripheral dependencies: RCC clock gating, DMA channel assignments, GPIO alternate functions, timer trigger paths, and EXTI wakeup chains.
- **Hardened inference** — All inference modules return valid-but-empty models on error or insufficient input; they never abort the pipeline.

### QEMU Code Generation
- **Targets QEMU v9.2.4 exclusively** with correct API usage:
  - `device_class_set_legacy_reset()` (not deprecated `dc->reset`)
  - `OBJECT_DECLARE_SIMPLE_TYPE` for type declarations
  - `MemoryRegionOps` for register read/write
  - Bare field names in `VMSTATE` macros
  - `hw/qdev-properties.h` for `DeviceClass`
- **Generated artifacts per peripheral:**
  - `stm32_<peripheral>.h` — QEMU device header with register offsets and bit field defines
  - `stm32_<peripheral>.c` — Full QEMU device implementation (MemoryRegionOps, IRQ wiring, VMState, reset)
  - `meson.build` — Meson build system integration snippet
  - `qtest_stm32_<peripheral>.c` — QTest harness (reset values, write-read, W1C, read-only protection)
  - `test_stm32_<peripheral>.c` — Standalone C test harness
  - `*_model.json` — Complete peripheral model in JSON

### Validation & Security
- **Register Validator** — Structural checks: overlapping offsets/fields, duplicate names, field width violations, reset value consistency
- **Behavior Validator** — Cross-validates model against driver analysis: missing registers, ISR flag mismatches, unreachable states
- **Driver Replay** — Replays register write/read sequences with full lifecycle support (init → configure → operate → error → teardown), per-stage accuracy metrics, and multi-version driver comparison
- **Compilation Validator** — Compiles generated C/H files against QEMU v9.2.4 headers using `cc -fsyntax-only`, validates meson.build structural correctness
- **Security Validator** — 5 rule categories: DMA boundary checks, privilege escalation detection, interrupt safety (infinite IRQ loop prevention), reserved field write warnings, configuration lock bypass detection
- **Fuzz Generator** — Generates AFL/libFuzzer harnesses targeting QEMU `MemoryRegionOps`: register fuzzer (random offsets + values) and state transition fuzzer (FSM race sequences)

### Platform Abstraction
- **Platform ABC** — Abstract interface (`Platform`) with methods: `discover_inputs()`, `parse_registers()`, `parse_drivers()`, `qemu_target_info()`, `naming_convention()`
- **Platform Registry** — `get_platform("stm32")`, `get_platform("mips")`, `list_platforms()`
- **STM32 Plugin** — Wraps existing SVD/header/HAL parsers behind the platform interface
- **MIPS Plugin** — PDF register table extractor, device tree parser, vendor header parser (non-CMSIS), Linux kernel driver parser (`readl`/`writel`, `platform_driver`, `request_irq`)
- **Generic Fetcher Base** — Reusable HTTP/cache/manifest logic extracted from STM32 fetcher

### Agent Framework
- **Backend abstraction** — `AgentBackend` ABC with two implementations:
  - `ClaudeBackend` (claude-agent-sdk)
  - `OpenAIAgentsBackend` (openai-agents)
- **Harness-first philosophy** — The deterministic pipeline is the primary path; LLM backends are fallbacks when heuristics fail
- **Tool-driven workflow** — 15+ backend-agnostic tool specifications for both fetch and model phases
- **6-phase orchestrator** — fetch → extract → infer SM → infer interrupts → infer dependencies → generate bundle

## Project Structure

```
src/autoemu/
├── main.py                          # CLI entry point (Click-based)
├── pipeline.py                      # End-to-end pipeline orchestrator
├── modeling_utils.py                # Shared utilities
├── models/                          # Pydantic v2 data layer
│   ├── register.py                  #   AccessType, BitField, Register, RegisterBlock
│   ├── peripheral.py                #   Peripheral, PeripheralType, ClockConfig
│   ├── state_machine.py             #   State, Transition, StateMachine
│   ├── interrupt.py                 #   InterruptLine, InterruptModel, FlagBehavior
│   └── dependency.py                #   DependencyEdge, DependencyGraph, DependencyType
├── parsers/                         # Input parsers
│   ├── svd_parser.py                #   CMSIS-SVD XML → RegisterBlock
│   ├── header_parser.py             #   CMSIS C headers → RegisterBlock
│   ├── driver_parser.py             #   HAL/LL drivers → DriverAnalysis
│   └── register_extractor.py        #   Merges SVD + header sources
├── fetchers/                        # Data fetching
│   └── stm32.py                     #   STM32 fetcher with DuckDuckGo + GitHub
├── inference/                       # Behavior inference
│   ├── state_machine_inference.py   #   FSM inference from driver patterns
│   ├── interrupt_inference.py       #   Interrupt model from ISR analysis
│   └── dependency_inference.py      #   Cross-peripheral dependency detection
├── generators/                      # Code generation
│   ├── qemu_generator.py            #   QEMU v9.2.4 C code (.h, .c, meson, QTest)
│   ├── test_generator.py            #   Standalone C test harness
│   ├── bundle_generator.py          #   Full model bundle + validation
│   └── fuzz_generator.py            #   AFL/libFuzzer harness generation
├── validators/                      # Model validation
│   ├── register_validator.py        #   Structural register checks
│   ├── behavior_validator.py        #   Driver behavior cross-validation
│   ├── driver_replay.py             #   Lifecycle replay + version comparison
│   ├── compile_validator.py         #   C compilation + meson validation
│   └── security_validator.py        #   Security audit (5 rule categories)
├── platforms/                       # Platform plugin system
│   ├── __init__.py                  #   Registry: get_platform(), list_platforms()
│   ├── base.py                      #   Platform ABC, QEMUTargetInfo, NamingInfo
│   ├── stm32/__init__.py            #   STM32Platform (wraps existing parsers)
│   └── mips/                        #   MIPS platform plugin
│       ├── __init__.py              #     MIPSPlatform implementation
│       ├── naming.py                #     MIPS naming conventions
│       └── parsers/                 #     PDF, DT, header, kernel driver parsers
├── fetchers/
│   ├── base.py                      #   Generic HTTP/cache/manifest logic
│   └── stm32.py                     #   STM32-specific fetcher
└── agent/                           # LLM agent backends
    ├── backend.py                   #   AgentBackend ABC, ToolSpec, AgentEvent
    ├── runtime.py                   #   Harness-first CLI runtime
    ├── orchestrator.py              #   6-phase prompt-driven pipeline
    ├── prompts.py                   #   System prompts, AGENTS.md loader
    ├── tools.py                     #   Backend-agnostic tool specs
    └── backends/
        ├── claude_backend.py        #   claude-agent-sdk integration
        └── openai_backend.py        #   openai-agents integration

tests/                               # 207 tests (pytest + pytest-asyncio)
├── test_models.py                   #   Core model tests
├── test_parsers.py                  #   Parser tests
├── test_inference.py                #   State machine inference tests
├── test_inference_hardening.py      #   Inference edge case / robustness tests
├── test_interrupt_inference.py      #   Interrupt inference tests
├── test_dependency_inference.py     #   Dependency inference tests
├── test_generators.py               #   QEMU code generation tests
├── test_validators.py               #   Validation tests
├── test_compile_validator.py        #   Compilation validation tests
├── test_bundle_generator.py         #   Bundle generation tests
├── test_fetchers.py                 #   Fetcher tests
├── test_backend.py                  #   Agent backend tests
├── test_cli.py                      #   CLI command tests
├── test_runtime.py                  #   Runtime tests
├── test_integration.py              #   End-to-end pipeline tests (3+ targets)
├── test_platforms.py                #   Platform abstraction tests
├── test_mips_platform.py            #   MIPS platform parser tests
├── test_security_validator.py       #   Security audit validator tests
├── test_fuzz_generator.py           #   Fuzz harness generation tests
└── test_driver_replay_lifecycle.py  #   Lifecycle replay + version comparison

scripts/                             # Build and validation harnesses
├── build_stm32_guest_firmware.sh    #   Build bare-metal probe firmware
├── run_stm32_guest_firmware.sh      #   Run firmware under QEMU board model
├── build_stm32_linux_probe_kernel.sh
├── build_stm32_linux_rootfs.sh
├── run_stm32_linux_probe_qemu.sh    #   Boot Buildroot Linux + BusyBox shell
└── common.sh

data/stm32/                          # Fetched input bundles
├── <mcu>/                           #   Per-MCU target data
│   ├── docs/                        #     Reference manuals, datasheets
│   ├── svd/                         #     CMSIS-SVD device descriptions
│   ├── headers/                     #     CMSIS device headers
│   └── drivers/                     #     HAL/LL/RTOS driver sources
└── manifests/                       #   Fetch manifests (JSON)

output/                              # Generated artifacts
├── <peripheral>/                    #   Per-peripheral output
│   ├── stm32_<peripheral>.h         #     QEMU device header
│   ├── stm32_<peripheral>.c         #     QEMU device implementation
│   ├── meson.build                  #     Build integration
│   ├── qtest_stm32_<peripheral>.c   #     QTest harness
│   ├── test_stm32_<peripheral>.c    #     Standalone C test
│   ├── *_model.json                 #     Peripheral model
│   └── *_validation.json            #     Validation report
```

## Install

Requires Python 3.11+.

```bash
pip install -e ".[dev]"
```

## CLI Commands

### `fetch-data` — Fetch input data for one MCU/peripheral target

```bash
autoemu fetch-data \
  --target-mcu STM32F407VG \
  --target-peripheral ETH \
  --output data/stm32
```

Options:
- `--target-mcu` — Target MCU (e.g., `STM32F407VG`, `STM32WL55JC`)
- `--target-peripheral` — Target peripheral (e.g., `ETH`, `USB`, `DMA`, `USART`)
- `--output` / `-o` — Output directory (default: `data/stm32`)
- `--refresh` — Re-download files even if cached
- `--offline` — Use only cached artifacts, skip all network requests

### `build-qemu-peripheral` — Build QEMU peripheral from fetched data

```bash
autoemu build-qemu-peripheral \
  --target-mcu STM32F407VG \
  --target-peripheral ETH \
  --data-dir data/stm32 \
  --output-dir output/eth
```

Options:
- `--target-mcu` — Target MCU
- `--target-peripheral` — Target peripheral
- `--data-dir` — Directory with fetched data (default: `data/stm32`)
- `--output-dir` / `-o` — Output directory (default: `output`)
- `--offline` — Use only cached data

### `validate-compile` — Validate generated C code compiles against QEMU headers

```bash
autoemu validate-compile \
  --output-dir output/eth \
  --qemu-src build/qemu-src/qemu-9.2.4
```

## Pipeline

The `build-qemu-peripheral` command runs a 6-step pipeline:

1. **Register Extraction** — Parse SVD/headers into unified `RegisterBlock` models
2. **Driver Analysis** — Analyze HAL/LL drivers for access patterns, ISR logic, init sequences
3. **State Machine Inference** — Build FSM from driver transitions and documentation
4. **Interrupt Model Inference** — Map flags → registers → enable bits → clear mechanisms
5. **Dependency Graph Inference** — Detect DMA, clock, GPIO, timer, EXTI dependencies
6. **Bundle Generation** — Emit QEMU C code, tests, and validation report

## Agent Backends

The pipeline defaults to the deterministic **harness** backend. Set environment variables to route through an LLM:

```bash
export AUTOEMU_AGENT_BACKEND=harness     # Default: deterministic pipeline
export AUTOEMU_AGENT_BACKEND=claude      # claude-agent-sdk backend
export AUTOEMU_AGENT_BACKEND=openai      # openai-agents backend
export AUTOEMU_AGENT_MODEL=gpt-5.4      # Model override (external backends only)
export AUTOEMU_AGENT_MAX_BUDGET_USD=5   # Spending cap
```

## Build the Binary

```bash
pyinstaller autoemu.spec --clean
# Produces: ./dist/autoemu
```

## Testing

```bash
pytest                              # All 207 tests
pytest tests/test_integration.py    # End-to-end pipeline tests (USART, SPI, TIM)
pytest -m integration               # Only integration-marked tests
pytest -k "test_w1c" -v             # Pattern matching
```

## Validation Harnesses

### Bare-metal guest firmware

Build and run the probe firmware under the in-repo QEMU board model:

```bash
./scripts/build_stm32_guest_firmware.sh
./scripts/run_stm32_guest_firmware.sh
```

Exercises: `HAL_DMA_Init()`, `HAL_ETH_Init()`, `HAL_PCD_Init()`, SUBGHZ MMIO/IRQ.

### Linux probe harness

Boot a Buildroot rootfs with automatic driver probing:

```bash
./scripts/run_stm32_linux_probe_qemu.sh
```

Probes DMA, ETH, USB, and Radio, then provides a BusyBox shell on `ttyAMA0`.

## External Source Trees

```
build/
├── qemu-src/qemu-9.2.4/           # QEMU source (compilation validation target)
├── linux-src/linux-6.17.0-rc1/    # Linux kernel (probe harness)
├── buildroot-src/buildroot-2025.05/ # Buildroot (rootfs builder)
└── third_party/STM32CubeF4/       # STM32 HAL/LL drivers
```

## Register Access Semantics

The model precisely handles all CMSIS access types:

| Type | Behavior |
|------|----------|
| `RW` | Standard read-write |
| `RO` | Read-only, writes ignored |
| `WO` | Write-only, reads return 0 |
| `W1C` | Write-1-to-clear (status flags) |
| `W1S` | Write-1-to-set |
| `W0C` | Write-0-to-clear |
| `RC_W1` | Read-clear + write-1-to-clear |
| `RC_W0` | Read-clear + write-0-to-clear |
| `RS` | Read-to-set |
| `RSVD` | Reserved, writes preserved |

## License

MIT
