"""Unit tests for merging overlapping segments from different sources."""

from __future__ import annotations

from cleanplex import database as db


async def _job(guid: str = "title") -> None:
    await db.upsert_scan_job(
        plex_guid=guid,
        title="Oppenheimer",
        file_path="/media/opp.mkv",
        rating_key="1",
        library_id="1",
        library_title="Movies",
    )


def _seg(start: int, end: int, category: str, **kwargs) -> dict:
    return {
        "start_ms": start,
        "end_ms": end,
        "category": category,
        "severity": kwargs.get("severity", "medium"),
        "action": kwargs.get("action", "skip"),
        "channel": kwargs.get("channel", "both"),
        "labels": kwargs.get("labels", ""),
    }


async def test_same_category_overlaps_merge_and_keep_all_sources(setup_db):
    await _job()
    await db.insert_segments_bulk(
        "title", "Oppenheimer", [_seg(1000, 5000, "nudity", labels="breast")], "scanner"
    )
    await db.insert_segments_bulk(
        "title",
        "Oppenheimer",
        [_seg(4000, 8000, "nudity", severity="high", labels="buttocks")],
        "videoskip:776",
    )

    removed = await db.merge_title_segments("title")

    assert removed == 1
    segments = await db.get_segments_for_guid("title")
    assert len(segments) == 1
    merged = segments[0]
    assert (merged["start_ms"], merged["end_ms"]) == (1000, 8000)
    assert merged["severity"] == "high"
    assert set(merged["labels"].split(",")) == {"breast", "buttocks"}
    assert "scanner" in merged["sources"]
    assert "videoskip:776" in merged["sources"]
    # Manual/imported provenance outranks scanner for the primary source.
    assert merged["source"] == "videoskip:776"


async def test_different_categories_are_not_merged(setup_db):
    await _job()
    await db.insert_segments_bulk(
        "title", "Oppenheimer", [_seg(1000, 5000, "nudity")], "scanner"
    )
    await db.insert_segments_bulk(
        "title", "Oppenheimer", [_seg(2000, 6000, "violence")], "videoskip:776"
    )

    assert await db.merge_title_segments("title") == 0
    assert len(await db.get_segments_for_guid("title")) == 2


async def test_small_gap_merges_but_a_large_gap_does_not(setup_db):
    await _job()
    await db.insert_segments_bulk(
        "title", "Oppenheimer", [_seg(1000, 2000, "nudity")], "manual"
    )
    await db.insert_segments_bulk(
        "title", "Oppenheimer", [_seg(2100, 3000, "nudity")], "manual"
    )

    # Default tolerance is 250 ms: the 100 ms gap closes.
    assert await db.merge_title_segments("title") == 1

    await db.upsert_scan_job("title2", "Other", "/media/o.mkv", "2", "1", "Movies")
    await db.insert_segments_bulk(
        "title2", "Other", [_seg(1000, 2000, "nudity")], "manual"
    )
    await db.insert_segments_bulk(
        "title2", "Other", [_seg(3000, 4000, "nudity")], "manual"
    )
    assert await db.merge_title_segments("title2") == 0


async def test_strict_categories_are_collected_from_user_preferences(setup_db):
    await db.upsert_user_category_pref("alice", "fear", 3)
    await db.upsert_user_category_pref("alice", "violence", 2)
    await db.upsert_user_category_pref("alice", "sex", 1)
    await db.upsert_user_category_pref("bob", "language", 3)

    categories = await db.get_strict_categories(min_level=2)

    assert set(categories) == {"fear", "violence", "language"}


async def test_merged_source_still_counts_as_videoskip_coverage(setup_db):
    await _job()
    await db.insert_segments_bulk(
        "title", "Oppenheimer", [_seg(1000, 5000, "nudity")], "scanner"
    )
    await db.insert_segments_bulk(
        "title", "Oppenheimer", [_seg(4000, 8000, "nudity")], "videoskip:776"
    )
    await db.merge_title_segments("title")

    coverage = await db.get_videoskip_coverage()

    assert coverage["with_videoskip"] == 1
