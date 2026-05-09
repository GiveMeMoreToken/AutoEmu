"""SVD (System View Description) file parser for register maps.

Parses CMSIS-SVD XML files to extract peripheral register structures,
bit field definitions, and access types.
"""

from __future__ import annotations

import logging
from pathlib import Path

from lxml import etree

from autoemu.models.register import AccessType, BitField, Register, RegisterBlock

logger = logging.getLogger(__name__)

#: Module-level warnings accumulated during parsing.
svd_warnings: list[str] = []


_SVD_ACCESS_MAP: dict[str, AccessType] = {
    "read-write": AccessType.RW,
    "read-only": AccessType.RO,
    "write-only": AccessType.WO,
    "writeOnce": AccessType.WO,
    "read-writeOnce": AccessType.RW,
}

_SVD_MODIFIED_WRITE_MAP: dict[str, AccessType] = {
    "oneToClear": AccessType.W1C,
    "oneToSet": AccessType.W1S,
    "zeroToClear": AccessType.W0C,
}


def _text(element: etree._Element, tag: str, default: str = "") -> str:
    child = element.find(tag)
    return child.text.strip() if child is not None and child.text else default


def _int(element: etree._Element, tag: str, default: int = 0) -> int:
    text = _text(element, tag)
    if not text:
        return default
    text = text.strip().lower()
    if text.startswith("0x"):
        return int(text, 16)
    if text.startswith("#"):
        return int(text[1:], 2)
    return int(text)


def _parse_access(
    element: etree._Element, parent_access: AccessType = AccessType.RW
) -> AccessType:
    text = _text(element, "access")
    access = _SVD_ACCESS_MAP.get(text, parent_access)
    modified_write = _text(element, "modifiedWriteValues")
    if modified_write in _SVD_MODIFIED_WRITE_MAP:
        access = _SVD_MODIFIED_WRITE_MAP[modified_write]

    read_action = _text(element, "readAction")
    if read_action == "clear":
        if access == AccessType.W1C:
            return AccessType.RC_W1
        if access == AccessType.W0C:
            return AccessType.RC_W0
    if read_action == "set":
        return AccessType.RS
    return access


def parse_field(field_elem: etree._Element, reg_access: AccessType) -> BitField | None:
    """Parse a single SVD field element.

    Returns ``None`` when the field has an invalid width (0 or negative) so
    the caller can skip it with a warning.
    """
    name = _text(field_elem, "name")
    description = _text(field_elem, "description")
    bit_offset = _int(field_elem, "bitOffset", -1)
    bit_width = _int(field_elem, "bitWidth", 1)

    # Handle bitRange format [msb:lsb]
    if bit_offset < 0:
        bit_range = _text(field_elem, "bitRange")
        if bit_range:
            bit_range = bit_range.strip("[]")
            msb_text, lsb_text = bit_range.split(":")
            lsb_value = int(lsb_text)
            msb_value = int(msb_text)
            bit_offset = lsb_value
            bit_width = msb_value - lsb_value + 1
        else:
            lsb_value = _int(field_elem, "lsb", 0)
            msb_value = _int(field_elem, "msb", 0)
            bit_offset = lsb_value
            bit_width = msb_value - lsb_value + 1

    # Skip fields with non-standard (invalid) widths
    if bit_width <= 0:
        msg = f"Skipping field '{name}': invalid bitWidth {bit_width}"
        logger.warning(msg)
        svd_warnings.append(msg)
        return None

    access = _parse_access(field_elem, reg_access)

    # Parse enumerated values
    enum_values: dict[str, int] = {}
    for ev_container in field_elem.findall("enumeratedValues"):
        for ev in ev_container.findall("enumeratedValue"):
            ev_name = _text(ev, "name")
            ev_val = _int(ev, "value", -1)
            if ev_name and ev_val >= 0:
                enum_values[ev_name] = ev_val

    return BitField(
        name=name,
        description=description,
        bit_offset=bit_offset,
        bit_width=bit_width,
        access=access,
        reset_value=0,
        enum_values=enum_values,
    )


def _inherit_reset_value(
    reg_elem: etree._Element, periph_elem: etree._Element | None
) -> int:
    """Resolve a register's reset value, inheriting from peripheral level if missing."""
    rv_text = _text(reg_elem, "resetValue")
    if rv_text:
        return _int(reg_elem, "resetValue")
    # Fall back to the peripheral-level resetValue if present
    if periph_elem is not None:
        periph_rv = _text(periph_elem, "resetValue")
        if periph_rv:
            return _int(periph_elem, "resetValue")
    # Default to 0x0
    return 0


def parse_register(
    reg_elem: etree._Element,
    periph_access: AccessType,
    periph_elem: etree._Element | None = None,
) -> Register:
    """Parse a single SVD register element."""
    name = _text(reg_elem, "name")
    description = _text(reg_elem, "description")
    offset = _int(reg_elem, "addressOffset")
    size = _int(reg_elem, "size", 32)
    reset_value = _inherit_reset_value(reg_elem, periph_elem)
    access = _parse_access(reg_elem, periph_access)

    fields: list[BitField] = []
    fields_elem = reg_elem.find("fields")
    if fields_elem is not None:
        for field_elem in fields_elem.findall("field"):
            parsed = parse_field(field_elem, access)
            if parsed is not None:
                fields.append(parsed)

    # Set per-field reset values from register reset value
    for f in fields:
        f.reset_value = f.extract(reset_value)

    return Register(
        name=name,
        description=description,
        offset=offset,
        size=size,
        access=access,
        reset_value=reset_value,
        fields=fields,
    )


def parse_peripheral(periph_elem: etree._Element) -> RegisterBlock:
    """Parse a single SVD peripheral element into a RegisterBlock."""
    name = _text(periph_elem, "name")
    description = _text(periph_elem, "description")
    base_address = _int(periph_elem, "baseAddress")
    access = _parse_access(periph_elem)

    registers: list[Register] = []

    # Handle derivedFrom attribute (peripheral inherits from another)
    regs_elem = periph_elem.find("registers")
    if regs_elem is not None:
        for reg_elem in regs_elem.findall("register"):
            # Handle register clusters
            dim = _int(reg_elem, "dim", 0)
            if dim > 0:
                dim_increment = _int(reg_elem, "dimIncrement", 4)
                dim_index = _text(reg_elem, "dimIndex")
                base_name = _text(reg_elem, "name")
                base_offset = _int(reg_elem, "addressOffset")

                if dim_index:
                    indices = (
                        dim_index.split(",")
                        if "," in dim_index
                        else [
                            str(i)
                            for i in range(
                                int(dim_index.split("-")[0]),
                                int(dim_index.split("-")[1]) + 1,
                            )
                        ]
                        if "-" in dim_index
                        else [str(i) for i in range(dim)]
                    )
                else:
                    indices = [str(i) for i in range(dim)]

                for i, idx in enumerate(indices):
                    reg = parse_register(reg_elem, access, periph_elem)
                    reg.name = base_name.replace("%s", idx)
                    reg.offset = base_offset + i * dim_increment
                    registers.append(reg)
            else:
                registers.append(parse_register(reg_elem, access, periph_elem))

        # Handle clusters
        for cluster_elem in regs_elem.findall("cluster"):
            cluster_name = _text(cluster_elem, "name")
            cluster_offset = _int(cluster_elem, "addressOffset")
            for reg_elem in cluster_elem.findall("register"):
                reg = parse_register(reg_elem, access, periph_elem)
                reg.name = f"{cluster_name}_{reg.name}"
                reg.offset += cluster_offset
                registers.append(reg)

    return RegisterBlock(
        name=name,
        description=description,
        base_address=base_address,
        registers=registers,
    )


def _topological_sort_peripherals(
    periph_elements: list[etree._Element],
) -> list[etree._Element]:
    """Sort peripheral elements so that derivedFrom bases come before dependents.

    Non-derived peripherals come first; derived peripherals are topologically
    sorted so that transitive chains (A derives B derives C) are resolved in
    the correct order.
    """
    by_name: dict[str, etree._Element] = {}
    for elem in periph_elements:
        name = _text(elem, "name")
        if name:
            by_name[name] = elem

    non_derived = [e for e in periph_elements if not e.get("derivedFrom")]
    derived = [e for e in periph_elements if e.get("derivedFrom")]

    # Build adjacency: derived_name -> base_name
    deps: dict[str, str] = {}
    for e in derived:
        name = _text(e, "name")
        base = e.get("derivedFrom", "")
        if name and base:
            deps[name] = base

    # Topological sort via DFS
    visited: set[str] = set()
    order: list[str] = []

    def visit(name: str) -> None:
        if name in visited:
            return
        visited.add(name)
        base = deps.get(name)
        if base and base in deps:
            visit(base)
        order.append(name)

    for name in deps:
        visit(name)

    derived_sorted = [by_name[n] for n in order if n in by_name]
    return non_derived + derived_sorted


def _parse_peripherals_from_root(root: etree._Element) -> dict[str, RegisterBlock]:
    """Core logic shared by ``parse_svd_file`` and ``parse_svd_string``."""
    periph_elements: list[etree._Element] = root.findall(".//peripheral")
    sorted_elements = _topological_sort_peripherals(periph_elements)

    results: dict[str, RegisterBlock] = {}
    for periph_elem in sorted_elements:
        name = _text(periph_elem, "name")
        if not name:
            continue

        derived_from = periph_elem.get("derivedFrom")
        if derived_from and derived_from in results:
            # Clone from base peripheral
            base = results[derived_from].model_copy(deep=True)
            base.name = name
            base.base_address = _int(periph_elem, "baseAddress", base.base_address)
            desc = _text(periph_elem, "description")
            if desc:
                base.description = desc
            results[name] = base
        else:
            try:
                results[name] = parse_peripheral(periph_elem)
            except Exception as exc:
                msg = f"Failed to parse peripheral '{name}': {exc}"
                logger.warning(msg)
                svd_warnings.append(msg)

    return results


def parse_svd_file(path: str | Path) -> dict[str, RegisterBlock]:
    """Parse an SVD file and return a dict of peripheral name -> RegisterBlock.

    Returns partial results on parse errors rather than crashing.
    """
    svd_warnings.clear()
    try:
        tree = etree.parse(str(path))
        root = tree.getroot()
        return _parse_peripherals_from_root(root)
    except Exception as exc:
        msg = f"SVD parse error for '{path}': {exc}"
        logger.warning(msg)
        svd_warnings.append(msg)
        return {}


def parse_svd_string(xml_content: str) -> dict[str, RegisterBlock]:
    """Parse SVD XML content from a string.

    Returns partial results on parse errors rather than crashing.
    """
    svd_warnings.clear()
    try:
        root = etree.fromstring(xml_content.encode())
        return _parse_peripherals_from_root(root)
    except Exception as exc:
        msg = f"SVD string parse error: {exc}"
        logger.warning(msg)
        svd_warnings.append(msg)
        return {}
