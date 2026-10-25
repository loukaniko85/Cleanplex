"""Unit tests for the WhenJumpScare fear-timestamp parser."""

from __future__ import annotations

import httpx
import pytest

from cleanplex import whenjumpscare as wjs


JSONLD_PAGE = """
<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"ItemList","numberOfItems":3,
 "itemListElement":[
   {"@type":"ListItem","position":1,"name":"Minor jumpscare at 00:03:36"},
   {"@type":"ListItem","position":2,"name":"Major jumpscare at 00:40:38"},
   {"@type":"ListItem","position":3,"name":"Minor jumpscare at 01:25:13"}
 ]}
</script></head><body></body></html>
"""


def test_parse_movie_page_reads_the_jsonld_item_list():
    scares = wjs.parse_movie_page(JSONLD_PAGE)

    assert [(s.timestamp, s.severity) for s in scares] == [
        ("00:03:36", "low"),
        ("00:40:38", "high"),
        ("01:25:13", "low"),
    ]
    assert scares[0].start_ms == 216_000


def test_parse_movie_page_falls_back_to_visible_text():
    html = "<p>Major jumpscare at 00:10:00</p>"

    scares = wjs.parse_movie_page(html)

    assert len(scares) == 1
    assert scares[0].severity == "high"
    assert scares[0].start_ms == 600_000


def test_parse_movie_page_deduplicates_and_sorts():
    page = JSONLD_PAGE + JSONLD_PAGE

    scares = wjs.parse_movie_page(page)

    assert len(scares) == 3
    assert [s.start_ms for s in scares] == sorted(s.start_ms for s in scares)


def test_parse_movie_page_returns_empty_for_a_clean_film():
    assert wjs.parse_movie_page("<html><body>No jumpscares.</body></html>") == []


def test_to_segments_builds_fear_skips_around_the_moment():
    scares = [wjs.Jumpscare(start_ms=100_000, severity="high", timestamp="00:01:40")]

    segments = wjs.to_segments(scares)

    assert segments == [
        {
            "start_ms": 100_000 - wjs.WINDOW_BEFORE_MS,
            "end_ms": 100_000 + wjs.WINDOW_AFTER_MS,
            "category": "fear",
            "severity": "high",
            "action": "skip",
            "channel": "both",
            "labels": "jumpscare",
        }
    ]


def test_to_segments_clamps_at_zero():
    scares = [wjs.Jumpscare(start_ms=500, severity="low", timestamp="00:00:00")]

    segments = wjs.to_segments(scares)

    assert segments[0]["start_ms"] == 0


def _client(handler) -> wjs.WhenJumpScareClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return wjs.WhenJumpScareClient(
        base_url="https://example.test", http=http, min_interval=0
    )


async def test_find_movie_rejects_unreadable_json():
    client = _client(lambda request: httpx.Response(200, text="<html>oops</html>"))

    with pytest.raises(wjs.WhenJumpScareError, match="unreadable"):
        await client.find_movie("138843")


async def test_find_movie_rejects_a_link_to_another_host():
    body = {
        "jumpscareCount": 1,
        "movieLink": "https://example.test.evil.example/movie/1",
    }
    client = _client(lambda request: httpx.Response(200, json=body))

    with pytest.raises(wjs.WhenJumpScareError, match="unexpected movie link"):
        await client.find_movie("138843")


async def test_find_movie_returns_empty_when_the_count_is_zero():
    client = _client(
        lambda request: httpx.Response(200, json={"jumpscareCount": 0})
    )

    assert await client.find_movie("138843") == []
