"""Client for the VideoSkip Exchange: search, match, and download `.skp` files.

The Exchange is a free, community-run catalogue of timestamp files. It has no
public API, so this module reads the same server-rendered pages a browser sees.
Parsing is deliberately forgiving: the selectors are small, documented in
`parse_*` below, and a page-shape change raises `VideoSkipError` rather than
silently returning wrong matches.

The client never mirrors the catalogue. It performs one lookup at a time, at the
user's request, and hands the downloaded text to the existing `.skp` parser.
"""

from __future__ import annotations

import asyncio
import html
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field

import httpx

from .logger import get_logger

logger = get_logger(__name__)

# videoskip.org currently 301s deep links to the Heroku root, which loses the
# path, so talk to the application host directly. Override for a mirror.
DEFAULT_BASE_URL = os.environ.get(
    "CLEANPLEX_VIDEOSKIP_URL", "https://videoskip.herokuapp.com/exchange"
).rstrip("/")

REQUEST_TIMEOUT = 20.0
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024
MAX_ATTEMPTS = 3
# The Exchange is a small volunteer service. One request per second is enough
# for interactive use and keeps a bulk fetch from looking like an attack.
MIN_REQUEST_INTERVAL = 1.0

_HEADERS = {
    "User-Agent": "Cleanplex/0.1 (Plex scene filter; +https://github.com/loukaniko85/Cleanplex)",
    "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8",
}

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_VIDEO_LINK_RE = re.compile(
    r'<a\s+href="/exchange/videos/(\d+)/"[^>]*>(.*?)</a>', re.S | re.I
)
_EPISODE_TITLE_RE = re.compile(
    r"^(?P<series>.+?)\s+S(?P<season>\d+)\s+E(?P<episode>\d+)"
    r"(?:\s*:\s*(?P<title>.*?))?(?:\s*\((?P<year>\d{4})\))?$",
    re.I,
)
_MOVIE_TITLE_RE = re.compile(r"^(?P<title>.+?)(?:\s*\((?P<year>\d{4})\))?$")
_IMDB_RE = re.compile(r"imdb\.com/title/(tt\d+)", re.I)
_RATING_RE = re.compile(r"Content Rating:\s*([^<]+)</li>", re.I)
_RELEASED_RE = re.compile(r"Released:\s*[^<]*?(\d{4})", re.I)
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S | re.I)
_SKIP_ID_RE = re.compile(r"/exchange/skip/(\d+)/")
_AUTHOR_RE = re.compile(r"by\s*<a[^>]*>\s*<strong>(.*?)</strong>", re.S | re.I)
_DATE_RE = re.compile(
    r"on\s+((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},\s+\d{4})",
    re.I,
)
_FILTERS_RE = re.compile(r"Filters:\s*</strong>(.*?)</p>", re.S | re.I)
_DOWNLOADS_RE = re.compile(r"<em>\s*(\d+)\s+downloads?\s*</em>", re.I)
_AUDIENCE_RE = re.compile(r"for\s+(Children|Teens|Adults)", re.I)


class VideoSkipError(RuntimeError):
    """Raised when the Exchange cannot be reached or its markup is unrecognised."""


class VideoSkipNotFound(VideoSkipError):
    """Raised when a requested page or skip file does not exist."""


@dataclass(frozen=True)
class SkipFileRef:
    """One downloadable skip file listed on a video page."""

    skip_id: str
    author: str = ""
    uploaded: str = ""
    filters: str = ""
    downloads: int = 0

    @property
    def url(self) -> str:
        return f"/skip/{self.skip_id}/"


@dataclass(frozen=True)
class VideoRef:
    """A movie or episode page on the Exchange."""

    video_id: str
    title: str
    year: int | None = None
    series: str = ""
    season: int | None = None
    episode: int | None = None
    imdb_id: str = ""
    content_rating: str = ""
    skip_files: tuple[SkipFileRef, ...] = field(default_factory=tuple)

    @property
    def is_episode(self) -> bool:
        return self.season is not None and self.episode is not None

    @property
    def url(self) -> str:
        return f"/videos/{self.video_id}/"

    @property
    def display_title(self) -> str:
        if self.is_episode:
            return f"{self.series} S{self.season} E{self.episode}: {self.title}"
        return self.title


# ── Markup parsing (pure functions, unit-testable without HTTP) ───────────────


def _text(fragment: str) -> str:
    """Strip tags/entities and collapse whitespace."""
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", fragment))).strip()


def _parse_title(raw: str) -> dict:
    """Split 'Show S1 E2: Name (2011)' or 'Movie (2023)' into fields."""
    text = _text(raw)
    episode = _EPISODE_TITLE_RE.match(text)
    if episode:
        return {
            "series": episode.group("series").strip(),
            "season": int(episode.group("season")),
            "episode": int(episode.group("episode")),
            "title": (episode.group("title") or "").strip(),
            "year": int(episode.group("year")) if episode.group("year") else None,
        }
    movie = _MOVIE_TITLE_RE.match(text)
    return {
        "series": "",
        "season": None,
        "episode": None,
        "title": (movie.group("title") if movie else text).strip(),
        "year": int(movie.group("year")) if movie and movie.group("year") else None,
    }


def parse_search_page(page: str) -> list[VideoRef]:
    """Return every video row from a `/search/?q=` results page."""
    seen: set[str] = set()
    results: list[VideoRef] = []
    for match in _VIDEO_LINK_RE.finditer(page):
        video_id = match.group(1)
        if video_id in seen:
            continue
        seen.add(video_id)
        fields = _parse_title(match.group(2))
        results.append(VideoRef(video_id=video_id, **fields))
    return results


def parse_video_page(page: str, video_id: str = "") -> VideoRef:
    """Parse a `/videos/<id>/` page, including its skip-file rows."""
    heading = _H1_RE.search(page)
    if not heading:
        raise VideoSkipError("VideoSkip page did not contain a title heading.")
    fields = _parse_title(heading.group(1))

    imdb = _IMDB_RE.search(page)
    rating = _RATING_RE.search(page)
    released = _RELEASED_RE.search(page)
    if fields.get("year") is None and released:
        fields["year"] = int(released.group(1))

    return VideoRef(
        video_id=video_id,
        imdb_id=imdb.group(1) if imdb else "",
        content_rating=_text(rating.group(1)) if rating else "",
        skip_files=tuple(_parse_skip_rows(page)),
        **fields,
    )


def _parse_skip_rows(page: str) -> list[SkipFileRef]:
    """Extract the `<tr>` blocks that list downloadable skip files."""
    files: list[SkipFileRef] = []
    # Split on row starts; a row without a skip id is a header or a request row.
    for block in re.split(r"<tr\b[^>]*>", page, flags=re.I)[1:]:
        skip = _SKIP_ID_RE.search(block)
        if not skip:
            continue
        author = _AUTHOR_RE.search(block)
        date = _DATE_RE.search(block)
        filters = _FILTERS_RE.search(block)
        downloads = _DOWNLOADS_RE.search(block)
        files.append(
            SkipFileRef(
                skip_id=skip.group(1),
                author=_text(author.group(1)) if author else "",
                uploaded=_text(date.group(1)) if date else "",
                filters=_text(filters.group(1)).rstrip(";").strip() if filters else "",
                downloads=int(downloads.group(1)) if downloads else 0,
            )
        )
    return files


# ── Matching ──────────────────────────────────────────────────────────────────


def _norm(value: str) -> str:
    """Lowercase and strip punctuation so localized titles still compare."""
    value = value.lower().replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return _WS_RE.sub(" ", value).strip()


def score_match(
    video: VideoRef,
    *,
    media_type: str,
    title: str,
    year: int | None = None,
    season: int | None = None,
    episode: int | None = None,
    imdb_id: str = "",
) -> int | None:
    """Score a candidate, or return None when it cannot be the same title.

    `title` is the movie title, or the show title for an episode. IMDb wins over
    everything: if both sides carry an ID and they differ, the candidate is
    rejected even when the title matches, which is what disambiguates remakes.
    """
    score = 0
    if imdb_id and video.imdb_id:
        if imdb_id.lower() == video.imdb_id.lower():
            score += 100
        elif media_type == "movie":
            # A different IMDb ID is conclusive for a film (remake, same title).
            return None
        else:
            # Plex sometimes exposes the series IMDb ID on an episode row, while
            # the Exchange page carries the episode ID. Season/episode still
            # decides; the mismatch only lowers the ranking.
            score -= 100

    if media_type == "episode":
        if video.season is None or video.episode is None:
            return None
        if video.season != season or video.episode != episode:
            return None
        score += 100

        wanted = _norm(title)
        found = _norm(video.series)
        if wanted and found:
            if wanted == found:
                score += 50
            elif wanted in found or found in wanted:
                score += 20
            else:
                score -= 10
        if year and video.year:
            score += 10 if year == video.year else -5
        return score

    # A movie must not match an episode row that happens to share its name.
    if video.is_episode:
        return None

    wanted = _norm(title)
    found = _norm(video.title)
    if not wanted or not found:
        return None
    if wanted == found:
        score += 100
    elif wanted.startswith(found) or found.startswith(wanted):
        score += 50
    elif wanted in found or found in wanted:
        score += 25
    else:
        return None

    if year and video.year:
        score += 30 if year == video.year else -10
    return score


_CATEGORY_HINTS = (
    (re.compile(r"sex", re.I), "sex"),
    (re.compile(r"nud", re.I), "nudity"),
    (re.compile(r"vio|gor", re.I), "violence"),
    (re.compile(r"pro|hat|lang", re.I), "language"),
    (re.compile(r"dru|alc", re.I), "drugs"),
    (re.compile(r"fri|sca|intense", re.I), "fear"),
)


def _categories_in(filters: str) -> set[str]:
    """Return the content categories named in an Exchange filter description."""
    return {category for pattern, category in _CATEGORY_HINTS if pattern.search(filters)}


def recommend_skip(
    video: VideoRef, preferred_categories: list[str] | None = None
) -> SkipFileRef | None:
    """Pick the most suitable file: audience, coverage, household priorities, popularity.

    The Exchange labels audience as Children/Teens/Adults. A "for Children" file
    is usually stricter than an "Adults" one, and a file that covers more
    categories is worth more than a single-category file with more downloads.
    Categories the household actually filters (from user preferences) get an
    extra weight, so a file covering fear/gore wins over a sex-only file when
    frightening scenes are set to strict.
    """
    if not video.skip_files:
        return None

    preferred = set(preferred_categories or [])

    def rank(item: SkipFileRef) -> tuple[int, int, int]:
        audience = _AUDIENCE_RE.search(item.filters)
        audience_score = {
            "children": 30,
            "teens": 20,
            "adults": 10,
        }.get(audience.group(1).lower(), 0) if audience else 0
        categories = _categories_in(item.filters)
        category_count = len([part for part in item.filters.split(";") if part.strip()])
        priority_matches = len(categories & preferred)
        return (
            audience_score + category_count * 5 + priority_matches * 8,
            item.downloads,
            int(item.skip_id),
        )

    return max(video.skip_files, key=rank)


def summarize_segments(segments: list[dict]) -> dict[str, int]:
    """Return a `{category: count}` summary for display after an import."""
    counts = Counter(seg.get("category") or "other" for seg in segments)
    return dict(sorted(counts.items()))


# ── HTTP client ───────────────────────────────────────────────────────────────


class VideoSkipClient:
    """Small async client for the Exchange, serialized to one request at a time."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        http: httpx.AsyncClient | None = None,
        min_interval: float = MIN_REQUEST_INTERVAL,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT, follow_redirects=True, headers=_HEADERS
        )
        self._min_interval = min_interval
        self._rate_lock = asyncio.Lock()
        self._last_request = 0.0

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _request(self, path: str) -> httpx.Response:
        """Fetch one path with a rate limit ceiling and bounded retries."""
        if not path.startswith("/"):
            path = "/" + path
        url = f"{self.base_url}{path}"

        async with self._rate_lock:
            wait = self._min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)

            last_error: Exception | None = None
            for attempt in range(MAX_ATTEMPTS):
                try:
                    response = await self._http.get(url)
                    self._last_request = time.monotonic()
                    if response.status_code == 404:
                        raise VideoSkipNotFound(f"VideoSkip page not found: {path}")
                    if response.status_code == 429 or response.status_code >= 500:
                        last_error = VideoSkipError(
                            f"VideoSkip returned HTTP {response.status_code}"
                        )
                    else:
                        response.raise_for_status()
                        return response
                except VideoSkipNotFound:
                    raise
                except httpx.HTTPError as exc:
                    self._last_request = time.monotonic()
                    last_error = exc
                # No sleep after the final attempt.
                if attempt < MAX_ATTEMPTS - 1:
                    await asyncio.sleep(1.5 * (attempt + 1))

            raise VideoSkipError(f"Could not reach VideoSkip: {last_error}")

    async def search(self, query: str) -> list[VideoRef]:
        """Search by title and return video rows (not yet populated with files)."""
        if not query.strip():
            return []
        params = httpx.QueryParams({"q": query})
        response = await self._request(f"/search/?{params}")
        return parse_search_page(response.text)

    async def get_video(self, video_id: str) -> VideoRef:
        """Fetch a video page, including its skip-file list."""
        response = await self._request(f"/videos/{video_id}/")
        return parse_video_page(response.text, video_id=video_id)

    async def download_skip(self, skip_id: str) -> tuple[str, str]:
        """Download one `.skp` file, returning `(filename, text)`."""
        response = await self._request(f"/skip/{skip_id}/download/")
        raw = response.content
        if len(raw) > MAX_DOWNLOAD_BYTES:
            raise VideoSkipError("VideoSkip file is unexpectedly large.")
        disposition = response.headers.get("content-disposition", "")
        name = re.search(r'filename="?([^";]+)"?', disposition)
        filename = name.group(1) if name else f"videoskip_{skip_id}.skp"
        return filename, raw.decode("utf-8", errors="replace")

    async def find_candidates(
        self,
        *,
        media_type: str,
        title: str,
        year: int | None = None,
        season: int | None = None,
        episode: int | None = None,
        imdb_id: str = "",
        limit: int = 4,
    ) -> list[VideoRef]:
        """Search, filter and load pages for the most likely matches.

        Search is cheap and returns rows; loading a page is a second request, so
        only the highest-scoring few are fetched. Results without skip files are
        dropped — a "request" row is not actionable.
        """
        results = await self.search(title)
        scored: list[tuple[int, VideoRef]] = []
        for candidate in results:
            value = score_match(
                candidate,
                media_type=media_type,
                title=title,
                year=year,
                season=season,
                episode=episode,
                imdb_id=imdb_id,
            )
            if value is not None:
                scored.append((value, candidate))
        scored.sort(key=lambda pair: pair[0], reverse=True)

        loaded: list[tuple[int, VideoRef]] = []
        for value, candidate in scored[: max(limit * 2, limit)]:
            try:
                video = await self.get_video(candidate.video_id)
            except VideoSkipError as exc:
                logger.warning("Could not load VideoSkip video %s: %s", candidate.video_id, exc)
                continue
            if not video.skip_files:
                continue
            # The page carries the IMDb ID, which the search row does not. Re-score
            # with it so a remake with the same title/year is eliminated.
            rescored = score_match(
                video,
                media_type=media_type,
                title=title,
                year=year,
                season=season,
                episode=episode,
                imdb_id=imdb_id,
            )
            if rescored is None:
                continue
            loaded.append((max(value, rescored), video))
            if len(loaded) >= limit:
                break

        loaded.sort(key=lambda pair: pair[0], reverse=True)
        return [video for _, video in loaded]


# ── Process-wide client ───────────────────────────────────────────────────────

_client: VideoSkipClient | None = None


def get_client() -> VideoSkipClient:
    global _client
    if _client is None:
        _client = VideoSkipClient()
    return _client


def set_client(client: VideoSkipClient | None) -> None:
    """Replace the process client (tests inject a MockTransport-backed one)."""
    global _client
    _client = client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
