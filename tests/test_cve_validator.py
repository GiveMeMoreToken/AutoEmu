"""Tests for CVE validation and driver source fetching."""

from __future__ import annotations

from pathlib import Path

import pytest

from autoemu.cve_validator import (
    fetch_cve_driver_sources,
    is_cve_related_to_peripheral,
    validate_cve_format,
)


# ---------------------------------------------------------------------------
# Format validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "cve_id, expected",
    [
        ("CVE-2021-1234", True),
        ("cve-2021-1234", True),
        ("CVE-2021-12345", True),
        ("CVE-2021-1", False),
        ("not-a-cve", False),
        ("", False),
    ],
)
def test_validate_cve_format(cve_id, expected):
    assert validate_cve_format(cve_id) is expected


# ---------------------------------------------------------------------------
# Relation heuristic
# ---------------------------------------------------------------------------

def test_is_cve_related_to_peripheral_matches_description():
    details = {
        "found": True,
        "description": "The UART driver on STM32F4 devices allows buffer overflow.",
        "references": [],
        "affected_products": [],
    }
    assert is_cve_related_to_peripheral(details, "UART", "STM32F4") is True


def test_is_cve_related_to_peripheral_unrelated():
    details = {
        "found": True,
        "description": "Buffer overflow in a web server.",
        "references": [],
        "affected_products": [],
    }
    assert is_cve_related_to_peripheral(details, "UART", "STM32F4") is False


# ---------------------------------------------------------------------------
# CVE driver source fetching
# ---------------------------------------------------------------------------

def test_fetch_cve_driver_sources_returns_dict_with_downloaded(monkeypatch, tmp_path):
    """fetch_cve_driver_sources must return a dict with 'downloaded' and 'count'."""

    class FakeResult:
        url = "https://github.com/torvalds/linux/raw/master/drivers/char/demo.c"
        title = "demo driver"

    class FakeSearcher:
        def __init__(self, user_agent=""):
            pass
        def search(self, query, max_results=5):
            return [FakeResult()]

    monkeypatch.setattr(
        "autoemu.cve_validator.DuckDuckGoSearcher",
        FakeSearcher,
    )

    # Provide valid-looking C source content so _check_content passes
    def fake_urlopen(request, timeout=15):
        from io import BytesIO
        return BytesIO(b"int demo(void) { return 0; }\n")

    monkeypatch.setattr(
        "autoemu.cve_validator._urlopen_with_retry",
        fake_urlopen,
    )

    result = fetch_cve_driver_sources(
        cve_id="CVE-2021-1234",
        peripheral_name="UART",
        mcu_name="STM32F4",
        output_dir=tmp_path,
    )

    assert isinstance(result, dict)
    assert "downloaded" in result
    assert "count" in result
    assert result["count"] == 1
    assert len(result["downloaded"]) == 1
    assert result["downloaded"][0]["url"].endswith("demo.c")
    assert Path(result["downloaded"][0]["path"]).exists()


def test_fetch_cve_driver_sources_limits_to_three(monkeypatch, tmp_path):
    """At most 3 driver files should be downloaded."""

    class FakeResult:
        def __init__(self, idx):
            self.url = f"https://example.com/driver{idx}.c"
            self.title = f"driver {idx}"

    class FakeSearcher:
        def __init__(self, user_agent=""):
            pass
        def search(self, query, max_results=5):
            return [FakeResult(i) for i in range(10)]

    monkeypatch.setattr(
        "autoemu.cve_validator.DuckDuckGoSearcher",
        FakeSearcher,
    )

    def fake_urlopen(request, timeout=15):
        from io import BytesIO
        return BytesIO(b"int x(void) { return 0; }\n")

    monkeypatch.setattr(
        "autoemu.cve_validator._urlopen_with_retry",
        fake_urlopen,
    )

    result = fetch_cve_driver_sources(
        cve_id="CVE-2021-1234",
        peripheral_name="UART",
        mcu_name="STM32F4",
        output_dir=tmp_path,
    )

    assert result["count"] == 3
    assert len(result["downloaded"]) == 3


def test_fetch_cve_driver_sources_skips_non_source_urls(monkeypatch, tmp_path):
    """Non-.c/.h URLs should be skipped."""

    class FakeResult:
        url = "https://example.com/advisory.pdf"
        title = "advisory"

    class FakeSearcher:
        def __init__(self, user_agent=""):
            pass
        def search(self, query, max_results=5):
            return [FakeResult()]

    monkeypatch.setattr(
        "autoemu.cve_validator.DuckDuckGoSearcher",
        FakeSearcher,
    )

    result = fetch_cve_driver_sources(
        cve_id="CVE-2021-1234",
        peripheral_name="UART",
        mcu_name="STM32F4",
        output_dir=tmp_path,
    )

    assert result["count"] == 0
    assert result["downloaded"] == []


def test_fetch_cve_driver_sources_converts_github_blob_to_raw(monkeypatch, tmp_path):
    """GitHub blob URLs should be converted to raw URLs before download."""

    class FakeResult:
        url = "https://github.com/torvalds/linux/blob/master/drivers/char/demo.c"
        title = "demo driver"

    class FakeSearcher:
        def __init__(self, user_agent=""):
            pass
        def search(self, query, max_results=5):
            return [FakeResult()]

    monkeypatch.setattr(
        "autoemu.cve_validator.DuckDuckGoSearcher",
        FakeSearcher,
    )

    captured_url: str = ""

    def fake_urlopen(request, timeout=15):
        nonlocal captured_url
        captured_url = request.full_url
        from io import BytesIO
        return BytesIO(b"int demo(void) { return 0; }\n")

    monkeypatch.setattr(
        "autoemu.cve_validator._urlopen_with_retry",
        fake_urlopen,
    )

    result = fetch_cve_driver_sources(
        cve_id="CVE-2021-1234",
        peripheral_name="UART",
        mcu_name="STM32F4",
        output_dir=tmp_path,
    )

    assert result["count"] == 1
    assert "/raw/" in captured_url
    assert "/blob/" not in captured_url
