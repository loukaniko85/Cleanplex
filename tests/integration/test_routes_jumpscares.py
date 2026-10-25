"""Integration tests for the WhenJumpScare import route."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from cleanplex import database as db
from cleanplex import whenjumpscare

pytestmark = pytest.mark.usefixtures("setup_db")


async def _film(plex_guid: str = "film-1", tmdb_id: str = "138843", media_type: str = "movie"):
    await db.upsert_scan_job(
        plex_guid=plex_guid,
        title="The Conjuring",
        file_path="/movies/conjuring.mkv",
        rating_key="1",
        library_id="1",
        library_title="Movies",
        media_type=media_type,
        tmdb_id=tmdb_id,
        duration_ms=6_720_000,
    )


class _FakeClient:
    def __init__(self, scares):
        self.find_movie = AsyncMock(return_value=scares)


async def test_fetch_jumpscares_applies_fear_segments(http_client):
    await _film()
    scares = [
        whenjumpscare.Jumpscare(start_ms=216_000, severity="low", timestamp="00:03:36"),
        whenjumpscare.Jumpscare(start_ms=2_438_000, severity="high", timestamp="00:40:38"),
    ]
    fake = _FakeClient(scares)
    with patch(
        "cleanplex.web.routes.jumpscare_routes.whenjumpscare.get_client",
        return_value=fake,
    ):
        resp = await http_client.post("/api/jumpscares/title/film-1")

    assert resp.status_code == 200
    body = resp.json()
    assert body["scares"] == 2
    assert body["imported"] == 2
    fake.find_movie.assert_awaited_once_with("138843")

    stored = await db.get_segments_for_guid("film-1")
    assert {s["category"] for s in stored} == {"fear"}
    assert {s["source"] for s in stored} == {"whenjumpscare:138843"}
    assert sorted(s["start_ms"] for s in stored) == [
        216_000 - whenjumpscare.WINDOW_BEFORE_MS,
        2_438_000 - whenjumpscare.WINDOW_BEFORE_MS,
    ]


async def test_fetch_jumpscares_replaces_a_previous_fetch(http_client):
    await _film()
    fake = _FakeClient([
        whenjumpscare.Jumpscare(start_ms=100_000, severity="low", timestamp="00:01:40")
    ])
    with patch(
        "cleanplex.web.routes.jumpscare_routes.whenjumpscare.get_client",
        return_value=fake,
    ):
        await http_client.post("/api/jumpscares/title/film-1")
        await http_client.post("/api/jumpscares/title/film-1")

    assert len(await db.get_segments_for_guid("film-1")) == 1


async def test_fetch_jumpscares_requires_a_tmdb_id(http_client):
    await _film(tmdb_id="")

    resp = await http_client.post("/api/jumpscares/title/film-1")

    assert resp.status_code == 409
    assert "TMDB" in resp.json()["detail"]


async def test_fetch_jumpscares_rejects_episodes(http_client):
    await _film(media_type="episode")

    resp = await http_client.post("/api/jumpscares/title/film-1")

    assert resp.status_code == 409
    assert "films" in resp.json()["detail"]


async def test_fetch_jumpscares_reports_an_empty_list(http_client):
    await _film()
    with patch(
        "cleanplex.web.routes.jumpscare_routes.whenjumpscare.get_client",
        return_value=_FakeClient([]),
    ):
        resp = await http_client.post("/api/jumpscares/title/film-1")

    assert resp.status_code == 200
    assert resp.json()["imported"] == 0
    assert "no jumpscares" in resp.json()["message"].lower()
