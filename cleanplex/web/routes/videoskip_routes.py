"""VideoSkip Exchange lookup, import and auto-fetch queue endpoints."""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ...logger import get_logger
from ... import database as db
from ... import videoskip
from ... import videoskip_service
from ...importers._common import ParseError

logger = get_logger(__name__)
router = APIRouter(prefix="/api/videoskip", tags=["videoskip"])

MAX_BULK_TITLES = 100
MAX_QUEUE_ENQUEUE = 200


class ApplyPayload(BaseModel):
    plex_guid: str
    skip_id: str
    replace: bool = False


class AutoPayload(BaseModel):
    plex_guids: list[str] = Field(default_factory=list)
    library_id: str | None = None
    replace: bool = False
    limit: int = 25


class EnqueuePayload(BaseModel):
    limit: int = 50
    library_id: str | None = None
    force: bool = False


def _skip_payload(item: videoskip.SkipFileRef, recommended: str) -> dict:
    return {
        "skip_id": item.skip_id,
        "author": item.author,
        "uploaded": item.uploaded,
        "filters": item.filters,
        "downloads": item.downloads,
        "recommended": item.skip_id == recommended,
    }


def _video_payload(
    video: videoskip.VideoRef, preferred: list[str] | None = None
) -> dict:
    recommended = videoskip.recommend_skip(video, preferred)
    return {
        "video_id": video.video_id,
        "title": video.title,
        "display_title": video.display_title,
        "year": video.year,
        "series": video.series,
        "season": video.season,
        "episode": video.episode,
        "is_episode": video.is_episode,
        "imdb_id": video.imdb_id,
        "content_rating": video.content_rating,
        "skip_files": [
            _skip_payload(item, recommended.skip_id if recommended else "")
            for item in video.skip_files
        ],
        "recommended_skip_id": recommended.skip_id if recommended else "",
    }


@router.get("/health")
async def exchange_health():
    """Ping the Exchange with a harmless search, for the Settings test button."""
    started = time.monotonic()
    try:
        results = await videoskip.get_client().search("Oppenheimer")
    except videoskip.VideoSkipError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
    return {
        "ok": bool(results),
        "results": len(results),
        "latency_ms": int((time.monotonic() - started) * 1000),
    }


@router.get("/coverage")
async def coverage():
    """Return per-library segment and VideoSkip coverage for the Settings panel."""
    return await db.get_videoskip_coverage()


@router.get("/search")
async def search_exchange(q: str = Query(..., min_length=1, max_length=200)):
    """Search the Exchange directly, for a global search box."""
    try:
        results = await videoskip.get_client().search(q)
    except videoskip.VideoSkipError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    preferred = await db.get_strict_categories()
    return {"query": q, "results": [_video_payload(v, preferred) for v in results]}


@router.get("/title/{plex_guid:path}")
async def matches_for_title(plex_guid: str):
    """Return Exchange entries that match one library title."""
    job = await db.get_scan_job_by_guid(plex_guid)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown title: {plex_guid}")

    try:
        args = videoskip_service.match_args(job)
    except videoskip_service.MissingEpisodePosition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    try:
        matches = await videoskip.get_client().find_candidates(**args, limit=4)
    except videoskip.VideoSkipError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    existing = await db.count_segments_for_guid(plex_guid)
    existing_vs = await db.count_videoskip_segments_for_guid(plex_guid)
    preferred = await db.get_strict_categories()
    return {
        "plex_guid": plex_guid,
        "title": job.get("title") or "",
        "media_type": args["media_type"],
        "query": args["title"],
        "existing_segments": existing,
        "existing_videoskip_segments": existing_vs,
        "matches": [_video_payload(video, preferred) for video in matches],
    }


@router.post("/apply")
async def apply_skip_file(payload: ApplyPayload):
    """Download and apply one chosen VideoSkip file to a title."""
    job = await db.get_scan_job_by_guid(payload.plex_guid)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown title: {payload.plex_guid}")

    try:
        return await videoskip_service.apply_skip(job, payload.skip_id, payload.replace)
    except videoskip_service.InvalidSkipId as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ParseError as exc:
        raise HTTPException(
            status_code=422, detail=f"VideoSkip file could not be parsed: {exc}"
        ) from exc
    except videoskip.VideoSkipError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/auto")
async def auto_fetch(payload: AutoPayload):
    """Find and apply the best Exchange file for each selected title.

    This is the synchronous path used by the Library multi-select. The Settings
    panel and new-title path use the persistent queue instead.
    """
    limit = max(1, min(payload.limit, MAX_BULK_TITLES))
    if payload.plex_guids:
        guids = payload.plex_guids[:limit]
        jobs = await db.get_scan_jobs_by_guids(guids)
        targets = [jobs[guid] for guid in guids if guid in jobs]
    else:
        guids = await db.get_guids_without_videoskip(limit, payload.library_id)
        jobs = await db.get_scan_jobs_by_guids(guids)
        targets = [jobs[guid] for guid in guids if guid in jobs]

    # An explicit selection is the user pointing at those titles: skip any that
    # already have segments, as before. A library/global sweep is a coverage
    # operation, so only titles without VideoSkip data are skipped.
    explicit_selection = bool(payload.plex_guids)

    results = []
    applied = 0
    for job in targets:
        guid = job["plex_guid"]
        existing = (
            await db.count_segments_for_guid(guid)
            if explicit_selection
            else await db.count_videoskip_segments_for_guid(guid)
        )
        if existing and not payload.replace:
            results.append(
                {"plex_guid": guid, "title": job.get("title"), "status": "has_segments"}
            )
            continue

        try:
            outcome = await videoskip_service.fetch_best(job, replace=payload.replace)
            if outcome is None:
                results.append(
                    {"plex_guid": guid, "title": job.get("title"), "status": "no_match"}
                )
                continue
            applied += 1
            results.append(
                {
                    "plex_guid": guid,
                    "title": job.get("title"),
                    "status": "imported",
                    "skip_id": outcome["skip_id"],
                    "imported": outcome["imported"],
                    "categories": outcome["categories"],
                    "warning": outcome["warning"],
                }
            )
        except videoskip_service.MissingEpisodePosition as exc:
            results.append(
                {
                    "plex_guid": guid,
                    "title": job.get("title"),
                    "status": "error",
                    "error": str(exc),
                }
            )
        except (videoskip.VideoSkipError, ParseError, videoskip_service.InvalidSkipId) as exc:
            logger.warning("VideoSkip auto-fetch failed for '%s': %s", job.get("title"), exc)
            results.append(
                {
                    "plex_guid": guid,
                    "title": job.get("title"),
                    "status": "error",
                    "error": str(exc),
                }
            )

    return {"attempted": len(targets), "imported": applied, "results": results}


# ── Auto-fetch queue ──────────────────────────────────────────────────────────

@router.get("/queue")
async def queue_status():
    """Return queue counts, daily usage and recent history."""
    return await db.get_videoskip_queue_status()


@router.post("/queue/enqueue")
async def queue_missing(payload: EnqueuePayload):
    """Queue titles that have no VideoSkip segments yet."""
    limit = max(1, min(payload.limit, MAX_QUEUE_ENQUEUE))
    max_attempts = int(await db.get_setting("videoskip_auto_max_attempts", "2") or 2)
    guids = await db.get_guids_without_videoskip(limit, payload.library_id)
    jobs = await db.get_scan_jobs_by_guids(guids)

    queued = 0
    for guid in guids:
        job = jobs.get(guid)
        if job and await db.enqueue_videoskip_job(
            guid, job.get("title") or "", max_attempts=max_attempts, force=payload.force
        ):
            queued += 1
    return {"queued": queued, "candidates": len(guids)}


@router.post("/queue/pause")
async def pause_queue():
    """Stop the worker from claiming new jobs."""
    await db.set_setting("videoskip_queue_paused", "1")
    return {"paused": True}


@router.post("/queue/resume")
async def resume_queue():
    """Allow the worker to claim jobs again."""
    await db.set_setting("videoskip_queue_paused", "0")
    return {"paused": False}


@router.post("/queue/clear")
async def clear_queue():
    """Delete all queue rows except the one currently running."""
    return {"deleted": await db.clear_videoskip_queue()}


@router.post("/queue/retry-failed")
async def retry_failed_queue():
    """Requeue failed jobs with a fresh attempt count."""
    return {"queued": await db.retry_failed_videoskip_jobs()}
