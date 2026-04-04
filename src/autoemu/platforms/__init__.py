"""Platform plugin registry."""
from __future__ import annotations

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


_auto_register()
