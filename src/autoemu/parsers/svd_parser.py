"""SVD (System View Description) file parser for register maps.

Parses CMSIS-SVD XML files to extract peripheral register structures,
bit field definitions, and access types.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from lxml import etree

from autoemu.models.register import AccessType, BitField, Register, RegisterBlock


_SVD_ACCESS_MAP: dict[str, AccessType] = {
    "read-write": AccessType.RW,
    "read-only": AccessType.RO,
    "write-only": AccessType.WO,
    "writeOnce": AccessType.WO,
    "read-writeOnce": AccessType.RW,
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
    return _SVD_ACCESS_MAP.get(text, parent_access)


def parse_field(field_elem: etree._Element, reg_access: AccessType) -> BitField:
    """Parse a single SVD field element."""
    name = _text(field_elem, "name")
    description = _text(field_elem, "description")
    bit_offset = _int(field_elem, "bitOffset", -1)
    bit_width = _int(field_elem, "bitWidth", 1)

    # Handle bitRange format [msb:lsb]
    if bit_offset < 0:
        bit_range = _text(field_elem, "bitRange")
        if bit_range:
            bit_range = bit_range.strip("[]")
            msb, lsb = bit_range.split(":")
            bit_offset = int(lsb)
            bit_width = int(msb) - int(lsb) + 1
        else:
            lsb = _int(field_elem, "lsb", 0)
            msb = _int(field_elem, "msb", 0)
            bit_offset = lsb
            bit_width = msb - lsb + 1

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


def parse_register(reg_elem: etree._Element, periph_access: AccessType) -> Register:
    """Parse a single SVD register element."""
    name = _text(reg_elem, "name")
    description = _text(reg_elem, "description")
    offset = _int(reg_elem, "addressOffset")
    size = _int(reg_elem, "size", 32)
    reset_value = _int(reg_elem, "resetValue")
    access = _parse_access(reg_elem, periph_access)

    fields: list[BitField] = []
    fields_elem = reg_elem.find("fields")
    if fields_elem is not None:
        for field_elem in fields_elem.findall("field"):
            fields.append(parse_field(field_elem, access))

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
                    reg = parse_register(reg_elem, access)
                    reg.name = base_name.replace("%s", idx)
                    reg.offset = base_offset + i * dim_increment
                    registers.append(reg)
            else:
                registers.append(parse_register(reg_elem, access))

        # Handle clusters
        for cluster_elem in regs_elem.findall("cluster"):
            cluster_name = _text(cluster_elem, "name")
            cluster_offset = _int(cluster_elem, "addressOffset")
            for reg_elem in cluster_elem.findall("register"):
                reg = parse_register(reg_elem, access)
                reg.name = f"{cluster_name}_{reg.name}"
                reg.offset += cluster_offset
                registers.append(reg)

    return RegisterBlock(
        name=name,
        description=description,
        base_address=base_address,
        registers=registers,
    )


def parse_svd_file(path: str | Path) -> dict[str, RegisterBlock]:
    """Parse an SVD file and return a dict of peripheral name -> RegisterBlock."""
    tree = etree.parse(str(path))
    root = tree.getroot()

    # Collect all peripheral elements
    periph_elements: list[etree._Element] = root.findall(".//peripheral")

    # Sort: non-derived first, then derived (so base peripherals are available)
    non_derived = [e for e in periph_elements if not e.get("derivedFrom")]
    derived = [e for e in periph_elements if e.get("derivedFrom")]

    results: dict[str, RegisterBlock] = {}
    for periph_elem in non_derived + derived:
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
            results[name] = parse_peripheral(periph_elem)

    return results


def parse_svd_string(xml_content: str) -> dict[str, RegisterBlock]:
    """Parse SVD XML content from a string."""
    root = etree.fromstring(xml_content.encode())

    # Collect all peripheral elements
    periph_elements: list[etree._Element] = root.findall(".//peripheral")

    # Sort: non-derived first, then derived (so base peripherals are available)
    non_derived = [e for e in periph_elements if not e.get("derivedFrom")]
    derived = [e for e in periph_elements if e.get("derivedFrom")]

    results: dict[str, RegisterBlock] = {}
    for periph_elem in non_derived + derived:
        name = _text(periph_elem, "name")
        if not name:
            continue
        derived_from = periph_elem.get("derivedFrom")
        if derived_from and derived_from in results:
            base = results[derived_from].model_copy(deep=True)
            base.name = name
            base.base_address = _int(periph_elem, "baseAddress", base.base_address)
            results[name] = base
        else:
            results[name] = parse_peripheral(periph_elem)

    return results
