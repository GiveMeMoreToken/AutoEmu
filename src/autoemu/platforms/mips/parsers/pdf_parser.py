"""PDF register table extractor for MIPS vendor datasheets."""
from __future__ import annotations

import logging
import re
from pathlib import Path

from autoemu.models.register import AccessType, BitField, Register, RegisterBlock

logger = logging.getLogger(__name__)


def parse_pdf_register_tables(
    path: str | Path, peripheral_name: str = ""
) -> dict[str, RegisterBlock]:
    """Extract register tables from a PDF datasheet.

    Falls back to text extraction if pdfplumber is unavailable.
    Accepts ``.txt`` files containing pre-extracted text.
    """
    path = Path(path)
    if path.suffix == ".txt":
        text = path.read_text(encoding="utf-8", errors="replace")
    else:
        text = _extract_pdf_text(path)
    if not text:
        return {}
    return _parse_register_text(text, peripheral_name)


def _extract_pdf_text(path: Path) -> str:
    try:
        import pdfplumber

        with pdfplumber.open(str(path)) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    except ImportError:
        logger.warning("pdfplumber not installed; cannot parse PDF directly")
        return ""
    except Exception as exc:
        logger.warning("PDF extraction failed: %s", exc)
        return ""


# Patterns for register tables in datasheets
_RE_REG_HEADER = re.compile(
    r"(?:Register|Offset)\s*[:\s]*(0x[0-9A-Fa-f]+)\s+"
    r"(?:Name\s*[:\s]*)?(\w+)",
    re.IGNORECASE,
)
_RE_FIELD_ROW = re.compile(
    r"(\d+)(?:\s*:\s*(\d+))?\s+(\w+)\s+(R/?W?|RO|WO|R/W|RW|W1C)\s*(.*)",
    re.IGNORECASE,
)

_ACCESS_MAP: dict[str, AccessType] = {
    "r/w": AccessType.RW,
    "rw": AccessType.RW,
    "ro": AccessType.RO,
    "r": AccessType.RO,
    "wo": AccessType.WO,
    "w": AccessType.WO,
    "w1c": AccessType.W1C,
}


def _parse_register_text(
    text: str, peripheral_name: str
) -> dict[str, RegisterBlock]:
    """Parse register definitions from extracted text."""
    registers: list[Register] = []
    current_reg_name = ""
    current_offset = 0
    current_fields: list[BitField] = []

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # Try to match register header
        header = _RE_REG_HEADER.search(line)
        if header:
            if current_reg_name and current_fields:
                registers.append(
                    Register(
                        name=current_reg_name,
                        offset=current_offset,
                        size=32,
                        reset_value=0,
                        fields=current_fields,
                    )
                )
            current_offset = int(header.group(1), 16)
            current_reg_name = header.group(2)
            current_fields = []
            continue
        # Try to match field row
        field_match = _RE_FIELD_ROW.match(line)
        if field_match and current_reg_name:
            msb = int(field_match.group(1))
            lsb = int(field_match.group(2)) if field_match.group(2) else msb
            name = field_match.group(3)
            access_str = field_match.group(4).lower().strip()
            access = _ACCESS_MAP.get(access_str, AccessType.RW)
            current_fields.append(
                BitField(
                    name=name,
                    bit_offset=lsb,
                    bit_width=msb - lsb + 1,
                    access=access,
                )
            )

    # Flush last register
    if current_reg_name:
        registers.append(
            Register(
                name=current_reg_name,
                offset=current_offset,
                size=32,
                reset_value=0,
                fields=current_fields if current_fields else [],
            )
        )

    if not registers:
        return {}
    name = peripheral_name or "PERIPHERAL"
    return {name: RegisterBlock(name=name, registers=registers)}
