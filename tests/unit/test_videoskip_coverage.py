"""Unit tests for VideoSkip coverage queries."""

from __future__ import annotations

from cleanplex import database as db


async def _job(guid: str, library_id: str = "1", ignored: bool = False) -> None:
    await db.upsert_scan_job(
        plex_guid=guid,
        title=guid,
        file_path=f"/media/{guid}.mkv",
        rating_key=guid,
        library_id=library_id,
        library_title="Movies" if library_id == "1" else "TV",
    )
    if ignored:
        await db.set_ignored(guid, True)


async def test_coverage_counts_segments_and_videoskip_sources(setup_db):
    await _job("with-vs")
    await _job("with-scanner")
    await _job("empty")
    await _job("ignored", ignored=True)

    await db.insert_segments_bulk(
        "with-vs",
        "with-vs",
        [{"start_ms": 0, "end_ms": 1000, "category": "sex"}],
        "videoskip:776",
    )
    await db.insert_segments_bulk(
        "with-scanner",
        "with-scanner",
        [{"start_ms": 0, "end_ms": 1000, "category": "nudity"}],
        "scanner",
    )

    coverage = await db.get_videoskip_coverage()

    assert coverage["total"] == 3
    assert coverage["with_segments"] == 2
    assert coverage["with_videoskip"] == 1
    assert coverage["without_segments"] == 1
    assert coverage["without_videoskip"] == 2
    assert coverage["libraries"][0]["library_title"] == "Movies"


async def test_missing_guids_exclude_ignored_and_titles_with_segments(setup_db):
    await _job("done")
    await _job("missing-a")
    await _job("missing-b")
    await _job("ignored", ignored=True)
    await db.insert_segments_bulk(
        "done",
        "done",
        [{"start_ms": 0, "end_ms": 1000, "category": "sex"}],
        "manual",
    )

    guids = await db.get_guids_missing_segments()

    assert set(guids) == {"missing-a", "missing-b"}


async def test_missing_guids_respect_limit_and_library_filter(setup_db):
    await _job("movie-missing", library_id="1")
    await _job("tv-missing", library_id="2")

    assert await db.get_guids_missing_segments(limit=1) in (["movie-missing"], ["tv-missing"])
    assert await db.get_guids_missing_segments(limit=10, library_id="2") == ["tv-missing"]


async def test_scan_coverage_counts_scanned_and_nudenet_segments(setup_db):
    """Local-scan coverage mirrors the VideoSkip block: totals plus per library."""
    await _job("scanned-nudenet")
    await _job("scanned-empty")
    await _job("import-only")
    await _job("ignored", ignored=True)

    await db.update_scan_job_status("scanned-nudenet", "done", progress=1.0)
    await db.update_scan_job_status("scanned-empty", "done", progress=1.0)
    await db.insert_segment(
        plex_guid="scanned-nudenet",
        title="scanned-nudenet",
        start_ms=0,
        end_ms=1000,
        source="scanner",
    )
    await db.insert_segments_bulk(
        "import-only",
        "import-only",
        [{"start_ms": 0, "end_ms": 1000, "category": "sex"}],
        "videoskip:776",
    )

    coverage = await db.get_scan_coverage()

    assert coverage["total"] == 3
    assert coverage["scanned"] == 2
    assert coverage["with_segments"] == 2
    assert coverage["with_nudenet"] == 1
    assert coverage["without_segments"] == 1
    assert coverage["without_nudenet"] == 2
    movies = coverage["libraries"][0]
    assert movies["library_title"] == "Movies"
    assert movies["scanned"] == 2
    assert movies["with_nudenet"] == 1


async def test_scan_coverage_ignores_ignored_titles(setup_db):
    await _job("visible")
    await _job("hidden", ignored=True)

    coverage = await db.get_scan_coverage()

    assert coverage["total"] == 1
    assert coverage["libraries"][0]["total"] == 1


async def test_scan_coverage_reports_failed_scans_and_last_error(setup_db):
    """0 scanned + 0 NudeNet must not hide the reason: show failed/pending."""
    await _job("ok")
    await db.update_scan_job_status("ok", "done", progress=1.0)
    await _job("bad")
    await db.update_scan_job_status(
        "bad", "failed", error_msg="File not found: /tv/Show/x.mkv"
    )

    coverage = await db.get_scan_coverage()

    assert coverage["scanned"] == 1
    assert coverage["failed"] == 1
    assert coverage["pending"] == 0
    assert coverage["last_error"]["error"].startswith("File not found")
    assert coverage["last_error"]["title"] == "bad"


async def test_nudenet_analytics_counts_only_scanner_segments(setup_db):
    await _job("scanned")
    await db.insert_segment(
        plex_guid="scanned",
        title="scanned",
        start_ms=1000,
        end_ms=4001,
        confidence=0.9,
        labels="FEMALE_BREAST_EXPOSED,BUTTOCKS_EXPOSED",
    )
    await db.insert_segments_bulk(
        "scanned",
        "scanned",
        [{"start_ms": 0, "end_ms": 1000, "category": "violence",
          "labels": "violence skip 3", "action": "skip"}],
        "videoskip:776",
    )

    stats = await db.get_nudenet_analytics()

    assert stats["segments"] == 1
    by_label = {item["label"]: item for item in stats["labels"]}
    assert set(by_label) == {"FEMALE_BREAST_EXPOSED", "BUTTOCKS_EXPOSED"}
    assert by_label["FEMALE_BREAST_EXPOSED"]["avg_confidence"] == 0.9
    assert by_label["FEMALE_BREAST_EXPOSED"]["duration_ms"] == 3001
    assert stats["coverage"]["total"] == 1


async def test_nudenet_analytics_is_empty_without_scanner_segments(setup_db):
    await _job("import-only")
    await db.insert_segments_bulk(
        "import-only",
        "import-only",
        [{"start_ms": 0, "end_ms": 1000, "category": "sex"}],
        "videoskip:776",
    )

    stats = await db.get_nudenet_analytics()

    assert stats["segments"] == 0
    assert stats["labels"] == []


async def test_reset_failed_scan_jobs_requeues_only_failures(setup_db):
    await _job("ok")
    await db.update_scan_job_status("ok", "done", progress=1.0)
    await _job("bad")
    await db.update_scan_job_status("bad", "failed", error_msg="File not found")

    requeued = await db.reset_failed_scan_jobs()

    assert requeued == 1
    assert (await db.get_scan_job_by_guid("bad"))["status"] == "pending"
    assert (await db.get_scan_job_by_guid("bad"))["error_msg"] in (None, "")
    assert (await db.get_scan_job_by_guid("ok"))["status"] == "done"
