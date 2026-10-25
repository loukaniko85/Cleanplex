"""Cleanplex entry point — starts the watcher loops and web server."""

from __future__ import annotations

import asyncio
import os

import uvicorn

from .logger import setup_logging, get_logger
from . import database as db
from .config import Config
import cleanplex.plex_client as plex_mod
from .paths import DATA_DIR, ensure_data_dirs
from .watcher import session_watcher_loop, library_watcher_loop
from .scanner import scanner_loop
from .videoskip_queue import run_queue as videoskip_queue_loop
from . import videoskip
from .web.app import create_app
from .bg_jobs import recover_stale_jobs

logger = get_logger(__name__)


async def _amain() -> None:
    # ── Bootstrap ──────────────────────────────────────────────────────────
    ensure_data_dirs()
    db.set_db_path(DATA_DIR / "cleanplex.db")
    await db.init_db()

    config = await Config.load()
    setup_logging(config.log_level)

    # Mark any jobs left in running/queued state from a previous crashed process.
    await recover_stale_jobs()
    requeued = await db.reset_stale_videoskip_jobs()
    if requeued:
        logger.warning("Requeued %d stale VideoSkip job(s)", requeued)
    stale_scans = await db.recover_stale_scanning_jobs()
    if stale_scans:
        logger.warning("Reset %d interrupted scan job(s) to pending", stale_scans)

    pruned = await db.prune_skip_events(int(await db.get_setting("skip_event_retention_days", "90")))
    if pruned:
        logger.info("Pruned %d skip event(s) past the retention window", pruned)

    logger.info("Cleanplex starting — data dir: %s", DATA_DIR)

    if config.is_configured():
        client = plex_mod.init_client(config.plex_url, config.plex_token)
        # Restore the transport each client last accepted, so the first skip after a
        # restart doesn't pay the full probe cost again.
        await client.load_client_profiles()
        logger.info("Plex client initialised: %s", config.plex_url)
    else:
        logger.warning("Plex not yet configured — open the web UI to set up.")

    # ── Shared config factory ──────────────────────────────────────────────
    async def get_config():
        return await Config.load()

    def get_client():
        return plex_mod.get_client()

    # ── Web server ─────────────────────────────────────────────────────────
    web_app = create_app()
    host = os.environ.get("CLEANPLEX_HOST", "0.0.0.0")
    port = int(os.environ.get("CLEANPLEX_PORT", "7979"))

    config_uvicorn = uvicorn.Config(
        web_app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config_uvicorn)

    logger.info("Web UI available at http://%s:%d", host, port)

    # ── Run all loops concurrently, shutting down gracefully ──────────────────
    # server.serve() returns on SIGTERM/SIGINT. The background loops are `while
    # True` by design, so they must be cancelled explicitly — otherwise the
    # gather never completes and systemd/NSSM/Docker escalate to SIGKILL
    # mid-write. Long-lived HTTP clients are closed on the way out (§5.3).
    background = [
        asyncio.create_task(session_watcher_loop(get_config, get_client)),
        asyncio.create_task(library_watcher_loop(get_config, get_client)),
        asyncio.create_task(scanner_loop(get_config)),
        asyncio.create_task(videoskip_queue_loop()),
    ]
    try:
        await server.serve()
    finally:
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        await videoskip.close_client()
        try:
            plex_client = plex_mod.get_client()
        except RuntimeError:
            plex_client = None
        if plex_client is not None:
            await plex_client.close()
        logger.info("Cleanplex stopped cleanly")


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass
