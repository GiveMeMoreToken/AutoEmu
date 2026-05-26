"""CVE validation, disclosure check, and PoC search utilities."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from autoemu.fetchers.generic import DuckDuckGoSearcher, _urlopen_with_retry
from autoemu.platforms import analyze_target

logger = logging.getLogger(__name__)

_CVE_RE = re.compile(r"^CVE-\d{4}-\d{4,}$", re.IGNORECASE)


def validate_cve_format(cve_id: str) -> bool:
    """Return True if *cve_id* matches the CVE-YYYY-NNNNN+ pattern."""
    return bool(_CVE_RE.match(cve_id.strip()))


def fetch_cve_details(cve_id: str) -> dict[str, Any]:
    """Query the NVD API for a CVE record.

    Returns a dict with keys:
    - ``found``: bool
    - ``cve_id``: str
    - ``description``: str
    - ``severity``: str
    - ``references``: list[str]
    - ``affected_products``: list[str]
    - ``published``: str
    - ``error``: str (empty on success)
    """
    cve_id = cve_id.strip().upper()
    result: dict[str, Any] = {
        "found": False,
        "cve_id": cve_id,
        "description": "",
        "severity": "",
        "references": [],
        "affected_products": [],
        "published": "",
        "error": "",
    }

    url = f"https://services.nvd.nist.gov/rest/json/cves/2.0?cveId={cve_id}"
    try:
        from urllib.request import Request

        request = Request(url, headers={"User-Agent": "AutoEmu/0.1"})
        with _urlopen_with_retry(request, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))

        vulnerabilities = data.get("vulnerabilities", [])
        if not vulnerabilities:
            result["error"] = f"{cve_id} not found in NVD database."
            return result

        cve = vulnerabilities[0].get("cve", {})
        result["found"] = True

        # Description (prefer English)
        descriptions = cve.get("descriptions", [])
        for desc in descriptions:
            if desc.get("lang", "") == "en":
                result["description"] = desc.get("value", "")
                break
        if not result["description"] and descriptions:
            result["description"] = descriptions[0].get("value", "")

        # Severity
        metrics = cve.get("metrics", {})
        cvss = metrics.get("cvssMetricV31", metrics.get("cvssMetricV30", []))
        if cvss:
            result["severity"] = cvss[0].get("cvssData", {}).get("baseSeverity", "")

        # References
        refs = cve.get("references", [])
        result["references"] = [
            ref.get("url", "") for ref in refs if ref.get("url")
        ]

        # Affected products (CPE strings)
        configurations = cve.get("configurations", [])
        cpes: set[str] = set()
        for cfg in configurations:
            for node in cfg.get("nodes", []):
                for match in node.get("cpeMatch", []):
                    criteria = match.get("criteria", "")
                    if criteria:
                        cpes.add(criteria)
        result["affected_products"] = sorted(cpes)

        # Published date
        result["published"] = cve.get("published", "")

    except Exception as exc:
        result["error"] = f"NVD lookup failed: {exc}"
        logger.warning("NVD lookup failed for %s: %s", cve_id, exc)

    return result


def is_cve_related_to_peripheral(
    cve_details: dict[str, Any],
    peripheral_name: str,
    mcu_name: str = "",
) -> bool:
    """Heuristic: does the CVE description/references mention the peripheral or MCU?"""
    if not cve_details.get("found"):
        return False

    # Build search tokens
    tokens: set[str] = set()
    for name in (peripheral_name, mcu_name):
        if not name:
            continue
        raw = name.strip().lower()
        tokens.add(raw)
        tokens.add("".join(ch for ch in raw if ch.isalnum()))
        tokens.update(part for part in re.split(r"[^a-z0-9]+", raw) if len(part) > 2)

    # Also include platform aliases
    if mcu_name:
        try:
            info = analyze_target(mcu_name)
            for alias in info.aliases:
                alias_norm = alias.strip().lower()
                if alias_norm:
                    tokens.add(alias_norm)
                    tokens.add("".join(ch for ch in alias_norm if ch.isalnum()))
            if info.vendor:
                tokens.add(info.vendor.lower())
            if info.family:
                tokens.add(info.family.lower())
        except Exception:
            pass

    text = " ".join(
        [
            cve_details.get("description", "").lower(),
            *(
                ref.lower()
                for ref in cve_details.get("references", [])
            ),
            *(
                prod.lower()
                for prod in cve_details.get("affected_products", [])
            ),
        ]
    )

    return any(token in text for token in tokens if len(token) > 2)


def search_cve_poc(
    cve_id: str,
    peripheral_name: str = "",
    mcu_name: str = "",
) -> list[dict[str, str]]:
    """Search the web for PoCs, exploits, patches, and advisories.

    Returns a list of dicts with ``title`` and ``url`` keys.
    """
    searcher = DuckDuckGoSearcher(user_agent="AutoEmu/0.1")
    queries: list[tuple[str, str]] = [
        (f"{cve_id} PoC github", "poc"),
        (f"{cve_id} exploit-db", "exploit"),
        (f"{cve_id} patch linux kernel", "patch"),
        (f"{cve_id} advisory", "advisory"),
    ]
    if peripheral_name:
        queries.append((f"{cve_id} {peripheral_name} vulnerability", "advisory"))
    if mcu_name:
        queries.append((f"{cve_id} {mcu_name} exploit", "exploit"))

    findings: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    for query, category in queries:
        try:
            results = searcher.search(query, max_results=5)
            for result in results:
                url = result.url
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    findings.append(
                        {
                            "title": result.title,
                            "url": url,
                            "category": category,
                        }
                    )
        except Exception as exc:
            logger.warning("CVE PoC search failed for %r: %s", query, exc)

    return findings


def run_cve_check(
    cve_id: str,
    peripheral_name: str = "",
    mcu_name: str = "",
) -> dict[str, Any]:
    """Run the full CVE validation pipeline and return a summary dict.

    The summary contains:
    - ``valid_format``: bool
    - ``disclosed``: bool
    - ``related``: bool
    - ``details``: dict from :func:`fetch_cve_details`
    - ``poc_findings``: list from :func:`search_cve_poc`
    - ``warnings``: list[str]
    """
    cve_id = cve_id.strip()
    summary: dict[str, Any] = {
        "cve_id": cve_id,
        "valid_format": False,
        "disclosed": False,
        "related": False,
        "details": {},
        "poc_findings": [],
        "warnings": [],
    }

    if not validate_cve_format(cve_id):
        summary["warnings"].append(f"'{cve_id}' does not match CVE format (CVE-YYYY-NNNNN+).")
        return summary

    summary["valid_format"] = True

    details = fetch_cve_details(cve_id)
    summary["details"] = details

    if not details.get("found"):
        summary["warnings"].append(
            f"{cve_id} was not found in the NVD database. It may be undisclosed or invalid."
        )
        return summary

    summary["disclosed"] = True

    related = is_cve_related_to_peripheral(details, peripheral_name, mcu_name)
    summary["related"] = related
    if not related:
        summary["warnings"].append(
            f"{cve_id} does not appear to be related to {peripheral_name}"
            + (f" / {mcu_name}" if mcu_name else "")
            + " based on description and references."
        )

    summary["poc_findings"] = search_cve_poc(cve_id, peripheral_name, mcu_name)

    return summary
