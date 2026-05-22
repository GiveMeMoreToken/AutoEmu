"""Compatibility wrapper for the generic device-tree parser."""

from __future__ import annotations

from autoemu.parsers.device_tree import parse_device_tree, parse_device_tree_string

__all__ = ["parse_device_tree", "parse_device_tree_string"]
