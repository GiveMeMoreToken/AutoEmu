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

    def test_parse_direct_macro_register_offsets_with_access_comments(self, tmp_path):
        header = tmp_path / "gpu_regs.h"
        header.write_text(
            """\
#define GPU_ID              0x0000 /* (RO) GPU identity */
#define GPU_IRQ_CLR         0x0020 /* (WO) interrupt clear alias */
#define GPU_IRQ_CLEAR       0x0024 /* (WO) interrupt clear register */
#define GPU_COMMAND         0x0030 /* write-only command register */
#define GPU_STATUS          0x0034 /* read-only status register */
#define GPU_IRQ_MASK        BIT(1)
#define GPU_CMD_SOFT_RESET  0x01
"""
        )

        block = parse_header_file(header, "GPU")["GPU"]

        assert [reg.name for reg in block.registers] == [
            "GPU_ID",
            "GPU_IRQ_CLR",
            "GPU_IRQ_CLEAR",
            "GPU_COMMAND",
            "GPU_STATUS",
        ]
        assert block.get_register("GPU_ID").offset == 0x0000
        assert block.get_register("GPU_ID").access.value == "RO"
        assert block.get_register("GPU_IRQ_CLR").access.value == "WO"
        assert block.get_register("GPU_IRQ_CLEAR").access.value == "WO"
        assert block.get_register("GPU_COMMAND").access.value == "WO"
        assert block.get_register("GPU_STATUS").access.value == "RO"
        assert block.get_register("GPU_IRQ_MASK") is None
        assert block.get_register("GPU_CMD_SOFT_RESET") is None

    def test_parse_macro_only_header_includes_related_unprefixed_families(self, tmp_path):
        header = tmp_path / "gpu_regs.h"
        header.write_text(
            """\
#define GPU_ID          0x0000 /* (RO) GPU identity */
#define GPU_STATUS      0x0034
#define JS_BASE         0x1000
#define JS_SLOT_STRIDE  0x80
#define JS_HEAD_LO(n)   (JS_BASE + ((n) * JS_SLOT_STRIDE) + 0x00) /* (RW) queue head */
#define JS_HEAD_HI(n)   (JS_BASE + ((n) * JS_SLOT_STRIDE) + 0x04)
"""
        )

        block = parse_header_file(header, "GPU")["GPU"]

        assert [reg.name for reg in block.registers] == [
            "GPU_ID",
            "GPU_STATUS",
            "JS_HEAD_LO0",
            "JS_HEAD_HI0",
            "JS_HEAD_LO1",
            "JS_HEAD_HI1",
            "JS_HEAD_LO2",
            "JS_HEAD_HI2",
            "JS_HEAD_LO3",
            "JS_HEAD_HI3",
        ]
        assert block.get_register("JS_HEAD_LO0").offset == 0x1000
        assert block.get_register("JS_HEAD_HI0").offset == 0x1004
        assert block.get_register("JS_HEAD_LO3").offset == 0x1180
        assert block.get_register("JS_HEAD_LO0").access.value == "RW"

    def test_macro_register_parser_merges_legacy_and_direct_offsets(self, tmp_path):
        header = tmp_path / "mixed_regs.h"
        header.write_text(
            """\
#define PERIPH_BASE  (0x40000000UL)
#define GPU_BASE     (PERIPH_BASE + 0x1000UL)
#define GPU_STATUS_LEGACY (GPU_BASE + 0x0034U) /* (RO) legacy status */
#define GPU_ID       0x0000 /* (RO) identity register */
#define GPU_CONTROL  0x0030 /* (RW) control register */
"""
        )

        block = parse_header_file(header, "GPU")["GPU"]

        assert block.base_address == 0x40001000
        assert block.get_register("STATUS_LEGACY").offset == 0x0034
        assert block.get_register("STATUS_LEGACY").access.value == "RO"
        assert block.get_register("GPU_ID").offset == 0x0000
        assert block.get_register("GPU_CONTROL").offset == 0x0030
        assert block.get_register("GPU_STATUS_LEGACY") is None

    def test_macro_register_parser_filters_aligned_values_and_masks(self, tmp_path):
        header = tmp_path / "gpu_regs.h"
        header.write_text(
            """\
#define GPU_ID                 0x0000 /* (RO) identity register */
#define GPU_STATUS             0x0034 /* status register */
#define GPU_CONFIG_MODE_MANUAL 0x0004
#define GPU_ALL_BITS           ~0
#define GPU_MODE_FLAGS         0xffff
"""
        )

        block = parse_header_file(header, "GPU")["GPU"]

        assert [reg.name for reg in block.registers] == ["GPU_ID", "GPU_STATUS"]
        assert block.get_register("GPU_CONFIG_MODE_MANUAL") is None
        assert block.get_register("GPU_ALL_BITS") is None
        assert block.get_register("GPU_MODE_FLAGS") is None

    def test_macro_parser_handles_hex_suffixes_and_excludes_unrelated_blocks(self, tmp_path):
        header = tmp_path / "mixed_soc_regs.h"
        header.write_text(
            """\
#define GPU_ID          0x0000U /* (RO) identity register */
#define GPU_STATUS      0x000CU /* status register */
#define JS_BASE         0x1000U
#define JS_SLOT_STRIDE  0x80U
#define JS_HEAD_LO(n)   (JS_BASE + ((n) * JS_SLOT_STRIDE) + 0x00U)
#define USB_CTRL        0x2000U
#define USB_BASE        0x2000U
#define USB_STRIDE      0x20U
#define USB_CTRL_INDEX(n) (USB_BASE + ((n) * USB_STRIDE) + 0x00U)
#define CLK_GATE        0x3000U
#define CLK_BASE        0x3000U
#define CLK_STRIDE      0x10U
#define CLK_GATE_INDEX(n) (CLK_BASE + ((n) * CLK_STRIDE) + 0x00U)
"""
        )

        block = parse_header_file(header, "GPU")["GPU"]

        assert block.get_register("GPU_STATUS").offset == 0x000C
        assert block.get_register("JS_HEAD_LO0").offset == 0x1000
        assert block.get_register("USB_CTRL") is None
        assert block.get_register("USB_CTRL_INDEX0") is None
        assert block.get_register("CLK_GATE") is None
        assert block.get_register("CLK_GATE_INDEX0") is None

    def test_macro_expression_evaluator_rejects_expensive_expressions(self, tmp_path):
        header = tmp_path / "gpu_regs.h"
        long_expr = " + ".join(["1"] * 1000)
        header.write_text(
            f"""\
#define GPU_ID       0x0000U
#define GPU_HUGE     (1 << 128)
#define GPU_DEEP     ({long_expr})
"""
        )

        block = parse_header_file(header, "GPU")["GPU"]

        assert block.get_register("GPU_ID") is not None
        assert block.get_register("GPU_HUGE") is None
        assert block.get_register("GPU_DEEP") is None

    def test_macro_parser_rejects_lowercase_suffixed_unaligned_values(self, tmp_path):
        header = tmp_path / "gpu_regs.h"
        header.write_text(
            """\
#define GPU_ID       0x0000u
#define GPU_FLAG     0x1u
"""
        )

        block = parse_header_file(header, "GPU")["GPU"]

        assert block.get_register("GPU_ID") is not None
        assert block.get_register("GPU_FLAG") is None

    def test_macro_parser_excludes_ambiguous_indexed_families(self, tmp_path):
        header = tmp_path / "mixed_soc_regs.h"
        header.write_text(
            """\
#define GPU_ID          0x0000U
#define GPU_STATUS      0x000CU
#define JS_BASE         0x1000U
#define JS_SLOT_STRIDE  0x80U
#define JS_HEAD_LO(n)   (JS_BASE + ((n) * JS_SLOT_STRIDE) + 0x00U)
#define DMA_BASE        0x2000U
#define DMA_STRIDE      0x40U
#define DMA_CH_CFG(n)   (DMA_BASE + ((n) * DMA_STRIDE) + 0x00U)
"""
        )

        block = parse_header_file(header, "GPU")["GPU"]

        assert block.get_register("DMA_CH_CFG0") is None
        assert block.get_register("JS_HEAD_LO0") is None


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

    def test_generic_linux_direct_mmio_accesses(self):
        analysis = analyze_driver_string(
            """\
static int foo_probe(struct platform_device *pdev)
{
    void __iomem *base = devm_platform_ioremap_resource(pdev, 0);
    u32 status;
    int ret;

    writel(FOO_ENABLE | mode, base + FOO_CTRL);
    status = readl(base + FOO_STATUS);
    ret = readl_poll_timeout(base + FOO_READY, status,
                             status & READY_BIT, 10, 1000);
    return ret;
}

static irqreturn_t foo_irq_handler(int irq, void *data)
{
    struct foo_dev *foo = data;
    u32 status = readl(foo->base + FOO_IRQ_STATUS);

    writel(status, foo->base + FOO_IRQ_CLEAR);
    return IRQ_HANDLED;
}
""",
            "FOO",
        )

        by_register = {a.register: a for a in analysis.register_accesses}
        assert by_register["FOO_CTRL"].access_type == "write"
        assert by_register["FOO_CTRL"].value_expr == "FOO_ENABLE | mode"
        assert by_register["FOO_CTRL"].in_function == "foo_probe"
        assert by_register["FOO_STATUS"].access_type == "read"
        assert by_register["FOO_READY"].access_type == "read"
        assert by_register["FOO_IRQ_STATUS"].context == "isr"
        assert by_register["FOO_IRQ_CLEAR"].in_function == "foo_irq_handler"

    def test_generic_linux_direct_mmio_preserves_function_like_register_macro(self):
        analysis = analyze_driver_string(
            """\
static u32 foo_read_indexed(struct foo_dev *foo, int idx)
{
    return readl(foo->base + FOO_REG(idx));
}
""",
            "FOO",
        )

        reads = [a for a in analysis.register_accesses if a.access_type == "read"]
        assert len(reads) == 1
        assert reads[0].register == "FOO_REG(idx)"
        assert reads[0].in_function == "foo_read_indexed"

    def test_generic_linux_mmio_wrapper_macros(self):
        analysis = analyze_driver_string(
            """\
#define gpu_write(dev, reg, data) writel(data, dev->iomem + reg)
#define gpu_read(dev, reg) readl(dev->iomem + reg)
#define job_write(dev, reg, data) writel(data, dev->iomem + (reg))

static void foo_reset(struct foo_dev *foo)
{
    u32 status;

    gpu_write(foo, FOO_CTRL, CTRL_RESET);
    status = gpu_read(foo, FOO_STATUS);
    job_write(foo, JOB_CONTROL, start_value);
}
""",
            "FOO",
        )

        by_register = {a.register: a for a in analysis.register_accesses}
        assert by_register["FOO_CTRL"].access_type == "write"
        assert by_register["FOO_CTRL"].value_expr == "CTRL_RESET"
        assert by_register["FOO_CTRL"].in_function == "foo_reset"
        assert by_register["FOO_STATUS"].access_type == "read"
        assert by_register["JOB_CONTROL"].access_type == "write"
        assert by_register["JOB_CONTROL"].value_expr == "start_value"

    def test_generic_linux_read_modify_write_wrapper_emits_read_and_write(self):
        analysis = analyze_driver_string(
            """\
#define foo_rmw(dev, reg, mask, val) \\
    do { \\
        u32 tmp = readl((dev)->base + (reg)); \\
        tmp &= ~(mask); \\
        tmp |= (val); \\
        writel(tmp, (dev)->base + (reg)); \\
    } while (0)

static void foo_config(struct foo_dev *foo)
{
    foo_rmw(foo, FOO_CONFIG, FOO_MASK, FOO_VALUE);
}
""",
            "FOO",
        )

        config_accesses = [
            a for a in analysis.register_accesses if a.register == "FOO_CONFIG"
        ]
        assert [a.access_type for a in config_accesses].count("read") == 1
        assert [a.access_type for a in config_accesses].count("write") == 1
        assert all(a.in_function == "foo_config" for a in config_accesses)

    def test_generic_linux_multiline_mmio_wrapper_macro(self):
        analysis = analyze_driver_string(
            """\
#define foo_write(dev, reg, val) \\
    writel((val), (dev)->base + (reg))

static void foo_enable(struct foo_dev *foo)
{
    foo_write(foo, FOO_ENABLE, enable_value);
}
""",
            "FOO",
        )

        by_register = {a.register: a for a in analysis.register_accesses}
        assert by_register["FOO_ENABLE"].access_type == "write"
        assert by_register["FOO_ENABLE"].value_expr == "enable_value"
        assert by_register["FOO_ENABLE"].in_function == "foo_enable"

    def test_generic_linux_write_wrapper_infers_casted_value_param(self):
        analysis = analyze_driver_string(
            """\
#define foo_write(dev, reg, val) writel((u32)(val), (dev)->base + (reg))

static void foo_reset(struct foo_dev *foo)
{
    foo_write(foo, FOO_RESET, reset_value);
}
""",
            "FOO",
        )

        by_register = {a.register: a for a in analysis.register_accesses}
        assert by_register["FOO_RESET"].access_type == "write"
        assert by_register["FOO_RESET"].value_expr == "reset_value"

    def test_generic_linux_pointer_return_function_on_single_line(self):
        analysis = analyze_driver_string(
            """\
static const struct foo_desc *foo_get_desc(struct foo_dev *foo) { u32 status = readl(foo->base + FOO_DESC); return foo->desc; }
""",
            "FOO",
        )

        by_register = {a.register: a for a in analysis.register_accesses}
        assert by_register["FOO_DESC"].access_type == "read"
        assert by_register["FOO_DESC"].in_function == "foo_get_desc"

    def test_generic_linux_platform_hints(self):
        analysis = analyze_driver_string(
            """\
static const struct of_device_id foo_of_match[] = {
    { .compatible = "vendor,device" },
    { .compatible = "vendor,device-v2" },
    { /* sentinel */ }
};

static int foo_probe(struct platform_device *pdev)
{
    int irq;

    irq = platform_get_irq_byname(pdev, "job");
    return irq;
}
""",
            "FOO",
        )

        assert {"kind": "compatible", "value": "vendor,device"} in analysis.state_hints
        assert {"kind": "compatible", "value": "vendor,device-v2"} in analysis.state_hints
        assert {
            "kind": "irq_resource",
            "name": "job",
            "function": "foo_probe",
        } in analysis.state_hints

    def test_generic_linux_platform_irq_hint_requires_literal_name(self):
        analysis = analyze_driver_string(
            """\
static int foo_probe(struct platform_device *pdev)
{
    const char *irq_name = "job";

    return platform_get_irq_byname(pdev, irq_name);
}
""",
            "FOO",
        )

        assert not [
            hint for hint in analysis.state_hints
            if hint.get("kind") == "irq_resource"
        ]


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
