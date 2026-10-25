"""Unit tests for the persistent VideoSkip auto-fetch queue."""

from __future__ import annotations

from unittest.mock import AsyncMock

from cleanplex import database as db
from cleanplex import videoskip_queue


async def _job(guid: str, title: str = "Oppenheimer") -> None:
    await db.upsert_scan_job(
        plex_guid=guid,
        title=title,
        file_path=f"/media/{guid}.mkv",
        rating_key=guid,
        library_id="1",
        library_title="Movies",
    )


async def test_enqueue_deduplicates_and_claims_one_job_at_a_time(setup_db):
    await _job("a")

    assert await db.enqueue_videoskip_job("a", "Oppenheimer") is True
    assert await db.enqueue_videoskip_job("a", "Oppenheimer") is False

    claimed = await db.claim_next_videoskip_job()
    assert claimed is not None
    assert claimed["plex_guid"] == "a"
    assert claimed["attempts"] == 1
    assert await db.claim_next_videoskip_job() is None


async def test_terminal_results_are_not_retried_but_force_requeues(setup_db):
    await _job("a")
    await db.enqueue_videoskip_job("a", "Oppenheimer")
    claimed = await db.claim_next_videoskip_job()
    await db.finish_videoskip_job(claimed["id"], "no_match")

    assert await db.enqueue_videoskip_job("a", "Oppenheimer") is False
    assert await db.enqueue_videoskip_job("a", "Oppenheimer", force=True) is True


async def test_failed_job_retries_until_max_attempts(setup_db):
    await _job("a")
    await db.enqueue_videoskip_job("a", "Oppenheimer", max_attempts=2)

    claimed = await db.claim_next_videoskip_job()
    await db.finish_videoskip_job(claimed["id"], "failed", error="network")
    # attempts=1 is below the ceiling, so a retry is accepted.
    assert await db.enqueue_videoskip_job("a", "Oppenheimer", max_attempts=2) is True

    claimed = await db.claim_next_videoskip_job()
    await db.finish_videoskip_job(claimed["id"], "failed", error="network")
    # attempts=2 has hit the ceiling.
    assert await db.enqueue_videoskip_job("a", "Oppenheimer", max_attempts=2) is False


async def test_queue_status_counts_and_clear(setup_db):
    await _job("a")
    await _job("b")
    await db.enqueue_videoskip_job("a", "A")
    await db.enqueue_videoskip_job("b", "B")
    claimed = await db.claim_next_videoskip_job()
    await db.finish_videoskip_job(claimed["id"], "done", skip_id="776", imported=3)

    status = await db.get_videoskip_queue_status()
    assert status["done"] == 1
    assert status["queued"] == 1
    assert status["total"] == 2

    assert await db.clear_videoskip_queue() == 2
    assert (await db.get_videoskip_queue_status())["total"] == 0


async def test_status_reports_pause_and_daily_limit(setup_db):
    await db.set_setting("videoskip_queue_paused", "1")
    await db.set_setting("videoskip_auto_fetch", "1")
    await db.set_setting("videoskip_auto_daily_limit", "7")

    status = await db.get_videoskip_queue_status()

    assert status["paused"] is True
    assert status["auto_fetch"] is True
    assert status["daily_limit"] == 7


async def test_guids_without_videoskip_excludes_covered_titles(setup_db):
    await _job("covered")
    await _job("manual")
    await db.insert_segments_bulk(
        "covered",
        "Covered",
        [{"start_ms": 0, "end_ms": 1000, "category": "sex"}],
        "videoskip:776",
    )
    await db.insert_segments_bulk(
        "manual",
        "Manual",
        [{"start_ms": 0, "end_ms": 1000, "category": "sex"}],
        "manual",
    )

    guids = await db.get_guids_without_videoskip()

    assert guids == ["manual"]


async def test_resolved_queue_rows_do_not_clog_the_sweep(setup_db):
    """Regression: terminal no_match rows used to fill every sweep batch forever.

    After the first batch resolved, get_guids_without_videoskip kept returning
    the same oldest titles, enqueue refused them all, and no newer title could
    ever be swept. A resolved title must leave the candidate pool.
    """
    for i in range(30):
        await _job(f"title-{i}")

    first = await db.get_guids_without_videoskip(limit=25)
    assert len(first) == 25
    for guid in first:
        await db.enqueue_videoskip_job(guid, guid)
        claimed = await db.claim_next_videoskip_job()
        await db.finish_videoskip_job(claimed["id"], "no_match")

    second = await db.get_guids_without_videoskip(limit=25)

    assert second == [f"title-{i}" for i in range(25, 30)]


async def test_failed_below_attempt_ceiling_stays_eligible(setup_db):
    await _job("retryable")

    await db.enqueue_videoskip_job("retryable", "Retryable", max_attempts=2)
    claimed = await db.claim_next_videoskip_job()
    await db.finish_videoskip_job(claimed["id"], "failed", error="network")

    assert "retryable" in await db.get_guids_without_videoskip(max_attempts=2)


async def test_process_marks_done_and_records_source(monkeypatch, setup_db):
    await _job("a")
    await db.enqueue_videoskip_job("a", "Oppenheimer")
    claimed = await db.claim_next_videoskip_job()

    monkeypatch.setattr(
        videoskip_queue.videoskip_service,
        "fetch_best",
        AsyncMock(return_value={"skip_id": "776", "imported": 3}),
    )
    await videoskip_queue._process(claimed)

    status = await db.get_videoskip_queue_status()
    assert status["done"] == 1
    assert status["recent"][0]["skip_id"] == "776"
    assert status["recent"][0]["imported"] == 3


async def test_process_skips_a_title_that_already_got_videoskip_data(
    monkeypatch, setup_db
):
    await _job("a")
    await db.insert_segments_bulk(
        "a",
        "Oppenheimer",
        [{"start_ms": 0, "end_ms": 1000, "category": "sex"}],
        "videoskip:776",
    )
    await db.enqueue_videoskip_job("a", "Oppenheimer")
    claimed = await db.claim_next_videoskip_job()

    fetch = AsyncMock()
    monkeypatch.setattr(videoskip_queue.videoskip_service, "fetch_best", fetch)
    await videoskip_queue._process(claimed)

    fetch.assert_not_awaited()
    status = await db.get_videoskip_queue_status()
    assert status["skipped"] == 1
