"""Startup/bootstrap integration tests.

The container crash on v1.0.2 was a startup-path bug no test exercised:
recover_stale_scanning_jobs() referenced a column that scan_jobs does not have,
so the process died before serving its first request while every unit test
stayed green. This module runs the exact bootstrap chain from main._amain()
against a real database.
"""

from __future__ import annotations

from cleanplex import database as db
from cleanplex.bg_jobs import recover_stale_jobs


async def _bootstrap() -> None:
    """Mirror main._amain()'s database bootstrap, minus the web server."""
    await recover_stale_jobs()
    await db.reset_stale_videoskip_jobs()
    await db.recover_stale_scanning_jobs()
    await db.prune_skip_events(
        int(await db.get_setting("skip_event_retention_days", "90"))
    )


async def test_bootstrap_sequence_runs_on_a_fresh_database(setup_db):
    """A clean install must boot: every bootstrap query must match the schema."""
    await _bootstrap()


async def test_bootstrap_resets_a_scan_interrupted_mid_scan(setup_db):
    await db.upsert_scan_job(
        plex_guid="stuck",
        title="Stuck Movie",
        file_path="/media/stuck.mkv",
        rating_key="42",
        library_id="1",
        library_title="Movies",
    )
    await db.update_scan_job_status("stuck", "scanning", progress=0.4)

    await _bootstrap()

    job = await db.get_scan_job_by_guid("stuck")
    assert job["status"] == "pending"
    assert job["progress"] == 0
