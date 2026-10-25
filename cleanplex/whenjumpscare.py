"""On-demand client for WhenJumpScare's human-timed jumpscare lists.

The site publishes community-timed fear markers for films and exposes a small
JSON endpoint plus a schema.org JSON-LD list per movie page. Its terms allow
on-demand lookups but forbid bulk redistribution, so this module fetches one
title at a time and never mirrors or caches the catalogue.

This is a *second* external source alongside VideoSkip: fear timings only, films
only, but it covers titles VideoSkip does not.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from .logger import get_logger

logger = get_logger(__name__)

BASE_URL = os.environ.get(
    "CLEANPLEX_WHENJUMPSCARE_URL", "https://www.whenjumpscare.com"
).rstrip("/")

REQUEST_TIMEOUT = 20.0
MAX_ATTEMPTS = 3
# One request per second is enough for interactive use and keeps a bulk fetch
# from looking like an attack on a volunteer-run site.
MIN_REQUEST_INTERVAL = 1.0
_HEADERS = {
    "User-Agent": "Cleanplex/1.0.5 (Plex scene filter; +https://github.com/loukaniko85/Cleanplex)",
    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
}

# A jumpscare is a single moment; give it a short window so the seek lands past
# the spike without cutting the scene around it.
WINDOW_BEFORE_MS = 1200
WINDOW_AFTER_MS = 1500

_JSONLD_RE = re.compile(
    r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', re.S | re.I
)
_ITEM_RE = re.compile(
    r"(?P<severity>Minor|Major)\s+jumpscare\s+at\s+(?P<timestamp>\d{1,2}:\d{2}:\d{2})",
    re.I,
)


class WhenJumpScareError(RuntimeError):
    """The service is unreachable or returned an unusable page."""


@dataclass(frozen=True)
class Jumpscare:
    """One timestamped scare, normalized to milliseconds."""

    start_ms: int
    severity: str  # "low" (minor) or "high" (major)
    timestamp: str


def _to_ms(value: str) -> int:
    hours, minutes, seconds = value.split(":")
    return (int(hours) * 3600 + int(minutes) * 60 + int(seconds)) * 1000


def parse_movie_page(html: str) -> list[Jumpscare]:
    """Return the jumpscares embedded in a movie page, in time order.

    Prefers the schema.org `ItemList` JSON-LD block ("Minor jumpscare at
    00:03:36"), with a text fallback so a layout change does not silently return
    an empty list.
    """
    scares: list[Jumpscare] = []
    for block in _JSONLD_RE.findall(html):
        try:
            data = json.loads(block)
        except (TypeError, ValueError):
            continue
        for item in _iter_item_lists(data):
            for entry in item.get("itemListElement") or []:
                name = str(entry.get("name") or "") if isinstance(entry, dict) else ""
                scares.extend(_match(name))

    if not scares:
        scares = _match(html)

    # Deduplicate (JSON-LD repeats on some pages) and order chronologically.
    unique: dict[int, Jumpscare] = {}
    for scare in scares:
        unique.setdefault(scare.start_ms, scare)
    return sorted(unique.values(), key=lambda scare: scare.start_ms)


def _iter_item_lists(data):
    """Yield every dict that looks like a schema.org ItemList."""
    if isinstance(data, dict):
        if data.get("@type") == "ItemList":
            yield data
        for value in data.values():
            yield from _iter_item_lists(value)
    elif isinstance(data, list):
        for value in data:
            yield from _iter_item_lists(value)


def _match(text: str) -> list[Jumpscare]:
    return [
        Jumpscare(
            start_ms=_to_ms(match.group("timestamp")),
            severity="high" if match.group("severity").lower() == "major" else "low",
            timestamp=match.group("timestamp"),
        )
        for match in _ITEM_RE.finditer(text)
    ]


def to_segments(scares: list[Jumpscare]) -> list[dict]:
    """Convert jumpscares into normalized Cleanplex segments (fear, skip)."""
    return [
        {
            "start_ms": max(0, scare.start_ms - WINDOW_BEFORE_MS),
            "end_ms": scare.start_ms + WINDOW_AFTER_MS,
            "category": "fear",
            "severity": scare.severity,
            "action": "skip",
            "channel": "both",
            "labels": "jumpscare",
        }
        for scare in scares
    ]


class WhenJumpScareClient:
    """Serialized async client for WhenJumpScare lookups."""

    def __init__(
        self,
        base_url: str = BASE_URL,
        http: httpx.AsyncClient | None = None,
        min_interval: float = MIN_REQUEST_INTERVAL,
    ):
        self.base_url = base_url.rstrip("/")
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT, follow_redirects=True, headers=_HEADERS
        )
        self._lock = asyncio.Lock()
        self._last_request = 0.0
        self._min_interval = min_interval

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _get(self, url: str) -> httpx.Response:
        async with self._lock:
            wait = self._min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            last_error: Exception | None = None
            for attempt in range(MAX_ATTEMPTS):
                try:
                    response = await self._http.get(url)
                    self._last_request = time.monotonic()
                    if response.status_code == 404:
                        raise WhenJumpScareError("WhenJumpScare has no page for that id.")
                    if response.status_code == 429 or response.status_code >= 500:
                        last_error = WhenJumpScareError(
                            f"WhenJumpScare returned HTTP {response.status_code}"
                        )
                    else:
                        response.raise_for_status()
                        return response
                except httpx.HTTPError as exc:
                    self._last_request = time.monotonic()
                    last_error = exc
                if attempt < MAX_ATTEMPTS - 1:
                    await asyncio.sleep(1.5 * (attempt + 1))
            raise WhenJumpScareError(f"Could not reach WhenJumpScare: {last_error}")

    async def find_movie(self, tmdb_id: str) -> list[Jumpscare]:
        """Return every listed jumpscare for a TMDB id (empty when none)."""
        tmdb_id = str(tmdb_id).strip()
        if not tmdb_id.isdigit():
            raise WhenJumpScareError("A numeric TMDB id is required.")

        try:
            meta = (await self._get(f"{self.base_url}/api/movie/{tmdb_id}")).json()
        except (TypeError, ValueError) as exc:
            raise WhenJumpScareError(
                "WhenJumpScare returned an unreadable metadata response."
            ) from exc
        if not isinstance(meta, dict):
            raise WhenJumpScareError("WhenJumpScare returned unexpected metadata.")

        try:
            count = int(meta.get("jumpscareCount") or 0)
        except (TypeError, ValueError):
            count = 0
        if not count:
            return []

        link = str(meta.get("movieLink") or "").strip()
        link_parts = urlsplit(link)
        base_parts = urlsplit(self.base_url)
        # Exact host match, not a string prefix: "whenjumpscare.com.evil.example"
        # must not pass and receive a request from this server.
        if (
            link_parts.scheme not in ("http", "https")
            or link_parts.netloc != base_parts.netloc
            or not link_parts.path.startswith("/movie/")
        ):
            raise WhenJumpScareError("WhenJumpScare returned an unexpected movie link.")
        page = (await self._get(link)).text
        scares = parse_movie_page(page)
        if not scares:
            raise WhenJumpScareError(
                "The movie page listed jumpscares but none could be parsed."
            )
        return scares


# ── Process-wide client ───────────────────────────────────────────────────────

_client: WhenJumpScareClient | None = None


def get_client() -> WhenJumpScareClient:
    global _client
    if _client is None:
        _client = WhenJumpScareClient()
    return _client


def set_client(client: WhenJumpScareClient | None) -> None:
    """Replace the process client (tests inject a MockTransport-backed one)."""
    global _client
    _client = client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
