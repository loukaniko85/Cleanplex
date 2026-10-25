"""HTTP routes for the WhenJumpScare fear-timestamp source."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ... import database as db
from ... import whenjumpscare
from ...importers._common import runtime_warning
from ...logger import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/api/jumpscares", tags=["jumpscares"])


@router.post("/title/{plex_guid:path}")
async def fetch_jumpscares(plex_guid: str):
    """Fetch and apply WhenJumpScare's fear timestamps for one film.

    On-demand only: the service's terms permit lookups but forbid mirroring its
    catalogue, so nothing is cached and the file is applied straight to the title.
    """
    job = await db.get_scan_job_by_guid(plex_guid)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown title: {plex_guid}")

    if (job.get("media_type") or "movie") == "episode":
        raise HTTPException(
            status_code=409,
            detail="WhenJumpScare only indexes films, not TV episodes.",
        )

    tmdb_id = str(job.get("tmdb_id") or "").strip()
    if not tmdb_id:
        raise HTTPException(
            status_code=409,
            detail="This title has no TMDB id yet. Re-sync the library from Plex, then try again.",
        )

    try:
        scares = await whenjumpscare.get_client().find_movie(tmdb_id)
    except whenjumpscare.WhenJumpScareError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if not scares:
        return {
            "imported": 0,
            "scares": 0,
            "source": "",
            "title": job.get("title") or "",
            "message": "WhenJumpScare lists no jumpscares for this film.",
        }

    segments = whenjumpscare.to_segments(scares)
    # Re-fetching replaces only this source's rows, so repeated clicks do not
    # stack duplicate skips on top of each other.
    await db.delete_segments_by_source_prefix(plex_guid, "whenjumpscare:")
    source = f"whenjumpscare:{tmdb_id}"
    imported = await db.insert_segments_bulk(
        plex_guid, job.get("title") or "", segments, source
    )
    await db.merge_title_segments(plex_guid)
    logger.info(
        "Imported %d jump-scare segment(s) for '%s' from WhenJumpScare (%s)",
        imported,
        job.get("title"),
        tmdb_id,
    )
    return {
        "imported": imported,
        "scares": len(scares),
        "source": source,
        "title": job.get("title") or "",
        "warning": runtime_warning(segments, int(job.get("duration_ms") or 0)),
    }
