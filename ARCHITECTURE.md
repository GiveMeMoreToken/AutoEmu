# AutoEmu Architecture

## Overview

AutoEmu is an automated QEMU peripheral model generator. Given a **target board** (e.g., `HIKEY960`, `STM32F407VG`) and a **target peripheral** (e.g., `GPU`, `ETH`), it fetches documentation and source code from the web, analyzes register maps and driver behavior, and generates QEMU v9.2.4-compatible C code that emulates the peripheral.

## System Architecture

```
                            +------------------+
                            |     User         |
                            |  (TUI terminal)  |
                            +--------+---------+
                                     |
                                     | target_mcu + target_peripheral
                                     v
+===========================================================================+
|                          AutoEmu TUI (Textual)                            |
|  src/autoemu/tui/app.py                                                   |
|  - Input form: board + peripheral                                         |
|  - Pipeline phase indicators (1-4)                                        |
|  - Scrolling log panel with Rich markup                                   |
+============================+=============================================+
                             |
                             v
+===========================================================================+
|                    Unified Pipeline Runtime                               |
|  src/autoemu/agent/runtime.py :: AutoEmuAgentRuntime.run_pipeline()       |
|                                                                           |
|  Phase 1: DETECT PLATFORM                                                 |
|  +-------------------------------------------------------------------+   |
|  | platforms/__init__.py :: analyze_target()                          |   |
|  | Board knowledge base: 15+ vendors (HiSilicon, Qualcomm, Nordic,   |   |
|  | Espressif, NXP, TI, STMicro, MediaTek, Allwinner, Samsung, ...)   |   |
|  | Returns: vendor, arch, family, search aliases                     |   |
|  +-------------------------------------------------------------------+   |
|                             |                                             |
|  Phase 2: FETCH INPUT DATA                                                |
|  +-------------------------------------------------------------------+   |
|  | fetchers/stm32.py :: STM32DataFetcher (STM32 targets)             |   |
|  |   - DuckDuckGo web search + GitHub API                            |   |
|  |   - Downloads: SVD, CMSIS headers, HAL/LL drivers, datasheets     |   |
|  |                                                                   |   |
|  | fetchers/generic.py :: GenericDataFetcher (all other targets)      |   |
|  |   - Parallel web search (ThreadPoolExecutor)                      |   |
|  |   - Vendor-aware queries from analyze_target() aliases            |   |
|  |   - Candidate scoring (0-100) by relevance heuristics             |   |
|  +-------------------------------------------------------------------+   |
|                             |                                             |
|  Phase 3: BUILD QEMU MODEL                                               |
|  +-------------------------------------------------------------------+   |
|  | pipeline.py :: run_model_pipeline()                               |   |
|  |                                                                   |   |
|  | Step 3a: Parse registers (SVD + headers)                          |   |
|  |   parsers/svd_parser.py      -> RegisterBlock                     |   |
|  |   parsers/header_parser.py   -> RegisterBlock                     |   |
|  |   parsers/register_extractor.py -> merged RegisterBlock           |   |
|  |                                                                   |   |
|  | Step 3b: Analyze drivers                                          |   |
|  |   parsers/driver_parser.py -> DriverAnalysis                      |   |
|  |   (register accesses, ISR patterns, init sequences, DMA configs)  |   |
|  |                                                                   |   |
|  | Step 3c: Infer behavior models                                    |   |
|  |   inference/state_machine_inference.py -> StateMachine            |   |
|  |   inference/interrupt_inference.py     -> InterruptModel          |   |
|  |   inference/dependency_inference.py    -> DependencyGraph         |   |
|  |                                                                   |   |
|  | Step 3d: Generate QEMU C code                                     |   |
|  |   generators/qemu_generator.py  -> .h, .c, meson.build, QTest    |   |
|  |   generators/test_generator.py  -> standalone test harness        |   |
|  |   generators/bundle_generator.py -> assembled Peripheral model    |   |
|  +-------------------------------------------------------------------+   |
|                             |                                             |
|  Phase 4: VALIDATE                                                        |
|  +-------------------------------------------------------------------+   |
|  | validators/compile_validator.py                                   |   |
|  |   - gcc -fsyntax-only against QEMU v9.2.4 headers                |   |
|  |   - pkg-config auto-discovery (glib-2.0, pixman-1)               |   |
|  |   - meson.build structural validation                            |   |
|  |   - System header errors demoted to warnings                     |   |
|  |                                                                   |   |
|  | validators/register_validator.py                                  |   |
|  |   - Overlapping offsets/fields, duplicate names, access conflicts |   |
|  |                                                                   |   |
|  | validators/behavior_validator.py                                  |   |
|  |   - Cross-validates model against driver analysis                 |   |
|  |                                                                   |   |
|  | validators/security_validator.py                                  |   |
|  |   - DMA boundary, privilege escalation, interrupt safety          |   |
|  +-------------------------------------------------------------------+   |
+===========================================================================+


## Data Flow

```
 Target: "HIKEY960" + "GPU"
          |
          v
 +------------------+    analyze_target()     +-------------------+
 | Platform Detect  | ----------------------> | vendor: hisilicon  |
 |                  |                         | arch: arm64        |
 +------------------+                         | family: Kirin      |
          |                                   +-------------------+
          v
 +------------------+    web search           +-------------------+
 | Fetch            | ----+-- DuckDuckGo ---> | SVD files         |
 | (data/hikey960/) |     +-- GitHub API ---> | C headers         |
 |                  |     +-- vendor SDK ---> | Driver sources    |
 +------------------+                         | Datasheets        |
          |                                   +-------------------+
          v
 +------------------+
 | Parse & Extract  | ---> RegisterBlock (name, offset, fields, access)
 |                  | ---> DriverAnalysis (accesses, ISRs, init seqs)
 +------------------+
          |
          v
 +------------------+
 | Infer Behavior   | ---> StateMachine (states, transitions)
 |                  | ---> InterruptModel (IRQ lines, flags, enables)
 |                  | ---> DependencyGraph (clock, DMA, GPIO deps)
 +------------------+
          |
          v
 +------------------+    +--------------------------------------------+
 | Generate Code    | -> | hikey960_gpu.h   (QEMU device header)      |
 |                  |    | hikey960_gpu.c   (MemoryRegionOps impl)    |
 | (output/)        |    | meson.build      (QEMU build integration)  |
 |                  |    | qtest_hikey960_gpu.c (QTest harness)       |
 |                  |    | test_hikey960_gpu.c  (standalone tests)    |
 |                  |    | gpu_peripheral.json  (full model)          |
 +------------------+    +--------------------------------------------+
          |
          v
 +------------------+    gcc -fsyntax-only    +-------------------+
 | Validate         | ----------------------> | PASS / warnings   |
 +------------------+                         +-------------------+
```

## Where AI Agents Contribute

AutoEmu has a **dual execution mode**: a deterministic **harness** path (default) and an **AI agent** path.

### Harness Mode (default, `AUTOEMU_AGENT_BACKEND=harness`)

The entire pipeline runs **without any AI**. All steps are deterministic:
- Web search uses DuckDuckGo HTML scraping with heuristic scoring
- Parsers use regex and XML parsing
- Inference uses pattern matching on driver analysis (not ML)
- Code generation uses template-based string building

This is the mode used by the TUI. It requires no API keys.

### AI Agent Mode (`AUTOEMU_AGENT_BACKEND=claude` or `openai`)

When configured, the pipeline routes through an **LLM orchestrator** that can make intelligent decisions:

```
+===========================================================================+
|                     AI Agent Orchestrator                                  |
|  src/autoemu/agent/orchestrator.py :: AutoEmuOrchestrator                 |
|                                                                           |
|  The LLM agent receives the same tool registry as the harness pipeline    |
|  but can reason about WHICH tools to call and HOW to interpret results.   |
|                                                                           |
|  +-------------------------------+  +-------------------------------+    |
|  | Claude Backend                |  | OpenAI Backend                |    |
|  | (claude-agent-sdk)            |  | (openai-agents)               |    |
|  | src/autoemu/agent/backends/   |  | src/autoemu/agent/backends/   |    |
|  |   claude_backend.py           |  |   openai_backend.py           |    |
|  +-------------------------------+  +-------------------------------+    |
|                                                                           |
|  6-Phase Prompt Pipeline:                                                 |
|  1. EXTRACT  - "Parse SVD/headers for {peripheral}, build register model" |
|  2. ANALYZE  - "Analyze HAL drivers, extract ISR and init patterns"       |
|  3. INFER    - "Derive interrupt model from driver + register analysis"   |
|  4. CONNECT  - "Build dependency graph (DMA, clock, GPIO, EXTI)"         |
|  5. GENERATE - "Generate QEMU v9.2.4 C code with MemoryRegionOps"       |
|  6. VALIDATE - "Check register consistency and driver replay"             |
|                                                                           |
|  At each phase, the agent has access to ALL 23 tools:                    |
|    fetch_data, parse_svd, parse_header, analyze_driver,                  |
|    extract_register_structure, infer_state_machine,                      |
|    infer_interrupt_model, infer_dependency_graph,                        |
|    generate_qemu_peripheral, generate_model_bundle,                      |
|    validate_register_model, validate_behavior, run_model_pipeline,       |
|    read_file, write_file, list_files, search_web, ...                    |
|                                                                           |
|  The AI adds value by:                                                    |
|  - Choosing which parsers to use based on available inputs                |
|  - Inferring register semantics from documentation context                |
|  - Recognizing non-standard driver patterns the regex parser misses       |
|  - Generating richer state machine models with documentation grounding   |
|  - Filling gaps when SVD/headers are incomplete or missing               |
|  - Suggesting fixes when validation finds issues                         |
+===========================================================================+
```

### Key Distinction

| Aspect | Harness Mode | Agent Mode |
|--------|-------------|------------|
| Execution | Deterministic | LLM-guided |
| Speed | Fast (seconds) | Slow (minutes) |
| Cost | Free | API billing |
| Accuracy | Pattern-dependent | Context-aware |
| Requires | Nothing | API key |
| Best for | Well-documented MCUs (STM32) | Obscure targets, incomplete docs |

The **harness tools are the same functions** the AI agent calls. The agent just decides the order, parameters, and how to handle edge cases. This means every improvement to the deterministic parsers, inference, and generators also improves the agent mode.

## Module Map

```
src/autoemu/
  main.py                    CLI entry point (launches TUI)
  pipeline.py                End-to-end modeling pipeline
  modeling_utils.py          Shared helpers (snake_case, normalize)

  tui/
    app.py                   Textual TUI application
    widgets.py               LogPanel, PipelinePhaseList

  agent/
    runtime.py               Unified pipeline: detect -> fetch -> build -> validate
    orchestrator.py           6-phase LLM prompt orchestrator
    backend.py               AgentBackend ABC, AgentEvent, ToolSpec
    backends/
      claude_backend.py      Claude Agent SDK integration
      openai_backend.py      OpenAI Agents SDK integration
    tools.py                 23 backend-agnostic tool definitions
    prompts.py               System/phase prompts for agent mode

  platforms/
    __init__.py              Registry + analyze_target() board knowledge base
    base.py                  Platform ABC, QEMUTargetInfo, InputBundle
    stm32/__init__.py        STM32 platform plugin
    mips/__init__.py         MIPS platform plugin
    generic/__init__.py      Generic platform (any MCU)
    mips/parsers/            PDF, device tree, kernel driver parsers

  fetchers/
    base.py                  BaseFetcher ABC, HTTP retry, SHA256
    stm32.py                 STM32-specific DuckDuckGo + GitHub fetcher
    generic.py               Generic web search fetcher (any MCU)

  parsers/
    svd_parser.py            CMSIS-SVD XML -> RegisterBlock
    header_parser.py         C header regex -> RegisterBlock
    driver_parser.py         HAL/LL driver -> DriverAnalysis
    register_extractor.py    SVD + header merge

  models/
    register.py              Register, BitField, RegisterBlock, AccessType
    peripheral.py            Peripheral (top-level model)
    state_machine.py         StateMachine, State, Transition
    interrupt.py             InterruptModel, InterruptLine, InterruptFlag
    dependency.py            DependencyGraph, DependencyEdge

  inference/
    state_machine_inference.py   Driver patterns -> StateMachine
    interrupt_inference.py       ISR patterns -> InterruptModel
    dependency_inference.py      Cross-references -> DependencyGraph

  generators/
    qemu_generator.py        Peripheral -> QEMU C code (.h, .c, meson, QTest)
    test_generator.py         Peripheral -> standalone C test harness
    bundle_generator.py       Assemble + validate + emit full bundle
    fuzz_generator.py         Peripheral -> AFL/libFuzzer harness

  validators/
    register_validator.py     Structural register checks
    behavior_validator.py     Model vs driver cross-validation
    compile_validator.py      gcc -fsyntax-only + meson validation
    security_validator.py     DMA, privilege, interrupt safety
    driver_replay.py          Register write/read sequence replay
```
