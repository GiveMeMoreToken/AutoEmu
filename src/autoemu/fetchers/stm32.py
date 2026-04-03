"""Generic STM32 input-data fetcher and fetched-input resolver."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import Request, urlopen

from lxml import html


_USER_AGENT = "AutoEmu/0.1"
_ST_DOMAINS = ("st.com", "www.st.com", "r.jina.ai")
_GITHUB_DOMAINS = ("github.com", "raw.githubusercontent.com")
_DOWNLOAD_TIMEOUT = 10
_OK_STATUSES = {"downloaded", "cached"}


@dataclass(frozen=True)
class SearchResult:
    """A single search result."""

    title: str
    url: str


@dataclass(frozen=True)
class FetchRequest:
    """Target-specific fetch request."""

    target_mcu: str
    target_peripheral: str


@dataclass(frozen=True)
class AssetRequest:
    """Specification for one generic fetchable asset."""

    key: str
    category: str
    description: str
    relative_path: str
    queries: tuple[str, ...]
    preferred_domains: tuple[str, ...]
    expected_tokens: tuple[str, ...] = ()
    required: bool = False
    max_matches: int = 1
    file_extensions: tuple[str, ...] = ()
    target_mcu: str = ""
    target_peripheral: str = ""
    family_prefix: str = ""
    device_stem: str = ""
    header_name: str = ""


@dataclass
class ResolvedArtifact:
    """Manifest entry for a fetched artifact."""

    key: str
    category: str
    description: str
    status: str = "pending"
    source_url: str = ""
    resolved_via: str = ""
    query: str = ""
    local_path: str = ""
    sha256: str = ""
    size_bytes: int = 0
    error: str = ""


@dataclass
class FetchResult:
    """Result for one target fetch run."""

    request: FetchRequest
    output_dir: str
    manifest_path: str = ""
    artifacts: list[ResolvedArtifact] = field(default_factory=list)
    success: bool = False

    def to_manifest(self) -> dict[str, object]:
        return {
            "target_mcu": self.request.target_mcu,
            "target_peripheral": self.request.target_peripheral,
            "output_dir": self.output_dir,
            "manifest_path": self.manifest_path,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "success": self.success,
            "artifacts": [asdict(artifact) for artifact in self.artifacts],
        }

    def summary_lines(self) -> list[str]:
        lines = [
            f"Target MCU: {self.request.target_mcu}",
            f"Target peripheral: {self.request.target_peripheral}",
            f"Manifest: {self.manifest_path}",
        ]
        for artifact in self.artifacts:
            if artifact.status in _OK_STATUSES:
                lines.append(f"  [{artifact.status}] {artifact.key}: {artifact.local_path}")
            else:
                lines.append(f"  [{artifact.status}] {artifact.key}: {artifact.error or artifact.source_url}")
        return lines


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
        with urlopen(request, timeout=10) as response:
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


class STM32DataFetcher:
    """Fetch STM32 data for a target MCU and peripheral using generic heuristics."""

    def __init__(
        self,
        *,
        searcher: DuckDuckGoSearcher | None = None,
        user_agent: str = _USER_AGENT,
        max_results_per_query: int = 8,
        enable_fallbacks: bool = True,
    ) -> None:
        self.searcher = searcher or DuckDuckGoSearcher(user_agent=user_agent)
        self.user_agent = user_agent
        self.max_results_per_query = max_results_per_query
        self.enable_fallbacks = enable_fallbacks
        self._repo_listing_cache: dict[tuple[str, str], list[dict[str, str]]] = {}

    def fetch_data(
        self,
        *,
        target_mcu: str,
        target_peripheral: str,
        output_dir: str | Path = "data/stm32",
        refresh: bool = False,
    ) -> FetchResult:
        request = FetchRequest(
            target_mcu=target_mcu,
            target_peripheral=target_peripheral,
        )
        output_root = Path(output_dir)
        artifacts: list[ResolvedArtifact] = []

        for asset in build_asset_requests(request):
            artifacts.extend(
                self._fetch_asset_request(
                    asset,
                    output_root=output_root,
                    refresh=refresh,
                )
            )

        result = FetchResult(
            request=request,
            output_dir=str(output_root),
            artifacts=artifacts,
        )
        result.success = _has_register_source(artifacts) and _has_driver_source(artifacts)

        manifest_dir = output_root / "manifests"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = manifest_dir / f"{_target_slug(target_mcu)}_{_peripheral_slug(target_peripheral)}.json"
        result.manifest_path = str(manifest_path)
        manifest_path.write_text(
            json.dumps(result.to_manifest(), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return result

    def _fetch_asset_request(
        self,
        asset: AssetRequest,
        *,
        output_root: Path,
        refresh: bool,
    ) -> list[ResolvedArtifact]:
        resolved_candidates = self._resolve_asset_candidates(asset)
        if not resolved_candidates:
            return [
                ResolvedArtifact(
                    key=asset.key,
                    category=asset.category,
                    description=asset.description,
                    status="unresolved",
                    error="No matching source found",
                )
            ]

        if asset.max_matches <= 1:
            destination = output_root / asset.relative_path
            last_artifact: ResolvedArtifact | None = None
            for candidate in resolved_candidates:
                artifact = self._download_candidate(
                    key=asset.key,
                    category=asset.category,
                    description=asset.description,
                    destination=destination,
                    candidate=candidate,
                    refresh=refresh,
                )
                if artifact.status in _OK_STATUSES:
                    return [artifact]
                last_artifact = artifact
            return [last_artifact] if last_artifact else []

        destination_dir = output_root / asset.relative_path
        destination_dir.mkdir(parents=True, exist_ok=True)
        artifacts: list[ResolvedArtifact] = []
        seen_destinations: set[Path] = set()

        for index, candidate in enumerate(resolved_candidates):
            if len(artifacts) >= asset.max_matches:
                break
            destination = destination_dir / _candidate_filename(candidate[0], default=f"{asset.key}_{index}.c")
            if destination in seen_destinations:
                continue
            seen_destinations.add(destination)
            artifact = self._download_candidate(
                key=f"{asset.key}:{destination.name}",
                category=asset.category,
                description=asset.description,
                destination=destination,
                candidate=candidate,
                refresh=refresh,
            )
            if artifact.status in _OK_STATUSES:
                artifacts.append(artifact)

        if artifacts:
            return artifacts

        failed = ResolvedArtifact(
            key=asset.key,
            category=asset.category,
            description=asset.description,
            status="error",
            error="No candidates downloaded successfully",
        )
        return [failed]

    def _download_candidate(
        self,
        *,
        key: str,
        category: str,
        description: str,
        destination: Path,
        candidate: tuple[str, str, str],
        refresh: bool,
    ) -> ResolvedArtifact:
        source_url, resolved_via, query = candidate
        artifact = ResolvedArtifact(
            key=key,
            category=category,
            description=description,
            source_url=source_url,
            resolved_via=resolved_via,
            query=query,
        )
        destination.parent.mkdir(parents=True, exist_ok=True)

        if destination.exists() and not refresh:
            data = destination.read_bytes()
            artifact.status = "cached"
            artifact.local_path = str(destination)
            artifact.size_bytes = len(data)
            artifact.sha256 = hashlib.sha256(data).hexdigest()
            return artifact

        download_url = _materialize_download_url(source_url, category)
        try:
            request = Request(download_url, headers={"User-Agent": self.user_agent})
            with urlopen(request, timeout=_DOWNLOAD_TIMEOUT) as response:
                data = response.read()
            destination.write_bytes(data)
            artifact.status = "downloaded"
            artifact.local_path = str(destination)
            artifact.size_bytes = len(data)
            artifact.sha256 = hashlib.sha256(data).hexdigest()
            return artifact
        except Exception as exc:
            artifact.status = "error"
            artifact.error = f"{download_url}: {exc}"
            return artifact

    def _resolve_asset_candidates(
        self,
        asset: AssetRequest,
    ) -> list[tuple[str, str, str]]:
        seen: set[str] = set()
        candidates: list[tuple[int, str, str, str]] = []

        if self.enable_fallbacks:
            for fallback_url in self._resolve_fallback_urls(asset):
                normalized_url = _normalize_download_url(fallback_url)
                if normalized_url in seen:
                    continue
                seen.add(normalized_url)
                score = _score_candidate(asset, normalized_url, normalized_url)
                if score <= 0:
                    continue
                candidates.append((score + 10, normalized_url, "fallback", ""))

        if asset.category in {"headers", "drivers_hal", "drivers_ll"} and len(candidates) >= asset.max_matches:
            candidates.sort(key=lambda item: item[0], reverse=True)
            return [(url, resolved_via, query) for _, url, resolved_via, query in candidates]

        for query in asset.queries:
            try:
                results = self.searcher.search(query, max_results=self.max_results_per_query)
            except Exception:
                results = []

            for result in results:
                normalized_url = _normalize_download_url(result.url)
                if normalized_url in seen:
                    continue
                seen.add(normalized_url)

                score = _score_candidate(asset, normalized_url, result.title)
                if score <= 0:
                    continue
                candidates.append((score, normalized_url, "search", query))

        candidates.sort(key=lambda item: item[0], reverse=True)
        return [(url, resolved_via, query) for _, url, resolved_via, query in candidates]

    def _resolve_fallback_urls(self, asset: AssetRequest) -> list[str]:
        if asset.category == "headers":
            return _header_fallback_urls(asset.target_mcu, asset.header_name)
        if asset.category in {"drivers_hal", "drivers_ll"}:
            return self._discover_driver_urls(asset)
        return []

    def _discover_driver_urls(self, asset: AssetRequest) -> list[str]:
        if not asset.family_prefix:
            return []

        repo = f"STMicroelectronics/{asset.family_prefix}-hal-driver"
        entries = self._github_directory_listing(repo, "Src")
        if not entries:
            return []

        tokens = peripheral_search_tokens(asset.target_peripheral)
        category_hint = "hal" if asset.category == "drivers_hal" else "ll"
        scored: list[tuple[int, str]] = []

        for entry in entries:
            name = entry.get("name", "")
            download_url = entry.get("download_url", "")
            if not name.endswith(".c") or not download_url:
                continue

            text = name.lower()
            filename_matches = [token for token in tokens if _token_present(text, token)]
            preview = ""
            preview_matches: list[str] = []

            if not filename_matches:
                preview = self._download_text_preview(download_url).lower()
                preview_matches = [token for token in tokens if _token_present(preview, token)]

            if not filename_matches and not preview_matches:
                continue

            score = (len(filename_matches) * 30) + (len(preview_matches) * 12)
            if category_hint in text:
                score += 8
            elif preview and category_hint in preview:
                score += 4

            scored.append((score, download_url))

        scored.sort(key=lambda item: item[0], reverse=True)
        return [url for _, url in scored[: max(asset.max_matches * 2, 4)]]

    def _github_directory_listing(self, repo: str, path: str) -> list[dict[str, str]]:
        cache_key = (repo, path)
        if cache_key in self._repo_listing_cache:
            return self._repo_listing_cache[cache_key]

        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/vnd.github+json",
        }
        for branch in ("main", "master"):
            url = f"https://api.github.com/repos/{repo}/contents/{path}?ref={branch}"
            try:
                request = Request(url, headers=headers)
                with urlopen(request, timeout=_DOWNLOAD_TIMEOUT) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if isinstance(payload, list):
                    self._repo_listing_cache[cache_key] = payload
                    return payload
            except Exception:
                continue

        self._repo_listing_cache[cache_key] = []
        return []

    def _download_text_preview(self, url: str, *, limit: int = 12000) -> str:
        try:
            request = Request(url, headers={"User-Agent": self.user_agent})
            with urlopen(request, timeout=_DOWNLOAD_TIMEOUT) as response:
                data = response.read(limit)
            return data.decode("utf-8", errors="ignore")
        except Exception:
            return ""


def build_asset_requests(request: FetchRequest) -> list[AssetRequest]:
    """Build generic asset requests for one target."""
    target_root = _target_slug(request.target_mcu)
    peripheral = request.target_peripheral.strip()
    family_name = infer_stm32_mcu_family(request.target_mcu)
    family_driver_prefix = infer_stm32_driver_prefix(request.target_mcu)
    device_stem = infer_stm32_device_stem(request.target_mcu)
    header_name = infer_stm32_header_name(request.target_mcu)
    peripheral_tokens = peripheral_search_tokens(request.target_peripheral)
    peripheral_text = request.target_peripheral.replace("_", " ").strip() or request.target_peripheral
    primary_token = peripheral_tokens[0] if peripheral_tokens else peripheral.lower()

    return [
        AssetRequest(
            key="reference_manual",
            category="docs",
            description="Reference manual text",
            relative_path=f"{target_root}/docs/reference_manual.txt",
            queries=(
                f'site:st.com/resource/en/reference_manual "{request.target_mcu}" pdf',
                f'site:st.com "{request.target_mcu}" "reference manual" pdf',
                f'site:st.com "{family_name}" "reference manual" pdf',
            ),
            preferred_domains=_ST_DOMAINS,
            expected_tokens=(request.target_mcu.lower(), family_name.lower(), "reference", "manual"),
            file_extensions=(".pdf", ".txt"),
            target_mcu=request.target_mcu,
            target_peripheral=request.target_peripheral,
            family_prefix=family_driver_prefix,
            device_stem=device_stem,
            header_name=header_name,
        ),
        AssetRequest(
            key="datasheet",
            category="docs",
            description="Datasheet text",
            relative_path=f"{target_root}/docs/datasheet.txt",
            queries=(
                f'site:st.com/resource/en/datasheet "{request.target_mcu}" pdf',
                f'site:st.com "{request.target_mcu}" datasheet pdf',
            ),
            preferred_domains=_ST_DOMAINS,
            expected_tokens=(request.target_mcu.lower(), "datasheet", "pdf"),
            file_extensions=(".pdf", ".txt"),
            target_mcu=request.target_mcu,
            target_peripheral=request.target_peripheral,
            family_prefix=family_driver_prefix,
            device_stem=device_stem,
            header_name=header_name,
        ),
        AssetRequest(
            key="svd",
            category="svd",
            description="CMSIS-SVD device description",
            relative_path=f"{target_root}/svd/device.svd",
            queries=(
                f'site:github.com modm-io "{device_stem}.svd"',
                f'site:github.com modm-io "{request.target_mcu}" ".svd"',
            ),
            preferred_domains=_GITHUB_DOMAINS,
            expected_tokens=(device_stem.lower(), ".svd"),
            required=False,
            file_extensions=(".svd",),
            target_mcu=request.target_mcu,
            target_peripheral=request.target_peripheral,
            family_prefix=family_driver_prefix,
            device_stem=device_stem,
            header_name=header_name,
        ),
        AssetRequest(
            key="cmsis_header",
            category="headers",
            description="CMSIS device header",
            relative_path=f"{target_root}/headers/{header_name}",
            queries=(
                f'site:github.com STMicroelectronics "{header_name}"',
                f'site:github.com STMicroelectronics "{family_driver_prefix}" "{header_name}"',
            ),
            preferred_domains=_GITHUB_DOMAINS,
            expected_tokens=(header_name.lower(), family_driver_prefix.lower()),
            file_extensions=(".h",),
            target_mcu=request.target_mcu,
            target_peripheral=request.target_peripheral,
            family_prefix=family_driver_prefix,
            device_stem=device_stem,
            header_name=header_name,
        ),
        AssetRequest(
            key="hal_driver",
            category="drivers_hal",
            description="HAL driver sources related to the target peripheral",
            relative_path=f"{target_root}/drivers/hal",
            queries=(
                f'site:github.com STMicroelectronics "{family_driver_prefix}" hal "{peripheral_text}" ".c"',
                f'site:github.com STMicroelectronics "{family_driver_prefix}" "{primary_token}" ".c"',
            ),
            preferred_domains=_GITHUB_DOMAINS,
            expected_tokens=(family_driver_prefix.lower(), primary_token.lower(), "hal"),
            required=False,
            max_matches=3,
            file_extensions=(".c",),
            target_mcu=request.target_mcu,
            target_peripheral=request.target_peripheral,
            family_prefix=family_driver_prefix,
            device_stem=device_stem,
            header_name=header_name,
        ),
        AssetRequest(
            key="ll_driver",
            category="drivers_ll",
            description="LL driver sources related to the target peripheral",
            relative_path=f"{target_root}/drivers/ll",
            queries=(
                f'site:github.com STMicroelectronics "{family_driver_prefix}" ll "{peripheral_text}" ".c"',
                f'site:github.com STMicroelectronics "{family_driver_prefix}" "{primary_token}" ".c"',
            ),
            preferred_domains=_GITHUB_DOMAINS,
            expected_tokens=(family_driver_prefix.lower(), primary_token.lower(), "ll"),
            required=False,
            max_matches=2,
            file_extensions=(".c",),
            target_mcu=request.target_mcu,
            target_peripheral=request.target_peripheral,
            family_prefix=family_driver_prefix,
            device_stem=device_stem,
            header_name=header_name,
        ),
        AssetRequest(
            key="rtos_driver",
            category="drivers_rtos",
            description="RTOS adaptation sources related to the target peripheral",
            relative_path=f"{target_root}/drivers/rtos",
            queries=(
                f'site:github.com zephyrproject-rtos stm32 "{peripheral_text}" ".c"',
                f'site:github.com zephyrproject-rtos "{family_name}" "{peripheral_text}" ".c"',
            ),
            preferred_domains=_GITHUB_DOMAINS,
            expected_tokens=(primary_token.lower(), "stm32"),
            required=False,
            max_matches=2,
            file_extensions=(".c",),
            target_mcu=request.target_mcu,
            target_peripheral=request.target_peripheral,
            family_prefix=family_driver_prefix,
            device_stem=device_stem,
            header_name=header_name,
        ),
    ]


def infer_stm32_mcu_family(target_mcu: str) -> str:
    """Infer a family label such as ``STM32F4`` from a concrete MCU name."""
    normalized = normalize_stm32_target_mcu(target_mcu)
    match = re.match(r"STM32([A-Z]+)(\d*)", normalized)
    if not match:
        return normalized or target_mcu.upper()
    letters, digits = match.groups()
    if len(letters) == 1 and digits:
        return f"STM32{letters}{digits[0]}"
    return f"STM32{letters}"


def infer_stm32_driver_prefix(target_mcu: str) -> str:
    """Infer the CMSIS/HAL family prefix such as ``stm32f4xx``."""
    normalized = normalize_stm32_target_mcu(target_mcu)
    match = re.match(r"STM32([A-Z]+)(\d*)", normalized)
    if not match:
        return f"{normalized.lower()}xx"
    letters, digits = match.groups()
    if len(letters) == 1 and digits:
        return f"stm32{letters.lower()}{digits[0]}xx"
    return f"stm32{letters.lower()}xx"


def infer_stm32_repo_suffix(target_mcu: str) -> str:
    """Infer the repository family suffix such as ``f4`` or ``wl``."""
    return infer_stm32_driver_prefix(target_mcu).removeprefix("stm32").removesuffix("xx")


def infer_stm32_device_stem(target_mcu: str) -> str:
    """Infer a device stem such as ``STM32F407`` from ``STM32F407VG``."""
    normalized = normalize_stm32_target_mcu(target_mcu)
    match = re.match(r"(STM32[A-Z]+\d+)", normalized)
    return match.group(1) if match else normalized


def infer_stm32_header_name(target_mcu: str) -> str:
    """Infer a CMSIS device header name."""
    return f"{infer_stm32_device_stem(target_mcu).lower()}xx.h"


def normalize_stm32_target_mcu(target_mcu: str) -> str:
    """Normalize an STM32 target MCU or board name."""
    return "".join(ch for ch in target_mcu.upper() if ch.isalnum())


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
            if category == "docs":
                docs.append(local_path)
            elif category == "svd":
                svds.append(local_path)
            elif category == "headers":
                headers.append(local_path)
            elif category.startswith("drivers_"):
                drivers.append(local_path)
    else:
        target_root = output_root / _target_slug(target_mcu)
        docs = [str(path) for path in sorted((target_root / "docs").glob("*.txt")) if path.is_file()]
        svds = [str(path) for path in sorted((target_root / "svd").glob("*.svd")) if path.is_file()]
        headers = [str(path) for path in sorted((target_root / "headers").glob("*.h")) if path.is_file()]
        drivers = [
            str(path)
            for path in sorted((target_root / "drivers").rglob("*.c"))
            if path.is_file()
        ]

    return FetchedInputBundle(
        target_mcu=target_mcu,
        target_peripheral=target_peripheral,
        manifest_path=str(manifest_path),
        svd_path=svds[0] if svds else "",
        header_path=headers[0] if headers else "",
        driver_paths=tuple(drivers),
        documentation_paths=tuple(docs),
    )


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


def _normalize_download_url(url: str) -> str:
    normalized = _normalize_search_result_url(url)
    parsed = urlparse(normalized)

    if parsed.netloc == "github.com":
        parts = parsed.path.strip("/").split("/")
        if len(parts) >= 5 and parts[2] == "blob":
            owner, repo, _blob, branch = parts[:4]
            tail = "/".join(parts[4:])
            return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{tail}"
        if len(parts) >= 5 and parts[2] == "raw":
            owner, repo, _raw, branch = parts[:4]
            tail = "/".join(parts[4:])
            return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{tail}"

    return normalized


def _materialize_download_url(url: str, category: str) -> str:
    if category == "docs" and _looks_like_pdf_url(url):
        parsed = urlparse(url)
        if not parsed.netloc or parsed.scheme == "file":
            return url
        normalized_domain = parsed.netloc.lower().removeprefix("www.")
        if normalized_domain == "r.jina.ai":
            return url
        target = f"{parsed.netloc}{parsed.path}"
        if parsed.query:
            target = f"{target}?{parsed.query}"
        return f"https://r.jina.ai/http://{target}"
    return url


def _candidate_filename(url: str, *, default: str) -> str:
    normalized = url
    if "/http://" in normalized:
        normalized = normalized.split("/http://", 1)[1]
    elif "/https://" in normalized:
        normalized = normalized.split("/https://", 1)[1]
    parsed = urlparse(normalized if "://" in normalized else f"https://{normalized}")
    name = Path(parsed.path).name
    if not name:
        return default
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)


def _looks_like_pdf_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.path.lower().endswith(".pdf")


def _token_present(text: str, token: str) -> bool:
    if not token:
        return False
    normalized_text = text.lower()
    normalized_token = token.lower()
    if not normalized_token.isalnum():
        return normalized_token in normalized_text
    pattern = rf"(?<![a-z0-9]){re.escape(normalized_token)}(?![a-z0-9])"
    return re.search(pattern, normalized_text) is not None


def _score_candidate(asset: AssetRequest, url: str, title: str) -> int:
    parsed = urlparse(url)
    if parsed.scheme == "file":
        domain_score = 80
    else:
        domain_score = _score_domain(parsed.netloc, asset.preferred_domains)
        if domain_score <= 0:
            return 0

    combined = f"{url} {title}".lower()
    token_score = 0
    for token in asset.expected_tokens:
        if _token_present(combined, token):
            token_score += 15

    if asset.file_extensions and any(combined.endswith(ext) for ext in asset.file_extensions):
        token_score += 5
    elif asset.file_extensions and any(ext in combined for ext in asset.file_extensions):
        token_score += 3

    if asset.category == "docs" and ".pdf" in combined:
        token_score += 10
    if asset.category == "headers" and ".h" in combined:
        token_score += 10
    if asset.category == "svd" and ".svd" in combined:
        token_score += 10

    return domain_score + token_score


def _score_domain(domain: str, preferred_domains: tuple[str, ...]) -> int:
    normalized_domain = domain.lower().removeprefix("www.")
    for index, preferred in enumerate(preferred_domains):
        normalized_preferred = preferred.lower().removeprefix("www.")
        if normalized_domain == normalized_preferred or normalized_domain.endswith(f".{normalized_preferred}"):
            return 100 - (index * 5)
    return 0


def _has_register_source(artifacts: list[ResolvedArtifact]) -> bool:
    return any(
        artifact.status in _OK_STATUSES and artifact.category in {"svd", "headers"}
        for artifact in artifacts
    )


def _has_driver_source(artifacts: list[ResolvedArtifact]) -> bool:
    return any(
        artifact.status in _OK_STATUSES and artifact.category.startswith("drivers_")
        for artifact in artifacts
    )


def _target_slug(target_mcu: str) -> str:
    return _path_slug(target_mcu)


def _peripheral_slug(target_peripheral: str) -> str:
    return _path_slug(target_peripheral)


def _path_slug(text: str) -> str:
    normalized = "".join(ch.lower() if ch.isalnum() else "_" for ch in text.strip())
    while "__" in normalized:
        normalized = normalized.replace("__", "_")
    return normalized.strip("_")


def _header_fallback_urls(target_mcu: str, header_name: str) -> list[str]:
    if not header_name:
        return []
    repo_suffix = infer_stm32_repo_suffix(target_mcu)
    return [
        f"https://raw.githubusercontent.com/STMicroelectronics/cmsis-device-{repo_suffix}/main/Include/{header_name}",
        f"https://raw.githubusercontent.com/STMicroelectronics/cmsis-device-{repo_suffix}/master/Include/{header_name}",
    ]
