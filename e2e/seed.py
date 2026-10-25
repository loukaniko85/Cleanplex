"""Seed a throwaway database for the Playwright end-to-end suite.

Run by e2e/start-server.sh before the app boots. The data is deliberately
realistic: completed, failed and pending scans, NudeNet and imported segments,
and a failure whose message explains the container media-mount problem.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Running `python e2e/seed.py` puts e2e/ on sys.path, not the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cleanplex import database as db  # noqa: E402


async def main() -> None:
    data_dir = Path(os.environ.get("CLEANPLEX_DATA", "e2e/.data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    db.set_db_path(data_dir / "cleanplex.db")
    await db.init_db()

    libraries = {
        "1": "Movies",
        "2": "TV Shows",
        "3": "Kids",
    }
    titles = [
        ("m1", "Movie One", "1", "done", None),
        ("m2", "Movie Two", "1", "failed", "File not found: /tv/Movie Two/x.mkv"),
        ("m3", "Movie Three", "1", "pending", None),
        ("t1", "Show – Season 1 – Pilot", "2", "done", None),
        ("k1", "Kids Movie", "3", "failed", "File not found: /kids/Kids Movie.mkv"),
    ]
    for guid, title, library_id, status, error in titles:
        await db.upsert_scan_job(
            plex_guid=guid,
            title=title,
            file_path=f"/media/{guid}.mkv",
            rating_key=guid,
            library_id=library_id,
            library_title=libraries[library_id],
            media_type="episode" if library_id == "2" else "movie",
        )
        await db.update_scan_job_status(guid, status, progress=1.0 if status == "done" else 0.0,
                                        error_msg=error)

    # NudeNet detections (source defaults to 'scanner').
    await db.insert_segment(
        plex_guid="m1", title="Movie One", start_ms=60_000, end_ms=66_000,
        confidence=0.92, labels="FEMALE_BREAST_EXPOSED",
    )
    await db.insert_segment(
        plex_guid="m1", title="Movie One", start_ms=300_000, end_ms=306_000,
        confidence=0.88, labels="FEMALE_BREAST_EXPOSED,BUTTOCKS_EXPOSED",
    )
    await db.insert_segment(
        plex_guid="t1", title="Show – Season 1 – Pilot", start_ms=120_000, end_ms=126_000,
        confidence=0.71, labels="FEMALE_GENITALIA_EXPOSED",
    )
    # Imported file on a title that failed to scan: must not count as NudeNet.
    await db.insert_segments_bulk(
        "m2", "Movie Two",
        [{"start_ms": 5_000, "end_ms": 8_000, "category": "violence",
          "severity": "high", "action": "skip", "channel": "both",
          "labels": "violence skip 3"}],
        "videoskip:776",
    )

    await db.set_setting("plex_url", "http://plex:32400")
    await db.set_setting("plex_token", "e2e-token")
    sys.stdout.write("Seeded e2e database at %s\n" % (data_dir / "cleanplex.db"))


if __name__ == "__main__":
    asyncio.run(main())
