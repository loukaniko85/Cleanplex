"""Session watcher and library poller."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from datetime import datetime

from .logger import get_logger
from . import database as db
from . import filter_engine
from . import importers
from . import videoskip_service
from .scanner import enqueue, enqueue_pending, scanner_loop

logger = get_logger(__name__)

# Ring buffer of recent skip events for the live dashboard. The durable history
# lives in the skip_events table; this is just the hot cache.
skip_events: deque[dict] = deque(maxlen=50)

# Floor for adaptive polling, so a session sitting right before a segment cannot
# drive the poll rate high enough to hammer the Plex API.
MIN_POLL_INTERVAL_S = 1.0


async def session_watcher_loop(get_config_fn, get_client_fn) -> None:
    """Poll Plex sessions every `poll_interval` seconds and fire skips."""
    while True:
        config = await get_config_fn()

        if not config.is_configured():
            await asyncio.sleep(10)
            continue

        sessions = []
        try:
            client = get_client_fn()
            sessions = await client.get_active_sessions()

            # Drop tracking state for sessions that ended, restoring volume for any
            # that stopped mid-mute. Plex reuses session keys, so stale entries
            # would otherwise suppress skips for unrelated playback.
            await filter_engine.reap({s.session_key for s in sessions}, client)

            # One filter lookup per tick, not one per session. The list is tiny
            # (Plex accounts, not sessions) and this runs every poll interval.
            all_filters = {
                f["plex_username"]: f for f in await db.get_all_user_filters()
            }
            for session in sessions:
                user_filter = all_filters.get(session.user)
                # Default: filter enabled if no explicit record
                if user_filter is None or user_filter["enabled"]:
                    await filter_engine.process(
                        session,
                        client,
                        config.pre_buffer_ms,
                        config.post_buffer_ms,
                        config.poll_interval * 1000,
                    )

                    # Log skip event if a skip just happened (detect by checking _recently_skipped)
                    sk = filter_engine._recently_skipped.get(session.session_key, 0)
                    if sk and sk > session.position_ms:
                        skip_events.appendleft({
                            "time": datetime.now().isoformat(timespec="seconds"),
                            "user": session.user,
                            "title": session.full_title,
                            "position_ms": session.position_ms,
                            "client": session.client_title,
                        })

        except Exception as exc:
            logger.warning("Session watcher error: %s", exc)
            sessions = []

        await asyncio.sleep(await _next_poll_delay(sessions, config))


async def _next_poll_delay(sessions, config) -> float:
    """Return how long to sleep before the next session poll.

    Polling tightens as a session approaches a segment and relaxes when every
    session is far from one. A fixed interval forces a trade between Plex API load
    and skip precision; this gives precision only where it is needed.
    """
    delay = float(config.poll_interval)
    if not sessions:
        return delay

    for session in sessions:
        try:
            segments = await db.get_segments_for_guid(session.plex_guid)
            if not segments and session.rating_key:
                segments = await db.get_segments_by_rating_key(session.rating_key)
            upcoming = [
                s["start_ms"] - config.pre_buffer_ms - session.position_ms
                for s in segments
                if s["start_ms"] - config.pre_buffer_ms > session.position_ms
            ]
            if upcoming:
                delay = min(delay, max(MIN_POLL_INTERVAL_S, min(upcoming) / 1000.0))
        except Exception as exc:
            logger.debug("Could not compute poll delay for %s: %s", session.session_key, exc)

    return max(MIN_POLL_INTERVAL_S, delay)


async def _import_sidecar(item) -> bool:
    """Import a skip file sitting next to the media, if there is one.

    Returns True when segments were imported, meaning the title needs no ML scan.
    File I/O is small (a few KB of text) and runs once per newly discovered title,
    so it is read inline rather than through a thread.
    """
    try:
        sidecar = importers.find_sidecar(item.file_path)
        if sidecar is None:
            return False

        segments, source = importers.parse_file(sidecar)
        count = await db.insert_segments_bulk(item.plex_guid, item.title, segments, source)
        # "done" is the vocabulary the rest of the app uses (sync pickup, finished_at,
        # dashboard); "completed" left sidecar-imported titles invisible to sync.
        await db.update_scan_job_status(item.plex_guid, "done", progress=100)
        logger.info(
            "Imported %d segment(s) for '%s' from %s — skipping ML scan",
            count, item.title, sidecar.name,
        )
        return count > 0
    except Exception as exc:
        # A malformed sidecar must not stop the title being scanned normally.
        logger.warning("Could not import sidecar for '%s': %s", item.title, exc)
        return False


async def library_watcher_loop(get_config_fn, get_client_fn) -> None:
    """Periodically check for new Plex library items and enqueue unscanned ones."""
    first_run = True
    while True:
        if not first_run:
            await asyncio.sleep(60)
        first_run = False

        config = await get_config_fn()
        if not config.is_configured():
            continue

        try:
            client = get_client_fn()
            sections = await client.get_library_sections()
            excluded = set(await db.get_list_setting("excluded_library_ids", []))
            scan_ratings = set(await db.get_list_setting("scan_ratings", []))

            for section in sections:
                if section.section_id in excluded:
                    continue
                items = await client.get_library_items(section.section_id)
                # Read the auto-fetch settings once per library, not once per item.
                auto_videoskip = (await db.get_setting("videoskip_auto_fetch", "0")) == "1"
                try:
                    vs_max_attempts = int(
                        await db.get_setting("videoskip_auto_max_attempts", "2") or 2
                    )
                except ValueError:
                    vs_max_attempts = 2

                # Filter first, then resolve existence for the whole section in one
                # query. The old per-item get_scan_job_by_guid ran 24,000 lookups
                # every 60 seconds on a large library.
                candidates = [
                    item for item in items
                    if item.file_path
                    and (not scan_ratings or (item.content_rating or "") in scan_ratings)
                ]
                existing_jobs = await db.get_scan_jobs_by_guids(
                    [item.plex_guid for item in candidates]
                )

                for item in candidates:
                    existing = existing_jobs.get(item.plex_guid)
                    if existing is None:
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
                            part_files=json.dumps(item.part_files),
                            duration_ms=item.duration_ms,
                            imdb_id=item.imdb_id,
                            tmdb_id=item.tmdb_id,
                            season=item.season,
                            episode=item.episode,
                        )
                        # A skip file sitting beside the media is authoritative and
                        # free: importing it skips frame extraction and inference
                        # entirely, so only enqueue a scan when there isn't one.
                        if await _import_sidecar(item):
                            continue
                        if (
                            auto_videoskip
                            and await videoskip_service.library_allowed(item.library_id)
                            and await db.enqueue_videoskip_job(
                                item.plex_guid,
                                item.title,
                                max_attempts=vs_max_attempts,
                            )
                        ):
                            logger.info("New item queued for VideoSkip: %s", item.title)
                        await enqueue(item.plex_guid)
                        logger.info("New item queued for scan: %s", item.title)
                    elif not existing.get("part_files") and len(item.part_files) > 1:
                        # Backfill part_files for existing jobs that predate multi-part support.
                        await db.update_part_files(item.plex_guid, json.dumps(item.part_files))
                        logger.info("Updated part_files for existing job: %s (%d parts)", item.title, len(item.part_files))

        except Exception as exc:
            logger.warning("Library watcher error: %s", exc)
