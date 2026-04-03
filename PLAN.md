# AutoEmu Reconstruction Plan

## Overview

AutoEmu is an LLM Agent-driven framework that automatically generates QEMU-compatible
virtual peripherals for microcontrollers. This plan covers completing the STM32
foundation, expanding to multi-architecture support (MIPS), and adding security and
verification capabilities.

**Approach:** Pipeline-First, Platform-Second. Make the existing STM32 pipeline
bulletproof, then extract platform abstractions informed by real variance, then
implement MIPS as validation of the abstraction, then build security/verification on
the stable multi-arch pipeline.

**Priority order:** Pipeline robustness > Multi-arch expansion > Security/verification > Peripheral fidelity

**Agent philosophy:** Harness-first. The deterministic pipeline is the primary path.
LLM backends are fallbacks for when heuristics fail, not the default engine.

---

## Phase 1: Pipeline Hardening (STM32)

**Goal:** `fetch-data` followed by `build-qemu-peripheral` works reliably for any STM32
MCU + peripheral combination with zero manual intervention.

### 1.1 Fetcher Reliability

- Add retry with exponential backoff to all HTTP requests in `STM32DataFetcher`
- Implement local cache layer: if `data/stm32/{mcu}/` has a valid manifest with
  matching SHA256 hashes, skip re-fetching
- Add manifest schema validation — reject corrupt or incomplete downloads before
  the pipeline consumes them
- Graceful degradation for missing inputs:
  - No SVD available: fall back to header-only register parsing
  - No LL driver: use HAL-only driver analysis
  - No reference manual: skip documentation-enhanced inference, use code-only heuristics
- Add `--offline` CLI flag that refuses network access and uses only cached data

### 1.2 Parser Robustness

- `svd_parser`: handle `derivedFrom` chains that span multiple peripheral groups,
  missing `resetValue` fields (default to 0x0), non-standard field widths, and
  clusters/arrays
- `header_parser`: handle vendor headers that use `#define` macros instead of struct
  layouts, multi-file `#include` chains, and conditional compilation (`#ifdef`)
- `driver_parser`: handle pure polling drivers (no ISR patterns), drivers split
  across multiple `.c` files, and C++ HAL wrappers
- `register_extractor`: accumulate all parser warnings without aborting; return
  partial results with a warnings list so the pipeline can continue

### 1.3 Inference Hardening

- State machine inference: produce a trivial single-state model for peripherals
  with no clear FSM (e.g., GPIO pass-through), rather than raising an exception
- Interrupt inference: produce an empty `InterruptModel` for peripherals with no
  interrupts (e.g., basic GPIO, some timers in PWM mode)
- Dependency inference: produce an empty `DependencyGraph` for standalone
  peripherals with no cross-peripheral references
- All inference modules must return valid-but-empty models when inputs are
  insufficient — never exceptions

### 1.4 Generation & Compilation Validation

- Add a `validate-compile` pipeline step: compile generated `.c`/`.h` files against
  QEMU v9.2.4 headers (from `build/qemu-src/`) using `cc -fsyntax-only -I<qemu-includes>`
- Fix any QEMU API mismatches found during compilation (ensure
  `OBJECT_DECLARE_SIMPLE_TYPE`, `MemoryRegionOps`, `device_class_set_legacy_reset`,
  `VMSTATE` macros all match v9.2.4 signatures)
- Validate that `meson.build` snippets integrate correctly into QEMU's build tree
- Ensure QTest harnesses compile and link against QEMU's test framework

### 1.5 End-to-End Integration Tests

- Add `tests/test_integration.py` with full pipeline runs on at least 3 STM32
  targets: F407/ETH, F407/DMA, WL55/Radio
- Assertions per target:
  - Pipeline completes without error
  - All expected output files exist (`.h`, `.c`, `meson.build`, QTest, model JSONs)
  - Generated C code compiles against QEMU v9.2.4 headers
  - Validation report contains no errors (warnings are acceptable)
- Use `pytest -m integration` marker to separate slow integration tests from fast
  unit tests
- Firmware probe smoke test: run `scripts/run_stm32_guest_firmware.sh` in CI,
  verify semihosting output contains PASS and no FAIL

### Phase 1 Deliverables

- Reliable `autoemu fetch-data` with caching, retries, and graceful degradation
- Robust `autoemu build-qemu-peripheral` that handles missing/partial inputs
- Generated QEMU code that compiles cleanly against v9.2.4
- Integration test suite covering 3+ STM32 targets
- Firmware probe validation passing

---

## Phase 2: Platform Abstraction

**Goal:** Extract STM32-specific logic into a platform plugin interface so the pipeline
is architecture-agnostic. Zero behavior change for STM32.

### 2.1 Define Platform Interface

```python
class Platform(ABC):
    name: str  # "stm32", "mips"

    @abstractmethod
    def discover_inputs(self, mcu: str, peripheral: str) -> list[AssetRequest]:
        """Return fetchable asset descriptors for this target."""

    @abstractmethod
    def parse_registers(self, bundle: InputBundle) -> dict[str, RegisterBlock]:
        """Parse register descriptions from platform-specific input formats."""

    @abstractmethod
    def parse_drivers(self, bundle: InputBundle) -> DriverAnalysis:
        """Analyze driver code in platform-specific style."""

    @abstractmethod
    def qemu_target_info(self, mcu: str) -> QEMUTargetInfo:
        """Return QEMU arch, machine type, CPU model, include paths."""

    @abstractmethod
    def naming_convention(self, peripheral: str) -> NamingInfo:
        """Return file prefixes, QEMU type names, object names."""
```

**Key design decision:** Inference stays generic. `StateMachine`, `InterruptModel`,
and `DependencyGraph` are architecture-independent concepts that operate on the
common model types. Only parsing (input formats) and generation (QEMU target
specifics) are platform-specific.

### 2.2 Refactor Fetchers

- Extract generic HTTP/cache/manifest logic into `fetchers/base.py`
- Move `fetchers/stm32.py` into `platforms/stm32/fetcher.py`
- Each platform provides its own asset catalog and URL resolution strategy
- The generic fetcher handles retries, caching, SHA256 validation

### 2.3 Refactor Parsers

- Current parsers (`svd_parser`, `header_parser`, `driver_parser`) become the
  STM32 platform's parser suite under `platforms/stm32/parsers/`
- `register_extractor` becomes a generic orchestrator that calls
  `platform.parse_registers()` and `platform.parse_drivers()`
- The interface contract: accept an `InputBundle`, return `RegisterBlock` and
  `DriverAnalysis` — same model types regardless of platform

### 2.4 Refactor Generators

- Parameterize `qemu_generator` with `QEMUTargetInfo`:
  - Include paths differ per architecture
  - Interrupt wiring differs (NVIC for ARM, CP0/external for MIPS)
  - Machine integration and device tree references differ
- File naming and QEMU type names use `platform.naming_convention()`
- Core generation logic stays shared: `MemoryRegionOps`, `VMState`,
  `DeviceClass` setup are QEMU concepts, not MCU concepts

### 2.5 STM32 as First Plugin

- All STM32-specific code moves under `platforms/stm32/`:
  ```
  platforms/
  └── stm32/
      ├── __init__.py          # STM32Platform class
      ├── fetcher.py           # STM32DataFetcher
      ├── parsers/
      │   ├── svd_parser.py
      │   ├── header_parser.py
      │   └── driver_parser.py
      └── naming.py            # STM32 naming conventions
  ```
- Purely structural refactor — zero behavior change
- All existing tests must pass with only import path updates
- Platform registry: `get_platform("stm32")` → `STM32Platform()`

### Phase 2 Deliverables

- `Platform` ABC with clear interface contract
- STM32 platform plugin fully extracted
- Generic pipeline orchestration using `Platform` interface
- All existing tests passing
- Platform conformance test suite

---

## Phase 3: MIPS Platform & Multi-Architecture

**Goal:** Implement MIPS as the second platform, validating the abstraction layer.

### 3.1 MIPS Input Research

- Survey common MIPS MCU vendors: Microchip PIC32 (MIPS32), MediaTek (MT76xx),
  Ingenic (JZ47xx/X series)
- Catalog available input formats:
  - PDF datasheets with tabular register maps
  - Linux kernel device tree bindings (`.dts`/`.dtsi`)
  - Vendor C headers (non-CMSIS conventions)
  - Linux kernel drivers (`platform_driver`, `ioremap`, `readl`/`writel`)
- Select 1-2 reference MIPS MCUs with good documentation for initial implementation

### 3.2 MIPS Parser Layer

- **PDF register extractor**: tabular extraction from datasheet PDFs using
  pdfplumber or camelot; extract register names, offsets, field definitions,
  access types from register map tables
- **Device tree parser**: extract `compatible` strings, `reg` regions (base
  addresses + sizes), `interrupts` specifiers, clock references from `.dts`/`.dtsi`
- **Vendor header parser**: adapt regex-based parsing for MIPS naming conventions
  (no CMSIS, different macro patterns, vendor-specific struct layouts)
- **Linux kernel driver parser**: adapt `driver_parser` patterns for Linux kernel
  driver style (`probe`/`remove` lifecycle, `platform_driver` registration,
  `ioremap`/`readl`/`writel` register access, `request_irq` interrupt setup)

### 3.3 MIPS QEMU Generation

- Target QEMU MIPS machine types (`mipsel-softmmu`, relevant board models)
- MIPS interrupt wiring: CP0 Cause register, external interrupt controllers
  (vendor-specific — no standard NVIC equivalent)
- Different memory map conventions (KSEG0/KSEG1 uncached regions for MMIO)
- MIPS-specific `QEMUTargetInfo`: arch flags, include paths, CPU model strings

### 3.4 Cross-Platform Test Suite

- Parameterized tests that run identical pipeline assertions for both STM32 and MIPS:
  - Pipeline completes, output files exist, C code compiles, validation passes
- Platform conformance tests: verify each platform plugin implements the full
  `Platform` interface correctly
- Generated code compilation tests against QEMU headers for both architectures

### 3.5 Interface Refinement

- Refine `Platform` interface based on what breaks or feels forced during MIPS
  implementation
- Document platform-specific vs. generic boundary decisions
- Update platform conformance tests to cover any new interface methods

### Phase 3 Deliverables

- MIPS platform plugin with PDF + DT + header + kernel driver parsers
- MIPS QEMU code generation producing compilable peripherals
- Cross-platform test suite validating both STM32 and MIPS
- Refined `Platform` interface informed by two real implementations
- At least 1 MIPS MCU peripheral generating correct QEMU code

---

## Phase 4: Security & Verification

**Goal:** Build automated security testing, driver compatibility verification, and
firmware-level validation on the stable multi-arch pipeline.

### 4.1 Driver Replay Hardening

- Extend `driver_replay.py` to replay full lifecycle sequences:
  init → configure → operate → error handling → teardown
- Record expected register state at each step, compare against model state
- Report divergences with: register name, offset, step index, expected vs. actual
  value, and the source driver line that triggered the write
- Support replaying sequences from multiple driver versions to detect regressions

### 4.2 Fuzzing Harness Generation

- Generate AFL/libFuzzer harnesses targeting QEMU `MemoryRegionOps` callbacks
- Fuzz targets:
  - Random register offsets (including out-of-range and reserved)
  - Random write values (boundary values, all-ones, all-zeros)
  - Rapid state transitions (write sequences that race through FSM states)
  - DMA descriptor manipulation (invalid addresses, circular chains)
- Integrate with `generate_peripheral_code()` as an optional output artifact

### 4.3 Security Audit Validators

Extend the `validators/` module with security-focused checks:

- **DMA boundary validation**: descriptor addresses must fall within valid memory
  regions; flag unbounded DMA configurations
- **Privilege escalation**: registers writable from unprivileged mode that control
  security-sensitive features (clock gating, memory protection, debug access)
- **Interrupt safety**: flag patterns that could cause infinite IRQ loops
  (flag set in ISR without clear, enable bit permanently asserted)
- **Reserved field writes**: warn when driver code writes to reserved/undocumented
  register fields
- **Configuration lock bypass**: detect registers that should be write-once but
  lack lock mechanism in the model

### 4.4 Driver Compatibility Matrix

- Automated testing of generated peripherals against multiple driver versions:
  - STM32: CubeF4 v1.27 vs v1.28, CubeWL latest
  - MIPS: Linux kernel driver versions across kernel releases
- Test against RTOS adaptation layers: FreeRTOS, Zephyr, RT-Thread
- Output: compatibility matrix (peripheral x driver version x pass/fail/warnings)
- Regression detection: alert when a model change breaks a previously compatible
  driver version

### 4.5 Firmware Simulation Validation

- Extend firmware probe harnesses for automated CI runs
- Add QEMU guest test framework integration: run generated QTest harnesses as
  part of QEMU's test suite
- Compare simulated peripheral behavior against real hardware traces (when
  available) using logic analyzer capture files or OpenOCD register dumps

### Phase 4 Deliverables

- Full-lifecycle driver replay with divergence reporting
- Fuzzing harness generation (AFL/libFuzzer) for generated peripherals
- Security audit validator suite (5+ rule categories)
- Driver compatibility matrix across HAL versions and RTOS layers
- CI-integrated firmware simulation validation

---

## Directory Structure (Target State)

```
src/autoemu/
├── platforms/                    # Platform plugins (NEW)
│   ├── __init__.py               # Platform registry, get_platform()
│   ├── base.py                   # Platform ABC, QEMUTargetInfo, NamingInfo
│   ├── stm32/                    # STM32 platform (refactored from current code)
│   │   ├── __init__.py
│   │   ├── fetcher.py
│   │   ├── parsers/
│   │   │   ├── svd_parser.py
│   │   │   ├── header_parser.py
│   │   │   └── driver_parser.py
│   │   └── naming.py
│   └── mips/                     # MIPS platform (NEW in Phase 3)
│       ├── __init__.py
│       ├── fetcher.py
│       ├── parsers/
│       │   ├── pdf_parser.py
│       │   ├── dt_parser.py
│       │   ├── header_parser.py
│       │   └── kernel_driver_parser.py
│       └── naming.py
├── models/                       # Unchanged — architecture-independent
├── inference/                    # Unchanged — operates on generic models
├── generators/
│   ├── qemu_generator.py         # Parameterized by QEMUTargetInfo
│   ├── bundle_generator.py
│   ├── test_generator.py
│   └── fuzz_generator.py         # NEW in Phase 4
├── validators/
│   ├── register_validator.py
│   ├── behavior_validator.py
│   ├── driver_replay.py
│   └── security_validator.py     # NEW in Phase 4
├── fetchers/
│   └── base.py                   # Generic HTTP/cache/manifest logic
├── agent/                        # Unchanged
├── pipeline.py                   # Uses Platform interface
├── modeling_utils.py
└── main.py                       # CLI adds --platform flag
```

---

## Success Criteria

| Phase | Criterion |
|-------|-----------|
| 1 | `autoemu fetch-data && autoemu build-qemu-peripheral` succeeds for 3+ STM32 targets; generated C compiles against QEMU v9.2.4 |
| 2 | All existing tests pass after refactor; `Platform` ABC defined with STM32 as first plugin |
| 3 | MIPS platform produces compilable QEMU peripheral code for at least 1 MCU; cross-platform tests pass |
| 4 | Security validators catch known-bad patterns; driver replay detects intentional model regressions; fuzzing harness compiles and runs |

---

## Dependencies & Risks

| Risk | Mitigation |
|------|------------|
| MIPS documentation varies wildly across vendors | Start with best-documented vendor (Ingenic or PIC32); build PDF parser to handle table format variations |
| Platform abstraction may be premature | Phase 1 hardens STM32 first; abstraction is informed by concrete experience, not speculation |
| QEMU API differences across architectures | Use `QEMUTargetInfo` to parameterize; keep a compatibility matrix of tested QEMU versions |
| PDF register table extraction is unreliable | LLM fallback for ambiguous PDF parsing (consistent with harness-first philosophy — deterministic first, LLM when heuristics fail) |
| Generated code may not match real hardware behavior | Driver replay + firmware probe validation catch behavioral mismatches early |
