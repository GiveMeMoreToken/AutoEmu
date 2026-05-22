"""Unified data fetcher: web search, download, and input resolution."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

from lxml import html

logger = logging.getLogger(__name__)


_USER_AGENT = "AutoEmu/0.1"
_DOWNLOAD_TIMEOUT = 10
_OK_STATUSES = {"downloaded", "cached"}
_DOC_GLOBS = ("*.txt", "*.pdf", "*.md", "*.html", "*.htm", "*.dts", "*.dtsi")
_DEVICE_TREE_EXTS = {".dts", ".dtsi"}


def _urlopen_with_retry(
    request: Request,
    *,
    timeout: int = _DOWNLOAD_TIMEOUT,
    max_retries: int = 3,
    base_delay: float = 1.0,
):
    """Wrap :func:`urlopen` with exponential-backoff retry logic.

    Retries on :class:`URLError`, :class:`TimeoutError`, and general
    :class:`Exception`.  Returns the response object on success or
    re-raises the last exception after all retries are exhausted.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            return urlopen(request, timeout=timeout)
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                logger.debug(
                    "Retry %d/%d for %s after %.1fs: %s",
                    attempt + 1,
                    max_retries,
                    request.full_url,
                    delay,
                    exc,
                )
                time.sleep(delay)
    raise last_exc  # type: ignore[misc]


@dataclass(frozen=True)
class SearchResult:
    """A single search result."""

    title: str
    url: str


@dataclass(frozen=True)
class FetchedInputBundle:
    """Resolved local input bundle for one target."""

    target_mcu: str
    target_peripheral: str
    manifest_path: str
    svd_path: str = ""
    header_path: str = ""
    driver_paths: tuple[str, ...] = ()
    documentation_paths: tuple[str, ...] = ()


class DuckDuckGoSearcher:
    """Minimal HTML search client for deterministic web lookups."""

    def __init__(self, *, user_agent: str = _USER_AGENT) -> None:
        self.user_agent = user_agent

    def search(self, query: str, *, max_results: int = 8) -> list[SearchResult]:
        url = f"https://html.duckduckgo.com/html/?q={quote(query)}"
        request = Request(url, headers={"User-Agent": self.user_agent})
        with _urlopen_with_retry(request, timeout=10) as response:
            doc = html.fromstring(response.read())

        results: list[SearchResult] = []
        for link in doc.xpath("//a[contains(@class, 'result__a')]"):
            href = _normalize_search_result_url(link.get("href", ""))
            if not href:
                continue
            title = " ".join(part.strip() for part in link.itertext()).strip()
            results.append(SearchResult(title=title, url=href))
            if len(results) >= max_results:
                break
        return results


def infer_stm32_mcu_family(target_mcu: str) -> str:
    """Infer a family label such as ``STM32F4`` from a concrete MCU name."""
    normalized = "".join(ch for ch in target_mcu.upper() if ch.isalnum())
    match = re.match(r"STM32([A-Z]+)(\d*)", normalized)
    if not match:
        return normalized or target_mcu.upper()
    letters, digits = match.groups()
    if len(letters) == 1 and digits:
        return f"STM32{letters}{digits[0]}"
    return f"STM32{letters}"



def normalize_target_peripheral(target_peripheral: str) -> str:
    """Normalize a peripheral name."""
    return "".join(ch for ch in target_peripheral.upper() if ch.isalnum())


def peripheral_search_tokens(target_peripheral: str) -> list[str]:
    """Build generic search tokens from a target peripheral string."""
    raw = target_peripheral.strip()
    tokens = [part.lower() for part in re.split(r"[^A-Za-z0-9]+", raw) if part]
    condensed = "".join(ch.lower() for ch in raw if ch.isalnum())
    if condensed and condensed not in tokens:
        tokens.append(condensed)
    return tokens or [raw.lower()]


def _path_slug(text: str) -> str:
    normalized = "".join(ch.lower() if ch.isalnum() else "_" for ch in text.strip())
    while "__" in normalized:
        normalized = normalized.replace("__", "_")
    return normalized.strip("_")


def _target_slug(target_mcu: str) -> str:
    return _path_slug(target_mcu)


def _peripheral_slug(target_peripheral: str) -> str:
    return _path_slug(target_peripheral)


def _normalize_search_result_url(url: str) -> str:
    if not url:
        return ""
    if url.startswith("//"):
        url = f"https:{url}"
    parsed = urlparse(url)
    if "duckduckgo.com" in parsed.netloc and parsed.path == "/l/":
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        return unquote(target) if target else ""
    return url


def _sanitize_url(url: str) -> str:
    """Percent-encode spaces and other unsafe characters in the URL path."""
    parsed = urlparse(url)
    safe_path = quote(parsed.path, safe="/:@!$&'()*+,;=-._~")
    safe_query = quote(parsed.query, safe="/:@!$&'()*+,;=-._~?=")
    return urlunparse((
        parsed.scheme, parsed.netloc, safe_path,
        parsed.params, safe_query, parsed.fragment,
    ))


def _normalize_download_url(url: str) -> str:
    normalized = _normalize_search_result_url(url)
    parsed = urlparse(normalized)
    if parsed.netloc == "github.com":
        parts = parsed.path.strip("/").split("/")
        if len(parts) >= 5 and parts[2] == "blob":
            owner, repo, _blob, branch = parts[:4]
            tail = "/".join(parts[4:])
            normalized = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{tail}"
        elif len(parts) >= 5 and parts[2] == "raw":
            owner, repo, _raw, branch = parts[:4]
            tail = "/".join(parts[4:])
            normalized = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{tail}"
    elif _is_gitiles_url(parsed):
        query = parse_qs(parsed.query)
        query["format"] = ["TEXT"]
        normalized = urlunparse(parsed._replace(query=urlencode(query, doseq=True)))
    return _sanitize_url(normalized)


def _is_gitiles_url(parsed) -> bool:
    """Return True for Gitiles blob URLs such as android.googlesource.com/.../+/."""
    return parsed.netloc.endswith("googlesource.com") and "/+/" in parsed.path


def _decode_gitiles_text_blob(download_url: str, data: bytes) -> bytes:
    parsed = urlparse(download_url)
    query = parse_qs(parsed.query)
    if not (_is_gitiles_url(parsed) and query.get("format") == ["TEXT"]):
        return data
    try:
        decoded = base64.b64decode(data.strip())
    except Exception:
        return data
    return decoded or data


def resolve_fetched_input_bundle(
    *,
    target_mcu: str,
    target_peripheral: str,
    data_dir: str | Path = "data/stm32",
) -> FetchedInputBundle:
    """Resolve local fetched inputs for one target."""
    output_root = Path(data_dir)
    manifest_path = output_root / "manifests" / f"{_target_slug(target_mcu)}_{_peripheral_slug(target_peripheral)}.json"

    docs: list[str] = []
    svds: list[str] = []
    headers: list[str] = []
    drivers: list[str] = []

    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for artifact in manifest.get("artifacts", []):
            if artifact.get("status") not in _OK_STATUSES:
                continue
            local_path = artifact.get("local_path", "")
            category = artifact.get("category", "")
            if not local_path or not Path(local_path).exists():
                continue
            # Verify SHA256 integrity when manifest records a hash
            expected_sha = artifact.get("sha256", "")
            if expected_sha:
                actual_sha = hashlib.sha256(Path(local_path).read_bytes()).hexdigest()
                if actual_sha != expected_sha:
                    logger.warning(
                        "SHA256 mismatch for %s: expected %s, got %s — skipping",
                        local_path, expected_sha[:12], actual_sha[:12],
                    )
                    continue
            if category == "docs":
                docs.append(local_path)
            elif category == "svd":
                svds.append(local_path)
            elif category in ("headers", "header"):
                headers.append(local_path)
            elif category.startswith("driver"):
                drivers.append(local_path)
    else:
        # Try STM32-style layout: <data_dir>/<target_slug>/{svd,headers,drivers,...}
        target_root = output_root / _target_slug(target_mcu)
        docs = _collect_doc_paths(target_root / "docs")
        svds = [str(path) for path in sorted((target_root / "svd").glob("*.svd")) if path.is_file()]
        headers = [str(path) for path in sorted((target_root / "headers").glob("*.h")) if path.is_file()]
        drivers = [
            str(path)
            for path in sorted((target_root / "drivers").rglob("*.c"))
            if path.is_file()
        ]

        # Also scan generic flat layout: <data_dir>/{svd,header,driver,docs}/*
        if not svds and not headers and not drivers:
            for scan_dir, ext, target_list in [
                ("svd", "*.svd", svds),
                ("svd", "*.xml", svds),
                ("header", "*.h", headers),
                ("headers", "*.h", headers),
                ("driver", "*.c", drivers),
                ("drivers", "*.c", drivers),
                ("docs", "*.txt", docs),
                ("docs", "*.pdf", docs),
                ("docs", "*.md", docs),
                ("docs", "*.html", docs),
                ("docs", "*.htm", docs),
                ("docs", "*.dts", docs),
                ("docs", "*.dtsi", docs),
            ]:
                candidate_dir = output_root / scan_dir
                if candidate_dir.is_dir():
                    target_list.extend(
                        str(p) for p in sorted(candidate_dir.glob(ext)) if p.is_file()
                    )

    return FetchedInputBundle(
        target_mcu=target_mcu,
        target_peripheral=target_peripheral,
        manifest_path=str(manifest_path),
        svd_path=svds[0] if svds else "",
        header_path=_select_best_header(headers, target_peripheral),
        driver_paths=tuple(drivers),
        documentation_paths=tuple(docs),
    )


def _collect_doc_paths(directory: Path) -> list[str]:
    docs: list[str] = []
    if not directory.is_dir():
        return docs
    for pattern in _DOC_GLOBS:
        docs.extend(str(path) for path in sorted(directory.glob(pattern)) if path.is_file())
    return docs


def _select_best_header(headers: list[str], target_peripheral: str) -> str:
    if not headers:
        return ""
    if len(headers) == 1:
        return headers[0]
    return sorted(
        headers,
        key=lambda path: (-_score_header_for_peripheral(path, target_peripheral), path),
    )[0]


def _score_header_for_peripheral(path: str, target_peripheral: str) -> int:
    peripheral = normalize_target_peripheral(target_peripheral)
    if not peripheral:
        return 0
    candidate = Path(path)
    score = 0
    if peripheral.lower() in candidate.name.lower():
        score += 50
    try:
        content = candidate.read_text(encoding="utf-8", errors="replace")[:262_144]
    except Exception:
        return score
    upper = content.upper()
    if re.search(rf"^\s*#\s*DEFINE\s+{re.escape(peripheral)}(?:_|$)", upper, re.MULTILINE):
        score += 120
    if re.search(rf"\b{re.escape(peripheral)}_TYPEDEF\b", upper):
        score += 100
    if re.search(rf"\b{re.escape(peripheral)}[A-Z0-9]*_BASE\b", upper):
        score += 80
    if f"{peripheral}_" in upper:
        score += 30
    elif peripheral in upper:
        score += 5
    return score


@dataclass
class SearchCandidate:
    """A discovered candidate resource for a target MCU/peripheral."""

    title: str
    url: str
    category: str  # svd, header, docs, driver
    score: int  # relevance 0-100
    description: str


def _is_device_tree_candidate(candidate: SearchCandidate) -> bool:
    text = f"{candidate.title} {candidate.url} {candidate.description}".lower()
    path = urlparse(candidate.url).path.lower()
    return (
        candidate.category == "docs"
        and (
            Path(path).suffix in _DEVICE_TREE_EXTS
            or "device tree" in text
            or "device-tree" in text
            or "dts" in text
            or "dtsi" in text
        )
    )


def _candidate_file_type(candidate: SearchCandidate) -> str:
    path = urlparse(candidate.url).path.lower()
    suffix = Path(path).suffix
    if suffix in _DEVICE_TREE_EXTS or _is_device_tree_candidate(candidate):
        return "device_tree"
    if suffix in {".c", ".cc", ".cpp"} or candidate.category.startswith("driver"):
        return "driver"
    if suffix == ".h" or candidate.category in {"header", "headers"}:
        return "header"
    if suffix in {".svd", ".xml"} or candidate.category == "svd":
        return "svd"
    if suffix in {".pdf", ".txt", ".md", ".html", ".htm"}:
        return "docs"
    return candidate.category


@dataclass
class GenericFetchResult:
    """Result of fetching selected candidates."""

    target_mcu: str
    target_peripheral: str
    output_dir: str
    downloaded: list[dict[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _score_result(result: SearchResult, category: str, mcu: str, peripheral: str) -> int:
    """Heuristic relevance score for a search result."""
    score = 50
    title_lower = result.title.lower()
    url_lower = result.url.lower()
    mcu_lower = mcu.lower()
    periph_lower = peripheral.lower()

    if mcu_lower in title_lower or mcu_lower in url_lower:
        score += 15
    if periph_lower in title_lower or periph_lower in url_lower:
        score += 10
    if "github.com" in url_lower:
        score += 5
    if "raw.githubusercontent.com" in url_lower:
        score += 5

    if category == "svd":
        if ".svd" in url_lower or "svd" in title_lower:
            score += 15
        if "cmsis" in title_lower or "cmsis" in url_lower:
            score += 5
    elif category == "header":
        if ".h" in url_lower:
            score += 10
        if "register" in title_lower:
            score += 5
    elif category == "docs":
        if ".pdf" in url_lower:
            score += 10
        if "datasheet" in title_lower or "reference manual" in title_lower:
            score += 10
        if "register map" in title_lower:
            score += 5
        if any(ext in url_lower for ext in _DEVICE_TREE_EXTS):
            score += 20
        if "device tree" in title_lower or "device-tree" in title_lower:
            score += 15
        if "dts" in title_lower or "dtsi" in title_lower:
            score += 10
    elif category == "driver":
        if ".c" in url_lower:
            score += 10
        if "driver" in title_lower:
            score += 5
        if "linux" in title_lower or "torvalds" in url_lower:
            score += 5

    return min(score, 100)


class GenericDataFetcher:
    """Fetch data for any MCU/peripheral target using web search."""

    def __init__(
        self,
        *,
        searcher: DuckDuckGoSearcher | None = None,
        search_timeout: int = 30,
        max_results_per_query: int = 8,
    ) -> None:
        self.searcher = searcher or DuckDuckGoSearcher(user_agent=_USER_AGENT)
        self.search_timeout = search_timeout
        self.max_results_per_query = max_results_per_query

    # Known raw source URLs for well-known Linux kernel drivers, keyed by
    # (vendor_substring, peripheral_keyword) → list of (url, category).
    # These supplement web search when the platform is recognised.
    _KNOWN_DRIVER_URLS: list[tuple[str, str, str, str]] = [
        # (vendor_match, peripheral_match, url, category)
        # HiSilicon / Kirin — Mali G71 (Bifrost) via panfrost
        ("hisilicon", "gpu",
         "https://raw.githubusercontent.com/torvalds/linux/master/drivers/gpu/drm/panfrost/panfrost_device.c",
         "driver"),
        ("hisilicon", "gpu",
         "https://raw.githubusercontent.com/torvalds/linux/master/drivers/gpu/drm/panfrost/panfrost_device.h",
         "header"),
        ("hisilicon", "gpu",
         "https://raw.githubusercontent.com/torvalds/linux/master/drivers/gpu/drm/panfrost/panfrost_regs.h",
         "header"),
        ("hisilicon", "gpu",
         "https://raw.githubusercontent.com/torvalds/linux/master/drivers/gpu/drm/panfrost/panfrost_job.c",
         "driver"),
        # HiSilicon / Kirin — ADE display engine (kirin_drm_dss was never in mainline)
        ("hisilicon", "display",
         "https://raw.githubusercontent.com/torvalds/linux/master/drivers/gpu/drm/hisilicon/kirin/kirin_drm_ade.c",
         "driver"),
        ("hisilicon", "display",
         "https://raw.githubusercontent.com/torvalds/linux/master/drivers/gpu/drm/hisilicon/kirin/kirin_ade_reg.h",
         "header"),
        ("hisilicon", "display",
         "https://raw.githubusercontent.com/torvalds/linux/master/drivers/gpu/drm/hisilicon/kirin/kirin_drm_drv.c",
         "driver"),
        ("hisilicon", "display",
         "https://raw.githubusercontent.com/torvalds/linux/master/drivers/gpu/drm/hisilicon/kirin/kirin_drm_drv.h",
         "header"),
        # STM32 — common peripheral headers from cmsis-svd / stm32-rs
        ("stm32", "uart",
         "https://raw.githubusercontent.com/stm32-rs/stm32-rs/master/stm32f4/src/stm32f407/usart1.rs",
         "docs"),
    ]

    def _known_candidates(self, mcu: str, peripheral: str) -> list[SearchCandidate]:
        """Return pre-scored candidates from the known-driver URL table."""
        from autoemu.platforms import analyze_target
        info = analyze_target(mcu)
        vendor = info.vendor.lower()
        periph_lower = peripheral.lower()
        candidates = []
        for vendor_key, periph_key, url, category in self._KNOWN_DRIVER_URLS:
            if vendor_key in vendor and periph_key in periph_lower:
                candidates.append(SearchCandidate(
                    title=f"[known] {url.split('/')[-1]}",
                    url=url,
                    category=category,
                    score=90,
                    description=f"[{category}] known kernel driver source",
                ))
        return candidates

    def _build_queries(self, mcu: str, peripheral: str) -> list[tuple[str, str]]:
        """Return (query_string, category) pairs for this target.

        Uses board analysis to generate smarter queries with vendor
        aliases and broader fallbacks.
        """
        from autoemu.platforms import analyze_target

        info = analyze_target(mcu)
        aliases = list(info.aliases) + [info.family, info.vendor]
        # Deduplicate and filter empties
        extra_terms = list(dict.fromkeys(t for t in aliases if t))

        queries: list[tuple[str, str]] = []

        # SVD queries (progressively broader)
        queries.append((f"{mcu} svd site:github.com", "svd"))
        queries.append((f"{mcu} svd register description", "svd"))

        # Header queries — include raw.githubusercontent.com variant to find actual .h files
        queries.append((f"{mcu} {peripheral} register header .h site:github.com", "header"))
        queries.append((f"{mcu} register map header definition", "header"))
        queries.append((f"site:raw.githubusercontent.com {mcu} {peripheral} .h", "header"))

        # Documentation queries (broad: include vendor terms)
        queries.append((f"{mcu} {peripheral} datasheet register map", "docs"))
        queries.append((f"{mcu} {peripheral} device tree dts dtsi", "docs"))
        for term in extra_terms[:2]:
            queries.append((f"{term} {peripheral} register map datasheet", "docs"))
            queries.append((f"{term} {peripheral} device tree dts dtsi", "docs"))

        # Driver queries — target raw source files specifically
        queries.append((f"{mcu} {peripheral} driver source code site:github.com", "driver"))
        queries.append((f"linux kernel driver {mcu} {peripheral} filetype:c", "driver"))
        queries.append((f"site:raw.githubusercontent.com linux {peripheral} driver .c", "driver"))
        for term in extra_terms[:2]:
            queries.append((f"linux kernel {term} {peripheral} driver site:github.com", "driver"))

        return queries

    def _run_single_search(
        self, query: str, category: str, mcu: str, peripheral: str
    ) -> list[SearchCandidate]:
        """Execute one search query and return scored candidates."""
        try:
            results = self.searcher.search(query, max_results=self.max_results_per_query)
        except Exception as exc:
            logger.warning("Search failed for query %r: %s", query, exc)
            return []

        candidates = []
        for r in results:
            score = _score_result(r, category, mcu, peripheral)
            candidates.append(
                SearchCandidate(
                    title=r.title,
                    url=r.url,
                    category=category,
                    score=score,
                    description=f"[{category}] {r.title}",
                )
            )
        return candidates

    def discover_candidates(
        self, target_mcu: str, target_peripheral: str
    ) -> list[SearchCandidate]:
        """Search for all candidate resources in parallel, return sorted by score."""
        queries = self._build_queries(target_mcu, target_peripheral)
        all_candidates: list[SearchCandidate] = []
        seen_urls: set[str] = set()

        # Seed with known-good driver URLs before any web search
        for candidate in self._known_candidates(target_mcu, target_peripheral):
            normalized = candidate.url.split("?")[0].rstrip("/")
            if normalized not in seen_urls:
                seen_urls.add(normalized)
                all_candidates.append(candidate)

        with ThreadPoolExecutor(max_workers=min(len(queries), 5)) as pool:
            futures = {
                pool.submit(
                    self._run_single_search, query, category, target_mcu, target_peripheral
                ): (query, category)
                for query, category in queries
            }
            for future in as_completed(futures, timeout=self.search_timeout):
                try:
                    results = future.result(timeout=10)
                    for candidate in results:
                        normalized = candidate.url.split("?")[0].rstrip("/")
                        if normalized not in seen_urls:
                            seen_urls.add(normalized)
                            all_candidates.append(candidate)
                except Exception as exc:
                    query, category = futures[future]
                    logger.warning("Search task failed for %r: %s", query, exc)

        all_candidates.sort(key=lambda c: c.score, reverse=True)
        return all_candidates

    def select_candidates(
        self,
        candidates: list[SearchCandidate],
        *,
        limit: int = 10,
    ) -> list[SearchCandidate]:
        """Select top-k candidates independently for each useful file type."""
        selected: list[SearchCandidate] = []
        seen_urls: set[str] = set()
        by_file_type: dict[str, list[SearchCandidate]] = {}
        type_order: list[str] = []

        for candidate in candidates:
            file_type = _candidate_file_type(candidate)
            if file_type not in by_file_type:
                by_file_type[file_type] = []
                type_order.append(file_type)
            by_file_type[file_type].append(candidate)

        for file_type in type_order:
            ranked = sorted(
                by_file_type[file_type],
                key=lambda item: item.score,
                reverse=True,
            )
            for candidate in ranked[:limit]:
                normalized = candidate.url.split("?")[0].rstrip("/")
                if normalized in seen_urls:
                    continue
                seen_urls.add(normalized)
                selected.append(candidate)

        selected.sort(key=lambda item: item.score, reverse=True)
        return selected

    def fetch_selected(
        self,
        candidates: list[SearchCandidate],
        output_dir: str | Path,
        target_mcu: str = "",
        target_peripheral: str = "",
    ) -> GenericFetchResult:
        """Download the selected candidates to output_dir."""
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        result = GenericFetchResult(
            target_mcu=target_mcu,
            target_peripheral=target_peripheral,
            output_dir=str(out),
        )

        # Exact filenames (case-insensitive, without extension) that are never
        # acceptable source files for svd/header/driver categories.
        _NON_SOURCE_STEMS = frozenset({
            "readme", "changelog", "license", "contributing", "authors",
            "kconfig", "makefile", "cmakelists", "build", "configure",
            "dockerfile", "gitignore", "gitattributes",
        })
        # Extensions that are documentation, not source code
        _DOC_EXTS = frozenset({".md", ".rst", ".txt", ".adoc", ".asciidoc", ".wiki"})

        for candidate in candidates:
            try:
                download_url = _normalize_download_url(candidate.url)

                # Pre-filter: reject build/doc/README URLs for source categories before download
                if candidate.category in ("svd", "header", "driver"):
                    url_tail = download_url.lower().split("/")[-1].split("?")[0]
                    url_stem = url_tail.rsplit(".", 1)[0] if "." in url_tail else url_tail
                    url_ext = ("." + url_tail.rsplit(".", 1)[1]) if "." in url_tail else ""
                    if url_stem in _NON_SOURCE_STEMS or url_ext in _DOC_EXTS:
                        msg = f"Skipped {download_url}: build/doc file not suitable for {candidate.category}"
                        result.errors.append(msg)
                        logger.info(msg)
                        continue

                filename = _url_to_filename(download_url, candidate.category)
                category_dir = out / candidate.category
                category_dir.mkdir(parents=True, exist_ok=True)
                dest = category_dir / filename

                request = Request(
                    download_url, headers={"User-Agent": _USER_AGENT}
                )
                response = _urlopen_with_retry(request, timeout=self.search_timeout)
                data = response.read()
                data = _decode_gitiles_text_blob(download_url, data)

                # Validate content matches expected category
                rejection = _check_content(data, candidate.category, filename)
                if rejection:
                    msg = f"Skipped {filename}: {rejection}"
                    result.errors.append(msg)
                    logger.info(msg)
                    continue

                dest.write_bytes(data)
                sha = hashlib.sha256(data).hexdigest()

                result.downloaded.append(
                    {
                        "title": candidate.title,
                        "url": candidate.url,
                        "category": candidate.category,
                        "local_path": str(dest),
                        "sha256": sha,
                        "size_bytes": str(len(data)),
                    }
                )
                logger.info("Downloaded %s -> %s", candidate.url, dest)

            except Exception as exc:
                msg = f"Failed to download {candidate.url}: {exc}"
                result.errors.append(msg)
                logger.warning(msg)

        return result


def _url_to_filename(url: str, category: str) -> str:
    """Derive a safe filename from a URL, ensuring a proper extension."""
    parsed = urlparse(url)
    name = Path(parsed.path).name
    if not name:
        name = f"{category}_{hashlib.md5(url.encode()).hexdigest()[:12]}"
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)

    # Extensions that are clearly web pages and should never be the final name
    _web_exts = (".html", ".htm", ".php", ".asp", ".aspx", ".jsp")
    # Extensions that are acceptable for each category (no further appending)
    _ok_exts_per_cat: dict[str, set[str]] = {
        "svd":    {".svd", ".xml"},
        "header": {".h"},
        "driver": {".c"},
        "docs":   {".pdf", ".txt", ".md", ".html", ".htm", ".dts", ".dtsi"},
    }
    _default_ext = {"svd": ".svd", "header": ".h", "driver": ".c", "docs": ".txt"}

    if category not in _default_ext:
        return name

    ok_exts = _ok_exts_per_cat[category]

    def _strip_ext(s: str) -> tuple[str, str]:
        """Return (stem, '.ext') for the last extension, or (s, '') if none."""
        if "." in s:
            stem, suf = s.rsplit(".", 1)
            return stem, f".{suf}"
        return s, ""

    stem, current_ext = _strip_ext(name)

    if category in ("driver", "header", "svd"):
        if current_ext.lower() in _web_exts:
            # Strip ALL non-source extensions from the compound stem.
            # e.g. "hardware-user-manual.md.html" → strip ".html", then ".md"
            clean = stem
            while True:
                inner_stem, inner_ext = _strip_ext(clean)
                if inner_ext and inner_ext not in ok_exts:
                    clean = inner_stem
                else:
                    break
            name = f"{clean}{_default_ext[category]}"
        elif current_ext not in ok_exts:
            # Unknown extension (e.g. ".md" for svd) → replace it
            name = f"{stem}{_default_ext[category]}"
        # else: extension is already correct for this category
    elif category == "docs":
        # For docs, strip trailing web ext if there's an inner extension
        if current_ext.lower() in _web_exts and "." in stem:
            name = stem  # e.g. "manual.md.html" → "manual.md"

    return name


def _check_content(data: bytes, category: str, filename: str) -> str | None:
    """Validate downloaded content matches the expected category.

    Returns a rejection reason string, or None if the content is acceptable.
    """
    # Empty files are never useful
    if len(data) < 10:
        return "file is empty or too small"

    # Extension-based early rejection: if the original filename reveals the true
    # type, reject before inspecting content.
    _doc_exts = {".md", ".rst", ".txt", ".adoc", ".wiki"}
    # Stems (case-insensitive) that are never valid source files regardless of extension
    _non_source_stems = frozenset({
        "readme", "changelog", "license", "licence", "contributing", "authors",
        "notice", "install", "todo", "credits",
        "kconfig", "makefile", "cmakelists", "configure",
        "dockerfile", "gitignore", "gitattributes",
    })
    fname_lower = filename.lower()
    fname_stem = fname_lower.rsplit(".", 1)[0] if "." in fname_lower else fname_lower
    fname_ext = ("." + fname_lower.rsplit(".", 1)[1]) if "." in fname_lower else ""
    if category in ("driver", "header", "svd"):
        if fname_ext in _doc_exts:
            return f"documentation file ({fname_ext}), not {category} source"
        if fname_stem in _non_source_stems:
            return f"non-source file ({fname_stem}), not {category} source"

    # Reject binary/non-UTF8 content for text-based categories
    if category in ("driver", "header", "svd"):
        null_count = data[:4096].count(b"\x00")
        non_printable = sum(
            1 for b in data[:4096]
            if b < 9 or (13 < b < 32 and b not in (9, 10, 13))
        )
        if null_count > 0 or non_printable > len(data[:4096]) * 0.05:
            return "binary file, not text source"

    # Try to detect HTML pages masquerading as source/data files
    head = data[:500].lstrip()
    is_html = (
        head.startswith(b"<!DOCTYPE") or head.startswith(b"<!doctype")
        or head.startswith(b"<html") or head.startswith(b"<HTML")
        or b"<head>" in head or b"<HEAD>" in head
    )

    if category == "driver":
        # Driver files should be C source, not HTML
        if is_html:
            return "HTML page, not C source code"
        # Check for at least some C-like content
        text = data[:2000].decode("utf-8", errors="ignore")
        if not any(tok in text for tok in ("void ", "int ", "#include", "#define", "return", "struct ", "typedef ")):
            # Kconfig-style files: start with "config " or "menuconfig "
            if text.lstrip().startswith(("config ", "menuconfig ", "source ")):
                return "Kconfig/build file, not C source code"
            # Markdown
            if text.startswith("#") and "\n##" in text:
                return "Markdown document, not C source code"
    elif category == "header":
        if is_html:
            return "HTML page, not C header"
        text = data[:2000].decode("utf-8", errors="ignore")
        if not any(tok in text for tok in ("#ifndef", "#define", "#pragma", "typedef ", "struct ", "#include")):
            if text.startswith("#") and "\n##" in text:
                return "Markdown document, not C header"
    elif category == "svd":
        if is_html:
            return "HTML page, not SVD/XML"
        text = data[:500].decode("utf-8", errors="ignore").lstrip()
        if not (text.startswith("<?xml") or text.startswith("<device") or text.startswith("<peripheral")):
            # SVD must be XML — reject anything that isn't
            if text.startswith("#"):
                return "Markdown document, not SVD file"
            if not text.startswith("<"):
                return "Not an XML/SVD file"
    elif category == "docs":
        if fname_ext in _DEVICE_TREE_EXTS and is_html:
            return "HTML page, not DTS/DTSI source"
    # docs category: accept anything else (PDF, text, markdown, HTML)

    return None
