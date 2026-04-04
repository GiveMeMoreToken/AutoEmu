"""Generic fetcher base with HTTP, caching, and manifest logic."""
from __future__ import annotations

import hashlib
import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


def urlopen_with_retry(
    request: Request,
    *,
    timeout: int = 10,
    max_retries: int = 3,
    base_delay: float = 1.0,
):
    """HTTP request with exponential-backoff retry."""
    last_exc = None
    for attempt in range(max_retries):
        try:
            return urlopen(request, timeout=timeout)
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                time.sleep(base_delay * (2 ** attempt))
    raise last_exc  # type: ignore[misc]


def compute_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass
class FetchManifest:
    target: str
    peripheral: str
    platform: str
    output_dir: str
    generated_at: str = ""
    success: bool = False
    warnings: list[str] = field(default_factory=list)
    artifacts: list[dict] = field(default_factory=list)

    def save(self, path: Path) -> None:
        self.generated_at = datetime.now(timezone.utc).isoformat()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(asdict(self), indent=2, sort_keys=True), encoding="utf-8"
        )

    @classmethod
    def load(cls, path: Path) -> FetchManifest:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class BaseFetcher(ABC):
    """Abstract base for platform-specific data fetchers."""

    def __init__(self, *, user_agent: str = "AutoEmu/0.1", offline: bool = False):
        self.user_agent = user_agent
        self.offline = offline

    @abstractmethod
    def fetch(
        self,
        *,
        target: str,
        peripheral: str,
        output_dir: Path,
        refresh: bool = False,
    ) -> FetchManifest:
        ...

    def download_file(
        self, url: str, destination: Path, *, refresh: bool = False
    ) -> tuple[bool, str]:
        """Download a URL to destination. Returns (success, sha256)."""
        if destination.exists() and not refresh:
            return True, compute_sha256(destination)
        if self.offline:
            return False, ""
        try:
            request = Request(url, headers={"User-Agent": self.user_agent})
            with urlopen_with_retry(request, timeout=10) as response:
                data = response.read()
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            return True, hashlib.sha256(data).hexdigest()
        except Exception:
            return False, ""
