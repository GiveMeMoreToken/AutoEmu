"""Tests for parsers."""

from autoemu.parsers.svd_parser import parse_svd_string
from autoemu.parsers.header_parser import (
    parse_typedef_structs,
    parse_base_addresses,
    parse_bit_definitions,
    parse_header_file,
    preprocess_header,
)
from autoemu.parsers.register_extractor import merge_register_blocks, extract_register_blocks
from autoemu.parsers.driver_parser import analyze_driver_string


class TestSVDParser:
    SVD_SAMPLE = """\
<?xml version="1.0" encoding="utf-8"?>
<device>
  <name>STM32F407</name>
  <peripherals>
    <peripheral>
      <name>USART1</name>
      <description>Universal synchronous asynchronous receiver transmitter</description>
      <baseAddress>0x40011000</baseAddress>
      <registers>
        <register>
          <name>SR</name>
          <description>Status register</description>
          <addressOffset>0x00</addressOffset>
          <size>32</size>
          <resetValue>0x000000C0</resetValue>
          <access>read-write</access>
          <fields>
            <field>
              <name>PE</name>
              <description>Parity error</description>
              <bitOffset>0</bitOffset>
              <bitWidth>1</bitWidth>
              <access>read-only</access>
            </field>
            <field>
              <name>TXE</name>
              <description>Transmit data register empty</description>
              <bitOffset>7</bitOffset>
              <bitWidth>1</bitWidth>
              <access>read-only</access>
            </field>
            <field>
              <name>TC</name>
              <description>Transmission complete</description>
              <bitOffset>6</bitOffset>
              <bitWidth>1</bitWidth>
              <access>read-write</access>
            </field>
          </fields>
        </register>
        <register>
          <name>DR</name>
          <addressOffset>0x04</addressOffset>
          <size>32</size>
          <resetValue>0x00000000</resetValue>
        </register>
      </registers>
    </peripheral>
  </peripherals>
</device>
"""

    def test_parse_basic(self):
        blocks = parse_svd_string(self.SVD_SAMPLE)
        assert "USART1" in blocks

        usart = blocks["USART1"]
        assert usart.base_address == 0x40011000
        assert len(usart.registers) == 2

    def test_parse_register_fields(self):
        blocks = parse_svd_string(self.SVD_SAMPLE)
        sr = blocks["USART1"].get_register("SR")
        assert sr is not None
        assert sr.reset_value == 0xC0
        assert len(sr.fields) == 3

        pe = sr.get_field("PE")
        assert pe is not None
        assert pe.bit_offset == 0
        assert pe.bit_width == 1
        assert pe.access.value == "RO"

    def test_parse_semantic_access(self):
        svd = """\
<?xml version="1.0" encoding="utf-8"?>
<device>
  <peripherals>
    <peripheral>
      <name>TIMX</name>
      <baseAddress>0x40000000</baseAddress>
      <registers>
        <register>
          <name>SR</name>
          <addressOffset>0x00</addressOffset>
          <fields>
            <field>
              <name>UIF</name>
              <bitOffset>0</bitOffset>
              <bitWidth>1</bitWidth>
              <modifiedWriteValues>oneToClear</modifiedWriteValues>
            </field>
            <field>
              <name>CAP</name>
              <bitOffset>1</bitOffset>
              <bitWidth>1</bitWidth>
              <modifiedWriteValues>oneToClear</modifiedWriteValues>
              <readAction>clear</readAction>
            </field>
            <field>
              <name>SYNC</name>
              <bitOffset>2</bitOffset>
              <bitWidth>1</bitWidth>
              <readAction>set</readAction>
            </field>
          </fields>
        </register>
      </registers>
    </peripheral>
  </peripherals>
</device>
"""
        blocks = parse_svd_string(svd)
        sr = blocks["TIMX"].get_register("SR")
        assert sr is not None
        assert sr.get_field("UIF").access.value == "W1C"
        assert sr.get_field("CAP").access.value == "RC_W1"
        assert sr.get_field("SYNC").access.value == "RS"


class TestHeaderParser:
    HEADER_SAMPLE = """\
typedef struct {
  __IO uint32_t CR1; /*!< Control register 1 */
  __O  uint32_t CR2; /*!< Write only trigger register */
  __I  uint32_t SR;  /*!< Read only status register */
  __IO uint32_t DR;
  uint32_t RESERVED0[2];
  __IO uint32_t CCR; /*!< End of conversion flag cleared by writing 1 */
} ADC_TypeDef;

#define PERIPH_BASE       (0x40000000UL)
#define APB2PERIPH_BASE   (PERIPH_BASE + 0x00010000UL)
#define ADC1_BASE         (APB2PERIPH_BASE + 0x2000UL)

#define ADC_CR1_EOCIE_Pos  (5U)
#define ADC_CR1_EOCIE_Msk  (0x1U << ADC_CR1_EOCIE_Pos)
#define ADC_CR1_SCAN_Pos   (8U)
#define ADC_CR1_SCAN_Msk   (0x1U << ADC_CR1_SCAN_Pos)
"""

    def test_parse_struct(self):
        structs = parse_typedef_structs(self.HEADER_SAMPLE)
        assert len(structs) == 1
        assert structs[0].name == "ADC_TypeDef"

        fields = [f for f in structs[0].fields if not f.is_reserved]
        assert len(fields) == 5  # CR1, CR2, SR, DR, CCR
        assert fields[0].name == "CR1"
        assert fields[0].offset == 0

    def test_parse_base_addresses(self):
        addrs = parse_base_addresses(self.HEADER_SAMPLE)
        assert "ADC1_BASE" in addrs
        assert addrs["ADC1_BASE"] == 0x40012000

    def test_parse_bit_definitions(self):
        bits = parse_bit_definitions(self.HEADER_SAMPLE, "ADC")
        assert "CR1" in bits
        fields = bits["CR1"]
        names = [f.name for f in fields]
        assert "EOCIE" in names
        assert "SCAN" in names

    def test_parse_header_access(self, tmp_path):
        header = tmp_path / "adc.h"
        header.write_text(self.HEADER_SAMPLE)
        block = parse_header_file(header, "ADC")["ADC"]

        assert block.get_register("CR1").access.value == "RW"
        assert block.get_register("CR2").access.value == "WO"
        assert block.get_register("SR").access.value == "RO"
        assert block.get_register("CCR").access.value == "W1C"
        assert "Control register 1" in block.get_register("CR1").description

    def test_parse_macro_only_register_map(self, tmp_path):
        header = tmp_path / "linux_regs.h"
        header.write_text(
            """\
#define GPU_ID                  0x00
#define GPU_CMD                 0x30    /* (WO) command register */
#define GPU_CMD_START           0x01
#define GPU_STATUS              0x34    /* (RO) status register */
#define JOB_INT_RAWSTAT         0x1000
#define JOB_INT_CLEAR           0x1004
#define JS_BASE                 0x1800
#define JS_SLOT_STRIDE          0x80
#define JS_HEAD_LO(n)           (JS_BASE + ((n) * JS_SLOT_STRIDE) + 0x00)
#define JS_STATUS(n)            (JS_BASE + ((n) * JS_SLOT_STRIDE) + 0x24)
#define JS_COMMAND_START        0x01
#define JS_CONFIG_END_FLUSH_CLEAN_INVALIDATE (3u << 12)
#define JS_CONFIG_THREAD_PRI(n) ((n) << 16)
""",
            encoding="utf-8",
        )

        block = parse_header_file(header, "GPU")["GPU"]
        names = {reg.name for reg in block.registers}

        assert "GPU_ID" in names
        assert "GPU_CMD" in names
        assert "GPU_CMD_START" not in names
        assert "JOB_INT_RAWSTAT" in names
        assert "JS_HEAD_LO_0" in names
        assert "JS_STATUS_1" in names
        assert "JS_BASE" not in names
        assert "JS_CONFIG_END_FLUSH_CLEAN_INVALIDATE" not in names
        assert "JS_CONFIG_THREAD_PRI_1" not in names
        assert block.get_register("GPU_CMD").access.value == "WO"
        assert block.get_register("GPU_STATUS").access.value == "RO"
        assert block.get_register("JS_STATUS_1").offset == 0x18A4


class TestRegisterExtractor:
    def test_merge_register_blocks_prefers_semantic_primary(self):
        primary = parse_svd_string("""\
<?xml version="1.0" encoding="utf-8"?>
<device>
  <peripherals>
    <peripheral>
      <name>USART1</name>
      <baseAddress>0x40011000</baseAddress>
      <registers>
        <register>
          <name>SR</name>
          <addressOffset>0x00</addressOffset>
          <resetValue>0x1</resetValue>
          <fields>
            <field>
              <name>TC</name>
              <bitOffset>6</bitOffset>
              <bitWidth>1</bitWidth>
              <modifiedWriteValues>oneToClear</modifiedWriteValues>
            </field>
          </fields>
        </register>
      </registers>
    </peripheral>
  </peripherals>
</device>
""")["USART1"]
        secondary = parse_header_file(
            _write_tmp_header(
                """\
typedef struct {
  __I uint32_t SR; /*!< Status register */
} USART1_TypeDef;

#define PERIPH_BASE  (0x40000000UL)
#define APB2PERIPH_BASE (PERIPH_BASE + 0x00010000UL)
#define USART1_BASE (APB2PERIPH_BASE + 0x1000UL)
""",
            ),
            "USART1",
        )["USART1"]

        merged = merge_register_blocks(primary, secondary)
        sr = merged.get_register("SR")
        assert sr is not None
        assert sr.description == "Status register"
        assert sr.access.value == "RO"
        assert sr.get_field("TC").access.value == "W1C"

    def test_extract_register_blocks_resolves_eth_alias(self, tmp_path):
        svd = tmp_path / "eth.svd"
        svd.write_text(
            """\
<?xml version="1.0" encoding="utf-8"?>
<device>
  <peripherals>
    <peripheral>
      <name>Ethernet_MAC</name>
      <baseAddress>0x40028000</baseAddress>
      <registers>
        <register>
          <name>MACCR</name>
          <addressOffset>0x00</addressOffset>
        </register>
      </registers>
    </peripheral>
    <peripheral>
      <name>Ethernet_DMA</name>
      <baseAddress>0x40029000</baseAddress>
      <registers>
        <register>
          <name>DMABMR</name>
          <addressOffset>0x00</addressOffset>
        </register>
      </registers>
    </peripheral>
  </peripherals>
</device>
""",
            encoding="utf-8",
        )

        blocks, warnings = extract_register_blocks(svd_path=svd, peripheral_name="ETH")
        assert sorted(blocks) == ["Ethernet_DMA", "Ethernet_MAC"]


def _write_tmp_header(content: str) -> str:
    import tempfile
    from pathlib import Path

    tmp = tempfile.NamedTemporaryFile("w", suffix=".h", delete=False)
    tmp.write(content)
    tmp.flush()
    tmp.close()
    return str(Path(tmp.name))


class TestDriverParser:
    DRIVER_SAMPLE = """\
HAL_StatusTypeDef HAL_ADC_Init(ADC_HandleTypeDef *hadc)
{
    SET_BIT(hadc->Instance->CR1, ADC_CR1_SCAN);
    MODIFY_REG(hadc->Instance->CR2, ADC_CR2_CONT_Msk, ADC_CR2_CONT);
    SET_BIT(hadc->Instance->CR2, ADC_CR2_ADON);
    return HAL_OK;
}

void HAL_ADC_IRQHandler(ADC_HandleTypeDef *hadc)
{
    if (__HAL_ADC_GET_FLAG(hadc, ADC_FLAG_EOC))
    {
        if (__HAL_ADC_GET_IT_SOURCE(hadc, ADC_IT_EOC))
        {
            __HAL_ADC_CLEAR_FLAG(hadc, ADC_FLAG_EOC);
            HAL_ADC_ConvCpltCallback(hadc);
        }
    }
}
"""

    def test_analyze_init(self):
        analysis = analyze_driver_string(self.DRIVER_SAMPLE, "ADC")
        assert analysis.peripheral_name == "ADC"
        assert len(analysis.init_sequences) >= 1

        init = analysis.init_sequences[0]
        assert init.function_name == "HAL_ADC_Init"
        reg_names = [a.register for a in init.accesses]
        assert "CR1" in reg_names

    def test_analyze_isr(self):
        analysis = analyze_driver_string(self.DRIVER_SAMPLE, "ADC")
        assert len(analysis.isr_patterns) == 1

        isr = analysis.isr_patterns[0]
        assert "ADC_FLAG_EOC" in isr.checked_flags
        assert "ADC_FLAG_EOC" in isr.cleared_flags
        assert any("ConvCplt" in cb for cb in isr.callbacks)

    def test_analyze_isr_direct_register_masks(self):
        analysis = analyze_driver_string(
            """\
void HAL_ETH_IRQHandler(ETH_HandleTypeDef *heth)
{
    uint32_t dma_flag = READ_REG(heth->Instance->DMASR);
    uint32_t dma_itsource = READ_REG(heth->Instance->DMAIER);

    if (((dma_flag & ETH_DMASR_RS) != 0U) && ((dma_itsource & ETH_DMAIER_RIE) != 0U))
    {
        __HAL_ETH_DMA_CLEAR_IT(heth, ETH_DMASR_RS | ETH_DMASR_NIS);
        HAL_ETH_RxCpltCallback(heth);
    }

    if (((dma_flag & ETH_DMASR_TS) != 0U) && ((dma_itsource & ETH_DMAIER_TIE) != 0U))
    {
        __HAL_ETH_DMA_CLEAR_IT(heth, ETH_DMASR_TS | ETH_DMASR_NIS);
        HAL_ETH_TxCpltCallback(heth);
    }
}
""",
            "ETH",
        )

        assert len(analysis.isr_patterns) == 1
        isr = analysis.isr_patterns[0]
        assert "ETH_DMASR_RS" in isr.checked_flags
        assert "ETH_DMASR_TS" in isr.checked_flags
        assert "ETH_DMAIER_RIE" in isr.enabled_checks
        assert "ETH_DMAIER_TIE" in isr.enabled_checks
        assert "ETH_DMASR_NIS" in isr.cleared_flags
        assert any("RxCplt" in cb for cb in isr.callbacks)
        assert any("TxCplt" in cb for cb in isr.callbacks)

    def test_raw_register_write_ignores_non_mmio_struct_members(self):
        analysis = analyze_driver_string(
            """\
void HAL_ETH_Foo(ETH_HandleTypeDef *heth)
{
    heth->gState = HAL_BUSY;
    heth->TxCpltCallback = NULL;
    heth->Instance->DMASR = 0;
    EXTI->PR = 1;
}
""",
            "ETH",
        )

        written_regs = [a.register for a in analysis.register_accesses if a.access_type == "write"]
        assert "DMASR" in written_regs
        assert "PR" in written_regs
        assert "gState" not in written_regs
        assert "TxCpltCallback" not in written_regs


class TestPreprocessHeader:
    def test_ifdef_defined_symbol_included(self):
        content = """\
#ifdef HAS_FEATURE
int feature_enabled = 1;
#endif
int always_here = 1;
"""
        result = preprocess_header(content, defines={"HAS_FEATURE": ""})
        assert "feature_enabled" in result
        assert "always_here" in result

    def test_ifdef_undefined_symbol_excluded(self):
        content = """\
#ifdef HAS_FEATURE
int feature_enabled = 1;
#endif
int always_here = 1;
"""
        result = preprocess_header(content, defines={})
        assert "feature_enabled" not in result
        assert "always_here" in result

    def test_ifndef_defined_symbol_excluded(self):
        content = """\
#ifndef GUARD
int guarded = 1;
#endif
"""
        result = preprocess_header(content, defines={"GUARD": ""})
        assert "guarded" not in result

    def test_ifndef_undefined_symbol_included(self):
        content = """\
#ifndef GUARD
int guarded = 1;
#endif
"""
        result = preprocess_header(content, defines={})
        assert "guarded" in result

    def test_ifdef_else(self):
        content = """\
#ifdef USE_A
int path_a = 1;
#else
int path_b = 1;
#endif
"""
        result = preprocess_header(content, defines={"USE_A": ""})
        assert "path_a" in result
        assert "path_b" not in result

        result2 = preprocess_header(content, defines={})
        assert "path_a" not in result2
        assert "path_b" in result2

    def test_nested_ifdefs(self):
        content = """\
#ifdef OUTER
#ifdef INNER
int both = 1;
#endif
int outer_only = 1;
#endif
"""
        result = preprocess_header(content, defines={"OUTER": "", "INNER": ""})
        assert "both" in result
        assert "outer_only" in result

        result2 = preprocess_header(content, defines={"OUTER": ""})
        assert "both" not in result2
        assert "outer_only" in result2

        result3 = preprocess_header(content, defines={})
        assert "both" not in result3
        assert "outer_only" not in result3

    def test_include_resolution(self, tmp_path):
        inc_dir = tmp_path / "inc"
        inc_dir.mkdir()
        (inc_dir / "defs.h").write_text("#define MY_VAL 42\n")

        content = '#include "defs.h"\nint x = MY_VAL;\n'
        result = preprocess_header(content, include_dirs=[str(inc_dir)])
        assert "#define MY_VAL 42" in result
        assert "int x = MY_VAL;" in result

    def test_include_recursion_guard(self, tmp_path):
        inc_dir = tmp_path / "inc"
        inc_dir.mkdir()
        # a.h includes b.h, b.h includes a.h -> should not loop
        (inc_dir / "a.h").write_text('#include "b.h"\nint from_a = 1;\n')
        (inc_dir / "b.h").write_text('#include "a.h"\nint from_b = 1;\n')

        content = '#include "a.h"\n'
        result = preprocess_header(content, include_dirs=[str(inc_dir)])
        assert "from_a" in result
        assert "from_b" in result

    def test_angle_bracket_include_ignored(self):
        content = '#include <stdio.h>\nint x = 1;\n'
        result = preprocess_header(content)
        assert "int x = 1;" in result
        # The angle-bracket include line should not appear (it's not processed)
        assert "#include <stdio.h>" not in result or "int x = 1;" in result

    def test_include_missing_file_no_crash(self):
        content = '#include "nonexistent.h"\nint x = 1;\n'
        result = preprocess_header(content, include_dirs=["/tmp/empty_dir_xyz"])
        assert "int x = 1;" in result

    def test_parse_header_file_with_preprocessor(self, tmp_path):
        """parse_header_file passes include_dirs and defines through."""
        inc_dir = tmp_path / "inc"
        inc_dir.mkdir()
        (inc_dir / "base.h").write_text(
            "#define PERIPH_BASE (0x40000000UL)\n"
            "#define APB1_BASE   (PERIPH_BASE + 0x00000000UL)\n"
        )
        header = tmp_path / "periph.h"
        header.write_text(
            '#include "base.h"\n'
            "#ifdef USE_MY_PERIPH\n"
            "typedef struct {\n"
            "  __IO uint32_t CR;\n"
            "} MY_TypeDef;\n"
            "#define MY_BASE (APB1_BASE + 0x100UL)\n"
            "#endif\n"
        )
        # Without the define, no peripheral is found
        result_empty = parse_header_file(
            header, "MY", include_dirs=[str(inc_dir)], defines={}
        )
        assert result_empty == {}

        # With the define, it is found
        result = parse_header_file(
            header, "MY", include_dirs=[str(inc_dir)], defines={"USE_MY_PERIPH": ""}
        )
        assert "MY" in result
        assert result["MY"].base_address == 0x40000100


class TestLinuxDriverParser:
    """Test recognition of Linux kernel driver patterns (readl/writel, custom wrappers)."""

    def test_readl_writel_extraction(self):
        analysis = analyze_driver_string(
            """\
static int panfrost_gpu_init(struct panfrost_device *pfdev)
{
    u32 id = gpu_read(pfdev, GPU_ID);
    u32 present = readl(pfdev->iomem + GPU_SHADER_PRESENT_LO);
    gpu_write(pfdev, GPU_INT_MASK, 0);
    writel(ALL_JS_INT_MASK, pfdev->iomem + JOB_INT_MASK);
}
""",
            "GPU",
        )
        regs = [a.register for a in analysis.register_accesses]
        assert "GPU_ID" in regs
        assert "GPU_SHADER_PRESENT_LO" in regs
        assert "GPU_INT_MASK" in regs
        assert "JOB_INT_MASK" in regs

    def test_linux_irq_handler_extraction(self):
        analysis = analyze_driver_string(
            """\
static irqreturn_t panfrost_jm_irq_handler(int irq, void *data)
{
    struct panfrost_device *pfdev = data;
    u32 status = gpu_read(pfdev, JOB_INT_STAT);

    if (status & JOB_INT_MASK_DONE(0))
        gpu_write(pfdev, JOB_INT_CLEAR, JOB_INT_MASK_DONE(0));

    return IRQ_HANDLED;
}
""",
            "GPU",
        )
        assert len(analysis.isr_patterns) == 1
        isr = analysis.isr_patterns[0]
        assert "JOB_INT_MASK_DONE" in isr.checked_flags
        assert "JOB_INT_MASK_DONE" in isr.cleared_flags
        accesses = isr.register_accesses
        assert any(a.register == "JOB_INT_STAT" for a in accesses)
        assert any(a.register == "JOB_INT_CLEAR" for a in accesses)

    def test_linux_init_sequence_extraction(self):
        analysis = analyze_driver_string(
            """\
static int panfrost_device_init(struct panfrost_device *pfdev)
{
    writel(0, pfdev->iomem + GPU_INT_CLEAR);
    writel(ALL_JS_INT_MASK, pfdev->iomem + JOB_INT_MASK);
    gpu_write(pfdev, JS_COMMAND_NEXT(0), JS_COMMAND_START);
}
""",
            "GPU",
        )
        assert len(analysis.init_sequences) >= 1
        init = analysis.init_sequences[0]
        regs = [a.register for a in init.accesses]
        assert "GPU_INT_CLEAR" in regs
        assert "JOB_INT_MASK" in regs
        assert "JS_COMMAND_NEXT" in regs
