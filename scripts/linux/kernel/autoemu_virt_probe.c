#include <linux/bitops.h>
#include <linux/device.h>
#include <linux/iopoll.h>
#include <linux/io.h>
#include <linux/mod_devicetable.h>
#include <linux/module.h>
#include <linux/of.h>
#include <linux/platform_device.h>

#define AUTOEMU_ETH_DMABMR          0x1000
#define AUTOEMU_ETH_DMATPDR         0x1004
#define AUTOEMU_ETH_DMARPDR         0x1008
#define AUTOEMU_ETH_DMASR           0x1014
#define AUTOEMU_ETH_DMAOMR          0x1018
#define AUTOEMU_ETH_DMAIER          0x101c
#define AUTOEMU_ETH_DMABMR_SR       BIT(0)
#define AUTOEMU_ETH_DMASR_TS        BIT(0)
#define AUTOEMU_ETH_DMASR_RS        BIT(6)
#define AUTOEMU_ETH_DMASR_NIS       BIT(16)
#define AUTOEMU_ETH_DMAIER_TIE      BIT(0)
#define AUTOEMU_ETH_DMAIER_RIE      BIT(6)
#define AUTOEMU_ETH_DMAIER_NISE     BIT(16)
#define AUTOEMU_ETH_DMAOMR_SR       BIT(1)
#define AUTOEMU_ETH_DMAOMR_ST       BIT(13)

#define AUTOEMU_USB_GAHBCFG         0x008
#define AUTOEMU_USB_GRSTCTL         0x010
#define AUTOEMU_USB_GINTSTS         0x014
#define AUTOEMU_USB_GINTMSK         0x018
#define AUTOEMU_USB_GAHBCFG_GINTMSK BIT(0)
#define AUTOEMU_USB_GRSTCTL_CSRST   BIT(0)
#define AUTOEMU_USB_GRSTCTL_AHBIDL  BIT(31)
#define AUTOEMU_USB_GINTSTS_USBRST  BIT(12)
#define AUTOEMU_USB_GINTSTS_ENUMDNE BIT(13)

#define AUTOEMU_SUBGHZ_SPIDR        0x008
#define AUTOEMU_SUBGHZ_STATUS       0x100
#define AUTOEMU_SUBGHZ_IRQSTATUS    0x104
#define AUTOEMU_SUBGHZ_IRQMASK      0x108
#define AUTOEMU_SUBGHZ_TX_DONE      BIT(0)
#define AUTOEMU_SUBGHZ_CHIP_READY   (0x2U << 4)

enum autoemu_probe_kind {
	AUTOEMU_PROBE_ETH,
	AUTOEMU_PROBE_USB,
	AUTOEMU_PROBE_SUBGHZ,
};

struct autoemu_probe_desc {
	const char *name;
	enum autoemu_probe_kind kind;
};

struct autoemu_probe_dev {
	struct device *dev;
	void __iomem *base;
	const struct autoemu_probe_desc *desc;
};

static int autoemu_probe_eth(struct autoemu_probe_dev *probe)
{
	u32 val;
	int ret;

	writel(AUTOEMU_ETH_DMABMR_SR, probe->base + AUTOEMU_ETH_DMABMR);
	ret = readl_poll_timeout(probe->base + AUTOEMU_ETH_DMABMR, val,
				 !(val & AUTOEMU_ETH_DMABMR_SR), 10, 100000);
	if (ret) {
		dev_err(probe->dev, "eth: DMA reset bit did not self-clear\n");
		return ret;
	}

	writel(AUTOEMU_ETH_DMAIER_TIE |
	       AUTOEMU_ETH_DMAIER_RIE |
	       AUTOEMU_ETH_DMAIER_NISE,
	       probe->base + AUTOEMU_ETH_DMAIER);
	writel(AUTOEMU_ETH_DMAOMR_ST | AUTOEMU_ETH_DMAOMR_SR,
	       probe->base + AUTOEMU_ETH_DMAOMR);

	writel(1, probe->base + AUTOEMU_ETH_DMATPDR);
	ret = readl_poll_timeout(probe->base + AUTOEMU_ETH_DMASR, val,
				 (val & (AUTOEMU_ETH_DMASR_TS |
					 AUTOEMU_ETH_DMASR_NIS)) ==
				 (AUTOEMU_ETH_DMASR_TS |
				  AUTOEMU_ETH_DMASR_NIS),
				 10, 100000);
	if (ret) {
		dev_err(probe->dev, "eth: TX completion bits not observed\n");
		return ret;
	}
	writel(val, probe->base + AUTOEMU_ETH_DMASR);

	writel(1, probe->base + AUTOEMU_ETH_DMARPDR);
	ret = readl_poll_timeout(probe->base + AUTOEMU_ETH_DMASR, val,
				 (val & (AUTOEMU_ETH_DMASR_RS |
					 AUTOEMU_ETH_DMASR_NIS)) ==
				 (AUTOEMU_ETH_DMASR_RS |
				  AUTOEMU_ETH_DMASR_NIS),
				 10, 100000);
	if (ret) {
		dev_err(probe->dev, "eth: RX completion bits not observed\n");
		return ret;
	}
	writel(val, probe->base + AUTOEMU_ETH_DMASR);

	dev_info(probe->dev, "eth probe succeeded\n");
	return 0;
}

static int autoemu_probe_usb(struct autoemu_probe_dev *probe)
{
	u32 val;
	int ret;

	val = readl(probe->base + AUTOEMU_USB_GRSTCTL);
	if (!(val & AUTOEMU_USB_GRSTCTL_AHBIDL)) {
		dev_err(probe->dev, "usb: AHB idle bit missing at reset\n");
		return -EIO;
	}

	writel(AUTOEMU_USB_GRSTCTL_CSRST, probe->base + AUTOEMU_USB_GRSTCTL);
	ret = readl_poll_timeout(probe->base + AUTOEMU_USB_GRSTCTL, val,
				 !(val & AUTOEMU_USB_GRSTCTL_CSRST) &&
				 (val & AUTOEMU_USB_GRSTCTL_AHBIDL),
				 10, 100000);
	if (ret) {
		dev_err(probe->dev, "usb: core reset did not complete\n");
		return ret;
	}

	writel(AUTOEMU_USB_GINTSTS_USBRST | AUTOEMU_USB_GINTSTS_ENUMDNE,
	       probe->base + AUTOEMU_USB_GINTMSK);
	writel(AUTOEMU_USB_GAHBCFG_GINTMSK, probe->base + AUTOEMU_USB_GAHBCFG);

	ret = readl_poll_timeout(probe->base + AUTOEMU_USB_GINTSTS, val,
				 val & (AUTOEMU_USB_GINTSTS_USBRST |
					AUTOEMU_USB_GINTSTS_ENUMDNE),
				 10, 100000);
	if (ret) {
		dev_err(probe->dev, "usb: reset/enumeration bits not observed\n");
		return ret;
	}
	writel(val & (AUTOEMU_USB_GINTSTS_USBRST |
		      AUTOEMU_USB_GINTSTS_ENUMDNE),
	       probe->base + AUTOEMU_USB_GINTSTS);

	dev_info(probe->dev, "usb probe succeeded\n");
	return 0;
}

static int autoemu_probe_subghz(struct autoemu_probe_dev *probe)
{
	u32 val;
	int ret;

	writel(AUTOEMU_SUBGHZ_TX_DONE, probe->base + AUTOEMU_SUBGHZ_IRQMASK);
	writel(0x82, probe->base + AUTOEMU_SUBGHZ_SPIDR);

	ret = readl_poll_timeout(probe->base + AUTOEMU_SUBGHZ_IRQSTATUS, val,
				 val & AUTOEMU_SUBGHZ_TX_DONE, 10, 100000);
	if (ret) {
		dev_err(probe->dev, "subghz: TX_DONE not observed\n");
		return ret;
	}

	val = readl(probe->base + AUTOEMU_SUBGHZ_STATUS);
	if ((val & AUTOEMU_SUBGHZ_CHIP_READY) != AUTOEMU_SUBGHZ_CHIP_READY) {
		dev_err(probe->dev, "subghz: chip status not in ready state\n");
		return -EIO;
	}

	writel(AUTOEMU_SUBGHZ_TX_DONE, probe->base + AUTOEMU_SUBGHZ_IRQSTATUS);
	ret = readl_poll_timeout(probe->base + AUTOEMU_SUBGHZ_IRQSTATUS, val,
				 !(val & AUTOEMU_SUBGHZ_TX_DONE), 10, 100000);
	if (ret) {
		dev_err(probe->dev, "subghz: TX_DONE did not clear\n");
		return ret;
	}

	dev_info(probe->dev, "subghz probe succeeded\n");
	return 0;
}

static int autoemu_probe_run(struct autoemu_probe_dev *probe)
{
	switch (probe->desc->kind) {
	case AUTOEMU_PROBE_ETH:
		return autoemu_probe_eth(probe);
	case AUTOEMU_PROBE_USB:
		return autoemu_probe_usb(probe);
	case AUTOEMU_PROBE_SUBGHZ:
		return autoemu_probe_subghz(probe);
	}

	return -EINVAL;
}

static int autoemu_probe_platform_probe(struct platform_device *pdev)
{
	struct autoemu_probe_dev *probe;
	const struct autoemu_probe_desc *desc;
	int ret;

	desc = device_get_match_data(&pdev->dev);
	if (!desc) {
		return -EINVAL;
	}

	probe = devm_kzalloc(&pdev->dev, sizeof(*probe), GFP_KERNEL);
	if (!probe) {
		return -ENOMEM;
	}

	probe->dev = &pdev->dev;
	probe->desc = desc;
	probe->base = devm_platform_ioremap_resource(pdev, 0);
	if (IS_ERR(probe->base)) {
		return PTR_ERR(probe->base);
	}

	platform_set_drvdata(pdev, probe);
	dev_info(&pdev->dev, "%s probe start\n", desc->name);

	ret = autoemu_probe_run(probe);
	if (ret) {
		dev_err(&pdev->dev, "%s validation failed: %d\n",
			desc->name, ret);
		return ret;
	}

	dev_info(&pdev->dev, "%s validation complete\n", desc->name);
	return 0;
}

static const struct autoemu_probe_desc autoemu_eth_desc = {
	.name = "eth",
	.kind = AUTOEMU_PROBE_ETH,
};

static const struct autoemu_probe_desc autoemu_usb_desc = {
	.name = "usb",
	.kind = AUTOEMU_PROBE_USB,
};

static const struct autoemu_probe_desc autoemu_subghz_desc = {
	.name = "subghz",
	.kind = AUTOEMU_PROBE_SUBGHZ,
};

static const struct of_device_id autoemu_probe_of_match[] = {
	{ .compatible = "autoemu,stm32-eth-probe", .data = &autoemu_eth_desc },
	{ .compatible = "autoemu,stm32-usb-otg-fs-probe", .data = &autoemu_usb_desc },
	{ .compatible = "autoemu,stm32-subghz-probe", .data = &autoemu_subghz_desc },
	{ }
};
MODULE_DEVICE_TABLE(of, autoemu_probe_of_match);

static struct platform_driver autoemu_probe_driver = {
	.probe = autoemu_probe_platform_probe,
	.driver = {
		.name = "autoemu-virt-probe",
		.of_match_table = autoemu_probe_of_match,
	},
};
module_platform_driver(autoemu_probe_driver);

MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("AutoEmu virtual peripheral probe drivers");
