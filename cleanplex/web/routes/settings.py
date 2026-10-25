from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import json

from ...logger import get_logger
from ... import database as db
import cleanplex.plex_client as plex_mod
from ... import scanner as scan_mod
from .segments import _invalidate_scan_labels_cache

logger = get_logger(__name__)
router = APIRouter(prefix="/api/settings", tags=["settings"])

# Sent out instead of the Plex token; echoing it back on PUT means "unchanged".
REDACTED_TOKEN = "__stored__"

DETECTOR_LABELS = [
    # EXPOSED categories
    "FEMALE_GENITALIA_EXPOSED",
    "FEMALE_BREAST_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "MALE_BREAST_EXPOSED",
    "ANUS_EXPOSED",
    "BUTTOCKS_EXPOSED",
    "FEET_EXPOSED",
    "BELLY_EXPOSED",
    "ARMPITS_EXPOSED",
    # COVERED categories
    "FEMALE_GENITALIA_COVERED",
    "FEMALE_BREAST_COVERED",
    "MALE_BREAST_COVERED",
    "ANUS_COVERED",
    "BUTTOCKS_COVERED",
    "FEET_COVERED",
    "BELLY_COVERED",
    "ARMPITS_COVERED",
    # FACE categories
    "FACE_FEMALE",
    "FACE_MALE",
]


class SettingsPayload(BaseModel):
    plex_url: str | None = None
    plex_token: str | None = None
    poll_interval: str | None = None
    confidence_threshold: str | None = None
    skip_buffer_ms: str | None = None
    pre_buffer_ms: str | None = None
    post_buffer_ms: str | None = None
    scan_step_ms: str | None = None
    scan_workers: str | None = None
    nudenet_model: str | None = None
    nudenet_model_path: str | None = None
    segment_gap_ms: str | None = None
    segment_min_hits: str | None = None
    scan_window_start: str | None = None
    scan_window_end: str | None = None
    log_level: str | None = None
    excluded_library_ids: str | None = None
    scan_ratings: str | None = None
    scan_labels: str | None = None
    profanity_wordlist: str | None = None
    videoskip_auto_fetch: str | None = None
    videoskip_auto_daily_limit: str | None = None
    videoskip_auto_delay_ms: str | None = None
    videoskip_auto_sweep_minutes: str | None = None
    videoskip_auto_max_attempts: str | None = None
    videoskip_auto_libraries: str | None = None
    videoskip_auto_respect_scan_window: str | None = None


class ValidateModelPathPayload(BaseModel):
    nudenet_model: str = "320n"
    nudenet_model_path: str = ""


@router.get("")
async def get_settings():
    """Return all settings for the UI, without the Plex token.

    Every dashboard/library page fetches this endpoint; handing the raw Plex admin
    token to any LAN peer (default deployment has no auth) was a full takeover
    path. The UI only needs to know that a token is stored.
    """
    settings = await db.get_all_settings()
    if settings.get("plex_token"):
        settings["plex_token"] = REDACTED_TOKEN
    return settings


@router.get("/plex-server-id")
async def get_plex_server_id():
    """Return the Plex server machine identifier for constructing web deep links."""
    try:
        client = plex_mod.get_client()
        machine_id = await client.get_machine_identifier()
        return {"machine_identifier": machine_id}
    except RuntimeError:
        return {"machine_identifier": ""}
    except Exception as exc:
        logger.warning("Failed to get machine identifier: %s", exc)
        return {"machine_identifier": ""}


@router.get("/plex-diagnostics")
async def plex_diagnostics():
    """Return server identity and a session snapshot for troubleshooting."""
    try:
        client = plex_mod.get_client()
    except RuntimeError:
        return {"configured": False, "server": None, "session_count": 0, "sessions": []}

    info = await client.get_server_info()
    sessions = await client.get_active_sessions()
    return {
        "configured": True,
        "server": info,
        "session_count": len(sessions),
        "sessions": [
            {
                "title": s.full_title,
                "user": s.user,
                "client": s.client_title,
                "media_type": s.media_type,
                "position_ms": s.position_ms,
                "controllable": s.is_controllable,
            }
            for s in sessions
        ],
        "last_session_error": client.last_session_error,
    }


def _require_time_setting(value: str | None, name: str) -> None:
    """Reject malformed HH:MM values before they are persisted.

    Config.load tolerates bad values at runtime, but accepting them means the
    UI looks saved while the scanner silently runs on defaults.
    """
    if value is None:
        return
    try:
        hours, minutes = value.strip().split(":")
        if 0 <= int(hours) < 24 and 0 <= int(minutes) < 60:
            return
    except (AttributeError, ValueError):
        pass
    raise HTTPException(status_code=422, detail=f"{name} must be HH:MM (00:00–23:59)")


def _require_json_list_setting(value: str | None, name: str) -> None:
    """Reject malformed JSON-array settings before they are persisted.

    These values are parsed by the scanner, watcher and eligibility checks; an
    invalid array used to crash the pool at startup and keep it dead on restart.
    """
    if value is None:
        return
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail=f"{name} must be a JSON array.")
    if not isinstance(parsed, list):
        raise HTTPException(status_code=422, detail=f"{name} must be a JSON array.")


_LIST_SETTINGS = (
    "excluded_library_ids",
    "scan_ratings",
    "scan_labels",
    "videoskip_auto_libraries",
    "profanity_wordlist",
)


@router.put("")
async def update_settings(payload: SettingsPayload):
    data = {k: v for k, v in payload.model_dump().items() if v is not None}

    # The GET endpoint redacts the token; an unchanged form echoes the sentinel.
    if data.get("plex_token") == REDACTED_TOKEN:
        data.pop("plex_token", None)

    _require_time_setting(data.get("scan_window_start"), "scan_window_start")
    _require_time_setting(data.get("scan_window_end"), "scan_window_end")
    for key in _LIST_SETTINGS:
        _require_json_list_setting(data.get(key), key)
    
    # Check if scan_workers is being changed
    scan_workers_changed = False
    if "scan_workers" in data:
        current_workers = await db.get_setting("scan_workers", "2")
        if data["scan_workers"] != current_workers:
            scan_workers_changed = True
            logger.info("Scan workers changing from %s to %s", current_workers, data["scan_workers"])

    old_auto = await db.get_setting("videoskip_auto_fetch", "0")
    old_auto_libraries = await db.get_setting("videoskip_auto_libraries", "[]")

    await db.update_settings(data)

    auto_turned_on = data.get("videoskip_auto_fetch") == "1" and old_auto != "1"
    libraries_changed = (
        "videoskip_auto_libraries" in data
        and data["videoskip_auto_libraries"] != old_auto_libraries
    )
    if auto_turned_on:
        requeued = await db.requeue_videoskip_skipped()
        if requeued:
            logger.info("Requeued %d VideoSkip row(s) after enabling auto-fetch", requeued)
    elif libraries_changed:
        requeued = await db.requeue_videoskip_skipped("Library is excluded from auto-fetch")
        if requeued:
            logger.info("Requeued %d VideoSkip row(s) after library policy change", requeued)

    # Invalidate scan_labels cache so the next segment expand reflects the new value.
    if "scan_labels" in data:
        _invalidate_scan_labels_cache()

    # Reinitialise client if connection details changed
    if "plex_url" in data or "plex_token" in data:
        settings = await db.get_all_settings()
        url = settings.get("plex_url", "")
        token = settings.get("plex_token", "")
        if url and token:
            plex_mod.init_client(url, token)
    
    # Restart scanner pool if worker count changed
    if scan_workers_changed:
        await scan_mod.request_scanner_restart()
    
    return {"ok": True}


@router.post("/test-connection")
async def test_connection():
    settings = await db.get_all_settings()
    url = settings.get("plex_url", "")
    token = settings.get("plex_token", "")
    if not url or not token:
        return {"ok": False, "message": "Plex URL and token are required"}
    client = plex_mod.PlexClient(url, token)
    try:
        ok, message = await client.test_connection()
    finally:
        # One AsyncClient per PlexClient, always closed (§5.3) — this route used
        # to leak a client and its socket pool on every "test" press.
        await client.close()
    return {"ok": ok, "message": message}


@router.get("/detector-labels")
async def get_detector_labels():
    return {"labels": DETECTOR_LABELS}


@router.post("/validate-model-path")
async def validate_model_path(payload: ValidateModelPathPayload):
    model_name = (payload.nudenet_model or "320n").strip().lower()

    # 320n is bundled with nudenet package; no download required.
    if not model_name.startswith("640"):
        return {
            "ok": True,
            "message": "320n uses bundled model; no custom file path required.",
        }

    try:
        # Scanner auto-downloads 640m model into app storage on first use.
        from ... import scanner as scanner_mod

        # Hold the shared download lock and keep the blocking download off the
        # event loop; calling the sync helper here froze skips/session polling and
        # could race a concurrent scan's download into a corrupt model file.
        path = await scanner_mod._ensure_640m_model_async()
        if not path:
            return {
                "ok": False,
                "message": "Could not download 640m model automatically. Check internet access and try again.",
            }
        return {
            "ok": True,
            "message": "640m model is ready in local app storage.",
        }
    except Exception as exc:
        logger.warning("NudeNet auto-download validation failed: %s", exc)
        return {
            "ok": False,
            "message": f"Model could not be prepared: {exc}",
        }
