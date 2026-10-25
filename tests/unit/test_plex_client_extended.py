"""Extended unit tests for plex_client.py — mocking asyncio.to_thread paths."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from cleanplex.plex_client import (
    PlexClient, ActiveSession, LibrarySection, MediaItem, PlexUser, PlexClientInfo,
)


def _make_client(seek_transport=None) -> PlexClient:
    c = PlexClient("http://plex:32400", "token")
    if seek_transport:
        c._http = httpx.AsyncClient(transport=seek_transport)
    return c


def _mock_server():
    srv = MagicMock()
    srv.friendlyName = "My Plex"
    return srv


# ── test_connection ────────────────────────────────────────────────────────────

async def test_test_connection_success():
    c = _make_client()
    srv = _mock_server()

    with patch("cleanplex.plex_client.asyncio.to_thread", new=AsyncMock(return_value=srv)):
        ok, name = await c.test_connection()

    assert ok is True
    assert name == "My Plex"


async def test_test_connection_failure():
    c = _make_client()
    with patch("cleanplex.plex_client.asyncio.to_thread", side_effect=Exception("connection refused")):
        ok, msg = await c.test_connection()
    assert ok is False
    assert "connection refused" in msg


# ── get_active_sessions ────────────────────────────────────────────────────────

async def test_get_active_sessions_empty():
    c = _make_client()
    srv = _mock_server()
    srv.sessions = MagicMock(return_value=[])

    call_no = 0

    async def fake_to_thread(func, *args, **kwargs):
        nonlocal call_no
        call_no += 1
        if call_no == 1:
            return srv  # _get_server
        return srv.sessions()  # sessions()

    with patch("cleanplex.plex_client.asyncio.to_thread", side_effect=fake_to_thread):
        sessions = await c.get_active_sessions()

    assert sessions == []


async def test_get_active_sessions_returns_empty_on_exception():
    c = _make_client()
    with patch("cleanplex.plex_client.asyncio.to_thread", side_effect=Exception("network error")):
        sessions = await c.get_active_sessions()
    assert sessions == []


def _fake_session(**overrides):
    """A Plex session whose collections are a mix of lists and methods."""
    part = SimpleNamespace(
        file="/movies/movie.mkv",
        # plexapi exposes this as a method; iterating it used to raise.
        audioStreams=lambda: [SimpleNamespace(selected=True, languageCode="eng")],
    )
    session = SimpleNamespace(
        sessionKey="sess-1",
        usernames=lambda: ["alice"],  # method form
        title="Movie",
        type="movie",
        ratingKey="100",
        viewOffset=60_000,
        duration=7_200_000,
        thumb="/library/metadata/100/thumb",
        librarySectionID="1",
        players=[SimpleNamespace(
            machineIdentifier="client-1",
            title="Plex Web",
            state="playing",
            address="192.168.1.10",
            port=32500,
            volume=70,
        )],
        media=[SimpleNamespace(parts=[part])],
        guids=[SimpleNamespace(id="plex://movie/abc")],
    )
    for key, value in overrides.items():
        setattr(session, key, value)
    return session


async def test_get_active_sessions_parses_method_valued_attributes():
    """Regression: 'method' object is not iterable dropped every session."""
    c = _make_client()
    session = _fake_session()
    srv = _mock_server()
    srv.sessions = MagicMock(return_value=[session])

    async def fake_to_thread(func, *args, **kwargs):
        if func == c._get_server:
            return srv
        return srv.sessions()

    with patch.object(
        c, "get_clients",
        new=AsyncMock(return_value=[
            PlexClientInfo("client-1", "Plex Web", "Plex Web", "Firefox", ("playback", "timeline"))
        ]),
    ), patch("cleanplex.plex_client.asyncio.to_thread", side_effect=fake_to_thread):
        sessions = await c.get_active_sessions()

    assert len(sessions) == 1
    parsed = sessions[0]
    assert parsed.user == "alice"
    assert parsed.audio_language == "eng"
    assert parsed.client_identifier == "client-1"
    assert parsed.plex_guid == "plex://movie/abc"
    assert parsed.is_controllable is True
    assert parsed.control_note == ""
    assert c.last_session_error == ""


async def test_get_active_sessions_marks_browser_players_uncontrollable():
    """Plex Web rejects remote commands; the UI must not claim it is controllable."""
    c = _make_client()
    session = _fake_session()
    srv = _mock_server()
    srv.sessions = MagicMock(return_value=[session])

    async def fake_to_thread(func, *args, **kwargs):
        if func == c._get_server:
            return srv
        return srv.sessions()

    with patch.object(
        c, "get_clients", new=AsyncMock(return_value=[])
    ), patch.object(
        c, "_probe_client_control", new=AsyncMock(return_value=(False, "HTTP 400 Bad Request"))
    ), patch("cleanplex.plex_client.asyncio.to_thread", side_effect=fake_to_thread):
        sessions = await c.get_active_sessions()

    assert sessions[0].is_controllable is False
    assert "could not control this client" in sessions[0].control_note
    assert "HTTP 400" in sessions[0].control_note


async def test_get_active_sessions_probes_clients_missing_from_registry():
    """Mobile apps often play without appearing in /clients but accept commands."""
    c = _make_client()
    session = _fake_session()
    srv = _mock_server()
    srv.sessions = MagicMock(return_value=[session])

    async def fake_to_thread(func, *args, **kwargs):
        if func == c._get_server:
            return srv
        return srv.sessions()

    with patch.object(
        c, "get_clients", new=AsyncMock(return_value=[])
    ), patch.object(
        c, "_probe_client_control", new=AsyncMock(return_value=(True, ""))
    ), patch("cleanplex.plex_client.asyncio.to_thread", side_effect=fake_to_thread):
        sessions = await c.get_active_sessions()

    assert sessions[0].is_controllable is True
    assert sessions[0].control_note == ""


async def test_probe_client_control_caches_the_result():
    c = _make_client()
    with patch.object(c, "_try_proxy", new=AsyncMock(return_value=False)) as proxy:
        first = await c._probe_client_control("phone-1", "", 32500)
        second = await c._probe_client_control("phone-1", "", 32500)

    assert first[0] is False
    assert second[0] is False
    assert proxy.await_count == 1  # second call served from cache


async def test_get_active_sessions_falls_back_when_capability_registry_fails():
    c = _make_client()
    session = _fake_session()
    srv = _mock_server()
    srv.sessions = MagicMock(return_value=[session])

    async def fake_to_thread(func, *args, **kwargs):
        if func == c._get_server:
            return srv
        return srv.sessions()

    with patch.object(
        c, "get_clients", new=AsyncMock(return_value=None)
    ), patch.object(
        c, "_probe_client_control", new=AsyncMock(return_value=(True, ""))
    ), patch("cleanplex.plex_client.asyncio.to_thread", side_effect=fake_to_thread):
        sessions = await c.get_active_sessions()

    assert sessions[0].is_controllable is True
    assert sessions[0].control_note == ""


async def test_get_active_sessions_tolerates_missing_players_and_media():
    c = _make_client()
    session = _fake_session(players=None, media=None, guids=[])
    srv = _mock_server()
    srv.sessions = MagicMock(return_value=[session])

    async def fake_to_thread(func, *args, **kwargs):
        if func == c._get_server:
            return srv
        return srv.sessions()

    with patch.object(
        c, "get_clients", new=AsyncMock(return_value=[])
    ), patch.object(
        c, "_probe_client_control", new=AsyncMock(return_value=(False, "no player"))
    ), patch("cleanplex.plex_client.asyncio.to_thread", side_effect=fake_to_thread):
        sessions = await c.get_active_sessions()

    assert len(sessions) == 1
    assert sessions[0].client_title == "Unknown"
    assert sessions[0].audio_language == ""


async def test_get_clients_parses_the_registry():
    c = _make_client()

    def handler(request):
        return httpx.Response(200, json={"MediaContainer": {"Device": [
            {"machineIdentifier": "tv-1", "title": "Living Room",
             "product": "Plex for Android", "device": "Nvidia Shield",
             "protocolCapabilities": "playback,timeline,navigation"},
            {"machineIdentifier": "web-1", "title": "Firefox",
             "product": "Plex Web", "device": "PC",
             "protocolCapabilities": "timeline"},
        ]}})

    c._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    clients = await c.get_clients()

    assert clients is not None
    by_id = {client.machine_identifier: client for client in clients}
    assert by_id["tv-1"].controllable is True
    assert by_id["web-1"].controllable is False


async def test_get_clients_returns_none_when_unreadable():
    c = _make_client()
    c._http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(500))
    )

    assert await c.get_clients() is None


async def test_command_ids_are_monotonic_and_unique():
    c = _make_client()
    first = c._next_command_id()
    second = c._next_command_id()

    assert second == first + 1


# ── seek ──────────────────────────────────────────────────────────────────────

async def test_seek_success_via_server_proxy():
    c = _make_client()
    srv = _mock_server()
    srv.query = MagicMock(return_value=None)

    call_no = 0

    async def fake_to_thread(func, *args, **kwargs):
        nonlocal call_no
        call_no += 1
        if call_no == 1:
            return srv
        return None  # srv.query

    with patch("cleanplex.plex_client.asyncio.to_thread", side_effect=fake_to_thread):
        result = await c.seek("client-id", 30000)

    assert result is True


async def test_seek_falls_back_to_direct_http_on_proxy_failure():
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=b"ok"))
    c = _make_client(seek_transport=transport)

    with patch("cleanplex.plex_client.asyncio.to_thread", side_effect=Exception("proxy failed")):
        result = await c.seek("client-id", 30000, client_address="192.168.1.10", client_port=32500)

    assert result is True


async def test_seek_returns_false_when_no_client_address_and_proxy_fails():
    c = _make_client()
    with patch("cleanplex.plex_client.asyncio.to_thread", side_effect=Exception("proxy failed")):
        result = await c.seek("client-id", 30000, client_address="")
    assert result is False


async def test_seek_returns_false_when_all_variants_fail():
    transport = httpx.MockTransport(lambda req: httpx.Response(400, content=b"bad"))
    c = _make_client(seek_transport=transport)
    with patch("cleanplex.plex_client.asyncio.to_thread", side_effect=Exception("proxy failed")):
        result = await c.seek("client-id", 30000, client_address="192.168.1.10")
    assert result is False


# ── get_library_sections ──────────────────────────────────────────────────────

async def test_get_library_sections_returns_list():
    c = _make_client()
    srv = _mock_server()

    mock_section = MagicMock()
    mock_section.key = "1"
    mock_section.title = "Movies"
    mock_section.type = "movie"

    call_no = 0

    async def fake_to_thread(func, *args, **kwargs):
        nonlocal call_no
        call_no += 1
        if call_no == 1:
            return srv
        return [mock_section]  # sections()

    with patch("cleanplex.plex_client.asyncio.to_thread", side_effect=fake_to_thread):
        sections = await c.get_library_sections()

    assert len(sections) == 1
    assert isinstance(sections[0], LibrarySection)
    assert sections[0].section_id == "1"
    assert sections[0].section_type == "movie"


async def test_get_library_sections_excludes_non_video_sections():
    c = _make_client()
    srv = _mock_server()

    music = MagicMock()
    music.key = "5"
    music.title = "Music"
    music.type = "artist"  # Not movie or show → excluded

    call_no = 0

    async def fake_to_thread(func, *args, **kwargs):
        nonlocal call_no
        call_no += 1
        if call_no == 1:
            return srv
        return [music]

    with patch("cleanplex.plex_client.asyncio.to_thread", side_effect=fake_to_thread):
        sections = await c.get_library_sections()

    assert sections == []


async def test_get_library_sections_returns_empty_on_exception():
    c = _make_client()
    with patch("cleanplex.plex_client.asyncio.to_thread", side_effect=Exception("plex down")):
        sections = await c.get_library_sections()
    assert sections == []


# ── _media_item_from_plex ─────────────────────────────────────────────────────

def test_media_item_from_plex_movie():
    c = _make_client()

    mock_item = MagicMock()
    mock_item.type = "movie"
    mock_item.ratingKey = 42
    mock_item.title = "Inception"
    mock_item.year = 2010
    mock_item.thumb = "/thumb/42"
    mock_item.contentRating = "PG-13"
    mock_item.media = [MagicMock()]
    mock_item.media[0].parts = [MagicMock()]
    mock_item.media[0].parts[0].file = "/media/inception.mkv"
    mock_item.guids = [MagicMock()]
    mock_item.guids[0].id = "imdb://tt1375666"
    del mock_item.grandparentTitle

    item = c._media_item_from_plex(mock_item, "lib1", "Movies")
    assert isinstance(item, MediaItem)
    assert item.title == "Inception"
    assert item.file_path == "/media/inception.mkv"
    assert item.rating_key == "42"


def test_media_item_from_plex_episode_includes_show_in_title():
    c = _make_client()

    mock_ep = MagicMock()
    mock_ep.type = "episode"
    mock_ep.ratingKey = 100
    mock_ep.title = "Pilot"
    mock_ep.year = 2020
    mock_ep.thumb = ""
    mock_ep.contentRating = "TV-MA"
    mock_ep.grandparentTitle = "Breaking Bad"
    mock_ep.parentTitle = "Season 1"
    mock_ep.media = [MagicMock()]
    mock_ep.media[0].parts = [MagicMock()]
    mock_ep.media[0].parts[0].file = "/ep.mkv"
    mock_ep.guids = [MagicMock()]
    mock_ep.guids[0].id = "imdb://ep1"

    item = c._media_item_from_plex(mock_ep, "lib2", "Shows")
    assert "Breaking Bad" in item.title
    assert "Pilot" in item.title


def test_media_item_from_plex_returns_none_on_error():
    c = _make_client()
    result = c._media_item_from_plex(None, "lib", "L")
    assert result is None


# ── close ─────────────────────────────────────────────────────────────────────

async def test_close_calls_aclose():
    c = _make_client()
    mock_http = AsyncMock()
    c._http = mock_http
    await c.close()
    mock_http.aclose.assert_awaited_once()


# ── invalidate ────────────────────────────────────────────────────────────────

def test_invalidate_clears_server():
    c = _make_client()
    c._server = MagicMock()
    c.invalidate()
    assert c._server is None


# ── update_cleanplex_summary ──────────────────────────────────────────────────

async def test_update_cleanplex_summary_success():
    c = _make_client()
    srv = _mock_server()

    mock_item = MagicMock()
    mock_item.summary = "Original summary."
    mock_item.editSummary = MagicMock()

    call_no = 0

    async def fake_to_thread(func, *args, **kwargs):
        nonlocal call_no
        call_no += 1
        if call_no == 1:
            return srv
        if "fetchItem" in str(func) or (args and isinstance(args[0], int)):
            return mock_item
        return None

    with patch("cleanplex.plex_client.asyncio.to_thread", side_effect=fake_to_thread):
        result = await c.update_cleanplex_summary("42", "Scanned", 3)

    assert result is True


async def test_update_cleanplex_summary_returns_false_on_exception():
    c = _make_client()
    with patch("cleanplex.plex_client.asyncio.to_thread", side_effect=Exception("fetch failed")):
        result = await c.update_cleanplex_summary("42", "Scanned", 3)
    assert result is False
