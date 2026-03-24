# AutoEmu

LLM Agent-driven automated embedded peripheral modeling for STM32 MCUs. Generates QEMU v9.2.4-compatible C device models from SVD files, CMSIS headers, and HAL/LL driver source code.

## Project Structure

```
AutoEmu/
├── src/autoemu/
│   ├── models/              # Core data models (Pydantic)
│   │   ├── register.py      # BitField, Register, RegisterBlock
│   │   ├── peripheral.py    # Peripheral, PeripheralType, ClockConfig
│   │   ├── state_machine.py # State, Transition, StateMachine
│   │   ├── interrupt.py     # InterruptModel, InterruptLine, InterruptFlag
│   │   └── dependency.py    # DependencyGraph, DependencyEdge
│   ├── parsers/             # Input file parsers
│   │   ├── svd_parser.py    # CMSIS-SVD XML parser
│   │   ├── header_parser.py # C header file parser (CMSIS/HAL)
│   │   └── driver_parser.py # HAL/LL driver code analyzer
│   ├── generators/          # Code generators
│   │   ├── qemu_generator.py    # QEMU v9.2.4 C code generator
│   │   └── test_generator.py    # C test harness generator
│   ├── validators/          # Model validation
│   │   ├── register_validator.py # Register structure validation
│   │   └── behavior_validator.py # Behavioral validation against drivers
│   ├── peripherals/         # Built-in peripheral templates
│   │   ├── dma.py           # DMA controller (STM32F4/F7/H7)
│   │   ├── eth.py           # Ethernet MAC with DMA
│   │   ├── usb.py           # USB OTG FS/HS controller
│   │   └── radio.py         # Sub-GHz radio (SX1262, STM32WL)
│   ├── agent/               # LLM agent orchestration
│   │   ├── backend.py       # Abstract backend interface
│   │   ├── backends/        # Backend implementations (Claude, OpenAI)
│   │   ├── tools.py         # Agent tool definitions
│   │   ├── orchestrator.py  # Modeling pipeline orchestrator
│   │   └── prompts.py       # System prompts for the agent
│   └── main.py              # CLI entry point
├── tests/                   # Test suite
├── data/                    # Input data (SVD files, headers)
├── output/                  # Generated output
└── pyproject.toml           # Project configuration
```

## Installation

Requires Python 3.11+.

```bash
pip install -e ".[dev]"
```

## Quick Start

### Generate a Built-in Peripheral Model

Use pre-defined templates without an LLM:

```bash
autoemu builtin DMA1
autoemu builtin ETH
autoemu builtin USB_OTG_FS
autoemu builtin SUBGHZ
```

Output goes to `output/` by default. Use `-o` to change:

```bash
autoemu builtin ETH -o ./my_output
```

### Model a Peripheral with the LLM Agent

The `model` command runs a multi-phase LLM pipeline that parses input files, analyzes driver code, builds models, and generates QEMU C code:

```bash
autoemu model ETH \
  --svd data/STM32F407.svd \
  --header data/stm32f407xx.h \
  --driver data/stm32f4xx_hal_eth.c \
  --mcu STM32F4 \
  -o output/eth
```

Options:

| Flag | Description |
|------|-------------|
| `--mcu` | MCU family (default: `STM32F4`) |
| `--svd` | Path to SVD file |
| `--header` | Path to CMSIS header file |
| `--driver` | Path to HAL/LL driver source (repeatable) |
| `-o, --output` | Output directory (default: `output`) |
| `--model` | LLM model identifier |
| `--budget` | Max spend in USD (default: `5.0`) |
| `--phases` | Comma-separated pipeline phases |
| `-b, --backend` | Agent backend: `claude` or `openai` |
| `-v, --verbose` | Verbose output |

The pipeline runs these phases in order:

1. **extract** - Parse register maps from SVD/headers
2. **analyze** - Study driver code for access patterns and ISR logic
3. **infer** - Build state machines and interrupt models
4. **connect** - Map cross-peripheral dependencies (DMA, clocks, triggers)
5. **generate** - Output QEMU v9.2.4 C code
6. **validate** - Cross-check model against driver expectations

Run a subset of phases:

```bash
autoemu model ETH --svd data/STM32F407.svd --phases extract,generate
```

### Parse an SVD File

```bash
autoemu parse-svd data/STM32F407.svd
autoemu parse-svd data/STM32F407.svd -p USART1
autoemu parse-svd data/STM32F407.svd -j  # JSON output
```

### Analyze a Driver Source File

```bash
autoemu analyze data/stm32f4xx_hal_eth.c
autoemu analyze data/stm32f4xx_hal_eth.c -p ETH
```

### Validate a Peripheral Model

```bash
autoemu validate output/eth/stm32_eth_model.json
```

### Free-form Query

```bash
autoemu query "What registers does STM32F4 ETH need for DMA descriptors?"
```

### Batch Mode

Model multiple peripherals at once:

```bash
autoemu batch --mcu STM32F4 -o output
```

## Usage as a Library

### Build a Peripheral Programmatically

```python
from autoemu.peripherals.eth import build_eth_peripheral
from autoemu.generators.qemu_generator import generate_peripheral_code

eth = build_eth_peripheral(base_address=0x40028000, mcu_family="STM32F4")
files = generate_peripheral_code(eth, "output/eth")
```

### Parse SVD and Build a Model

```python
from autoemu.parsers.svd_parser import parse_svd_file

blocks = parse_svd_file("data/STM32F407.svd")
usart = blocks["USART1"]
print(f"USART1 base: 0x{usart.base_address:08X}")
for reg in usart.registers:
    print(f"  {reg.name} @ 0x{reg.offset:03X} [{reg.access.value}]")
```

### Analyze Driver Code

```python
from autoemu.parsers.driver_parser import analyze_driver_file

analysis = analyze_driver_file("stm32f4xx_hal_eth.c", "ETH")
for isr in analysis.isr_patterns:
    print(f"ISR: {isr.function_name}")
    print(f"  Checked: {isr.checked_flags}")
    print(f"  Cleared: {isr.cleared_flags}")
```

### Validate a Register Model

```python
from autoemu.models.register import Register, RegisterBlock, AccessType, BitField
from autoemu.validators.register_validator import validate_register_block

block = RegisterBlock(name="TEST", registers=[
    Register(name="CR", offset=0x00, fields=[
        BitField(name="EN", bit_offset=0, bit_width=1, access=AccessType.RW),
    ]),
])
issues = validate_register_block(block)
```

### Replay Register Operations

```python
from autoemu.models.register import Register, RegisterBlock
from autoemu.models.peripheral import Peripheral
from autoemu.validators.behavior_validator import replay_register_sequence

periph = Peripheral(
    name="TEST",
    register_block=RegisterBlock(name="TEST", registers=[
        Register(name="CR", offset=0x00, reset_value=0),
    ]),
)
mismatches = replay_register_sequence(periph, [
    {"type": "write", "offset": 0x00, "value": 0x1234},
    {"type": "read", "offset": 0x00, "expected": 0x1234},
])
```

### Use the Agent Pipeline Programmatically

```python
import asyncio
from autoemu.agent.orchestrator import AutoEmuOrchestrator, ModelingTask

async def main():
    orchestrator = AutoEmuOrchestrator(backend="claude", max_budget_usd=3.0)
    task = ModelingTask(
        peripheral_name="ETH",
        mcu_family="STM32F4",
        svd_path="data/STM32F407.svd",
        output_dir="output/eth",
    )
    result = await orchestrator.model_peripheral(task)
    print(f"Success: {result.success}, Cost: ${result.total_cost_usd:.4f}")

asyncio.run(main())
```

## Generated Output

The QEMU code generator produces these files per peripheral:

| File | Description |
|------|-------------|
| `stm32_<name>.h` | Header with register offsets, bit field defines, state struct |
| `stm32_<name>.c` | Source with read/write handlers, reset, init, VMState |
| `meson.build` | Meson build integration snippet |
| `qtest_stm32_<name>.c` | QTest harness for in-tree validation |
| `stm32_<name>_model.json` | Peripheral model as JSON |

Generated code targets QEMU v9.2.4 and uses:

- `device_class_set_legacy_reset()` (not deprecated `dc->reset`)
- `OBJECT_DECLARE_SIMPLE_TYPE` for type declarations
- `MemoryRegionOps` for register access
- Bare field names in `VMSTATE` macros
- `hw/qdev-properties.h` for DeviceClass

## Data Models

The model layer (`src/autoemu/models/`) defines the core abstractions:

- **Register / BitField** - Memory-mapped registers with access types (RW, RO, WO, W1C, W0C, etc.)
- **RegisterBlock** - A group of registers at sequential offsets
- **Peripheral** - Complete peripheral model with registers, state machines, and interrupts
- **StateMachine** - Finite state machine with transitions triggered by register writes or internal events
- **InterruptModel** - IRQ lines with flag-to-event mappings and enable/clear semantics
- **DependencyGraph** - Cross-peripheral dependencies (DMA channels, clock gates, triggers)

## Testing

```bash
pytest
pytest tests/test_models.py -v
pytest -k "test_w1c" -v
```

## License

MIT
