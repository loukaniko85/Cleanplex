"""SQLite database access layer using aiosqlite."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

from .logger import get_logger

logger = get_logger(__name__)

_DB_PATH: Path | None = None
# Serializes concurrent upsert_plex_markers calls so DELETE+INSERT never interleave.
_marker_upsert_lock = asyncio.Lock()


def set_db_path(path: Path) -> None:
    global _DB_PATH
    _DB_PATH = path


def get_db_path() -> Path:
    if _DB_PATH is None:
        raise RuntimeError("Database path not configured. Call set_db_path() first.")
    return _DB_PATH


@asynccontextmanager
async def get_connection():
    async with aiosqlite.connect(str(get_db_path())) as conn:
        conn.row_factory = aiosqlite.Row
        # foreign_keys is connection-scoped and must be set per connection.
        # journal_mode=WAL is persistent after init_db(); no need to repeat it here.
        await conn.execute("PRAGMA foreign_keys=ON")
        yield conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS segments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    plex_guid     TEXT    NOT NULL,
    title         TEXT,
    start_ms      INTEGER NOT NULL,
    end_ms        INTEGER NOT NULL,
    confidence    REAL    DEFAULT 0,
    labels        TEXT    DEFAULT '',
    thumbnail_path TEXT,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_segments_guid ON segments(plex_guid);

CREATE TABLE IF NOT EXISTS skip_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    plex_guid         TEXT    NOT NULL,
    title             TEXT    DEFAULT '',
    username          TEXT    DEFAULT '',
    client_identifier TEXT    DEFAULT '',
    client_title      TEXT    DEFAULT '',
    category          TEXT    DEFAULT '',
    action            TEXT    DEFAULT 'skip',
    segment_id        INTEGER,
    position_ms       INTEGER DEFAULT 0,
    target_ms         INTEGER DEFAULT 0,
    success           INTEGER DEFAULT 1,
    latency_ms        INTEGER DEFAULT 0,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_skip_events_created ON skip_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_skip_events_client ON skip_events(client_identifier);

CREATE TABLE IF NOT EXISTS user_category_prefs (
    plex_username TEXT    NOT NULL,
    category      TEXT    NOT NULL,
    level         INTEGER DEFAULT 0,
    action        TEXT    DEFAULT '',
    PRIMARY KEY (plex_username, category)
);

CREATE TABLE IF NOT EXISTS user_filters (
    plex_username TEXT PRIMARY KEY,
    enabled       INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS scan_jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    plex_guid   TEXT    UNIQUE NOT NULL,
    title       TEXT,
    file_path   TEXT,
    part_files  TEXT    DEFAULT '',
    rating_key  TEXT,
    library_id  TEXT,
    library_title TEXT,
    media_type  TEXT DEFAULT 'movie',
    content_rating TEXT DEFAULT '',
    year        INTEGER,
    status      TEXT DEFAULT 'pending',
    progress    REAL DEFAULT 0,
    force_scan  INTEGER DEFAULT 0,
    ignored     INTEGER DEFAULT 0,
    started_at  TIMESTAMP,
    finished_at TIMESTAMP,
    error_msg   TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS segment_library_entries (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    file_hash         TEXT    NOT NULL,
    file_name         TEXT    NOT NULL,
    file_size         INTEGER,
    duration_ms       INTEGER,
    segments_json     TEXT    NOT NULL,
    cloud_version     INTEGER DEFAULT 0,
    source_instance   TEXT    NOT NULL,
    confidence_level  TEXT    DEFAULT 'local',
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(file_hash, source_instance)
);
CREATE INDEX IF NOT EXISTS idx_segment_library_hash ON segment_library_entries(file_hash);

CREATE TABLE IF NOT EXISTS sync_metadata (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_name             TEXT    NOT NULL UNIQUE,
    github_repo               TEXT,
    github_token              TEXT,
    last_sync_time            TIMESTAMP,
    sync_enabled              INTEGER DEFAULT 0,
    conflict_resolution       TEXT    DEFAULT 'consensus',
    verified_threshold        INTEGER DEFAULT 2,
    timing_tolerance_ms       INTEGER DEFAULT 2000,
    created_at                TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS bg_jobs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type           TEXT    NOT NULL,
    status             TEXT    DEFAULT 'queued',
    progress_percent   INTEGER DEFAULT 0,
    result_data        TEXT,
    error_message      TEXT,
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at         TIMESTAMP,
    completed_at       TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_bg_jobs_status ON bg_jobs(status, created_at DESC);

CREATE TABLE IF NOT EXISTS videoskip_queue (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    plex_guid    TEXT    NOT NULL UNIQUE,
    title        TEXT    DEFAULT '',
    status       TEXT    DEFAULT 'queued',
    attempts     INTEGER DEFAULT 0,
    last_error   TEXT    DEFAULT '',
    skip_id      TEXT    DEFAULT '',
    imported     INTEGER DEFAULT 0,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_videoskip_queue_status ON videoskip_queue(status, created_at);

CREATE TABLE IF NOT EXISTS plex_markers (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    plex_guid      TEXT    NOT NULL,
    rating_key     TEXT    NOT NULL,
    marker_type    TEXT    NOT NULL,
    start_ms       INTEGER NOT NULL,
    end_ms         INTEGER NOT NULL,
    plex_marker_id INTEGER,
    final          INTEGER DEFAULT 0,
    synced_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(rating_key, plex_marker_id)
);
CREATE INDEX IF NOT EXISTS idx_plex_markers_guid ON plex_markers(plex_guid);
CREATE INDEX IF NOT EXISTS idx_plex_markers_rating_key ON plex_markers(rating_key);

CREATE TABLE IF NOT EXISTS plex_title_cache (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    library_id      TEXT    NOT NULL,
    rating_key      TEXT    NOT NULL,
    plex_guid       TEXT    NOT NULL,
    title           TEXT    NOT NULL,
    media_type      TEXT    NOT NULL,
    show_guid       TEXT    DEFAULT '',
    show_title      TEXT    DEFAULT '',
    show_rating_key TEXT    DEFAULT '',
    content_rating  TEXT    DEFAULT '',
    year            INTEGER,
    cached_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(library_id, rating_key)
);
CREATE INDEX IF NOT EXISTS idx_plex_title_cache_library ON plex_title_cache(library_id);
"""

DEFAULT_SETTINGS = {
    "plex_url": "",
    "plex_token": "",
    "poll_interval": "5",
    "confidence_threshold": "0.6",
    "skip_buffer_ms": "3000",
    # Segment bounds are widened by these before matching, so a skip lands ahead of
    # the flagged content and does not re-trigger on the tail. Seeded from
    # skip_buffer_ms at migration time for installs that predate them.
    "pre_buffer_ms": "3000",
    "post_buffer_ms": "3000",
    "scan_step_ms": "5000",
    "scan_workers": "2",
    "segment_gap_ms": "12000",
    "segment_min_hits": "1",
    "scan_window_start": "23:00",
    "scan_window_end": "06:00",
    "log_level": "INFO",
    "excluded_library_ids": "[]",
    "scan_ratings": "[]",  # empty = scan all ratings
    "scan_labels": "[\"FEMALE_BREAST_EXPOSED\",\"FEMALE_GENITALIA_EXPOSED\",\"MALE_GENITALIA_EXPOSED\",\"ANUS_EXPOSED\",\"BUTTOCKS_EXPOSED\"]",
    "nudenet_model": "320n",
    # Empty means "use subtitle_scanner.DEFAULT_WORDLIST"; a JSON array overrides it.
    "profanity_wordlist": "",
    "nudenet_model_path": "",
    # Segment library sharing settings
    "sync_enabled": "0",
    "sync_instance_name": "",
    "sync_github_repo": "",
    "sync_conflict_resolution": "consensus",
    "sync_verified_threshold": "2",
    "sync_timing_tolerance_ms": "2000",
    # Days of skip history to keep; pruned at startup. 0 disables pruning.
    "skip_event_retention_days": "90",
}


async def init_db() -> None:
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    async with get_connection() as conn:
        # WAL mode is a persistent DB property; set it once at init rather than
        # on every connection to avoid redundant per-connection overhead.
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.executescript(SCHEMA)
        # Additive schema migrations — each ALTER TABLE is idempotent.
        # Catch only the specific OperationalError SQLite raises for duplicate columns;
        # any other error (e.g. corrupt DB, permission failure) is re-raised immediately.
        migrations = [
            "ALTER TABLE scan_jobs ADD COLUMN content_rating TEXT DEFAULT ''",
            "ALTER TABLE scan_jobs ADD COLUMN media_type TEXT DEFAULT 'movie'",
            "ALTER TABLE scan_jobs ADD COLUMN force_scan INTEGER DEFAULT 0",
            "ALTER TABLE scan_jobs ADD COLUMN year INTEGER",
            "ALTER TABLE segments ADD COLUMN labels TEXT DEFAULT ''",
            "ALTER TABLE scan_jobs ADD COLUMN ignored INTEGER DEFAULT 0",
            # show_guid stores the Plex grandparentGuid for episodes so the
            # scan queue can group all episodes of a show together.
            "ALTER TABLE scan_jobs ADD COLUMN show_guid TEXT DEFAULT ''",
            # show_rating_key stores the grandparentRatingKey so the library view
            # can build show poster URLs from DB without any Plex API call.
            "ALTER TABLE scan_jobs ADD COLUMN show_rating_key TEXT DEFAULT ''",
            # part_files stores a JSON array of all file paths for multi-part movies
            # (e.g. CD1/CD2 rips). Empty string means single-file title.
            "ALTER TABLE scan_jobs ADD COLUMN part_files TEXT DEFAULT ''",
            # Runtime in milliseconds, used to sanity-check imported skip files
            # against the release actually in the library. 0 means unknown.
            "ALTER TABLE scan_jobs ADD COLUMN duration_ms INTEGER DEFAULT 0",
            # Content classification, following the MovieContentFilter 1.1.0
            # vocabulary. Defaults describe what the NudeNet scanner produces, so
            # rows written before this migration read back correctly.
            "ALTER TABLE segments ADD COLUMN category TEXT DEFAULT 'nudity'",
            "ALTER TABLE segments ADD COLUMN severity TEXT DEFAULT 'high'",
            "ALTER TABLE segments ADD COLUMN action TEXT DEFAULT 'skip'",
            "ALTER TABLE segments ADD COLUMN channel TEXT DEFAULT 'both'",
            "ALTER TABLE segments ADD COLUMN source TEXT DEFAULT 'scanner'",
            "CREATE INDEX IF NOT EXISTS idx_segments_guid_category ON segments(plex_guid, category)",
            # Audio-track language a segment was authored against. Empty means it
            # applies to every track, which is right for image-derived segments.
            "ALTER TABLE segments ADD COLUMN language TEXT DEFAULT ''",
            # Comma-separated provenance for merged segments. `source` remains the
            # primary source shown in the UI; `sources` preserves every origin so
            # "from VideoSkip" coverage survives a merge with a scanner segment.
            "ALTER TABLE segments ADD COLUMN sources TEXT DEFAULT ''",
            # External IDs and episode position, used to match a title against the
            # VideoSkip Exchange. Empty/NULL means Plex did not supply it.
            "ALTER TABLE scan_jobs ADD COLUMN imdb_id TEXT DEFAULT ''",
            "ALTER TABLE scan_jobs ADD COLUMN tmdb_id TEXT DEFAULT ''",
            "ALTER TABLE scan_jobs ADD COLUMN season INTEGER",
            "ALTER TABLE scan_jobs ADD COLUMN episode INTEGER",
        ]
        for stmt in migrations:
            try:
                await conn.execute(stmt)
                await conn.commit()
            except aiosqlite.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    # Unexpected error — surface it rather than silently continuing.
                    logger.error("Unexpected migration failure: %s — %s", stmt, exc)
                    raise
        # Migrate plex_markers: replace UNIQUE(plex_guid, plex_marker_id) with
        # UNIQUE(rating_key, plex_marker_id). Episodes from the same show share the
        # same IMDB plex_guid, so the old constraint caused false collisions during
        # concurrent bulk sync. plex_markers is a Plex cache — safe to recreate.
        schema_row = await (await conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='plex_markers'"
        )).fetchone()
        if schema_row and "plex_guid, plex_marker_id" in (schema_row["sql"] or ""):
            await conn.executescript("""
                CREATE TABLE IF NOT EXISTS plex_markers_v2 (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    plex_guid      TEXT    NOT NULL,
                    rating_key     TEXT    NOT NULL,
                    marker_type    TEXT    NOT NULL,
                    start_ms       INTEGER NOT NULL,
                    end_ms         INTEGER NOT NULL,
                    plex_marker_id INTEGER,
                    final          INTEGER DEFAULT 0,
                    synced_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(rating_key, plex_marker_id)
                );
                INSERT OR IGNORE INTO plex_markers_v2
                    SELECT * FROM plex_markers;
                DROP TABLE plex_markers;
                ALTER TABLE plex_markers_v2 RENAME TO plex_markers;
                CREATE INDEX IF NOT EXISTS idx_plex_markers_guid ON plex_markers(plex_guid);
                CREATE INDEX IF NOT EXISTS idx_plex_markers_rating_key ON plex_markers(rating_key);
            """)
            await conn.commit()
            logger.info("Migrated plex_markers: UNIQUE constraint now on (rating_key, plex_marker_id)")

        # Seed the split buffers from the existing skip_buffer_ms before the
        # defaults below claim the keys, so an install that tuned skip_buffer_ms
        # keeps its value instead of silently reverting to 3000.
        legacy_buffer = await (await conn.execute(
            "SELECT value FROM settings WHERE key = 'skip_buffer_ms'"
        )).fetchone()
        if legacy_buffer and legacy_buffer["value"]:
            for key in ("pre_buffer_ms", "post_buffer_ms"):
                await conn.execute(
                    "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                    (key, legacy_buffer["value"]),
                )

        for key, value in DEFAULT_SETTINGS.items():
            await conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                (key, value),
            )
        await conn.commit()
    logger.info("Database initialised at %s", db_path)


# ── Settings ──────────────────────────────────────────────────────────────────

async def get_all_settings() -> dict[str, str]:
    async with get_connection() as conn:
        rows = await conn.execute_fetchall("SELECT key, value FROM settings")
        return {row["key"]: row["value"] for row in rows}


async def get_setting(key: str, default: str = "") -> str:
    async with get_connection() as conn:
        row = await (await conn.execute("SELECT value FROM settings WHERE key=?", (key,))).fetchone()
        return row["value"] if row else default


async def set_setting(key: str, value: str) -> None:
    async with get_connection() as conn:
        await conn.execute(
            "INSERT INTO settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await conn.commit()


async def update_settings(data: dict[str, str]) -> None:
    async with get_connection() as conn:
        for key, value in data.items():
            await conn.execute(
                "INSERT INTO settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
        await conn.commit()


# ── User Filters ──────────────────────────────────────────────────────────────

async def record_skip_event(
    plex_guid: str,
    title: str = "",
    username: str = "",
    client_identifier: str = "",
    client_title: str = "",
    category: str = "",
    action: str = "skip",
    segment_id: int | None = None,
    position_ms: int = 0,
    target_ms: int = 0,
    success: bool = True,
    latency_ms: int = 0,
) -> int:
    """Record one filter action and return its row id."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO skip_events(plex_guid, title, username, client_identifier, client_title, "
            "category, action, segment_id, position_ms, target_ms, success, latency_ms) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (plex_guid, title, username, client_identifier, client_title, category, action,
             segment_id, position_ms, target_ms, int(success), latency_ms),
        )
        await conn.commit()
        return cursor.lastrowid


async def get_skip_events(limit: int = 50, offset: int = 0) -> list[dict]:
    """Return recent skip events, newest first."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT * FROM skip_events ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return [dict(r) for r in rows]


async def count_skip_events() -> int:
    async with get_connection() as conn:
        row = await (await conn.execute("SELECT COUNT(*) FROM skip_events")).fetchone()
        return row[0] if row else 0


async def prune_skip_events(retention_days: int) -> int:
    """Delete events older than the retention window. 0 keeps everything."""
    if retention_days <= 0:
        return 0
    async with get_connection() as conn:
        cursor = await conn.execute(
            "DELETE FROM skip_events WHERE created_at < datetime('now', ?)",
            (f"-{int(retention_days)} days",),
        )
        await conn.commit()
        return cursor.rowcount


async def get_skip_counts_by_category() -> list[dict]:
    """Return skip counts grouped by content category, busiest first."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT COALESCE(NULLIF(category, ''), 'unknown') AS category, COUNT(*) AS count "
            "FROM skip_events GROUP BY category ORDER BY count DESC"
        )
        return [dict(r) for r in rows]


async def get_skip_counts_by_client() -> list[dict]:
    """Return per-client volume, failure rate and average latency, worst first.

    Aggregated in SQL rather than by loading rows: this table grows without bound
    between prunes.
    """
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            """
            SELECT COALESCE(NULLIF(client_title, ''), 'unknown') AS client_title,
                   client_identifier,
                   COUNT(*)                                   AS count,
                   SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS failures,
                   ROUND(AVG(latency_ms))                     AS avg_latency_ms
            FROM skip_events
            GROUP BY client_identifier, client_title
            ORDER BY failures DESC, count DESC
            """
        )
        result = []
        for row in rows:
            item = dict(row)
            item["failure_rate"] = round(item["failures"] / item["count"], 3) if item["count"] else 0.0
            result.append(item)
        return result


async def get_most_skipped_titles(limit: int = 20) -> list[dict]:
    """Return the titles whose segments fire most often."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT plex_guid, COALESCE(NULLIF(title, ''), plex_guid) AS title, COUNT(*) AS count "
            "FROM skip_events GROUP BY plex_guid, title ORDER BY count DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]


async def get_segment_categories() -> list[dict]:
    """Return the categories that actually have segments, with counts."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT COALESCE(NULLIF(category, ''), 'other') AS category, COUNT(*) AS count "
            "FROM segments GROUP BY category ORDER BY count DESC"
        )
        return [dict(r) for r in rows]


async def get_user_category_prefs(username: str) -> dict[str, dict]:
    """Return {category: {"level": int, "action": str}} for one user, empty if unset."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT category, level, action FROM user_category_prefs WHERE plex_username=?",
            (username,),
        )
        return {r["category"]: {"level": r["level"], "action": r["action"]} for r in rows}


async def upsert_user_category_pref(username: str, category: str, level: int, action: str = "") -> None:
    """Set one user's filtering level (0-3) and optional action override for a category."""
    async with get_connection() as conn:
        await conn.execute(
            "INSERT INTO user_category_prefs(plex_username, category, level, action) VALUES(?,?,?,?) "
            "ON CONFLICT(plex_username, category) DO UPDATE SET level=excluded.level, action=excluded.action",
            (username, category, level, action),
        )
        await conn.commit()


async def delete_user_category_prefs(username: str) -> int:
    """Remove all category preferences for a user, reverting them to defaults."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "DELETE FROM user_category_prefs WHERE plex_username=?", (username,)
        )
        await conn.commit()
        return cursor.rowcount


async def get_all_user_filters() -> list[dict]:
    async with get_connection() as conn:
        rows = await conn.execute_fetchall("SELECT plex_username, enabled FROM user_filters ORDER BY plex_username")
        return [dict(r) for r in rows]


async def get_user_filter(username: str) -> dict | None:
    async with get_connection() as conn:
        row = await (await conn.execute(
            "SELECT plex_username, enabled FROM user_filters WHERE plex_username=?", (username,)
        )).fetchone()
        return dict(row) if row else None


async def upsert_user_filter(username: str, enabled: bool) -> None:
    async with get_connection() as conn:
        await conn.execute(
            "INSERT INTO user_filters(plex_username, enabled) VALUES(?,?) "
            "ON CONFLICT(plex_username) DO UPDATE SET enabled=excluded.enabled",
            (username, 1 if enabled else 0),
        )
        await conn.commit()


# ── Segments ──────────────────────────────────────────────────────────────────

async def get_segments_for_guid(plex_guid: str) -> list[dict]:
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT * FROM segments WHERE plex_guid=? ORDER BY start_ms", (plex_guid,)
        )
        return [dict(r) for r in rows]


async def get_segments_for_guids(plex_guids: list[str]) -> list[dict]:
    """Return all segments for multiple guids in a single query, ordered by plex_guid, start_ms."""
    if not plex_guids:
        return []
    async with get_connection() as conn:
        placeholders = ",".join("?" * len(plex_guids))
        rows = await conn.execute_fetchall(
            f"SELECT * FROM segments WHERE plex_guid IN ({placeholders}) ORDER BY plex_guid, start_ms",
            plex_guids,
        )
        return [dict(r) for r in rows]


async def get_segments_for_guid_with_setting(
    plex_guid: str, setting_key: str, setting_default: str
) -> tuple[list[dict], str]:
    """Return segments for a guid and a setting value in a single connection.

    Avoids the cost of opening two aiosqlite connections sequentially when
    both pieces of data are needed together (e.g. segments + scan_labels).
    """
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT * FROM segments WHERE plex_guid=? ORDER BY start_ms", (plex_guid,)
        )
        row = await (
            await conn.execute("SELECT value FROM settings WHERE key=?", (setting_key,))
        ).fetchone()
        setting_val = row["value"] if row else setting_default
        return [dict(r) for r in rows], setting_val


async def get_segments_by_rating_key(rating_key: str) -> list[dict]:
    """Look up segments by scan_jobs.rating_key — fallback when session GUID differs from stored GUID."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            """SELECT s.* FROM segments s
               JOIN scan_jobs j ON j.plex_guid = s.plex_guid
               WHERE j.rating_key = ?
               ORDER BY s.start_ms""",
            (rating_key,),
        )
        return [dict(r) for r in rows]


async def count_segments_for_guid(plex_guid: str) -> int:
    """Return segment count for a title using SELECT COUNT — avoids loading full rows."""
    async with get_connection() as conn:
        row = await (
            await conn.execute("SELECT COUNT(*) FROM segments WHERE plex_guid=?", (plex_guid,))
        ).fetchone()
        return row[0] if row else 0


async def delete_segments_for_guid(plex_guid: str) -> int:
    """Delete all stored segments for a title and return deleted row count."""
    async with get_connection() as conn:
        cursor = await conn.execute("DELETE FROM segments WHERE plex_guid=?", (plex_guid,))
        await conn.commit()
        return cursor.rowcount


async def get_all_segments(limit: int = 200, offset: int = 0) -> list[dict]:
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT * FROM segments ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return [dict(r) for r in rows]


async def insert_segment(
    plex_guid: str,
    title: str,
    start_ms: int,
    end_ms: int,
    confidence: float = 0.0,
    thumbnail_path: str | None = None,
    labels: str = "",
    category: str = "nudity",
    severity: str = "high",
    action: str = "skip",
    channel: str = "both",
    source: str = "scanner",
) -> int:
    """Insert one segment and return its row id.

    Classification defaults describe scanner output so existing callers keep
    their behaviour; importers pass the values parsed from the source file.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO segments(plex_guid, title, start_ms, end_ms, confidence, thumbnail_path, "
            "labels, category, severity, action, channel, source, sources) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (plex_guid, title, start_ms, end_ms, confidence, thumbnail_path, labels,
             category, severity, action, channel, source, source),
        )
        await conn.commit()
        return cursor.lastrowid


async def insert_segments_bulk(plex_guid: str, title: str, segments: list[dict], source: str) -> int:
    """Insert many parsed segments in one transaction and return the row count.

    Used by the importers, where a single file routinely carries 100+ cues —
    one INSERT per cue would be a needless round trip each.
    """
    if not segments:
        return 0
    async with get_connection() as conn:
        await conn.executemany(
            "INSERT INTO segments(plex_guid, title, start_ms, end_ms, confidence, "
            "labels, category, severity, action, channel, language, source, sources) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    plex_guid,
                    title,
                    seg["start_ms"],
                    seg["end_ms"],
                    seg.get("confidence", 1.0),
                    seg.get("labels", ""),
                    seg.get("category", "other"),
                    seg.get("severity", "high"),
                    seg.get("action", "skip"),
                    seg.get("channel", "both"),
                    seg.get("language", ""),
                    source,
                    source,
                )
                for seg in segments
            ],
        )
        await conn.commit()
        return len(segments)


_SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3}


def _source_rank(source: str) -> int:
    """Prefer manual edits, then imported files, then generated data."""
    if source == "manual":
        return 5
    if source.startswith("videoskip:") or source in ("skp", "edl", "mcf", "paste"):
        return 4
    if source == "subtitles":
        return 3
    if source == "scanner":
        return 2
    return 1


def _merge_state(item: dict) -> dict:
    source = item.get("source") or "scanner"
    return {
        "ids": [item["id"]],
        "start_ms": int(item["start_ms"]),
        "end_ms": int(item["end_ms"]),
        "confidence": float(item.get("confidence") or 0),
        "labels": set(filter(None, (item.get("labels") or "").split(","))),
        "severity": item.get("severity") or "high",
        "source": source,
        "sources": set(filter(None, (item.get("sources") or source).split(","))),
        "thumbnail_path": item.get("thumbnail_path") or "",
        "title": item.get("title") or "",
        "category": item.get("category") or "other",
        "action": item.get("action") or "skip",
        "channel": item.get("channel") or "both",
        "language": item.get("language") or "",
    }


async def merge_title_segments(plex_guid: str, gap_ms: int = 250) -> int:
    """Merge overlapping segments sharing category/action/channel/language.

    Cross-category overlaps are deliberately kept: a viewer may filter violence
    but not nudity, so collapsing them would skip content they wanted to see.
    Returns the number of duplicate rows removed.
    """
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT * FROM segments WHERE plex_guid=? ORDER BY start_ms", (plex_guid,)
        )
    if len(rows) < 2:
        return 0

    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        item = dict(row)
        key = (
            item.get("category") or "other",
            item.get("action") or "skip",
            item.get("channel") or "both",
            item.get("language") or "",
        )
        groups.setdefault(key, []).append(item)

    merged: list[dict] = []
    for group in groups.values():
        group.sort(key=lambda r: (int(r["start_ms"]), int(r["end_ms"])))
        current: dict | None = None
        for item in group:
            if current is None:
                current = _merge_state(item)
                continue
            if int(item["start_ms"]) <= current["end_ms"] + gap_ms:
                current["ids"].append(item["id"])
                current["end_ms"] = max(current["end_ms"], int(item["end_ms"]))
                current["confidence"] = max(
                    current["confidence"], float(item.get("confidence") or 0)
                )
                current["labels"].update(
                    filter(None, (item.get("labels") or "").split(","))
                )
                current["sources"].update(
                    filter(
                        None,
                        (item.get("sources") or item.get("source") or "").split(","),
                    )
                )
                if _SEVERITY_RANK.get(
                    item.get("severity") or "high", 3
                ) > _SEVERITY_RANK.get(current["severity"], 3):
                    current["severity"] = item.get("severity") or "high"
                if not current["thumbnail_path"] and item.get("thumbnail_path"):
                    current["thumbnail_path"] = item["thumbnail_path"]
                if _source_rank(item.get("source") or "") > _source_rank(
                    current["source"]
                ):
                    current["source"] = item.get("source") or current["source"]
            else:
                merged.append(current)
                current = _merge_state(item)
        if current is not None:
            merged.append(current)

    removed = len(rows) - len(merged)
    if removed == 0:
        return 0

    async with get_connection() as conn:
        for row in merged:
            ids = row["ids"]
            primary = ids[0]
            sources = sorted(row["sources"], key=lambda s: (-_source_rank(s), s))
            await conn.execute(
                "UPDATE segments SET start_ms=?, end_ms=?, confidence=?, labels=?, "
                "severity=?, source=?, sources=?, thumbnail_path=? WHERE id=?",
                (
                    row["start_ms"],
                    row["end_ms"],
                    row["confidence"],
                    ",".join(sorted(row["labels"])),
                    row["severity"],
                    row["source"],
                    ",".join(sources),
                    row["thumbnail_path"] or None,
                    primary,
                ),
            )
            extras = ids[1:]
            if extras:
                placeholders = ",".join("?" * len(extras))
                await conn.execute(
                    f"DELETE FROM segments WHERE id IN ({placeholders})", extras
                )
        await conn.commit()
    return removed


async def delete_segment(segment_id: int) -> bool:
    async with get_connection() as conn:
        cursor = await conn.execute("DELETE FROM segments WHERE id=?", (segment_id,))
        await conn.commit()
        return cursor.rowcount > 0


async def get_segment_by_id(segment_id: int) -> dict | None:
    async with get_connection() as conn:
        row = await (await conn.execute("SELECT * FROM segments WHERE id=?", (segment_id,))).fetchone()
        return dict(row) if row else None


async def get_segments_grouped_by_title() -> list[dict]:
    """Return distinct (plex_guid, title) pairs that have segments."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT plex_guid, title, COUNT(*) as segment_count FROM segments GROUP BY plex_guid ORDER BY title"
        )
        return [dict(r) for r in rows]


# ── Analytics ─────────────────────────────────────────────────────────────────

async def get_segment_counts_by_label() -> list[dict]:
    """Return {label, count} for every label token found in segments.labels.

    Only counts segments that have a matching scan_jobs row so bar-chart totals
    are consistent with what get_segments_for_labels will actually return.
    Orphaned segments (scan_job deleted after scanning) are excluded from both.
    """
    async with get_connection() as conn:
        # JOIN with scan_jobs mirrors the filter used by get_segments_for_labels.
        rows = await conn.execute_fetchall(
            """
            SELECT s.labels FROM segments s
            JOIN scan_jobs j ON j.plex_guid = s.plex_guid
            WHERE s.labels IS NOT NULL AND s.labels != ''
            """
        )
    counts: dict[str, int] = {}
    for row in rows:
        for token in row["labels"].split(","):
            token = token.strip()
            if token:
                counts[token] = counts.get(token, 0) + 1
    return [{"label": k, "count": v} for k, v in sorted(counts.items(), key=lambda x: -x[1])]


async def get_segment_counts_by_rating_for_label(label: str) -> list[dict]:
    """Return {content_rating, count} for segments containing the given label."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            """
            SELECT j.content_rating, COUNT(*) as count
            FROM segments s
            JOIN scan_jobs j ON j.plex_guid = s.plex_guid
            WHERE (',' || s.labels || ',') LIKE ?
            GROUP BY j.content_rating
            ORDER BY count DESC
            """,
            (f"%,{label},%",),
        )
    return [{"content_rating": row["content_rating"] or "Unrated", "count": row["count"]} for row in rows]


async def get_segments_for_labels(
    labels: list[str],
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """Return segments (with scan_job metadata) that contain any of the given labels."""
    if not labels:
        return []
    async with get_connection() as conn:
        # Build OR conditions: one LIKE per label token.
        conditions = " OR ".join(["(',' || s.labels || ',') LIKE ?"] * len(labels))
        params = [f"%,{lbl},%" for lbl in labels] + [limit, offset]
        rows = await conn.execute_fetchall(
            f"""
            SELECT s.id, s.plex_guid, s.title, s.start_ms, s.end_ms,
                   s.confidence, s.labels, s.thumbnail_path,
                   j.content_rating, j.media_type, j.rating_key,
                   j.show_rating_key
            FROM segments s
            JOIN scan_jobs j ON j.plex_guid = s.plex_guid
            WHERE {conditions}
            ORDER BY s.confidence DESC
            LIMIT ? OFFSET ?
            """,
            params,
        )
        return [dict(r) for r in rows]


async def count_segments_for_labels(labels: list[str]) -> int:
    """Return total count of segments matching any of the given labels.

    Uses the same JOIN as get_segments_for_labels so the count reflects exactly
    what will be returned — orphaned segments (no matching scan_job) are excluded
    from both, preventing total/results mismatches.
    """
    if not labels:
        return 0
    async with get_connection() as conn:
        conditions = " OR ".join(["(',' || s.labels || ',') LIKE ?"] * len(labels))
        params = [f"%,{lbl},%" for lbl in labels]
        row = await (await conn.execute(
            f"SELECT COUNT(*) FROM segments s JOIN scan_jobs j ON j.plex_guid = s.plex_guid WHERE {conditions}",
            params,
        )).fetchone()
        return row[0] if row else 0


# ── Scan Jobs ─────────────────────────────────────────────────────────────────

async def upsert_scan_job(
    plex_guid: str,
    title: str,
    file_path: str,
    rating_key: str,
    library_id: str,
    library_title: str,
    content_rating: str = "",
    media_type: str = "movie",
    year: int | None = None,
    show_guid: str = "",
    show_rating_key: str = "",
    part_files: str = "",
    duration_ms: int = 0,
    imdb_id: str = "",
    tmdb_id: str = "",
    season: int | None = None,
    episode: int | None = None,
) -> None:
    async with get_connection() as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO scan_jobs"
            "(plex_guid, title, file_path, part_files, rating_key, library_id, library_title, content_rating, media_type, year, show_guid, show_rating_key, duration_ms, imdb_id, tmdb_id, season, episode) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (plex_guid, title, file_path, part_files, rating_key, library_id, library_title, content_rating, media_type, year, show_guid, show_rating_key, duration_ms, imdb_id, tmdb_id, season, episode),
        )
        await conn.commit()


async def get_existing_guids(guids: list[str]) -> set[str]:
    """Return the subset of guids that already exist as scan jobs (single query)."""
    if not guids:
        return set()
    placeholders = ",".join("?" * len(guids))
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            f"SELECT plex_guid FROM scan_jobs WHERE plex_guid IN ({placeholders})", guids
        )
        return {row["plex_guid"] for row in rows}


async def refresh_scan_job_metadata_batch(
    items: list[tuple[str, str, str, str, str, int | None, str, str, str, str, str, int | None, int | None, int]],
) -> None:
    """Update mutable Plex metadata for multiple existing scan jobs in one transaction.

    Each item is (plex_guid, title, file_path, rating_key, content_rating, year,
    show_guid, show_rating_key, part_files, imdb_id, tmdb_id, season, episode,
    duration_ms). Does not touch scan status or progress.
    """
    if not items:
        return
    async with get_connection() as conn:
        await conn.executemany(
            "UPDATE scan_jobs SET title=?, file_path=?, part_files=?, rating_key=?, content_rating=?, year=?,"
            # Backfill show_guid/show_rating_key from Plex for episodes synced before these columns existed.
            " show_guid=CASE WHEN ? != '' THEN ? ELSE show_guid END,"
            " show_rating_key=CASE WHEN ? != '' THEN ? ELSE show_rating_key END,"
            # External IDs are additive: never erase a good value because a later
            # Plex response omitted it.
            " imdb_id=CASE WHEN ? != '' THEN ? ELSE imdb_id END,"
            " tmdb_id=CASE WHEN ? != '' THEN ? ELSE tmdb_id END,"
            " season=COALESCE(?, season), episode=COALESCE(?, episode),"
            " duration_ms=CASE WHEN ? > 0 THEN ? ELSE duration_ms END"
            " WHERE plex_guid=?",
            [(title, file_path, part_files, rating_key, content_rating, year,
              show_guid, show_guid, show_rating_key, show_rating_key,
              imdb_id, imdb_id, tmdb_id, tmdb_id, season, episode,
              duration_ms, duration_ms, plex_guid)
             for plex_guid, title, file_path, rating_key, content_rating, year,
                 show_guid, show_rating_key, part_files, imdb_id, tmdb_id,
                 season, episode, duration_ms in items],
        )
        await conn.commit()


async def delete_scan_jobs_not_in(library_id: str, keep_guids: list[str]) -> int:
    """Delete scan jobs for a library whose plex_guid is not in keep_guids. Returns deleted count."""
    if not keep_guids:
        return 0
    placeholders = ",".join("?" * len(keep_guids))
    async with get_connection() as conn:
        cursor = await conn.execute(
            f"DELETE FROM scan_jobs WHERE library_id=? AND plex_guid NOT IN ({placeholders})",
            [library_id, *keep_guids],
        )
        await conn.commit()
        return cursor.rowcount


async def get_scan_jobs(status: str | None = None) -> list[dict]:
    async with get_connection() as conn:
        if status:
            rows = await conn.execute_fetchall(
                "SELECT * FROM scan_jobs WHERE status=? ORDER BY created_at DESC", (status,)
            )
        else:
            rows = await conn.execute_fetchall("SELECT * FROM scan_jobs ORDER BY created_at DESC")
        return [dict(r) for r in rows]


async def get_scan_job_by_guid(plex_guid: str) -> dict | None:
    async with get_connection() as conn:
        row = await (await conn.execute("SELECT * FROM scan_jobs WHERE plex_guid=?", (plex_guid,))).fetchone()
        return dict(row) if row else None


async def update_scan_job_status(
    plex_guid: str,
    status: str,
    progress: float = 0.0,
    error_msg: str | None = None,
) -> None:
    async with get_connection() as conn:
        if status == "scanning":
            await conn.execute(
                "UPDATE scan_jobs SET status=?, progress=?, started_at=COALESCE(started_at, CURRENT_TIMESTAMP) WHERE plex_guid=?",
                (status, progress, plex_guid),
            )
        elif status in ("done", "failed"):
            await conn.execute(
                "UPDATE scan_jobs SET status=?, progress=?, finished_at=CURRENT_TIMESTAMP, error_msg=? WHERE plex_guid=?",
                (status, progress, error_msg, plex_guid),
            )
        else:
            await conn.execute(
                "UPDATE scan_jobs SET status=?, progress=? WHERE plex_guid=?",
                (status, progress, plex_guid),
            )
        await conn.commit()


async def update_part_files(plex_guid: str, part_files: str) -> None:
    """Update the part_files JSON for an existing scan job without touching status or progress."""
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE scan_jobs SET part_files=? WHERE plex_guid=?",
            (part_files, plex_guid),
        )
        await conn.commit()


async def reset_scan_job(plex_guid: str) -> None:
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE scan_jobs SET status='pending', progress=0, started_at=NULL, finished_at=NULL, error_msg=NULL WHERE plex_guid=?",
            (plex_guid,),
        )
        await conn.commit()


async def set_force_scan(plex_guid: str, force: bool) -> None:
    """Set or unset the force_scan flag for a specific job."""
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE scan_jobs SET force_scan=? WHERE plex_guid=?",
            (1 if force else 0, plex_guid),
        )
        await conn.commit()


async def set_ignored(plex_guid: str, ignored: bool) -> None:
    """Set or unset the ignored flag for a specific job."""
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE scan_jobs SET ignored=? WHERE plex_guid=?",
            (1 if ignored else 0, plex_guid),
        )
        await conn.commit()


async def get_scan_jobs_by_guids(guids: list[str]) -> dict[str, dict]:
    """Return {plex_guid: job} for all provided guids in a single IN query."""
    if not guids:
        return {}
    placeholders = ",".join(["?"] * len(guids))
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            f"SELECT * FROM scan_jobs WHERE plex_guid IN ({placeholders})",
            guids,
        )
        return {row["plex_guid"]: dict(row) for row in rows}


async def get_scan_jobs_by_library(library_id: str) -> list[dict]:
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT * FROM scan_jobs WHERE library_id=? ORDER BY title", (library_id,)
        )
        return [dict(r) for r in rows]


async def get_strict_categories(min_level: int = 2) -> list[str]:
    """Return categories any user filters at `min_level` or stricter.

    Used to bias VideoSkip file selection toward the content the household
    actually cares about (e.g. pick a file that covers fear or gore rather than
    one that only covers nudity).
    """
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT DISTINCT category FROM user_category_prefs "
            "WHERE level >= ? AND category NOT IN ('commercial', 'dispensable')",
            (int(min_level),),
        )
        return [row["category"] for row in rows]


async def get_videoskip_coverage() -> dict:
    """Return segment coverage totals and a per-library breakdown.

    "without_segments" is the safe bulk-fetch target: a title that already has
    scanner segments is left alone, because layering a second source over the
    same ranges would duplicate them. "without_videoskip" is reported separately
    so the UI can show human-graded coverage without implying those titles are
    unfiltered.
    """
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            """
            SELECT
                COALESCE(j.library_id, '') AS library_id,
                COALESCE(NULLIF(j.library_title, ''), 'Unknown') AS library_title,
                COUNT(*) AS total,
                SUM(CASE WHEN EXISTS (
                    SELECT 1 FROM segments s WHERE s.plex_guid = j.plex_guid
                ) THEN 1 ELSE 0 END) AS with_segments,
                SUM(CASE WHEN EXISTS (
                    SELECT 1 FROM segments s
                    WHERE s.plex_guid = j.plex_guid
                      AND (s.source LIKE 'videoskip:%' OR s.sources LIKE '%videoskip:%')
                ) THEN 1 ELSE 0 END) AS with_videoskip
            FROM scan_jobs j
            WHERE COALESCE(j.ignored, 0) = 0
            GROUP BY j.library_id, j.library_title
            ORDER BY library_title COLLATE NOCASE
            """
        )

    totals = {
        "total": 0,
        "with_segments": 0,
        "with_videoskip": 0,
        "without_segments": 0,
        "without_videoskip": 0,
    }
    libraries: list[dict] = []
    for row in rows:
        count = int(row["total"] or 0)
        with_segments = int(row["with_segments"] or 0)
        with_videoskip = int(row["with_videoskip"] or 0)
        entry = {
            "library_id": row["library_id"],
            "library_title": row["library_title"],
            "total": count,
            "with_segments": with_segments,
            "with_videoskip": with_videoskip,
            "without_segments": count - with_segments,
            "without_videoskip": count - with_videoskip,
        }
        libraries.append(entry)
        for key in totals:
            totals[key] += entry[key]
    return {**totals, "libraries": libraries}


async def get_guids_missing_segments(
    limit: int = 25, library_id: str | None = None
) -> list[str]:
    """Return guids of non-ignored titles with no segments, newest first."""
    sql = """
        SELECT j.plex_guid
        FROM scan_jobs j
        WHERE COALESCE(j.ignored, 0) = 0
          AND NOT EXISTS (
              SELECT 1 FROM segments s WHERE s.plex_guid = j.plex_guid
          )
    """
    params: list = []
    if library_id:
        sql += " AND j.library_id = ?"
        params.append(library_id)
    sql += " ORDER BY j.created_at DESC LIMIT ?"
    params.append(max(1, int(limit)))

    async with get_connection() as conn:
        rows = await conn.execute_fetchall(sql, params)
        return [row["plex_guid"] for row in rows]


async def get_guids_without_videoskip(
    limit: int = 25,
    library_id: str | None = None,
    library_ids: list[str] | None = None,
) -> list[str]:
    """Return guids of non-ignored titles with no VideoSkip-sourced segments.

    Oldest titles come first so a sweep cannot starve the back of a large
    library. `library_ids` limits the sweep to the libraries the user enabled.
    """
    sql = """
        SELECT j.plex_guid
        FROM scan_jobs j
        WHERE COALESCE(j.ignored, 0) = 0
          AND NOT EXISTS (
              SELECT 1 FROM segments s
              WHERE s.plex_guid = j.plex_guid
                AND (s.source LIKE 'videoskip:%' OR s.sources LIKE '%videoskip:%')
          )
    """
    params: list = []
    if library_id:
        sql += " AND j.library_id = ?"
        params.append(library_id)
    elif library_ids:
        placeholders = ",".join("?" * len(library_ids))
        sql += f" AND j.library_id IN ({placeholders})"
        params.extend(library_ids)
    sql += " ORDER BY j.created_at ASC, j.id ASC LIMIT ?"
    params.append(max(1, int(limit)))

    async with get_connection() as conn:
        rows = await conn.execute_fetchall(sql, params)
        return [row["plex_guid"] for row in rows]


async def count_videoskip_segments_for_guid(plex_guid: str) -> int:
    """Return the number of VideoSkip-sourced segments stored for a title."""
    async with get_connection() as conn:
        row = await (await conn.execute(
            "SELECT COUNT(*) FROM segments WHERE plex_guid=? "
            "AND (source LIKE 'videoskip:%' OR sources LIKE '%videoskip:%')",
            (plex_guid,),
        )).fetchone()
        return int(row[0]) if row else 0


# ── VideoSkip auto-fetch queue ────────────────────────────────────────────────

async def enqueue_videoskip_job(
    plex_guid: str, title: str, max_attempts: int = 2, force: bool = False
) -> bool:
    """Queue a title for an automatic VideoSkip lookup.

    Terminal successes and no-match results are not retried unless `force` is
    set. Failed jobs are retried while their attempt count is below the limit.
    Returns True when the title is now queued.
    """
    async with get_connection() as conn:
        row = await (await conn.execute(
            "SELECT status, attempts FROM videoskip_queue WHERE plex_guid=?",
            (plex_guid,),
        )).fetchone()

        if row is None:
            await conn.execute(
                "INSERT INTO videoskip_queue(plex_guid, title) VALUES(?, ?)",
                (plex_guid, title),
            )
            await conn.commit()
            return True

        status = row["status"]
        attempts = int(row["attempts"] or 0)
        if status in ("queued", "running"):
            return False
        if not force:
            if status in ("done", "no_match", "skipped"):
                return False
            if status == "failed" and attempts >= max_attempts:
                return False

        await conn.execute(
            "UPDATE videoskip_queue SET status='queued', title=?, "
            "attempts=CASE WHEN ? THEN 0 ELSE attempts END, "
            "last_error='', updated_at=CURRENT_TIMESTAMP WHERE plex_guid=?",
            (title, 1 if force else 0, plex_guid),
        )
        await conn.commit()
        return True


async def claim_next_videoskip_job() -> dict | None:
    """Atomically move the oldest queued job to running and return it."""
    async with get_connection() as conn:
        row = await (await conn.execute(
            "SELECT * FROM videoskip_queue WHERE status='queued' "
            "ORDER BY created_at, id LIMIT 1"
        )).fetchone()
        if row is None:
            return None

        cursor = await conn.execute(
            "UPDATE videoskip_queue SET status='running', attempts=attempts+1, "
            "updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='queued'",
            (row["id"],),
        )
        await conn.commit()
        if cursor.rowcount != 1:
            return None

        job = dict(row)
        job["status"] = "running"
        job["attempts"] = int(row["attempts"] or 0) + 1
        return job


async def finish_videoskip_job(
    queue_id: int,
    status: str,
    *,
    skip_id: str = "",
    imported: int = 0,
    error: str = "",
) -> None:
    """Record the terminal state of one queue item."""
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE videoskip_queue SET status=?, skip_id=?, imported=?, "
            "last_error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, skip_id, imported, error[:500], queue_id),
        )
        await conn.commit()


async def get_videoskip_queue_status() -> dict:
    """Return queue counts, daily usage, current job and recent history."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT status, COUNT(*) AS cnt FROM videoskip_queue GROUP BY status"
        )
        counts = {row["status"]: int(row["cnt"]) for row in rows}

        daily = await (await conn.execute(
            "SELECT COUNT(*) FROM videoskip_queue "
            "WHERE status IN ('done','no_match','failed') "
            "AND updated_at >= datetime('now','-1 day')"
        )).fetchone()

        current = await (await conn.execute(
            "SELECT plex_guid, title FROM videoskip_queue "
            "WHERE status='running' ORDER BY updated_at DESC LIMIT 1"
        )).fetchone()

        recent_rows = await conn.execute_fetchall(
            "SELECT title, status, skip_id, imported, last_error, updated_at "
            "FROM videoskip_queue WHERE status NOT IN ('queued') "
            "ORDER BY updated_at DESC LIMIT 8"
        )

    auto_fetch = (await get_setting("videoskip_auto_fetch", "0")) == "1"
    paused = (await get_setting("videoskip_queue_paused", "0")) == "1"
    try:
        daily_limit = int(await get_setting("videoskip_auto_daily_limit", "50") or 50)
    except ValueError:
        daily_limit = 50

    return {
        "auto_fetch": auto_fetch,
        "paused": paused,
        "queued": counts.get("queued", 0),
        "running": counts.get("running", 0),
        "done": counts.get("done", 0),
        "no_match": counts.get("no_match", 0),
        "failed": counts.get("failed", 0),
        "skipped": counts.get("skipped", 0),
        "total": sum(counts.values()),
        "daily_used": int(daily[0]) if daily else 0,
        "daily_limit": daily_limit,
        "current": dict(current) if current else None,
        "recent": [dict(row) for row in recent_rows],
    }


async def clear_videoskip_queue() -> int:
    """Delete every queue row that is not currently running."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "DELETE FROM videoskip_queue WHERE status != 'running'"
        )
        await conn.commit()
        return cursor.rowcount


async def retry_failed_videoskip_jobs() -> int:
    """Requeue failed jobs with a fresh attempt count."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "UPDATE videoskip_queue SET status='queued', attempts=0, "
            "last_error='', updated_at=CURRENT_TIMESTAMP WHERE status='failed'"
        )
        await conn.commit()
        return cursor.rowcount


async def reset_stale_videoskip_jobs() -> int:
    """Requeue jobs left running by a previous process."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "UPDATE videoskip_queue SET status='queued', updated_at=CURRENT_TIMESTAMP "
            "WHERE status='running'"
        )
        await conn.commit()
        return cursor.rowcount


async def get_segment_counts_for_library(library_id: str) -> dict[str, int]:
    """Return {plex_guid: segment_count} for all titles in a library (single query)."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            """
            SELECT s.plex_guid, COUNT(*) as cnt
            FROM segments s
            JOIN scan_jobs j ON j.plex_guid = s.plex_guid
            WHERE j.library_id = ?
            GROUP BY s.plex_guid
            """,
            (library_id,),
        )
        return {row["plex_guid"]: row["cnt"] for row in rows}


# ── Segment Library Sharing ────────────────────────────────────────────────────

async def upsert_segment_library_entry(
    file_hash: str,
    file_name: str,
    file_size: int,
    duration_ms: int,
    segments_json: str,
    source_instance: str,
    confidence_level: str = "local",
) -> int:
    """Insert or update a segment library entry with source tracking."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            INSERT INTO segment_library_entries(
                file_hash, file_name, file_size, duration_ms, 
                segments_json, source_instance, confidence_level, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(file_hash, source_instance) 
            DO UPDATE SET 
                segments_json=excluded.segments_json,
                confidence_level=excluded.confidence_level,
                updated_at=CURRENT_TIMESTAMP
            """,
            (file_hash, file_name, file_size, duration_ms, segments_json, source_instance, confidence_level),
        )
        await conn.commit()
        return cursor.lastrowid


async def get_segment_library_entries_by_hashes(file_hashes: list[str]) -> list[dict]:
    """Get all library entries matching the given file hashes, grouped by hash."""
    if not file_hashes:
        return []
    
    placeholders = ",".join(["?"] * len(file_hashes))
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            f"SELECT * FROM segment_library_entries WHERE file_hash IN ({placeholders}) ORDER BY file_hash, created_at DESC",
            file_hashes,
        )
        return [dict(r) for r in rows]


async def get_segment_library_entries_by_hash(file_hash: str) -> list[dict]:
    """Get all sources for a single file hash."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT * FROM segment_library_entries WHERE file_hash=? ORDER BY created_at DESC",
            (file_hash,),
        )
        return [dict(r) for r in rows]


async def delete_segment_library_entry(file_hash: str, source_instance: str) -> bool:
    """Delete a specific entry (useful for removing outdated sources)."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "DELETE FROM segment_library_entries WHERE file_hash=? AND source_instance=?",
            (file_hash, source_instance),
        )
        await conn.commit()
        return cursor.rowcount > 0


async def get_sync_metadata() -> dict | None:
    """Get sync configuration for this instance."""
    async with get_connection() as conn:
        row = await (
            await conn.execute(
                "SELECT * FROM sync_metadata LIMIT 1"
            )
        ).fetchone()
        return dict(row) if row else None


async def upsert_sync_metadata(
    instance_name: str,
    github_repo: str | None = None,
    github_token: str | None = None,
    sync_enabled: bool = False,
    conflict_resolution: str = "consensus",
    verified_threshold: int = 2,
    timing_tolerance_ms: int = 2000,
) -> int:
    """Update or create sync metadata for this instance."""
    async with get_connection() as conn:
        # Delete existing if it exists (to update)
        await conn.execute("DELETE FROM sync_metadata")
        
        cursor = await conn.execute(
            """
            INSERT INTO sync_metadata(
                instance_name, github_repo, github_token, sync_enabled,
                conflict_resolution, verified_threshold, timing_tolerance_ms,
                last_sync_time
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                instance_name,
                github_repo,
                github_token,
                1 if sync_enabled else 0,
                conflict_resolution,
                verified_threshold,
                timing_tolerance_ms,
            ),
        )
        await conn.commit()
        return cursor.lastrowid


async def update_sync_last_time() -> None:
    """Update last_sync_time to current timestamp."""
    async with get_connection() as conn:
        await conn.execute("UPDATE sync_metadata SET last_sync_time=CURRENT_TIMESTAMP")
        await conn.commit()


# ── Background Job Management ──────────────────────────────────────────────────

async def create_bg_job(job_type: str) -> int:
    """Create a new background job. Returns job_id."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO bg_jobs(job_type, status, started_at) VALUES(?, ?, CURRENT_TIMESTAMP)",
            (job_type, 'running'),
        )
        await conn.commit()
        return cursor.lastrowid


async def get_bg_job(job_id: int) -> dict | None:
    """Get background job status."""
    async with get_connection() as conn:
        row = await (await conn.execute("SELECT * FROM bg_jobs WHERE id = ?", (job_id,))).fetchone()
        return dict(row) if row else None


async def update_bg_job(job_id: int, status: str = None, progress: int = None, error: str = None, result: str = None) -> None:
    """Update background job status."""
    async with get_connection() as conn:
        updates = []
        params = []
        
        if status is not None:
            updates.append("status = ?")
            params.append(status)
            if status == 'completed':
                updates.append("completed_at = CURRENT_TIMESTAMP")
        
        if progress is not None:
            updates.append("progress_percent = ?")
            params.append(progress)
        
        if error is not None:
            updates.append("error_message = ?")
            params.append(error)
        
        if result is not None:
            updates.append("result_data = ?")
            params.append(result)
        
        if updates:
            params.append(job_id)
            query = f"UPDATE bg_jobs SET {', '.join(updates)} WHERE id = ?"
            await conn.execute(query, params)
            await conn.commit()


async def get_local_library_for_sync() -> list[dict]:
    """Return all locally-scanned titles with their segments, grouped by plex_guid.

    Uses a single JOIN query instead of one query per title to avoid N+1 DB round-trips.
    Returns: [{plex_guid, title, file_path, segments_count, segments: [{...}]}]
    """
    async with get_connection() as conn:
        # Fetch all segment rows for done jobs in a single pass.
        seg_rows = await conn.execute_fetchall(
            """
            SELECT s.*, j.file_path, j.title AS job_title
            FROM segments s
            JOIN scan_jobs j ON j.plex_guid = s.plex_guid
            WHERE j.status = 'done' AND j.file_path IS NOT NULL
            ORDER BY j.title, s.start_ms
            """
        )
        # Also fetch done jobs with no segments so they still appear in the result.
        job_rows = await conn.execute_fetchall(
            """
            SELECT plex_guid, title, file_path
            FROM scan_jobs
            WHERE status = 'done' AND file_path IS NOT NULL
            ORDER BY title
            """
        )

    # Build {plex_guid: {title, file_path, segments: []}} in Python — O(1) per row.
    job_map: dict[str, dict] = {
        r["plex_guid"]: {
            "plex_guid": r["plex_guid"],
            "title": r["title"],
            "file_path": r["file_path"],
            "segments": [],
        }
        for r in job_rows
    }
    for row in seg_rows:
        guid = row["plex_guid"]
        if guid in job_map:
            job_map[guid]["segments"].append(dict(row))

    result = []
    for item in job_map.values():
        item["segments_count"] = len(item["segments"])
        result.append(item)
    return result


# ── Plex Markers ───────────────────────────────────────────────────────────────

async def upsert_plex_markers(plex_guid: str, rating_key: str, markers: list[dict]) -> None:
    """Replace all stored markers for a title with the current Plex state.

    Delete-then-insert keyed on rating_key. The module-level lock ensures concurrent
    Sync All calls never interleave their DELETE+INSERT pairs in the same connection.
    """
    async with _marker_upsert_lock:
        async with get_connection() as conn:
            await conn.execute("DELETE FROM plex_markers WHERE rating_key=?", (rating_key,))
            for m in markers:
                await conn.execute(
                    """
                    INSERT INTO plex_markers(plex_guid, rating_key, marker_type, start_ms, end_ms, plex_marker_id, final, synced_at)
                    VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                    """,
                    (plex_guid, rating_key, m["marker_type"], m["start_ms"], m["end_ms"], m["plex_marker_id"], 1 if m.get("final") else 0),
                )
            await conn.commit()


async def get_plex_markers_for_guid(plex_guid: str) -> list[dict]:
    """Return all markers for a title ordered by start_ms."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT * FROM plex_markers WHERE plex_guid=? ORDER BY start_ms", (plex_guid,)
        )
        return [dict(r) for r in rows]


async def get_plex_marker(marker_id: int) -> dict | None:
    """Return a single marker row including rating_key and plex_marker_id needed for Plex write-back."""
    async with get_connection() as conn:
        row = await (await conn.execute("SELECT * FROM plex_markers WHERE id=?", (marker_id,))).fetchone()
        return dict(row) if row else None


async def update_plex_marker_timestamps(marker_id: int, start_ms: int, end_ms: int) -> bool:
    """Update start_ms and end_ms for a stored marker. Returns True if a row was updated."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "UPDATE plex_markers SET start_ms=?, end_ms=? WHERE id=?",
            (start_ms, end_ms, marker_id),
        )
        await conn.commit()
        return cursor.rowcount > 0


async def delete_plex_marker(marker_id: int) -> bool:
    """Delete a single marker row. Returns True if a row was deleted."""
    async with get_connection() as conn:
        cursor = await conn.execute("DELETE FROM plex_markers WHERE id=?", (marker_id,))
        await conn.commit()
        return cursor.rowcount > 0


async def get_plex_marker_counts_for_library(library_id: str) -> dict[str, int]:
    """Return {plex_guid: marker_count} for all titles in a library (single query)."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            """
            SELECT m.plex_guid, COUNT(*) as cnt
            FROM plex_markers m
            JOIN scan_jobs j ON j.plex_guid = m.plex_guid
            WHERE j.library_id = ?
            GROUP BY m.plex_guid
            """,
            (library_id,),
        )
        return {row["plex_guid"]: row["cnt"] for row in rows}


async def get_plex_marker_counts_for_guids(guids: list[str]) -> dict[str, int]:
    """Return {plex_guid: marker_count} for an arbitrary list of guids (single query)."""
    if not guids:
        return {}
    placeholders = ",".join("?" * len(guids))
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            f"SELECT plex_guid, COUNT(*) as cnt FROM plex_markers WHERE plex_guid IN ({placeholders}) GROUP BY plex_guid",
            guids,
        )
        return {row["plex_guid"]: row["cnt"] for row in rows}


async def get_plex_marker_counts_for_rating_keys(rating_keys: list[str]) -> dict[str, int]:
    """Return {rating_key: marker_count} for an arbitrary list of rating keys (single query)."""
    if not rating_keys:
        return {}
    placeholders = ",".join("?" * len(rating_keys))
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            f"SELECT rating_key, COUNT(*) as cnt FROM plex_markers WHERE rating_key IN ({placeholders}) GROUP BY rating_key",
            rating_keys,
        )
        return {row["rating_key"]: row["cnt"] for row in rows}


async def insert_plex_marker(plex_guid: str, rating_key: str, marker_type: str, start_ms: int, end_ms: int) -> dict:
    """Insert a new manually-created marker (no Plex write). Returns the new row."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            INSERT INTO plex_markers(plex_guid, rating_key, marker_type, start_ms, end_ms, plex_marker_id, final, synced_at)
            VALUES(?,?,?,?,?,NULL,0,CURRENT_TIMESTAMP)
            """,
            (plex_guid, rating_key, marker_type, start_ms, end_ms),
        )
        await conn.commit()
        row = await (await conn.execute("SELECT * FROM plex_markers WHERE id=?", (cursor.lastrowid,))).fetchone()
        return dict(row)


async def get_plex_markers_for_rating_key(rating_key: str) -> list[dict]:
    """Return all markers for a Plex item by rating_key, ordered by start_ms."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT * FROM plex_markers WHERE rating_key=? ORDER BY start_ms", (rating_key,)
        )
        return [dict(r) for r in rows]


# ── Plex title cache ───────────────────────────────────────────────────────────

async def get_plex_title_cache(library_id: str) -> list[dict]:
    """Return cached title list for a library, joined with current marker counts."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            """
            SELECT c.*, COALESCE(m.cnt, 0) AS marker_count
            FROM plex_title_cache c
            LEFT JOIN (
                SELECT rating_key, COUNT(*) AS cnt
                FROM plex_markers
                GROUP BY rating_key
            ) m ON m.rating_key = c.rating_key
            WHERE c.library_id = ?
            ORDER BY c.title
            """,
            (library_id,),
        )
        return [dict(r) for r in rows]


async def upsert_plex_title_cache(library_id: str, items: list[dict]) -> None:
    """Replace the title cache for a library with a fresh Plex snapshot."""
    async with get_connection() as conn:
        await conn.execute("DELETE FROM plex_title_cache WHERE library_id=?", (library_id,))
        for item in items:
            await conn.execute(
                """
                INSERT INTO plex_title_cache
                    (library_id, rating_key, plex_guid, title, media_type,
                     show_guid, show_title, show_rating_key, content_rating, year)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    library_id,
                    item["rating_key"],
                    item["plex_guid"],
                    item["title"],
                    item["media_type"],
                    item.get("show_guid", ""),
                    item.get("show_title", ""),
                    item.get("show_rating_key", ""),
                    item.get("content_rating", ""),
                    item.get("year"),
                ),
            )
        await conn.commit()


async def plex_title_cache_exists(library_id: str) -> bool:
    """Return True if the cache has any rows for this library."""
    async with get_connection() as conn:
        row = await (await conn.execute(
            "SELECT 1 FROM plex_title_cache WHERE library_id=? LIMIT 1", (library_id,)
        )).fetchone()
        return row is not None

