"""Platform plugin registry."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from autoemu.platforms.base import Platform

_REGISTRY: dict[str, type[Platform]] = {}


def register_platform(name: str, cls: type[Platform]) -> None:
    _REGISTRY[name.lower()] = cls


def get_platform(name: str) -> Platform:
    cls = _REGISTRY.get(name.lower())
    if cls is None:
        available = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise ValueError(f"Unknown platform '{name}'. Available: {available}")
    return cls()


def list_platforms() -> list[str]:
    return sorted(_REGISTRY)


# ---------------------------------------------------------------------------
# Vendor / board knowledge base for platform inference
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BoardInfo:
    """Inferred metadata about a target board/MCU."""

    vendor: str          # e.g. "hisilicon", "espressif", "nordic"
    platform: str        # registered platform name: "stm32", "mips", "generic"
    arch: str            # "arm", "arm64", "xtensa", "riscv", "mips", ...
    family: str          # human label, e.g. "Kirin 960", "ESP32-S3"
    aliases: tuple[str, ...]  # alternative search terms


# Patterns: (regex on lowercased target, BoardInfo)
_BOARD_PATTERNS: list[tuple[str, BoardInfo]] = [
    # --- STM32 ---
    (r"stm32", BoardInfo("stmicro", "stm32", "arm", "STM32", ("STMicroelectronics",))),

    # --- MIPS-family ---
    (r"^(mips|pic32|mt7|rt\d)", BoardInfo("mips", "mips", "mips", "MIPS", ())),

    # --- HiSilicon / Kirin / HiKey ---
    (r"(kirin|hikey|hi36|hi38)", BoardInfo("hisilicon", "generic", "arm64", "HiSilicon Kirin",
                                           ("HiSilicon", "Kirin", "HiKey", "ARM Mali"))),

    # --- Qualcomm / Snapdragon ---
    (r"(snapdragon|qualcomm|sdm|sm[0-9]|qcom|msm)", BoardInfo("qualcomm", "generic", "arm64", "Snapdragon",
                                                                ("Qualcomm", "Snapdragon", "Adreno"))),

    # --- MediaTek ---
    (r"(mediatek|mt[0-9]{4}|helio|dimensity)", BoardInfo("mediatek", "generic", "arm64", "MediaTek",
                                                          ("MediaTek", "Helio", "Dimensity"))),

    # --- Espressif (ESP32) ---
    (r"esp32", BoardInfo("espressif", "generic", "xtensa", "ESP32",
                         ("Espressif", "ESP-IDF", "esp-idf"))),

    # --- Nordic (nRF) ---
    (r"(nrf|nordic)", BoardInfo("nordic", "generic", "arm", "nRF",
                                ("Nordic Semiconductor", "nRF SDK", "Zephyr"))),

    # --- Raspberry Pi ---
    (r"(raspi|raspberry|rpi|bcm2[0-9])", BoardInfo("broadcom", "generic", "arm64", "Raspberry Pi",
                                                     ("Broadcom", "Raspberry Pi", "BCM"))),

    # --- NXP / i.MX ---
    (r"(imx|lpc|kinetis|nxp)", BoardInfo("nxp", "generic", "arm", "NXP i.MX",
                                          ("NXP", "i.MX", "LPC", "Kinetis"))),

    # --- TI (Texas Instruments) ---
    (r"(am335|am57|omap|tiva|msp430|cc[0-9]{4}|beaglebone)", BoardInfo("ti", "generic", "arm", "Texas Instruments",
                                                                        ("Texas Instruments", "TI", "OMAP"))),

    # --- Allwinner ---
    (r"(allwinner|sun[0-9]|h[2-6]\b|a[0-9]{2}\b)", BoardInfo("allwinner", "generic", "arm64", "Allwinner",
                                                               ("Allwinner", "sunxi"))),

    # --- Samsung Exynos ---
    (r"(exynos|samsung|s5p)", BoardInfo("samsung", "generic", "arm64", "Exynos",
                                         ("Samsung", "Exynos"))),

    # --- RISC-V ---
    (r"(riscv|sifive|kendryte|k210|esp32c|bl60)", BoardInfo("riscv", "generic", "riscv", "RISC-V",
                                                              ("RISC-V", "SiFive"))),

    # --- Xilinx / AMD ---
    (r"(zynq|xilinx|versal)", BoardInfo("xilinx", "generic", "arm", "Xilinx Zynq",
                                         ("Xilinx", "Zynq", "Vivado"))),

    # --- Intel / Altera ---
    (r"(cyclone|stratix|altera|intel.*fpga)", BoardInfo("intel", "generic", "arm", "Intel FPGA",
                                                         ("Intel", "Altera", "Cyclone"))),
]


def detect_platform(target_mcu: str) -> str:
    """Return the registered platform name for a target MCU/board."""
    info = analyze_target(target_mcu)
    return info.platform


def analyze_target(target_mcu: str) -> BoardInfo:
    """Infer vendor, arch, family from a target MCU or board name."""
    name = target_mcu.lower().strip()
    for pattern, info in _BOARD_PATTERNS:
        if re.search(pattern, name):
            return info
    # Unknown — default generic ARM
    return BoardInfo("unknown", "generic", "arm", target_mcu, ())


def _auto_register():
    """Import built-in platform modules to trigger registration."""
    try:
        from autoemu.platforms import stm32 as _  # noqa: F401
    except ImportError:
        pass
    try:
        from autoemu.platforms import mips as _  # noqa: F401, F811
    except ImportError:
        pass
    try:
        from autoemu.platforms import generic as _  # noqa: F401, F811
    except ImportError:
        pass


_auto_register()
