"""Tests for fuzzing harness generator."""

from pathlib import Path

from autoemu.models.peripheral import Peripheral
from autoemu.models.register import AccessType, BitField, Register, RegisterBlock
from autoemu.models.state_machine import State, StateMachine, Transition
from autoemu.generators.fuzz_generator import (
    generate_fuzz_harness,
    _generate_register_fuzzer,
    _generate_state_fuzzer,
)


def _test_peripheral() -> Peripheral:
    return Peripheral(
        name="USART",
        register_block=RegisterBlock(
            name="USART",
            registers=[
                Register(name="CR1", offset=0x00, access=AccessType.RW),
                Register(name="CR2", offset=0x04, access=AccessType.RW),
                Register(
                    name="SR", offset=0x08, access=AccessType.RW,
                    fields=[
                        BitField(name="TXE", bit_offset=0, bit_width=1, access=AccessType.RO),
                        BitField(name="RXNE", bit_offset=1, bit_width=1, access=AccessType.RO),
                    ],
                ),
                Register(name="DR", offset=0x0C, access=AccessType.RW),
            ],
        ),
        state_machines=[
            StateMachine(
                name="tx",
                states=[
                    State(name="idle", is_initial=True),
                    State(name="transmitting"),
                ],
                transitions=[
                    Transition(source="idle", target="transmitting", trigger="reg_write:DR"),
                ],
            ),
        ],
    )


class TestGenerateRegisterFuzzer:
    def test_contains_llvm_fuzzer_entry(self):
        code = _generate_register_fuzzer(_test_peripheral())
        assert "LLVMFuzzerTestOneInput" in code

    def test_contains_read_write_calls(self):
        code = _generate_register_fuzzer(_test_peripheral())
        assert "stm32_usart_write" in code
        assert "stm32_usart_read" in code

    def test_contains_known_offsets(self):
        code = _generate_register_fuzzer(_test_peripheral())
        assert "0x0000" in code  # CR1
        assert "0x000C" in code  # DR

    def test_chunk_loop_present(self):
        code = _generate_register_fuzzer(_test_peripheral())
        assert "CHUNK_SIZE" in code
        assert "while" in code


class TestGenerateStateFuzzer:
    def test_contains_llvm_fuzzer_entry(self):
        code = _generate_state_fuzzer(_test_peripheral())
        assert "LLVMFuzzerTestOneInput" in code

    def test_contains_transition_registers(self):
        code = _generate_state_fuzzer(_test_peripheral())
        # DR is the trigger register for the state machine
        assert "DR" in code

    def test_contains_state_write_pattern(self):
        code = _generate_state_fuzzer(_test_peripheral())
        assert "stm32_usart_write" in code
        assert "stm32_usart_read" in code


class TestGenerateFuzzHarness:
    def test_creates_files(self, tmp_path: Path):
        periph = _test_peripheral()
        files = generate_fuzz_harness(periph, tmp_path)
        assert len(files) == 2
        names = {Path(f).name for f in files}
        assert names == {"fuzz_usart_regs.c", "fuzz_usart_states.c"}
        for f in files:
            assert Path(f).exists()

    def test_register_fuzzer_is_valid_c(self, tmp_path: Path):
        periph = _test_peripheral()
        files = generate_fuzz_harness(periph, tmp_path)
        reg_file = [f for f in files if "regs" in f][0]
        content = Path(reg_file).read_text()
        # Basic C validity checks
        assert "#include" in content
        assert "return 0;" in content
        assert content.count("{") == content.count("}")

    def test_state_fuzzer_is_valid_c(self, tmp_path: Path):
        periph = _test_peripheral()
        files = generate_fuzz_harness(periph, tmp_path)
        state_file = [f for f in files if "states" in f][0]
        content = Path(state_file).read_text()
        assert "#include" in content
        assert "return 0;" in content
        assert content.count("{") == content.count("}")
