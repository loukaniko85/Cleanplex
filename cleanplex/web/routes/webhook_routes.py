"""Plex webhook receiver for instant VideoSkip enqueue."""

from __future__ import annotations

import json
import os

from fastapi import APIRouter, HTTPException, Request

from ... import database as db
from ... import plex_client as plex_mod
from ... import videoskip_service
from ...logger import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])

WEBHOOK_TOKEN = os.environ.get("CLEANPLEX_WEBHOOK_TOKEN", "").strip()


@router.post("/plex")
async def plex_webhook(request: Request, token: str = ""):
    """Handle Plex's `library.new` webhook.

    Plex sends a multipart form with a JSON `payload` field. The normal library
    watcher is still the fallback; this path only removes the up-to-60-second
    delay when auto-fetch is enabled.
    """
    if WEBHOOK_TOKEN and token != WEBHOOK_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid webhook token")

    form = await request.form()
    raw = form.get("payload")
    if not raw:
        raise HTTPException(status_code=400, detail="Missing payload")

    try:
        payload = json.loads(str(raw))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid payload") from exc

    if payload.get("event") != "library.new":
        return {"ok": True, "ignored": "event"}

    if (await db.get_setting("videoskip_auto_fetch", "0")) != "1":
        return {"ok": True, "ignored": "auto-fetch off"}

    rating_key = (payload.get("Metadata") or {}).get("ratingKey")
    if not rating_key:
        return {"ok": True, "ignored": "no ratingKey"}

    try:
        client = plex_mod.get_client()
    except RuntimeError:
        return {"ok": True, "ignored": "plex not configured"}

    item = await client.get_library_item(str(rating_key))
    if item is None or not item.file_path:
        return {"ok": True, "ignored": "item not found"}

    if not await videoskip_service.library_allowed(item.library_id):
        return {"ok": True, "ignored": "library excluded"}

    await db.upsert_scan_job(
        plex_guid=item.plex_guid,
        title=item.title,
        file_path=item.file_path,
        rating_key=item.rating_key,
        library_id=item.library_id,
        library_title=item.library_title,
        content_rating=item.content_rating,
        media_type=item.media_type,
        year=item.year,
        show_guid=item.show_guid,
        show_rating_key=item.show_rating_key,
        part_files=json.dumps(item.part_files),
        duration_ms=item.duration_ms,
        imdb_id=item.imdb_id,
        tmdb_id=item.tmdb_id,
        season=item.season,
        episode=item.episode,
    )

    try:
        max_attempts = int(await db.get_setting("videoskip_auto_max_attempts", "2") or 2)
    except ValueError:
        max_attempts = 2

    queued = await db.enqueue_videoskip_job(
        item.plex_guid, item.title, max_attempts=max_attempts
    )
    logger.info("Plex webhook queued VideoSkip for '%s' (queued=%s)", item.title, queued)
    return {"ok": True, "queued": queued, "title": item.title}
