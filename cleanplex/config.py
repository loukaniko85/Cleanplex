"""Runtime configuration backed by the SQLite settings table."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import time

from . import database as db


@dataclass
class Config:
    plex_url: str = ""
    plex_token: str = ""
    poll_interval: int = 5
    confidence_threshold: float = 0.6
    skip_buffer_ms: int = 3000
    pre_buffer_ms: int = 3000
    post_buffer_ms: int = 3000
    scan_step_ms: int = 5000
    scan_workers: int = 2
    nudenet_model: str = "320n"
    nudenet_model_path: str = ""
    segment_gap_ms: int = 12000
    segment_min_hits: int = 1
    scan_ratings: list[str] = field(default_factory=list)  # empty = scan all ratings
    scan_labels: list[str] = field(default_factory=lambda: [
        "FEMALE_BREAST_EXPOSED",
        "FEMALE_GENITALIA_EXPOSED",
        "MALE_GENITALIA_EXPOSED",
        "ANUS_EXPOSED",
        "BUTTOCKS_EXPOSED",
    ])
    scan_window_start: time = field(default_factory=lambda: time(23, 0))
    scan_window_end: time = field(default_factory=lambda: time(6, 0))
    log_level: str = "INFO"

    @classmethod
    async def load(cls) -> "Config":
        s = await db.get_all_settings()

        def _time(val: str, fallback: time) -> time:
            """Parse 'HH:MM', falling back instead of raising.

            A malformed value used to raise inside every watcher loop, escape
            asyncio.gather and kill the process — and because the bad value was
            persisted, the process crashed again on every restart.
            """
            try:
                h, m = val.strip().split(":")
                parsed = time(int(h), int(m))
                return parsed
            except (AttributeError, TypeError, ValueError):
                return fallback

        def _int(val: str, fallback: int, minimum: int | None = None) -> int:
            try:
                parsed = int(val)
            except (TypeError, ValueError):
                return fallback
            return max(minimum, parsed) if minimum is not None else parsed

        def _float(val: str, fallback: float, minimum: float | None = None) -> float:
            try:
                parsed = float(val)
            except (TypeError, ValueError):
                return fallback
            return max(minimum, parsed) if minimum is not None else parsed

        def _labels(val: str) -> list[str]:
            try:
                parsed = json.loads(val)
                if isinstance(parsed, list):
                    return [str(x) for x in parsed if isinstance(x, str)]
            except Exception:
                pass
            # Return empty list if parsing fails - no scanning until labels are configured
            return []

        return cls(
            plex_url=s.get("plex_url", ""),
            plex_token=s.get("plex_token", ""),
            poll_interval=_int(s.get("poll_interval", "5"), 5, minimum=1),
            confidence_threshold=_float(s.get("confidence_threshold", "0.6"), 0.6),
            skip_buffer_ms=_int(s.get("skip_buffer_ms", "3000"), 3000),
            # Fall back to skip_buffer_ms so a settings row written before the split
            # still governs both edges rather than silently reverting to the default.
            pre_buffer_ms=_int(
                s.get("pre_buffer_ms") or s.get("skip_buffer_ms", "3000"), 3000
            ),
            post_buffer_ms=_int(
                s.get("post_buffer_ms") or s.get("skip_buffer_ms", "3000"), 3000
            ),
            scan_step_ms=_int(s.get("scan_step_ms", "5000"), 5000, minimum=500),
            scan_workers=_int(s.get("scan_workers", "2"), 2, minimum=1),
            nudenet_model=s.get("nudenet_model", "320n"),
            nudenet_model_path=s.get("nudenet_model_path", ""),
            segment_gap_ms=_int(s.get("segment_gap_ms", "12000"), 12000),
            segment_min_hits=_int(s.get("segment_min_hits", "1"), 1, minimum=1),
            scan_ratings=_labels(s.get("scan_ratings", "[]")),
            scan_labels=_labels(s.get("scan_labels", "[]")),
            scan_window_start=_time(s.get("scan_window_start", "23:00"), time(23, 0)),
            scan_window_end=_time(s.get("scan_window_end", "06:00"), time(6, 0)),
            log_level=s.get("log_level", "INFO"),
        )

    def is_configured(self) -> bool:
        return bool(self.plex_url and self.plex_token)

    def is_scan_window(self) -> bool:
        """Return True if current local time is within the scan window."""
        from datetime import datetime
        now = datetime.now().time().replace(second=0, microsecond=0)
        start = self.scan_window_start
        end = self.scan_window_end
        if start <= end:
            return start <= now <= end
        # Window wraps midnight (e.g. 23:00 – 06:00)
        return now >= start or now <= end
