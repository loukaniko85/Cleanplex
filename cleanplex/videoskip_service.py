"""Shared VideoSkip import logic used by HTTP routes and the background queue."""

from __future__ import annotations

import json
import re

from . import database as db
from . import videoskip
from .importers import skp
from .importers._common import ParseError, runtime_warning
from .logger import get_logger

logger = get_logger(__name__)

_SKIP_ID_RE = re.compile(r"^\d+$")


class MissingEpisodePosition(ValueError):
    """An episode has no season/episode number, so the Exchange cannot be matched."""


class InvalidSkipId(ValueError):
    """The id is not the numeric form the Exchange uses."""


async def allowed_libraries() -> set[str]:
    """Return the library ids the user enabled for auto-fetch (empty = all)."""
    raw = await db.get_setting("videoskip_auto_libraries", "[]")
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return {str(value) for value in parsed}
    except (TypeError, ValueError):
        pass
    return set()


async def library_allowed(library_id: str) -> bool:
    """True when auto-fetch may touch this library."""
    allowed = await allowed_libraries()
    return not allowed or str(library_id) in allowed


def show_title(job: dict) -> str:
    """Derive the show name from Cleanplex's `Show – Season – Episode` title."""
    return (job.get("title") or "").split("\u2013")[0].strip()


def match_args(job: dict) -> dict:
    """Build search/matching arguments from a scan job."""
    media_type = job.get("media_type") or "movie"
    if media_type == "episode":
        season = job.get("season")
        episode = job.get("episode")
        if season is None or episode is None:
            raise MissingEpisodePosition(
                "This episode has no season/episode number stored yet. "
                "Re-sync the library from Plex, then try again."
            )
        return {
            "media_type": "episode",
            "title": show_title(job),
            "year": job.get("year"),
            "season": int(season),
            "episode": int(episode),
            "imdb_id": job.get("imdb_id") or "",
        }
    return {
        "media_type": "movie",
        "title": job.get("title") or "",
        "year": job.get("year"),
        "imdb_id": job.get("imdb_id") or "",
    }


def cut_mismatch_error(segments: list[dict], duration_ms: int) -> str | None:
    """Return an error message when a file cannot belong to this title's cut.

    Skip files are authored against one release. A file whose last cue lands far
    outside the title's runtime is either a different edition (extended cut) or a
    wrong-title match, and auto-fetch must not apply it silently.
    """
    if duration_ms <= 0 or not segments:
        return None
    implied = max(seg["end_ms"] for seg in segments)
    if implied < duration_ms * 0.5 or implied > duration_ms * 1.5:
        return (
            f"Skip file's timings end at {implied}ms but this title is "
            f"{duration_ms}ms long — it was authored for a different cut or "
            "a different title."
        )
    return None


async def apply_skip(
    job: dict, skip_id: str, replace: bool = False, reject_mismatched_cut: bool = False
) -> dict:
    """Download, parse and store one skip file.

    `reject_mismatched_cut` is for the automatic path only: an Exchange match can
    be the right title but the wrong edition, and auto-fetch must not silently
    land meaningless timings. Manual imports keep the warning-only behaviour so
    the user stays in control.
    """
    if not _SKIP_ID_RE.match(str(skip_id)):
        raise InvalidSkipId("Invalid VideoSkip file id.")

    filename, body = await videoskip.get_client().download_skip(skip_id)
    segments = skp.parse(body)

    duration_ms = int(job.get("duration_ms") or 0)
    if reject_mismatched_cut:
        error = cut_mismatch_error(segments, duration_ms)
        if error:
            raise ParseError(error)

    # Auto-fetch appends: another source (e.g. the NudeNet scan) may already own
    # part of the timeline, and replacing it would silently drop categories the
    # new file does not cover. Only an explicit per-title Replace deletes.
    if replace:
        await db.delete_segments_for_guid(job["plex_guid"])

    source = f"videoskip:{skip_id}"
    imported = await db.insert_segments_bulk(
        job["plex_guid"], job.get("title") or "", segments, source
    )
    # Auto-fetch is additive, so the same scene may already have a scanner row.
    # Merge same-category ranges now; different categories stay separate because
    # per-user strictness is evaluated per category.
    await db.merge_title_segments(job["plex_guid"])
    logger.info(
        "Imported %d VideoSkip segment(s) for '%s' from file %s",
        imported,
        job.get("title"),
        skip_id,
    )
    return {
        "imported": imported,
        "source": source,
        "filename": filename,
        "title": job.get("title") or "",
        "categories": videoskip.summarize_segments(segments),
        "warning": runtime_warning(segments, int(job.get("duration_ms") or 0)),
        "skip_id": str(skip_id),
    }


async def fetch_best(job: dict, replace: bool = False) -> dict | None:
    """Find and apply the best Exchange file for one title.

    Returns None when the Exchange has no matching file. Network and parse
    failures propagate so the caller can record a retryable error.
    """
    args = match_args(job)
    matches = await videoskip.get_client().find_candidates(**args, limit=1)
    if not matches:
        return None
    preferred = await db.get_strict_categories()
    recommended = videoskip.recommend_skip(matches[0], preferred)
    if recommended is None:
        return None
    return await apply_skip(
        job, recommended.skip_id, replace, reject_mismatched_cut=True
    )
