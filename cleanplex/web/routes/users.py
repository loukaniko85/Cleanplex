from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ...logger import get_logger
import cleanplex.plex_client as plex_mod
from ... import database as db
from ...importers._common import CATEGORIES

logger = get_logger(__name__)
router = APIRouter(prefix="/api/users", tags=["users"])


class UserFilterUpdate(BaseModel):
    enabled: bool


@router.get("")
async def get_users():
    """Return all Plex users merged with their filter settings."""
    # Get users from Plex if available
    plex_users: list[dict] = []
    try:
        client = plex_mod.get_client()
        users = await client.get_all_users()
        plex_users = [{"username": u.username, "thumb": u.thumb} for u in users]
    except RuntimeError:
        pass

    # Get DB filter settings
    filters = {f["plex_username"]: f["enabled"] for f in await db.get_all_user_filters()}

    # Merge: if username not in DB, default enabled=True
    result = []
    seen = set()
    for u in plex_users:
        name = u["username"]
        seen.add(name)
        result.append({
            "username": name,
            "thumb": u.get("thumb", ""),
            "enabled": bool(filters.get(name, 1)),
        })

    # Also include any DB entries not returned by Plex
    for name, enabled in filters.items():
        if name not in seen:
            result.append({"username": name, "thumb": "", "enabled": bool(enabled)})

    return {"users": result}


@router.put("/{username}")
async def update_user_filter(username: str, payload: UserFilterUpdate):
    await db.upsert_user_filter(username, payload.enabled)
    return {"ok": True}


class CategoryPrefUpdate(BaseModel):
    """One category's strictness (0-3) and optional action override."""
    level: int = Field(0, ge=0, le=3)
    action: str = ""


@router.get("/{username}/categories")
async def get_user_categories(username: str):
    """Return a user's per-category filtering levels, plus which categories have segments.

    Categories with no segments are still listed so the UI can show the full set,
    but flagged so users are not tuning controls that cannot fire.
    """
    prefs = await db.get_user_category_prefs(username)
    populated = {c["category"] for c in await db.get_segment_categories()}

    return {
        "username": username,
        # An empty prefs map means "filter everything", which is the pre-category
        # default; the UI renders that rather than blank controls.
        "uses_defaults": not prefs,
        "categories": [
            {
                "category": name,
                "level": prefs.get(name, {}).get("level", 3 if not prefs else 0),
                "action": prefs.get(name, {}).get("action", ""),
                "has_segments": name in populated,
            }
            for name in CATEGORIES
        ],
    }


@router.put("/{username}/categories/{category}")
async def update_user_category(username: str, category: str, payload: CategoryPrefUpdate):
    if category not in CATEGORIES:
        raise HTTPException(status_code=422, detail=f"Unknown category: {category}")
    if payload.action and payload.action not in ("skip", "mute"):
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported action: {payload.action}. Expected 'skip' or 'mute'.",
        )
    await db.upsert_user_category_pref(username, category, payload.level, payload.action)
    return {"ok": True}


@router.delete("/{username}/categories")
async def reset_user_categories(username: str):
    """Clear a user's category preferences, reverting them to the defaults."""
    removed = await db.delete_user_category_prefs(username)
    return {"ok": True, "removed": removed}


# ClearPlay/VidAngel-style starter profiles. Levels follow the threshold model
# (level + severity rank > 3 fires); language mutes instead of skipping.
FILTER_PRESETS: dict[str, dict[str, dict]] = {
    "kids": {
        "nudity": {"level": 3, "action": "skip"},
        "sex": {"level": 3, "action": "skip"},
        "violence": {"level": 3, "action": "skip"},
        "language": {"level": 3, "action": "mute"},
        "drugs": {"level": 3, "action": "skip"},
        "fear": {"level": 2, "action": "skip"},
        "discrimination": {"level": 3, "action": "skip"},
        "commercial": {"level": 1, "action": "skip"},
        "dispensable": {"level": 1, "action": "skip"},
        "other": {"level": 3, "action": "skip"},
    },
    "teens": {
        "nudity": {"level": 3, "action": "skip"},
        "sex": {"level": 3, "action": "skip"},
        "violence": {"level": 2, "action": "skip"},
        "language": {"level": 2, "action": "mute"},
        "drugs": {"level": 2, "action": "skip"},
        "fear": {"level": 1, "action": "skip"},
        "discrimination": {"level": 2, "action": "skip"},
        "commercial": {"level": 1, "action": "skip"},
        "dispensable": {"level": 1, "action": "skip"},
        "other": {"level": 2, "action": "skip"},
    },
    "adults": {
        "nudity": {"level": 2, "action": "skip"},
        "sex": {"level": 2, "action": "skip"},
        "violence": {"level": 1, "action": "skip"},
        "language": {"level": 1, "action": "mute"},
        "drugs": {"level": 0, "action": ""},
        "fear": {"level": 0, "action": ""},
        "discrimination": {"level": 1, "action": "skip"},
        "commercial": {"level": 1, "action": "skip"},
        "dispensable": {"level": 0, "action": ""},
        "other": {"level": 1, "action": "skip"},
    },
}


@router.put("/{username}/preset/{preset}")
async def apply_user_preset(username: str, preset: str):
    """Apply a starter filtering profile to a user (kids / teens / adults).

    Commercial filters ship households with sensible profiles; Cleanplex only
    had per-category sliders, which forces every user to invent one from scratch.
    """
    profile = FILTER_PRESETS.get(preset)
    if profile is None:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown preset {preset!r}. Expected one of {', '.join(FILTER_PRESETS)}.",
        )
    await db.delete_user_category_prefs(username)
    for category, pref in profile.items():
        await db.upsert_user_category_pref(
            username, category, int(pref["level"]), str(pref["action"])
        )
    logger.info("Applied '%s' filter preset to user %s", preset, username)
    return {"ok": True, "preset": preset, "categories": len(profile)}
