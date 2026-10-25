"""Parser for VideoSkip `.skp` files.

The format is undocumented; this implementation follows the behaviour of the
VideoSkip player itself (github.com/fruiz500/VideoSkip) and was verified against
files downloaded from the VideoSkip Exchange.

Layout::

    0:00:22.36                          <- optional: screenshot time
    When lighting crosses the icon      <- optional: sync note

    0:00:50.76 --> 0:01:01.18
    Nudity 2 image (distant)

    0:26:23.5 --> 0:26:53.53
    Sex 2 (kissing)

    {"local":0}                         <- per-source time offsets, ignored
    data:image/jpeg;base64,...           <- reference screenshot, ignored

Label lines are matched on three-letter prefixes, case-insensitively, in the same
order the player uses. A digit 1-3 is the severity; a handling keyword selects the
action; text in parentheses is a comment; `//` disables the cue.
"""

from __future__ import annotations

import re

from ..logger import get_logger
from ._common import ParseError, to_ms, validate_segment

SOURCE = "skp"

logger = get_logger(__name__)

# IMDb Parents Guide buckets, as the player groups them, mapped onto the
# MovieContentFilter vocabulary we store.
_CATEGORY_PATTERNS = (
    (re.compile(r"nud"), "nudity"),
    (re.compile(r"sex"), "sex"),
    (re.compile(r"vio|gor"), "violence"),
    (re.compile(r"pro|cur|hat"), "language"),
    (re.compile(r"alc|dru|smo"), "drugs"),
    (re.compile(r"fri|sca|int"), "fear"),
    (re.compile(r"oth|bor"), "other"),
)

# Handling keywords. Audio is checked first because the player does the same, which
# is why a label like "profane word" resolves to mute: "wor" is an audio keyword.
_ACTION_PATTERNS = (
    (re.compile(r"aud|sou|spe|wor|mut"), "mute"),
    (re.compile(r"vid|ima|img|bla"), "blank"),
    (re.compile(r"blu"), "blur"),
    (re.compile(r"fas"), "fast"),
)

_SEVERITY_BY_DIGIT = {1: "low", 2: "medium", 3: "high"}

_CUE_RE = re.compile(r"^\s*([\d:,.]+)\s*-+>\s*([\d:,.]+)\s*$")


def _strip_trailer(text: str) -> str:
    """Remove the trailing offsets JSON and base64 screenshot the player appends."""
    cut = text.find("data:image/")
    if cut != -1:
        text = text[:cut]
    # Offsets are a single JSON object on its own line, e.g. {"local":0,"netflix":-12}
    return re.sub(r"^\s*\{.*\}\s*$", "", text, flags=re.MULTILINE)


def _parse_label(label: str) -> dict:
    """Turn a VideoSkip label line into category, severity and action."""
    # Parenthetical text is explanatory and must not influence matching — the
    # player strips it before testing, so "(male, from behind)" cannot trip the
    # 'bla' blank keyword.
    cleaned = re.sub(r"\(.*?\)", "", label.lower())

    category = "other"
    for pattern, name in _CATEGORY_PATTERNS:
        if pattern.search(cleaned):
            category = name
            break

    action = "skip"
    for pattern, name in _ACTION_PATTERNS:
        if pattern.search(cleaned):
            action = name
            break

    digit = re.search(r"\d", cleaned)
    severity = _SEVERITY_BY_DIGIT.get(int(digit.group()) if digit else 1, "high")

    channel = "audio" if action == "mute" else "both"
    return {
        "category": category,
        "severity": severity,
        "action": action,
        "channel": channel,
        "labels": label.strip(),
    }


def parse(text: str) -> list[dict]:
    """Parse a `.skp` file body into normalized segments.

    Cues are processed independently: one authored typo (an inverted range, a
    missing label) drops that cue with a warning instead of discarding the whole
    file, which is how a single bad line in a 100-cue Exchange file used to
    destroy an otherwise valid import.
    """
    body = _strip_trailer(text.replace("\r\n", "\n").replace("\r", "\n"))
    lines = body.split("\n")

    segments: list[dict] = []
    dropped: list[str] = []
    saw_cue = False
    index = 0
    while index < len(lines):
        match = _CUE_RE.match(lines[index])
        if not match:
            index += 1
            continue

        saw_cue = True
        cue_line = index + 1
        index += 1

        # Locate the label line. Blank lines between the cue and its label are
        # tolerated (up to three), but another cue line is never consumed as a
        # label — that used to swallow the following cue and invent a category
        # from its digits.
        label = ""
        probe = index
        while probe < len(lines) and not lines[probe].strip() and probe - index < 3:
            probe += 1
        if probe < len(lines) and not _CUE_RE.match(lines[probe]):
            label = lines[probe].strip()
            index = probe + 1

        # `//` marks a cue the author disabled; the player ignores it entirely.
        if "//" in label:
            continue

        if not label:
            dropped.append(f"line {cue_line}: cue has no label line")
            continue

        seg = _parse_label(label)
        seg["start_ms"] = to_ms(match.group(1))
        seg["end_ms"] = to_ms(match.group(2))
        try:
            segments.append(validate_segment(seg))
        except ParseError as exc:
            dropped.append(f"line {cue_line}: {exc}")

    if not saw_cue:
        raise ParseError(
            "No skip cues found. A .skp file needs lines of the form "
            "'0:01:02.5 --> 0:01:09' followed by a label line."
        )

    if dropped:
        logger.warning(
            "Skipped %d malformed cue(s) in .skp file: %s",
            len(dropped),
            "; ".join(dropped),
        )
    if not segments:
        raise ParseError(
            "No usable skip cues found. " + "; ".join(dropped[:5])
            if dropped
            else "No skip cues found."
        )

    segments.sort(key=lambda s: s["start_ms"])
    return segments
