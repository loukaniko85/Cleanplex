"""Import endpoints for externally produced skip files."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from ...logger import get_logger
from ... import database as db
from ... import importers
from ...importers._common import CATEGORIES, ParseError, runtime_warning

logger = get_logger(__name__)
router = APIRouter(prefix="/api/segments", tags=["import"])

# Uploaded skip files are small text documents; anything larger is not one.
MAX_UPLOAD_BYTES = 2 * 1024 * 1024
# Pasted lists are small too; bound the JSON field so one request cannot buffer
# an unbounded string.
MAX_PASTE_CHARS = 200_000


class PastePayload(BaseModel):
    plex_guid: str
    text: str = Field(..., max_length=MAX_PASTE_CHARS)
    replace: bool = False
    # Optional override for lines whose description carries no recognised keyword
    # (e.g. "Scar Missed Presentation of Simba"). Without it those cues land in
    # `other`, which many households do not filter.
    category: str | None = None


async def _store(plex_guid: str, segments: list[dict], source: str, replace: bool) -> dict:
    """Persist parsed segments against a known scan job."""
    job = await db.get_scan_job_by_guid(plex_guid)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown title: {plex_guid}")

    if replace:
        await db.delete_segments_for_guid(plex_guid)

    count = await db.insert_segments_bulk(plex_guid, job.get("title") or "", segments, source)
    await db.merge_title_segments(plex_guid)
    logger.info("Imported %d segment(s) for '%s' from %s", count, job.get("title"), source)

    return {
        "imported": count,
        "source": source,
        "title": job.get("title") or "",
        "warning": runtime_warning(segments, int(job.get("duration_ms") or 0)),
    }


@router.post("/import")
async def import_skip_file(
    plex_guid: str = Form(...),
    file: UploadFile = File(...),
    replace: bool = Form(False),
):
    """Import a .skp, .edl, .mcf or .txt skip file for one title."""
    # Read in bounded chunks: reading the whole body first meant a multi-GB
    # upload could exhaust memory before the size check ran.
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(64 * 1024):
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="Skip file is too large to be one.")
        chunks.append(chunk)
    raw = b"".join(chunks)

    suffix = Path(file.filename or "").suffix.lower()
    module = importers.PARSERS.get(suffix)
    if module is None:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported skip file type: {suffix or file.filename!r}. "
                   "Expected .skp, .edl, .mcf or .txt.",
        )

    try:
        # utf-8-sig: hand-edited files on Windows often carry a BOM, which would
        # otherwise break the `.mcf` header check.
        segments = module.parse(raw.decode("utf-8-sig", errors="replace"))
    except ParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return await _store(plex_guid, segments, module.SOURCE, replace)


@router.post("/import/paste")
async def import_pasted_list(payload: PastePayload):
    """Import a loosely formatted timestamp list pasted by the user."""
    try:
        segments = importers.parse(payload.text, "paste")
    except ParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if payload.category:
        if payload.category not in CATEGORIES:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown category {payload.category!r}.",
            )
        for seg in segments:
            # Only fill the gaps: an explicit keyword in the line still wins.
            if seg["category"] == "other":
                seg["category"] = payload.category
                if payload.category == "language":
                    seg["action"] = "mute"
                    seg["channel"] = "audio"

    return await _store(payload.plex_guid, segments, "paste", payload.replace)


class SubtitleScanPayload(BaseModel):
    plex_guid: str
    replace: bool = False


@router.post("/scan-subtitles")
async def scan_subtitles(payload: SubtitleScanPayload):
    """Scan a title's subtitles for profanity and store mute segments."""
    from ... import subtitle_scanner

    job = await db.get_scan_job_by_guid(payload.plex_guid)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown title: {payload.plex_guid}")
    if not job.get("file_path"):
        raise HTTPException(status_code=422, detail="This title has no file path on disk.")

    if payload.replace:
        await db.delete_segments_for_guid(payload.plex_guid)

    count = await subtitle_scanner.scan_title(
        payload.plex_guid, job.get("title") or "", job["file_path"]
    )
    await db.merge_title_segments(payload.plex_guid)
    return {"imported": count, "source": "subtitles", "title": job.get("title") or ""}


@router.get("/export/{plex_guid:path}")
async def export_segments(plex_guid: str, fmt: str = "edl"):
    """Export a title's segments as EDL or MCF for use in other players."""
    from ...importers import edl, mcf

    job = await db.get_scan_job_by_guid(plex_guid)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown title: {plex_guid}")

    segments = await db.get_segments_for_guid(plex_guid)
    if fmt == "edl":
        body = edl.export(segments)
    elif fmt == "mcf":
        body = mcf.export(segments, title=job.get("title") or "", year=job.get("year"))
    else:
        raise HTTPException(status_code=422, detail=f"Unsupported export format: {fmt}")

    return {"format": fmt, "body": body, "count": len(segments)}
