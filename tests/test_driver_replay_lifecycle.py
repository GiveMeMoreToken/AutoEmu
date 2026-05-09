"""Tests for driver replay lifecycle and version comparison."""


from autoemu.models.peripheral import Peripheral
from autoemu.models.register import AccessType, Register, RegisterBlock
from autoemu.validators.driver_replay import (
    LifecycleStage,
    TraceEntry,
    compare_driver_versions,
    replay_lifecycle,
)


def _basic_peripheral() -> Peripheral:
    return Peripheral(
        name="TEST",
        register_block=RegisterBlock(
            name="TEST",
            registers=[
                Register(name="CR", offset=0x00, access=AccessType.RW, reset_value=0),
                Register(name="SR", offset=0x04, access=AccessType.RW, reset_value=0),
                Register(name="DR", offset=0x08, access=AccessType.RW, reset_value=0),
            ],
        ),
    )


class TestReplayLifecycle:
    def test_basic_init_operate(self):
        periph = _basic_peripheral()
        stages = [
            LifecycleStage(
                name="init",
                trace=[
                    TraceEntry(operation="write", offset=0x00, value=0x01),
                ],
                expected_state={0x00: 0x01, 0x04: 0x00},
            ),
            LifecycleStage(
                name="operate",
                trace=[
                    TraceEntry(operation="write", offset=0x08, value=0xABCD),
                    TraceEntry(operation="read", offset=0x08, expected=0xABCD),
                ],
                expected_state={0x00: 0x01, 0x08: 0xABCD},
            ),
        ]
        result = replay_lifecycle(periph, stages)
        assert len(result.stages) == 2
        assert result.total_mismatches == 0
        assert result.stages[0]["name"] == "init"
        assert result.stages[1]["name"] == "operate"
        assert result.stages[0]["state_mismatches"] == 0
        assert result.stages[1]["state_mismatches"] == 0

    def test_detects_state_mismatch(self):
        periph = _basic_peripheral()
        stages = [
            LifecycleStage(
                name="init",
                trace=[
                    TraceEntry(operation="write", offset=0x00, value=0x01),
                ],
                expected_state={0x00: 0xFF},  # Wrong expectation
            ),
        ]
        result = replay_lifecycle(periph, stages)
        assert result.total_mismatches >= 1
        assert result.stages[0]["state_mismatches"] == 1
        divs = result.stages[0]["state_divergences"]
        assert len(divs) == 1
        assert divs[0]["expected"] == 0xFF
        assert divs[0]["actual"] == 0x01

    def test_detects_read_mismatch(self):
        periph = _basic_peripheral()
        stages = [
            LifecycleStage(
                name="operate",
                trace=[
                    TraceEntry(operation="read", offset=0x00, expected=0x99),
                ],
                expected_state={},
            ),
        ]
        result = replay_lifecycle(periph, stages)
        # The read mismatch (expected 0x99, got 0x00) is counted
        assert result.total_mismatches >= 1

    def test_summary_string(self):
        periph = _basic_peripheral()
        stages = [
            LifecycleStage(
                name="init",
                trace=[TraceEntry(operation="write", offset=0x00, value=1)],
                expected_state={},
            ),
        ]
        result = replay_lifecycle(periph, stages)
        summary = result.summary()
        assert "Lifecycle replay" in summary
        assert "init" in summary

    def test_state_carries_across_stages(self):
        """Writes in stage 1 should be visible in stage 2."""
        periph = _basic_peripheral()
        stages = [
            LifecycleStage(
                name="init",
                trace=[TraceEntry(operation="write", offset=0x00, value=0x42)],
                expected_state={},
            ),
            LifecycleStage(
                name="operate",
                trace=[TraceEntry(operation="read", offset=0x00, expected=0x42)],
                expected_state={0x00: 0x42},
            ),
        ]
        result = replay_lifecycle(periph, stages)
        assert result.total_mismatches == 0


class TestCompareDriverVersions:
    def test_identical_versions(self):
        periph = _basic_peripheral()
        trace = [
            TraceEntry(operation="write", offset=0x00, value=0x01),
            TraceEntry(operation="write", offset=0x04, value=0x02),
        ]
        result = compare_driver_versions(periph, {
            "v1.0": trace,
            "v1.1": trace,
        })
        assert len(result["divergences"]) == 0
        assert "v1.0" in result["versions"]
        assert "v1.1" in result["versions"]

    def test_diverging_versions(self):
        periph = _basic_peripheral()
        trace_v1 = [
            TraceEntry(operation="write", offset=0x00, value=0x01),
        ]
        trace_v2 = [
            TraceEntry(operation="write", offset=0x00, value=0xFF),
        ]
        result = compare_driver_versions(periph, {
            "v1.0": trace_v1,
            "v2.0": trace_v2,
        })
        assert len(result["divergences"]) >= 1
        div = result["divergences"][0]
        assert div["offset"] == 0x00
        assert div["versions"]["v1.0"] == 0x01
        assert div["versions"]["v2.0"] == 0xFF

    def test_accuracy_reported(self):
        periph = _basic_peripheral()
        trace = [
            TraceEntry(operation="write", offset=0x00, value=0x01),
            TraceEntry(operation="read", offset=0x00, expected=0x01),
        ]
        result = compare_driver_versions(periph, {"v1.0": trace})
        assert result["versions"]["v1.0"]["accuracy"] == 1.0
        assert result["versions"]["v1.0"]["total_operations"] == 2
