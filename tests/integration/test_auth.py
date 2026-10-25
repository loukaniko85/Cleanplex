"""Integration tests for the optional HTTP Basic auth gate."""

from __future__ import annotations

import base64

import httpx

from cleanplex.web.app import create_app


def _basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


async def test_health_stays_public_while_api_requires_auth(monkeypatch, setup_db):
    monkeypatch.setenv("CLEANPLEX_AUTH_USER", "alice")
    monkeypatch.setenv("CLEANPLEX_AUTH_PASS", "secret")
    app = create_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/api/health")).status_code == 200
        assert (await client.get("/api/settings")).status_code == 401
        # Plex cannot send Basic auth. With auth enabled and no webhook token the
        # receiver refuses (503) rather than silently opening an unauthenticated
        # endpoint; set CLEANPLEX_WEBHOOK_TOKEN to use it.
        assert (await client.post("/api/webhooks/plex", data={})).status_code == 503

        ok = await client.get(
            "/api/settings", headers={"Authorization": _basic("alice", "secret")}
        )
        assert ok.status_code == 200

        bad = await client.get(
            "/api/settings", headers={"Authorization": _basic("alice", "wrong")}
        )
        assert bad.status_code == 401


async def test_no_auth_env_keeps_api_open(monkeypatch, setup_db):
    monkeypatch.delenv("CLEANPLEX_AUTH_USER", raising=False)
    monkeypatch.delenv("CLEANPLEX_AUTH_PASS", raising=False)
    app = create_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/api/settings")).status_code == 200
