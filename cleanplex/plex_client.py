"""Plex Media Server API wrapper."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlsplit

# TTL for per-show metadata cache; 10 minutes is long enough to cover a full
# library-titles request without stale data causing visible issues.
_SHOW_ART_CACHE_TTL_S = 600
# Upper bound on entries; a 20k-episode library must not grow this without limit.
_SHOW_ART_CACHE_MAX = 512

import httpx
from plexapi.server import PlexServer
from plexapi.exceptions import PlexApiException

from .logger import get_logger

logger = get_logger(__name__)


def _is_private_address(address: str) -> bool:
    """True when a direct control attempt is worth trying (LAN, not loopback)."""
    if not address:
        return False
    if address in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return False
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return True  # hostname; bounded by the 2s per-attempt timeout
    return ip.is_private and not ip.is_loopback


def _as_list(value) -> list:
    """Normalize a plexapi attribute that may be a list, a method or None.

    plexapi exposes some collections as properties on one version and as
    zero-argument methods on another (for example ``MediaPart.audioStreams``).
    Iterating a bound method raised ``'method' object is not iterable``, which
    made healthy Plex sessions disappear from the dashboard.
    """
    if value is None:
        return []
    if callable(value):
        try:
            value = value()
        except Exception:
            return []
    try:
        return list(value)
    except TypeError:
        return []


@dataclass
class ActiveSession:
    session_key: str
    user: str
    title: str
    full_title: str
    plex_guid: str
    rating_key: str
    media_type: str       # "movie" or "episode"
    position_ms: int
    duration_ms: int
    client_identifier: str
    client_title: str
    is_controllable: bool
    thumb: str = ""       # relative Plex thumb URL
    client_address: str = ""
    client_port: int = 32500
    library_section_id: str = ""
    # Player volume 0-100, or None when the client does not report one. Captured so
    # a mute can restore the viewer's own level rather than guessing 100.
    volume: int | None = None
    # ISO code of the audio track being played, empty when it cannot be determined.
    # Language-specific segments (profanity mutes) only apply to their own track.
    audio_language: str = ""
    # Human-readable reason a session cannot be controlled (e.g. Plex Web has no
    # remote-control endpoint). Empty when control is supported or not yet known.
    control_note: str = ""
    # Client product/device names and advertised protocol capabilities, for the
    # Dashboard's client-capability display and diagnostics.
    client_product: str = ""
    capabilities: str = ""


@dataclass
class LibrarySection:
    section_id: str
    title: str
    section_type: str     # "movie" or "show"


@dataclass(frozen=True)
class PlexClientInfo:
    """A Companion-capable client reported by Plex's /clients registry."""

    machine_identifier: str
    title: str
    product: str
    device: str
    capabilities: tuple[str, ...]

    @property
    def controllable(self) -> bool:
        return "playback" in self.capabilities


@dataclass
class MediaItem:
    rating_key: str
    plex_guid: str
    title: str
    year: int | None
    thumb: str
    file_path: str
    library_id: str
    library_title: str
    media_type: str       # "movie" or "episode"
    content_rating: str = ""   # e.g. "PG-13", "R", "TV-MA"
    show_guid: str = ""        # grandparentGuid for episodes; empty for movies
    show_rating_key: str = ""  # grandparentRatingKey for episodes; used to build poster URLs from DB
    duration_ms: int = 0       # runtime, used to sanity-check imported skip files
    # External IDs from Plex's GUID list, used to match remote skip files. Plex
    # supplies these for movies reliably; episodes often only expose the series
    # GUID, so season/episode remain the fallback matching key.
    imdb_id: str = ""
    tmdb_id: str = ""
    season: int | None = None
    episode: int | None = None
    # Ordered list of all part file paths for multi-part movies (e.g. CD1/CD2).
    # file_path holds parts[0]; part_files holds the full list.
    # Empty for single-file titles.
    part_files: list = field(default_factory=list)


def _extract_external_ids(raw_guids: list[str]) -> tuple[str, str]:
    """Return `(imdb_id, tmdb_id)` from Plex's `scheme://id` GUID strings.

    Plex mixes the new agent (`imdb://tt123`) with legacy agent GUIDs
    (`com.plexapp.agents.imdb://tt123?lang=en`). Either form is accepted; an ID
    that cannot be parsed is simply left empty.
    """
    imdb_id = ""
    tmdb_id = ""
    for raw in raw_guids or []:
        value = (raw or "").strip()
        if not value:
            continue
        if "imdb://" in value:
            imdb_id = value.split("imdb://", 1)[1].split("?", 1)[0].strip("/")
        elif "themoviedb://" in value:
            tmdb_id = value.split("themoviedb://", 1)[1].split("?", 1)[0].strip("/")
        elif value.startswith("tmdb://"):
            tmdb_id = value[len("tmdb://"):].split("?", 1)[0]
    return imdb_id, tmdb_id


@dataclass
class PlexUser:
    username: str
    thumb: str = ""
    is_home_user: bool = True


class PlexClient:
    def __init__(self, url: str, token: str) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self._server: PlexServer | None = None
        self._http = httpx.AsyncClient(timeout=10)
        # Most recent /status/sessions failure, surfaced by the API so the UI
        # can distinguish "Plex unreachable" from "nothing is playing".
        self.last_session_error = ""
        # Most recent player-command failure (proxy/direct), surfaced by
        # /api/settings/plex-diagnostics so "controllable" that is not is visible.
        self.last_control_error = ""
        # Transport-specific errors, kept separate: the direct fallback used to
        # overwrite a meaningful proxy error with "All connection attempts failed".
        self.last_proxy_error = ""
        self.last_direct_error = ""
        # Monotonic command ids: two commands in the same second must not share an
        # id or some clients silently ignore the second one (200, no action).
        self._command_id = int(time.time() * 1000)
        # {rating_key: (monotonic_timestamp, (show_guid, show_title, show_thumb, show_rating_key, season_rating_key))}
        self._show_art_cache: dict[str, tuple[float, tuple[str, str, str, str, str]]] = {}
        # {client_identifier: {"transport": "proxy"|"direct", "port": int, "variant": int}}
        # How each client last accepted a player command. Populated by
        # load_client_profiles() at startup and updated whenever one is learned.
        self._client_profiles: dict[str, dict] = {}
        # machineIdentifier → (monotonic_ts, controllable, error) from a remote-control
        # probe. Plex's /clients registry only lists players that advertise over GDM;
        # mobile apps often play without appearing there, so absence is probed rather
        # than treated as "cannot be controlled".
        self._control_probe_cache: dict[str, tuple[float, bool, str]] = {}

    def _get_server(self) -> PlexServer:
        if self._server is None:
            self._server = PlexServer(self.url, self.token)
        return self._server

    def invalidate(self) -> None:
        self._server = None

    # ── Connectivity ──────────────────────────────────────────────────────────

    async def test_connection(self) -> tuple[bool, str]:
        try:
            srv = await asyncio.to_thread(self._get_server)
            return True, srv.friendlyName
        except Exception as exc:
            return False, str(exc)

    async def get_server_info(self) -> dict:
        """Return friendly name, version and machine id for diagnostics."""
        try:
            srv = await asyncio.to_thread(self._get_server)
            return {
                "friendly_name": srv.friendlyName,
                "version": getattr(srv, "version", ""),
                "machine_identifier": srv.machineIdentifier,
            }
        except Exception as exc:
            return {"error": str(exc)}

    async def get_machine_identifier(self) -> str:
        """Return the Plex server machine identifier, used to build web deep links."""
        try:
            srv = await asyncio.to_thread(self._get_server)
            return str(srv.machineIdentifier)
        except Exception as exc:
            logger.debug("Failed to get machine identifier: %s", exc)
            return ""

    # ── Sessions ──────────────────────────────────────────────────────────────

    async def get_active_sessions(self) -> list[ActiveSession]:
        try:
            srv = await asyncio.to_thread(self._get_server)
            sessions = await asyncio.to_thread(srv.sessions)
        except Exception as exc:
            self.last_session_error = str(exc)
            logger.warning("Failed to fetch sessions: %s", exc)
            return []

        self.last_session_error = ""
        # Plex Companion clients advertise their capabilities at /clients. Plex Web
        # (a browser player) does not, and silently rejects remote commands; asking
        # the registry is the only reliable way to tell the UI the truth.
        clients = await self.get_clients() if sessions else []
        registry = {c.machine_identifier: c for c in clients} if clients is not None else None
        result = []
        parse_failures = 0
        first_parse_error = ""
        for s in sessions:
            try:
                # Determine full title
                if hasattr(s, "grandparentTitle") and s.grandparentTitle:
                    full_title = f"{s.grandparentTitle} – {s.parentTitle} – {s.title}"
                else:
                    full_title = s.title

                # Find the first player. plexapi exposes some of these as lists on
                # one version and as methods on another; iterating a bound method
                # raised "'method' object is not iterable" and dropped the session.
                players = _as_list(getattr(s, "players", None))
                player = players[0] if players else None
                client_id = player.machineIdentifier if player else ""
                client_title = player.title if player else "Unknown"
                client_address = getattr(player, "address", "") if player else ""
                client_port_raw = getattr(player, "port", 32500) if player else 32500
                try:
                    client_port = int(client_port_raw or 32500)
                except Exception:
                    client_port = 32500

                # Plex's /clients registry lists players that advertise over GDM.
                # Mobile apps frequently play without appearing there, and Plex Web
                # never does but does reject commands. Absence is therefore probed
                # with a harmless command rather than assumed to be uncontrollable.
                info = registry.get(client_id) if registry is not None else None
                client_product = info.product if info else ""
                capabilities = ",".join(info.capabilities) if info else ""
                control_note = ""
                if info is not None:
                    controllable = info.controllable
                    if not controllable:
                        control_note = "This client does not advertise playback control."
                else:
                    controllable, probe_error = await self._probe_client_control(
                        client_id, client_address, client_port
                    )
                    if not controllable:
                        control_note = (
                            "Cleanplex could not control this client"
                            + (f" ({probe_error})" if probe_error else "")
                            + ". Use a Plex app that accepts remote commands; if this is a "
                            "Plex app, enable 'Advertise as player' in its settings."
                        )

                volume_raw = getattr(player, "volume", None) if player else None
                try:
                    volume = int(volume_raw) if volume_raw is not None else None
                except Exception:
                    volume = None

                # Resolve file path
                file_path = ""
                audio_language = ""
                media = _as_list(getattr(s, "media", None))
                parts = _as_list(getattr(media[0], "parts", None)) if media else []
                if parts:
                    part = parts[0]
                    file_path = part.file or ""
                    # MediaPart.audioStreams is a method in plexapi; call it when
                    # needed. The selected stream is what the viewer actually
                    # hears; fall back to the first audio stream when Plex marks
                    # none selected.
                    streams = _as_list(getattr(part, "audioStreams", None))
                    selected = next((st for st in streams if getattr(st, "selected", False)), None)
                    chosen = selected or (streams[0] if streams else None)
                    if chosen is not None:
                        audio_language = (
                            getattr(chosen, "languageCode", "") or getattr(chosen, "language", "") or ""
                        ).lower()

                # GUID — prefer the first one that looks useful
                guid = ""
                guids = _as_list(getattr(s, "guids", None))
                if guids:
                    guid = guids[0].id
                elif hasattr(s, "guid"):
                    guid = s.guid or ""

                section_id = str(s.librarySectionID) if hasattr(s, "librarySectionID") else ""

                usernames = _as_list(getattr(s, "usernames", None))

                result.append(
                    ActiveSession(
                        session_key=str(s.sessionKey),
                        user=str(usernames[0]) if usernames else "Unknown",
                        title=s.title,
                        full_title=full_title,
                        plex_guid=guid,
                        rating_key=str(s.ratingKey),
                        media_type=s.type,
                        position_ms=int(s.viewOffset or 0),
                        duration_ms=int(s.duration or 0),
                        client_identifier=client_id,
                        client_title=client_title,
                        is_controllable=controllable,
                        client_address=client_address,
                        client_port=client_port,
                        thumb=s.thumb or "",
                        library_section_id=section_id,
                        volume=volume,
                        audio_language=audio_language,
                        control_note=control_note,
                        client_product=client_product,
                        capabilities=capabilities,
                    )
                )
            except Exception as exc:
                parse_failures += 1
                if not first_parse_error:
                    first_parse_error = str(exc)
                logger.warning("Error parsing session: %s", exc)

        if parse_failures and not result:
            self.last_session_error = (
                f"Plex reported {len(sessions)} session(s) but none could be parsed: "
                f"{first_parse_error}"
            )
        return result

    async def get_clients(self) -> list[PlexClientInfo] | None:
        """Return Companion-capable clients from Plex's /clients registry.

        Returns None when the registry cannot be read, so callers can distinguish
        "no controllable clients" from "capability lookup failed".
        """
        try:
            response = await asyncio.wait_for(
                self._http.get(
                    f"{self.url}/clients",
                    params={"X-Plex-Token": self.token},
                    headers={"Accept": "application/json"},
                ),
                timeout=3,
            )
            response.raise_for_status()
            payload = response.json()
            devices = (payload.get("MediaContainer") or {}).get("Device") or []
            clients: list[PlexClientInfo] = []
            for device in devices:
                machine_id = str(device.get("machineIdentifier") or "")
                if not machine_id:
                    continue
                capabilities = tuple(sorted({
                    part.strip()
                    for part in str(device.get("protocolCapabilities") or "").split(",")
                    if part.strip()
                }))
                clients.append(
                    PlexClientInfo(
                        machine_identifier=machine_id,
                        title=str(device.get("title") or device.get("name") or "Unknown"),
                        product=str(device.get("product") or ""),
                        device=str(device.get("device") or device.get("model") or ""),
                        capabilities=capabilities,
                    )
                )
            return clients
        except Exception as exc:
            logger.info("Could not read Plex client capabilities: %s", exc)
            return None

    async def _remote_controllable_clients(self) -> dict[str, set[str]] | None:
        """Return machineIdentifier → protocol capabilities, or None if unknown."""
        clients = await self.get_clients()
        if clients is None:
            return None
        return {client.machine_identifier: set(client.capabilities) for client in clients}

    async def _probe_client_control(
        self, client_identifier: str, address: str, port: int
    ) -> tuple[bool, str]:
        """Ask a client to accept a harmless player command, caching the answer.

        Plex Web answers with 400; a Companion-capable app answers 200. This is the
        only honest way to classify clients that never appear in /clients. Direct
        attempts are only made for private addresses and are tightly bounded — the
        old code spent 80 seconds retrying unreachable phones.
        """
        if not client_identifier:
            return False, "the session did not expose a player identifier"

        now = time.monotonic()
        cached = self._control_probe_cache.get(client_identifier)
        if cached and now - cached[0] < 300:
            return cached[1], cached[2]

        ping = f"/player/playback/ping?commandID={self._next_command_id()}"
        controllable = await self._try_proxy(ping, client_identifier)
        if (
            not controllable
            and address
            and _is_private_address(address)
        ):
            for variant in range(2):
                if await self._try_direct(ping, client_identifier, address, port, variant):
                    controllable = True
                    break

        proxy_error = self.last_proxy_error
        direct_error = self.last_direct_error
        if controllable:
            error = ""
        elif proxy_error:
            error = f"server proxy: {proxy_error}"
        elif direct_error:
            error = f"direct: {direct_error}"
        else:
            error = "no response"

        self._control_probe_cache[client_identifier] = (now, controllable, error[:300])
        logger.info(
            "Control probe for %s: %s%s",
            client_identifier,
            "ok" if controllable else "not controllable",
            "" if controllable else f" ({error[:180]})",
        )
        return controllable, error

    def _record_control_result(self, client_identifier: str, success: bool) -> None:
        self._control_probe_cache[client_identifier] = (
            time.monotonic(),
            success,
            "" if success else (self.last_control_error or "command rejected"),
        )

    # ── Player commands ───────────────────────────────────────────────────────

    async def load_client_profiles(self) -> None:
        """Restore learned per-client transports so a restart does not re-probe."""
        from . import database as db

        try:
            raw = await db.get_setting("client_seek_profiles", "{}")
            loaded = json.loads(raw or "{}")
            if isinstance(loaded, dict):
                self._client_profiles = {k: v for k, v in loaded.items() if isinstance(v, dict)}
        except Exception as exc:
            logger.debug("Could not load client seek profiles: %s", exc)

    async def _persist_profiles(self) -> None:
        """Write profiles back to settings.

        Awaited rather than fired off as a task: a detached write can outlive the
        loop it was created on. Profiles change rarely — only when a client's
        working transport changes — so the cost sits outside the hot path.
        """
        from . import database as db

        try:
            await db.set_setting("client_seek_profiles", json.dumps(self._client_profiles))
        except Exception as exc:
            # Persistence is best-effort; the in-memory cache still serves this run.
            logger.debug("Could not persist client seek profiles: %s", exc)

    async def _remember_profile(self, client_identifier: str, profile: dict) -> None:
        if self._client_profiles.get(client_identifier) == profile:
            return
        self._client_profiles[client_identifier] = profile
        await self._persist_profiles()

    async def _forget_profile(self, client_identifier: str) -> None:
        if self._client_profiles.pop(client_identifier, None) is not None:
            await self._persist_profiles()

    # Direct-control fallbacks, ordered cheapest-first. A client that needs one of
    # the later variants needs it every time, so the winning combination is cached
    # per client rather than rediscovered on each command — see _send_command.
    _FALLBACK_PORTS = (32500, 3005)

    def _next_command_id(self) -> int:
        self._command_id += 1
        return self._command_id

    def _direct_variants(self, base: str, client_identifier: str) -> list[tuple[str, dict]]:
        """Return (url, headers) pairs to try when the server proxy will not relay."""
        full_headers = {
            "X-Plex-Target-Client-Identifier": client_identifier,
            "X-Plex-Client-Identifier": "cleanplex-server",
            "X-Plex-Product": "Cleanplex",
            "X-Plex-Device-Name": "Cleanplex",
            "X-Plex-Platform": "Windows",
            "X-Plex-Provides": "controller",
        }
        return [
            (f"{base}&X-Plex-Token={self.token}", {"X-Plex-Target-Client-Identifier": client_identifier}),
            (base, {"X-Plex-Token": self.token, "X-Plex-Target-Client-Identifier": client_identifier}),
            (f"{base}&X-Plex-Token={self.token}", full_headers),
            (base, {"X-Plex-Token": self.token, **full_headers}),
        ]

    async def _try_proxy(self, path: str, client_identifier: str) -> bool:
        """Relay a player command through the server. Works for most modern clients."""
        try:
            srv = await asyncio.to_thread(self._get_server)
            headers = {
                "X-Plex-Target-Client-Identifier": client_identifier,
                # Identify the controller as well: some clients only action proxied
                # commands that carry a controller identifier.
                "X-Plex-Client-Identifier": "cleanplex-server",
                "X-Plex-Provides": "controller",
            }
            await asyncio.to_thread(srv.query, path, headers=headers)
            self.last_proxy_error = ""
            self.last_control_error = ""
            return True
        except Exception as exc:
            self.last_proxy_error = str(exc)[:400]
            self.last_control_error = self.last_proxy_error
            logger.warning("Proxy %s failed for %s: %s", path.split("?")[0], client_identifier, exc)
            return False

    async def _try_direct(
        self, path: str, client_identifier: str, address: str, port: int, variant: int
    ) -> bool:
        """Send one specific direct-control attempt. Returns True on a 2xx response."""
        base = f"http://{address}:{port}{path}"
        variants = self._direct_variants(base, client_identifier)
        if variant >= len(variants):
            return False
        url, headers = variants[variant]
        try:
            resp = await self._http.get(url, headers=headers, timeout=2.0)
            if resp.status_code < 300:
                self.last_direct_error = ""
                self.last_control_error = ""
                return True
            self.last_direct_error = (
                f"HTTP {resp.status_code} from {address}:{port}: {resp.text[:300]}"
            )
            self.last_control_error = self.last_direct_error
            logger.warning(
                "Direct command HTTP %d for client %s at %s:%d (variant=%d, body=%s)",
                resp.status_code, client_identifier, address, port, variant + 1, resp.text[:500],
            )
        except Exception as exc:
            self.last_direct_error = str(exc)[:400]
            self.last_control_error = self.last_direct_error
            logger.warning(
                "Direct command failed for client %s at %s:%d (variant=%d): %s",
                client_identifier, address, port, variant + 1, exc,
            )
        return False

    async def _send_command(
        self, path: str, client_identifier: str, client_address: str = "", client_port: int = 32500
    ) -> bool:
        """Send a player command, preferring the transport that last worked for this client.

        Without the cache every command on a proxy-averse client re-runs the full
        search — up to 12 requests — before landing, which is exactly when latency
        matters most.
        """
        profile = self._client_profiles.get(client_identifier)

        # A client that just rejected a command (probe or seek) should not cost
        # another 10+ seconds on every poll; back off briefly.
        cached = self._control_probe_cache.get(client_identifier)
        if cached and not cached[1] and time.monotonic() - cached[0] < 120:
            self.last_control_error = cached[2]
            return False

        if profile is not None:
            if profile.get("transport") == "proxy":
                if await self._try_proxy(path, client_identifier):
                    return True
            elif client_address:
                if await self._try_direct(
                    path, client_identifier, client_address,
                    int(profile.get("port", client_port)), int(profile.get("variant", 0)),
                ):
                    return True
            # The learned path stopped working (client restarted, port changed):
            # fall through to a full search and re-learn.
            logger.info("Cached transport failed for %s — re-probing", client_identifier)
            await self._forget_profile(client_identifier)

        if await self._try_proxy(path, client_identifier):
            await self._remember_profile(client_identifier, {"transport": "proxy"})
            return True

        if not client_address:
            logger.warning("No client_address available for direct fallback (client=%s)", client_identifier)
            return False

        ports: list[int] = []
        for port in (client_port, *self._FALLBACK_PORTS):
            if port and port not in ports:
                ports.append(port)

        for port in ports:
            for variant in range(len(self._direct_variants("http://x", client_identifier))):
                if await self._try_direct(path, client_identifier, client_address, port, variant):
                    logger.info(
                        "Client %s reachable directly at %s:%d (variant=%d)",
                        client_identifier, client_address, port, variant + 1,
                    )
                    await self._remember_profile(
                        client_identifier,
                        {"transport": "direct", "port": port, "variant": variant},
                    )
                    return True

        return False

    async def seek(self, client_identifier: str, offset_ms: int, client_address: str = "", client_port: int = 32500) -> bool:
        """Seek the given client to offset_ms. Returns True if a command was accepted."""
        path = (
            f"/player/playback/seekTo?offset={offset_ms}&type=video"
            f"&commandID={self._next_command_id()}"
        )
        try:
            success = await asyncio.wait_for(
                self._send_command(path, client_identifier, client_address, client_port),
                timeout=15,
            )
        except asyncio.TimeoutError:
            self.last_control_error = "player command timed out"
            success = False
        self._record_control_result(client_identifier, success)
        if success:
            logger.info("Seeked client %s to %dms", client_identifier, offset_ms)
        return success

    async def set_volume(self, client_identifier: str, level: int, client_address: str = "", client_port: int = 32500) -> bool:
        """Set the client's playback volume (0-100). Used to mute rather than skip."""
        level = max(0, min(100, int(level)))
        path = (
            f"/player/playback/setParameters?volume={level}&type=video"
            f"&commandID={self._next_command_id()}"
        )
        success = await self._send_command(path, client_identifier, client_address, client_port)
        self._record_control_result(client_identifier, success)
        if success:
            logger.info("Set volume on client %s to %d", client_identifier, level)
        return success

    # ── Library ───────────────────────────────────────────────────────────────

    async def get_library_sections(self) -> list[LibrarySection]:
        try:
            srv = await asyncio.to_thread(self._get_server)
            sections = await asyncio.to_thread(srv.library.sections)
            return [
                LibrarySection(
                    section_id=str(s.key),
                    title=s.title,
                    section_type=s.type,
                )
                for s in sections
                if s.type in ("movie", "show")
            ]
        except Exception as exc:
            logger.warning("Failed to fetch library sections: %s", exc)
            return []

    async def get_library_items(self, section_id: str) -> list[MediaItem]:
        try:
            srv = await asyncio.to_thread(self._get_server)
            section = await asyncio.to_thread(srv.library.sectionByID, int(section_id))
        except Exception as exc:
            logger.warning("Failed to fetch library section %s: %s", section_id, exc)
            return []

        try:
            if section.type == "show":
                # One bulk call for all episodes — avoids N per-show API calls.
                raw_items = await asyncio.to_thread(lambda: section.search(libtype="episode"))
            else:
                raw_items = await asyncio.to_thread(section.all)
        except Exception as exc:
            logger.warning("Failed to fetch items for section %s: %s", section_id, exc)
            return []

        result = []
        for item in raw_items:
            try:
                media_item = self._media_item_from_plex(item, section_id, section.title)
                if media_item:
                    result.append(media_item)
            except Exception as exc:
                logger.debug("Error parsing library item: %s", exc)

        return result

    async def get_library_item(self, rating_key: str) -> MediaItem | None:
        """Fetch one Plex item by rating key, for the webhook fast path.

        Returns None when the item genuinely does not exist. Raises RuntimeError
        when Plex could not be reached or rejected the request, so callers can
        distinguish "not found" from "lookup failed" in their diagnostics.
        """
        try:
            srv = await asyncio.to_thread(self._get_server)
            item = await asyncio.to_thread(srv.fetchItem, int(rating_key))
            section_id = str(getattr(item, "librarySectionID", "") or "")
            section_title = ""
            if section_id:
                try:
                    section = await asyncio.to_thread(
                        srv.library.sectionByID, int(section_id)
                    )
                    section_title = section.title
                except Exception:
                    pass
            return self._media_item_from_plex(item, section_id, section_title)
        except Exception as exc:
            # A stale token, a Plex 5xx and a network timeout used to be reported
            # to the webhook as "item not found", hiding real failures.
            logger.warning("Could not fetch Plex item %s: %s", rating_key, exc)
            raise RuntimeError(f"Plex lookup failed for {rating_key}: {exc}") from exc

    def _media_item_from_plex(self, item: Any, library_id: str, library_title: str) -> MediaItem | None:
        try:
            # For multi-version items, try each media[] entry until we find one
            # whose primary part file exists on disk. This prevents scan jobs
            # pointing to stale/missing files when a version was removed.
            file_path = ""
            chosen_media = None
            for media in (item.media or []):
                if media.parts and media.parts[0].file:
                    file_path = media.parts[0].file
                    chosen_media = media
                    break

            # Collect all part file paths from the chosen media version.
            # Multi-part movies (CD1/CD2) have multiple parts[] under one media[].
            part_files: list[str] = []
            if chosen_media and chosen_media.parts:
                part_files = [p.file for p in chosen_media.parts if p.file]

            raw_guids = [g.id for g in (getattr(item, "guids", None) or []) if getattr(g, "id", "")]
            if not raw_guids and getattr(item, "guid", ""):
                raw_guids = [item.guid]
            guid = raw_guids[0] if raw_guids else ""
            imdb_id, tmdb_id = _extract_external_ids(raw_guids)

            title = item.title
            # For episodes, include full show/season/episode context
            if hasattr(item, "grandparentTitle") and item.grandparentTitle:
                title = f"{item.grandparentTitle} – {item.parentTitle} – {item.title}"

            year = getattr(item, "year", None)

            show_guid = getattr(item, "grandparentGuid", "") or ""
            show_rating_key = str(getattr(item, "grandparentRatingKey", "") or "")
            season = getattr(item, "parentIndex", None)
            episode = getattr(item, "index", None)

            return MediaItem(
                rating_key=str(item.ratingKey),
                plex_guid=guid,
                title=title,
                year=year,
                thumb=item.thumb or "",
                file_path=file_path,
                library_id=library_id,
                library_title=library_title,
                media_type=item.type,
                content_rating=getattr(item, "contentRating", "") or "",
                show_guid=show_guid,
                show_rating_key=show_rating_key,
                part_files=part_files,
                duration_ms=int(getattr(item, "duration", 0) or 0),
                imdb_id=imdb_id,
                tmdb_id=tmdb_id,
                season=int(season) if season is not None else None,
                episode=int(episode) if episode is not None else None,
            )
        except Exception:
            return None

    # ── Users ─────────────────────────────────────────────────────────────────

    async def get_all_users(self) -> list[PlexUser]:
        try:
            srv = await asyncio.to_thread(self._get_server)
            # Home users / managed users
            users: list[PlexUser] = []
            try:
                home_users = await asyncio.to_thread(srv.myPlexAccount().users)
                for u in home_users:
                    users.append(PlexUser(username=u.username or u.title, thumb=u.thumb or ""))
            except Exception:
                pass
            # Also add the owner
            try:
                account = await asyncio.to_thread(srv.myPlexAccount)
                users.insert(0, PlexUser(username=account.username, thumb=account.thumb or "", is_home_user=False))
            except Exception:
                pass
            return users
        except Exception as exc:
            logger.warning("Failed to fetch users: %s", exc)
            return []

    # ── Thumbnail proxy URL ───────────────────────────────────────────────────

    def thumb_url(self, thumb_path: str) -> str:
        """Return a local proxy URL for a Plex thumbnail, never the token.

        Handing out a token-bearing Plex URL leaked the admin credential to every
        browser that loaded /api/sessions; the proxy attaches the token server-side
        and validates the path (see fetch_image).
        """
        if not thumb_path:
            return ""
        return f"/api/plex-image?path={quote(thumb_path, safe='')}"

    async def get_episode_show_art(self, rating_key: str) -> tuple[str, str, str, str, str]:
        """Return (show_guid, show_title, show_thumb_path, show_rating_key, season_rating_key) for an episode rating key.

        Results are cached per rating_key with a TTL of _SHOW_ART_CACHE_TTL_S seconds
        to avoid redundant Plex API calls when listing large TV libraries.
        """
        now = time.monotonic()
        cached = self._show_art_cache.get(rating_key)
        if cached and now - cached[0] < _SHOW_ART_CACHE_TTL_S:
            return cached[1]

        try:
            srv = await asyncio.to_thread(self._get_server)
            item = await asyncio.to_thread(srv.fetchItem, int(rating_key))
            show_guid = getattr(item, "grandparentGuid", "") or ""
            show_title = getattr(item, "grandparentTitle", "") or ""
            show_thumb = getattr(item, "grandparentThumb", "") or ""
            show_rating_key = str(getattr(item, "grandparentRatingKey", "") or "")
            season_rating_key = str(getattr(item, "parentRatingKey", "") or "")
            result = (show_guid, show_title, show_thumb, show_rating_key, season_rating_key)
            self._show_art_cache[rating_key] = (now, result)
            # The cache is process-lifetime; prune expired entries (and, in the
            # worst case, the oldest) so a large TV library cannot grow it forever.
            if len(self._show_art_cache) > _SHOW_ART_CACHE_MAX:
                expired = [
                    key for key, (ts, _) in self._show_art_cache.items()
                    if now - ts >= _SHOW_ART_CACHE_TTL_S
                ]
                for key in expired:
                    self._show_art_cache.pop(key, None)
                while len(self._show_art_cache) > _SHOW_ART_CACHE_MAX:
                    oldest = min(self._show_art_cache, key=lambda k: self._show_art_cache[k][0])
                    self._show_art_cache.pop(oldest, None)
            return result
        except Exception as exc:
            logger.debug("Failed to resolve show art for rating_key %s: %s", rating_key, exc)
            return "", "", "", "", ""

    async def fetch_image(self, image_path: str) -> tuple[bytes, str]:
        """Fetch an image from Plex and return (bytes, content_type).

        `image_path` originates from a query parameter, so it is untrusted. A
        value like "@attacker.com/x" becomes URL userinfo and would send the Plex
        token (and any auth) to another host, so only relative paths that resolve
        to the configured Plex origin are accepted.
        """
        if not image_path:
            return b"", ""

        parsed = urlsplit(image_path)
        if parsed.scheme or parsed.netloc or parsed.username or parsed.password:
            logger.warning("Rejected absolute image path: %r", image_path[:120])
            return b"", ""

        path = image_path if image_path.startswith("/") else f"/{image_path}"
        url = f"{self.url}{path}"
        # The final URL must still point at the configured Plex origin.
        if urlsplit(url).netloc != urlsplit(self.url).netloc:
            logger.warning("Rejected off-origin image path: %r", image_path[:120])
            return b"", ""
        if "X-Plex-Token=" not in url:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}X-Plex-Token={self.token}"

        resp = await self._http.get(url)
        if resp.status_code >= 400:
            return b"", ""

        return resp.content, resp.headers.get("content-type", "image/jpeg")

    # ── Cleanplex metadata block ──────────────────────────────────────────────

    def _strip_cleanplex_block(self, summary: str) -> str:
        pattern = r"\n*\[\[CLEANPLEX\]\].*?\[\[/CLEANPLEX\]\]\n*"
        return re.sub(pattern, "\n", summary or "", flags=re.S).strip()

    def _build_cleanplex_block(self, status: str, segment_count: int, last_scan: str | None = None) -> str:
        stamp = last_scan or datetime.now().strftime("%Y-%m-%d %H:%M")
        return (
            "[[CLEANPLEX]]\n"
            "Cleanplex Scan\n"
            f"Status: {status}\n"
            f"Segments: {segment_count}\n"
            f"Last Scan: {stamp}\n"
            "[[/CLEANPLEX]]"
        )

    async def update_cleanplex_summary(
        self,
        rating_key: str,
        status: str,
        segment_count: int,
        last_scan: str | None = None,
    ) -> bool:
        """Insert/update a marker-based Cleanplex block in Plex summary metadata."""
        try:
            srv = await asyncio.to_thread(self._get_server)
            item = await asyncio.to_thread(srv.fetchItem, int(rating_key))
            current_summary = getattr(item, "summary", "") or ""

            base_summary = self._strip_cleanplex_block(current_summary)
            cleanplex_block = self._build_cleanplex_block(status, segment_count, last_scan)
            new_summary = f"{base_summary}\n\n{cleanplex_block}".strip() if base_summary else cleanplex_block

            try:
                await asyncio.to_thread(item.editSummary, new_summary)
            except Exception:
                await asyncio.to_thread(item.edit, summary=new_summary)

            logger.info("Updated Plex summary metadata for rating_key=%s", rating_key)
            return True
        except Exception as exc:
            logger.warning("Failed to update Plex summary metadata for rating_key=%s: %s", rating_key, exc)
            return False

    async def get_markers(self, rating_key: str) -> list[dict]:
        """Return intro/credits markers for a Plex item as a list of dicts.

        Each dict contains: plex_marker_id, marker_type, start_ms, end_ms, final.
        Returns empty list if the item has no markers or markers attribute is absent.
        """
        try:
            srv = await asyncio.to_thread(self._get_server)
            item = await asyncio.to_thread(srv.fetchItem, int(rating_key))
            raw_markers = await asyncio.to_thread(lambda: getattr(item, "markers", []))
            result = []
            for m in raw_markers:
                result.append({
                    "plex_marker_id": getattr(m, "id", None),
                    "marker_type": getattr(m, "type", "unknown"),
                    "start_ms": int(getattr(m, "start", 0)),
                    "end_ms": int(getattr(m, "end", 0)),
                    "final": bool(getattr(m, "final", False)),
                })
            return result
        except Exception as exc:
            logger.error("get_markers failed for rating_key=%s: %s", rating_key, exc)
            raise

    async def update_marker(self, rating_key: str, plex_marker_id: int, start_ms: int, end_ms: int) -> None:
        """Write updated marker timestamps back to the Plex server.

        Raises PermissionError if Plex rejects due to Plex Pass restriction.
        Raises RuntimeError for other Plex API failures.
        """
        try:
            srv = await asyncio.to_thread(self._get_server)
            item = await asyncio.to_thread(srv.fetchItem, int(rating_key))
            raw_markers = await asyncio.to_thread(lambda: getattr(item, "markers", []))
            target = next((m for m in raw_markers if getattr(m, "id", None) == plex_marker_id), None)
            if target is None:
                raise RuntimeError(f"Marker {plex_marker_id} not found on rating_key={rating_key}")
            await asyncio.to_thread(target.edit, startTimeOffset=start_ms, endTimeOffset=end_ms)
            logger.info("Updated Plex marker %s on rating_key=%s: %d–%d ms", plex_marker_id, rating_key, start_ms, end_ms)
        except Exception as exc:
            msg = str(exc).lower()
            if "unauthorized" in msg or "403" in msg or "plex pass" in msg or "subscription" in msg:
                raise PermissionError(f"Plex Pass required to edit markers: {exc}") from exc
            raise RuntimeError(f"Plex marker update failed: {exc}") from exc

    async def create_marker(self, rating_key: str, marker_type: str, start_ms: int, end_ms: int) -> None:
        """Create a new intro/credits marker on a Plex item via the undocumented markers API.

        Requires Plex Pass. Raises PermissionError if subscription check fails.
        """
        params = {
            "type": marker_type,
            "startTimeOffset": start_ms,
            "endTimeOffset": end_ms,
            "X-Plex-Token": self.token,
        }
        resp = await self._http.post(f"{self.url}/library/metadata/{rating_key}/markers", params=params)
        if resp.status_code in (401, 403):
            raise PermissionError(f"Plex Pass required to create markers (HTTP {resp.status_code})")
        if resp.status_code >= 400:
            raise RuntimeError(f"Plex marker create failed: HTTP {resp.status_code} — {resp.text[:200]}")
        logger.info("Created Plex marker type=%s on rating_key=%s: %d–%d ms", marker_type, rating_key, start_ms, end_ms)

    async def close(self) -> None:
        await self._http.aclose()


# Module-level singleton (set by main.py after config loads)
_client: PlexClient | None = None


def get_client() -> PlexClient:
    if _client is None:
        raise RuntimeError("PlexClient not initialised. Call init_client() first.")
    return _client


def init_client(url: str, token: str) -> PlexClient:
    """Create (or replace) the module-level PlexClient singleton.

    The previous AsyncClient is closed before replacement so open connections
    are not leaked on settings changes or reconnects.
    """
    global _client
    if _client is not None:
        # Schedule close on the running event loop without blocking the caller.
        try:
            loop = asyncio.get_event_loop()
            if not loop.is_closed():
                loop.create_task(_client.close())
        except RuntimeError:
            pass  # no running loop — process is tearing down
    _client = PlexClient(url, token)
    return _client
