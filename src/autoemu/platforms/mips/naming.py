"""MIPS naming conventions."""
from __future__ import annotations


def mips_snake(name: str) -> str:
    """Convert name to snake_case."""
    result: list[str] = []
    for i, c in enumerate(name):
        if c.isupper() and i > 0 and not name[i - 1].isupper():
            result.append("_")
        result.append(c.lower())
    return "".join(result).replace("__", "_")


def mips_type_name(peripheral: str) -> str:
    """Return the QEMU state type name for a MIPS peripheral."""
    return f"MIPS{mips_snake(peripheral).upper()}State"
