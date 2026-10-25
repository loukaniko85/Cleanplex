"""Background queue for automatic VideoSkip Exchange lookups.

One worker is deliberate: the Exchange is a volunteer service, and the client
already serializes requests to one per second. The queue adds persistence,
retries, a daily ceiling, and a pause switch on top of that.
"""

from __future__ import annotations

import asyncio
import time

from . import database as db
from . import videoskip_service
from .config import Config
from .importers._common import ParseError
from .logger import get_logger
from .videoskip import VideoSkipError

logger = get_logger(__name__)

# How many missing titles one sweep may enqueue. Small enough to keep the queue
# bounded, large enough to fill the daily allowance over a few sweeps.
SWEEP_BATCH = 25
IDLE_SLEEP_SECONDS = 20.0
DISABLED_SLEEP_SECONDS = 30.0

_last_sweep = 0.0
# Stop hammering the Exchange after a run of failures (outage, DNS, 5xx).
_consecutive_failures = 0


async def _setting_bool(key: str, default: str = "0") -> bool:
    return (await db.get_setting(key, default)) == "1"


async def _setting_int(key: str, default: int) -> int:
    raw = await db.get_setting(key, str(default))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


async def _delay_seconds() -> float:
    return max(0.0, await _setting_int("videoskip_auto_delay_ms", 1500) / 1000.0)


async def _daily_limit_reached() -> bool:
    limit = await _setting_int("videoskip_auto_daily_limit", 50)
    if limit <= 0:
        return False
    status = await db.get_videoskip_queue_status()
    return status["daily_used"] >= limit


async def _maybe_sweep() -> None:
    """Enqueue a bounded batch of titles that have no VideoSkip segments."""
    global _last_sweep

    minutes = await _setting_int("videoskip_auto_sweep_minutes", 60)
    if minutes <= 0:
        return
    now = time.monotonic()
    if _last_sweep and now - _last_sweep < minutes * 60:
        return

    max_attempts = await _setting_int("videoskip_auto_max_attempts", 2)
    allowed = await videoskip_service.allowed_libraries()
    guids = await db.get_guids_without_videoskip(
        SWEEP_BATCH,
        library_ids=list(allowed) or None,
        max_attempts=max_attempts,
    )
    jobs = await db.get_scan_jobs_by_guids(guids)
    queued = 0
    for guid in guids:
        job = jobs.get(guid)
        if job and await db.enqueue_videoskip_job(
            guid, job.get("title") or "", max_attempts=max_attempts
        ):
            queued += 1

    _last_sweep = now
    if queued:
        logger.info("VideoSkip sweep queued %d title(s)", queued)


async def _process(job: dict) -> str:
    queue_id = int(job["id"])
    plex_guid = job["plex_guid"]

    scan_job = await db.get_scan_job_by_guid(plex_guid)
    if scan_job is None or scan_job.get("ignored"):
        await db.finish_videoskip_job(
            queue_id, "skipped", error="Title is gone or ignored"
        )
        return "skipped"

    if not await videoskip_service.library_allowed(scan_job.get("library_id") or ""):
        await db.finish_videoskip_job(
            queue_id, "skipped", error="Library is excluded from auto-fetch"
        )
        return "skipped"

    # A manual lookup may have completed while this job waited in the queue.
    if await db.count_videoskip_segments_for_guid(plex_guid):
        await db.finish_videoskip_job(
            queue_id, "skipped", error="Already has VideoSkip segments"
        )
        return "skipped"

    try:
        result = await videoskip_service.fetch_best(scan_job)
        if result is None:
            await db.finish_videoskip_job(queue_id, "no_match")
            logger.info("VideoSkip has no file for '%s'", scan_job.get("title"))
            return "no_match"

        await db.finish_videoskip_job(
            queue_id,
            "done",
            skip_id=result.get("skip_id", ""),
            imported=int(result.get("imported", 0)),
        )
        logger.info(
            "VideoSkip auto-fetch applied %d segment(s) to '%s'",
            result.get("imported", 0),
            scan_job.get("title"),
        )
        return "done"
    except (VideoSkipError, ParseError, videoskip_service.InvalidSkipId) as exc:
        await db.finish_videoskip_job(queue_id, "failed", error=str(exc))
        logger.warning("VideoSkip auto-fetch failed for '%s': %s", scan_job.get("title"), exc)
        return "failed"
    except videoskip_service.MissingEpisodePosition as exc:
        await db.finish_videoskip_job(queue_id, "failed", error=str(exc))
        logger.info("VideoSkip skipped '%s': %s", scan_job.get("title"), exc)
        return "failed"
    except Exception as exc:  # noqa: BLE001 - one bad title must not kill the loop
        await db.finish_videoskip_job(queue_id, "failed", error=str(exc))
        logger.exception("Unexpected VideoSkip queue error for '%s'", scan_job.get("title"))
        return "failed"


async def run_queue() -> None:
    """Process the queue forever, respecting pause, window, cap and failures."""
    global _consecutive_failures
    logger.info("VideoSkip auto-fetch queue started")
    while True:
        try:
            if await _setting_bool("videoskip_queue_paused"):
                await asyncio.sleep(10)
                continue

            if await _daily_limit_reached():
                logger.info("VideoSkip daily limit reached — waiting")
                await asyncio.sleep(300)
                continue

            auto_enabled = await _setting_bool("videoskip_auto_fetch")
            if auto_enabled and await _setting_bool(
                "videoskip_auto_respect_scan_window"
            ):
                # The NudeNet scanner already uses this window; keeping automatic
                # network work out of prime viewing time is the least surprising
                # default once a user opts into it.
                if not (await Config.load()).is_scan_window():
                    await asyncio.sleep(300)
                    continue

            if auto_enabled:
                await _maybe_sweep()

            job = await db.claim_next_videoskip_job()
            if job is None:
                # Manual queue entries are processed even when automatic
                # enqueueing is off; only the sweep depends on the toggle.
                delay = (
                    IDLE_SLEEP_SECONDS
                    if await _setting_bool("videoskip_auto_fetch")
                    else DISABLED_SLEEP_SECONDS
                )
                await asyncio.sleep(delay)
                continue

            status = await _process(job)
            if status == "failed":
                _consecutive_failures += 1
            else:
                _consecutive_failures = 0

            if _consecutive_failures >= 5:
                logger.warning(
                    "VideoSkip queue saw %d consecutive failures — pausing for 15 minutes",
                    _consecutive_failures,
                )
                _consecutive_failures = 0
                await asyncio.sleep(900)
                continue

            await asyncio.sleep(await _delay_seconds())
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - keep the supervisor alive
            logger.exception("VideoSkip queue loop error")
            await asyncio.sleep(30)
