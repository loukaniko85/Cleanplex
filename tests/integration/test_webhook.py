"""Integration tests for the Plex webhook fast path."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

from cleanplex import database as db
from cleanplex.plex_client import MediaItem
from cleanplex.web.routes import webhook_routes


def _item() -> MediaItem:
    return MediaItem(
        rating_key="10",
        plex_guid="plex://movie/webhook",
        title="Oppenheimer",
        year=2023,
        thumb="",
        file_path="/media/opp.mkv",
        library_id="1",
        library_title="Movies",
        media_type="movie",
        imdb_id="tt15398776",
        duration_ms=7_200_000,
    )


def _payload(event: str = "library.new", rating_key: str = "10") -> dict:
    return {"event": event, "Metadata": {"ratingKey": rating_key, "type": "movie"}}


async def test_library_new_queues_videoskip_when_auto_is_on(
    http_client, monkeypatch
):
    await db.set_setting("videoskip_auto_fetch", "1")
    client = MagicMock()
    client.get_library_item = AsyncMock(return_value=_item())
    monkeypatch.setattr(webhook_routes.plex_mod, "get_client", lambda: client)

    response = await http_client.post(
        "/api/webhooks/plex",
        data={"payload": json.dumps(_payload())},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["queued"] is True
    assert (await db.get_videoskip_queue_status())["queued"] == 1


async def test_library_new_is_ignored_when_auto_is_off(http_client, monkeypatch):
    client = MagicMock()
    client.get_library_item = AsyncMock(return_value=_item())
    monkeypatch.setattr(webhook_routes.plex_mod, "get_client", lambda: client)

    response = await http_client.post(
        "/api/webhooks/plex",
        data={"payload": json.dumps(_payload())},
    )

    assert response.status_code == 200
    assert response.json()["ignored"] == "auto-fetch off"
    assert (await db.get_videoskip_queue_status())["total"] == 0


async def test_non_library_event_is_ignored(http_client):
    response = await http_client.post(
        "/api/webhooks/plex",
        data={"payload": json.dumps(_payload(event="media.play"))},
    )

    assert response.status_code == 200
    assert response.json()["ignored"] == "event"


async def test_webhook_status_records_every_delivery(http_client):
    await http_client.post(
        "/api/webhooks/plex",
        data={"payload": json.dumps(_payload(event="media.play"))},
    )
    await http_client.post("/api/webhooks/plex", data={})

    status = (await http_client.get("/api/webhooks/plex/status")).json()

    assert status["total"] == 2
    assert status["last_received_at"]
    assert {row["result"] for row in status["events"]} == {"ignored", "invalid"}
