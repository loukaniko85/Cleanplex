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
