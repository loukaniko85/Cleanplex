"""Shared helpers for skip file parsers: timestamps and the category vocabulary."""

from __future__ import annotations

import re

# MovieContentFilter 1.1.0 top-level categories. Parsers normalize onto these so
# user preferences and analytics work the same regardless of source format.
CATEGORIES = (
    "commercial",
    "discrimination",
    "dispensable",
    "drugs",
    "fear",
    "language",
    "nudity",
    "sex",
    "violence",
    "other",
)

SEVERITIES = ("low", "medium", "high")
ACTIONS = ("skip", "mute", "blank", "blur", "fast")
CHANNELS = ("both", "video", "audio")

_TIME_RE = re.compile(r"^\d+(:\d{1,2}){0,2}([.,]\d+)?$")

# Imported timings only hold for the cut they were authored against. Beyond this
# difference between the file's implied runtime and the media's own, the import
# is still accepted but flagged, since the user may have a different release.
DURATION_TOLERANCE_MS = 5000


class ParseError(ValueError):
    """Raised when a skip file cannot be understood. Message is user-facing."""


def to_ms(value: str) -> int:
    """Convert a skip-file timestamp to milliseconds.

    Accepts `H:MM:SS.ss`, `MM:SS.ss` and bare seconds, with `,` or `.` as the
    decimal separator. Hours are not zero-padded in VideoSkip output, and EDL
    files often use plain float seconds, so all three shapes are common.
    """
    raw = value.strip().replace(",", ".")
    if raw.startswith("#"):
        raise ParseError(
            f"Frame-based timing ({value}) is not supported: the frame rate is "
            "unknown at import time. Re-export the file with time-based timings."
        )
    if not _TIME_RE.match(raw):
        raise ParseError(f"Unrecognised timestamp: {value!r}")

    parts = raw.split(":")
    try:
        seconds = float(parts[-1])
        if len(parts) >= 2:
            seconds += int(parts[-2]) * 60
        if len(parts) == 3:
            seconds += int(parts[0]) * 3600
    except ValueError as exc:
        raise ParseError(f"Unrecognised timestamp: {value!r}") from exc
    return int(round(seconds * 1000))


def to_timestamp(ms: int) -> str:
    """Render milliseconds as `HH:MM:SS.mmm`, the form MCF requires."""
    total_seconds, millis = divmod(max(0, int(ms)), 1000)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def runtime_warning(
    segments: list[dict], duration_ms: int, tolerance_ms: int = DURATION_TOLERANCE_MS
) -> str | None:
    """Return a user-facing warning when a file's span exceeds the media runtime."""
    if not duration_ms or not segments:
        return None
    implied = max(seg["end_ms"] for seg in segments)
    if implied > duration_ms + tolerance_ms:
        return (
            f"The file's last segment ends at {implied}ms but this title is only "
            f"{duration_ms}ms long. The timings were probably authored against a "
            "different release, so skips may land in the wrong place."
        )
    return None


def validate_segment(seg: dict) -> dict:
    """Normalize one parsed segment, raising if the times make no sense.

    Parsers that process cues one by one (`.skp`) catch `ParseError` per cue so a
    single authored typo cannot discard the rest of a valid file. Whole-file
    parsers may let it propagate.
    """
    if seg["end_ms"] < seg["start_ms"]:
        raise ParseError(
            f"Segment ends before it starts ({seg['start_ms']}ms → {seg['end_ms']}ms)"
            f" — at {to_timestamp(seg['start_ms'])} → {to_timestamp(seg['end_ms'])}"
        )
    seg.setdefault("category", "other")
    seg.setdefault("severity", "high")
    seg.setdefault("action", "skip")
    seg.setdefault("channel", "both")
    seg.setdefault("labels", "")
    if seg["category"] not in CATEGORIES:
        seg["category"] = "other"
    if seg["severity"] not in SEVERITIES:
        seg["severity"] = "high"
    if seg["channel"] not in CHANNELS:
        seg["channel"] = "both"
    # CLAUDE.md §5.4: every segment carries a known action. An out-of-vocabulary
    # action used to pass straight through to the DB and was then ignored at
    # playback time with no explanation.
    if seg["action"] not in ACTIONS:
        raise ParseError(
            f"Unknown action {seg['action']!r}; expected one of {', '.join(ACTIONS)}."
        )
    return seg
