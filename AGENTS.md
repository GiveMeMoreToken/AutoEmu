# AGENTS.md — AutoEmu Repository Constraints

## Source Policy

All fetched artifacts must come from **trustworthy, official sources**:

- **Reference manuals and datasheets**: Only from `st.com` or official ST mirror domains.
- **CMSIS-SVD files**: Only from `github.com/modm-io` or `github.com/STMicroelectronics` repositories.
- **CMSIS device headers**: Only from `github.com/STMicroelectronics/cmsis-device-*` repositories.
- **HAL/LL driver sources**: Only from `github.com/STMicroelectronics/*-hal-driver` repositories.
- **RTOS adaptation layers**: Only from official RTOS project repositories (e.g., `github.com/zephyrproject-rtos`).

**Never** fabricate URLs, file contents, or artifact metadata. If a source cannot be found, report it as `unresolved` in the manifest.

## Fetch Constraints

- Each fetch run must produce a manifest JSON under `data/<platform>/manifests/`.
- Artifacts must be verified with SHA256 hashes recorded in the manifest.
- The fetcher must support `--offline` mode that refuses network access and uses only cached data.
- When an optional input (SVD, LL driver, reference manual) is unavailable, the pipeline must degrade gracefully rather than abort.

## Modeling Constraints

- All inference modules must return valid-but-empty models when inputs are insufficient — never raise exceptions that abort the pipeline.
- Generated QEMU code must target **QEMU v9.2.4** exclusively:
  - Use `device_class_set_legacy_reset()` (not `dc->reset`)
  - Use `OBJECT_DECLARE_SIMPLE_TYPE` for type declarations
  - Use `MemoryRegionOps` for register read/write handlers
  - Use bare field names in `VMSTATE` macros (not `s->field`)
  - Include `hw/qdev-properties.h` for `DeviceClass`
- Register access semantics (W1C, RC_W1, W1S, W0C, RO, RSVD) must be modeled precisely.
- Validation must run automatically after generation — structural, behavioral, and replay checks.

## Output Conventions

- All generated artifacts go to `output/<peripheral>/` by default.
- JSON model files use 2-space indentation.
- C code follows QEMU coding style (4-space indent, snake_case identifiers).
- File naming: `stm32_<peripheral>.c`, `stm32_<peripheral>.h`, `qtest_stm32_<peripheral>.c`.

## Testing

- Unit tests use `pytest` with `pytest-asyncio` (`asyncio_mode = "auto"`).
- Integration tests use the `@pytest.mark.integration` marker and are separated from fast unit tests.
- All tests must pass before any generated code is considered valid.
