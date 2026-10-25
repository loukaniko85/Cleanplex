"""Integration tests for the VideoSkip Exchange routes."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import httpx
import pytest_asyncio

from cleanplex import database as db
from cleanplex import videoskip

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "videoskip"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _title_path(guid: str) -> str:
    return f"/api/videoskip/title/{quote(guid, safe='')}"


@pytest_asyncio.fixture
async def mock_videoskip():
    """Point the process client at fixtures instead of the live Exchange."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/exchange/search/":
            query = request.url.params.get("q", "").lower()
            page = "search_game_of_thrones.html" if "thrones" in query else "search_oppenheimer.html"
            return httpx.Response(200, text=_fixture(page))
        if path == "/exchange/videos/805/":
            return httpx.Response(200, text=_fixture("video_oppenheimer.html"))
        if path == "/exchange/videos/435/":
            return httpx.Response(200, text=_fixture("video_got_s1e1.html"))
        if path == "/exchange/skip/776/download/":
            return httpx.Response(
                200,
                content=(FIXTURES / "oppenheimer_776.skp").read_bytes(),
                headers={
                    "content-type": "text/plain",
                    "content-disposition": 'attachment; filename="Oppenheimer_2023_local.skp"',
                },
            )
        return httpx.Response(404)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = videoskip.VideoSkipClient(
        base_url="https://example.test/exchange", http=http, min_interval=0
    )
    videoskip.set_client(client)
    yield client
    videoskip.set_client(None)
    await http.aclose()


async def _movie(guid: str = "plex://movie/opp", title: str = "Oppenheimer") -> None:
    await db.upsert_scan_job(
        plex_guid=guid,
        title=title,
        file_path="/media/opp.mkv",
        rating_key="10",
        library_id="1",
        library_title="Movies",
        media_type="movie",
        year=2023,
        imdb_id="tt15398776",
        duration_ms=7_200_000,
    )


async def test_title_lookup_returns_candidates_and_recommendation(http_client, mock_videoskip):
    await _movie()

    response = await http_client.get(_title_path("plex://movie/opp"))

    assert response.status_code == 200
    body = response.json()
    assert body["query"] == "Oppenheimer"
    assert len(body["matches"]) == 1
    match = body["matches"][0]
    assert match["video_id"] == "805"
    assert match["imdb_id"] == "tt15398776"
    assert match["recommended_skip_id"] == "776"
    assert len(match["skip_files"]) == 4


async def test_apply_downloads_and_stores_segments_with_provenance(http_client, mock_videoskip):
    await _movie()

    response = await http_client.post(
        "/api/videoskip/apply", json={"plex_guid": "plex://movie/opp", "skip_id": "776"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["imported"] == 4
    assert body["source"] == "videoskip:776"
    assert body["filename"] == "Oppenheimer_2023_local.skp"
    assert body["categories"] == {"nudity": 2, "sex": 2}
    assert body["warning"] is None

    segments = await db.get_segments_for_guid("plex://movie/opp")
    assert len(segments) == 4
    assert {seg["source"] for seg in segments} == {"videoskip:776"}


async def test_apply_rejects_an_unknown_title(http_client, mock_videoskip):
    response = await http_client.post(
        "/api/videoskip/apply", json={"plex_guid": "plex://movie/missing", "skip_id": "776"}
    )

    assert response.status_code == 404


async def test_apply_rejects_a_non_numeric_skip_id(http_client, mock_videoskip):
    await _movie()

    response = await http_client.post(
        "/api/videoskip/apply", json={"plex_guid": "plex://movie/opp", "skip_id": "../x"}
    )

    assert response.status_code == 422


async def test_episode_without_position_asks_for_a_resync(http_client, mock_videoskip):
    await db.upsert_scan_job(
        plex_guid="plex://episode/old",
        title="Game of Thrones – Season 1 – Winter Is Coming",
        file_path="/media/got.mkv",
        rating_key="20",
        library_id="2",
        library_title="TV",
        media_type="episode",
    )

    response = await http_client.get(_title_path("plex://episode/old"))

    assert response.status_code == 409
    assert "Re-sync" in response.json()["detail"]


async def test_auto_fetch_imports_missing_and_skips_existing(http_client, mock_videoskip):
    await _movie("plex://movie/opp", "Oppenheimer")
    await _movie("plex://movie/opp2", "Oppenheimer")
    await db.insert_segments_bulk(
        "plex://movie/opp",
        "Oppenheimer",
        [{"start_ms": 1000, "end_ms": 2000, "category": "sex", "severity": "high",
          "action": "skip", "channel": "both", "labels": ""}],
        "manual",
    )

    response = await http_client.post(
        "/api/videoskip/auto",
        json={"plex_guids": ["plex://movie/opp", "plex://movie/opp2"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["attempted"] == 2
    assert body["imported"] == 1
    by_guid = {item["plex_guid"]: item for item in body["results"]}
    assert by_guid["plex://movie/opp"]["status"] == "has_segments"
    assert by_guid["plex://movie/opp2"]["status"] == "imported"
    assert by_guid["plex://movie/opp2"]["skip_id"] == "776"

    assert len(await db.get_segments_for_guid("plex://movie/opp")) == 1
    assert len(await db.get_segments_for_guid("plex://movie/opp2")) == 4


async def test_search_endpoint_proxies_the_exchange(http_client, mock_videoskip):
    response = await http_client.get("/api/videoskip/search", params={"q": "Oppenheimer"})

    assert response.status_code == 200
    body = response.json()
    assert body["results"][0]["title"] == "Oppenheimer"


async def test_coverage_reports_totals_and_library_breakdown(http_client):
    await _movie("plex://movie/a", "Oppenheimer")
    await _movie("plex://movie/b", "Oppenheimer")
    await db.insert_segments_bulk(
        "plex://movie/a",
        "Oppenheimer",
        [{"start_ms": 0, "end_ms": 1000, "category": "sex"}],
        "videoskip:776",
    )

    response = await http_client.get("/api/videoskip/coverage")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["with_segments"] == 1
    assert body["with_videoskip"] == 1
    assert body["without_segments"] == 1
    assert body["without_videoskip"] == 1
    assert body["libraries"][0]["library_title"] == "Movies"


async def test_bulk_fetch_respects_the_daily_ceiling(http_client, mock_videoskip):
    await db.set_setting("videoskip_auto_daily_limit", "1")
    await _movie("plex://movie/a", "Oppenheimer")
    await _movie("plex://movie/b", "Oppenheimer")

    response = await http_client.post("/api/videoskip/auto", json={"limit": 10})

    assert response.status_code == 200
    body = response.json()
    assert body["imported"] == 1
    statuses = [row["status"] for row in body["results"]]
    assert "imported" in statuses
    assert statuses[-1] == "limit_reached"


async def test_exchange_health_reports_reachability(http_client, mock_videoskip):
    response = await http_client.get("/api/videoskip/health")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["results"] == 1
    assert body["latency_ms"] >= 0


async def test_queue_enqueue_targets_titles_without_videoskip(http_client):
    await _movie("plex://movie/covered", "Oppenheimer")
    await _movie("plex://movie/empty", "Oppenheimer")
    await db.insert_segments_bulk(
        "plex://movie/covered",
        "Oppenheimer",
        [{"start_ms": 0, "end_ms": 1000, "category": "sex"}],
        "videoskip:776",
    )

    response = await http_client.post("/api/videoskip/queue/enqueue", json={"limit": 10})

    assert response.status_code == 200
    assert response.json() == {"queued": 1, "candidates": 1}

    status = (await http_client.get("/api/videoskip/queue")).json()
    assert status["queued"] == 1
    assert status["total"] == 1


async def test_queue_pause_resume_clear_and_retry(http_client):
    await _movie("plex://movie/a", "Oppenheimer")
    await db.enqueue_videoskip_job("plex://movie/a", "Oppenheimer")
    claimed = await db.claim_next_videoskip_job()
    await db.finish_videoskip_job(claimed["id"], "failed", error="network")

    assert (await http_client.post("/api/videoskip/queue/pause")).json()["paused"] is True
    assert (await http_client.get("/api/videoskip/queue")).json()["paused"] is True
    assert (await http_client.post("/api/videoskip/queue/resume")).json()["paused"] is False

    retry = await http_client.post("/api/videoskip/queue/retry-failed")
    assert retry.json()["queued"] == 1
    assert (await http_client.get("/api/videoskip/queue")).json()["queued"] == 1

    cleared = await http_client.post("/api/videoskip/queue/clear")
    assert cleared.json()["deleted"] == 1
    assert (await http_client.get("/api/videoskip/queue")).json()["total"] == 0


async def test_auto_without_guids_targets_titles_without_videoskip_segments(
    http_client, mock_videoskip
):
    await _movie("plex://movie/covered", "Oppenheimer")
    await _movie("plex://movie/manual-only", "Oppenheimer")
    await _movie("plex://movie/empty", "Oppenheimer")
    await db.insert_segments_bulk(
        "plex://movie/covered",
        "Oppenheimer",
        [{"start_ms": 0, "end_ms": 1000, "category": "sex"}],
        "videoskip:776",
    )
    await db.insert_segments_bulk(
        "plex://movie/manual-only",
        "Oppenheimer",
        [{"start_ms": 0, "end_ms": 1000, "category": "nudity"}],
        "manual",
    )

    response = await http_client.post("/api/videoskip/auto", json={"limit": 10})

    assert response.status_code == 200
    body = response.json()
    assert body["attempted"] == 2
    assert body["imported"] == 2
    assert {item["plex_guid"] for item in body["results"]} == {
        "plex://movie/manual-only",
        "plex://movie/empty",
    }
    assert all(item["status"] == "imported" for item in body["results"])
