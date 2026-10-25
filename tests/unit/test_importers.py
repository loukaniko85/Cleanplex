"""Unit tests for the skip file parsers (issues #64, #65, #66, #67)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cleanplex import importers
from cleanplex.importers import edl, mcf, paste, skp
from cleanplex.importers._common import ParseError, to_ms, to_timestamp, validate_segment

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


# ── Timestamps ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    ("0:00:50.76", 50760),
    ("1:42:45.56", 6165560),
    ("0:26:23.5", 1583500),
    ("30.5", 30500),
    ("00:01:30,500", 90500),   # comma decimal separator
    ("12:30", 750000),         # MM:SS
])
def test_to_ms_accepts_every_shape_the_formats_use(value, expected):
    assert to_ms(value) == expected


def test_to_ms_rejects_frame_timings_with_a_clear_message():
    with pytest.raises(ParseError, match="[Ff]rame"):
        to_ms("#100")


def test_to_ms_rejects_nonsense():
    with pytest.raises(ParseError):
        to_ms("banana")


def test_to_timestamp_round_trips():
    assert to_timestamp(6165560) == "01:42:45.560"
    assert to_ms(to_timestamp(90500)) == 90500


# ── VideoSkip .skp (issue #64) ─────────────────────────────────────────────────

def test_skp_parses_real_exchange_file():
    segments = skp.parse((FIXTURES / "videoskip_an_ideal_husband.skp").read_text(encoding="utf-8"))

    # Two cues; the file's first two lines are the screenshot time and sync note.
    assert len(segments) == 2
    assert segments[0]["start_ms"] == 50760
    assert segments[0]["end_ms"] == 61180
    assert segments[0]["category"] == "nudity"
    assert segments[0]["severity"] == "medium"      # "Nudity 2 image"
    assert segments[0]["action"] == "blank"         # "image" is a video keyword
    assert segments[1]["category"] == "sex"


def test_skp_parses_large_profanity_file_as_mutes():
    """A Working Man: 101 subtitle-derived cues, all 'profane word 1'."""
    segments = skp.parse((FIXTURES / "videoskip_a_working_man.skp").read_text(encoding="utf-8"))

    assert len(segments) == 101
    assert all(s["action"] == "mute" for s in segments)
    assert all(s["category"] == "language" for s in segments)
    assert all(s["severity"] == "low" for s in segments)
    assert all(s["channel"] == "audio" for s in segments)


def test_skp_ignores_trailing_offsets_json_and_screenshot():
    segments = skp.parse((FIXTURES / "videoskip_a_working_man.skp").read_text(encoding="utf-8"))

    # The file ends with {"local":0} and a data: URI; neither may become a segment.
    assert all(s["end_ms"] > s["start_ms"] for s in segments)
    assert all("base64" not in s["labels"] for s in segments)


def test_skp_ignores_the_two_line_header():
    text = "0:00:22.36\nWhen lighting crosses the icon\n\n0:00:50.76 --> 0:01:01.18\nNudity 2\n"
    segments = skp.parse(text)

    assert len(segments) == 1
    assert segments[0]["start_ms"] == 50760


def test_skp_skips_disabled_cues():
    text = "0:00:10 --> 0:00:20\n// nudity 3\n\n0:00:30 --> 0:00:40\nviolence 3\n"
    segments = skp.parse(text)

    assert len(segments) == 1
    assert segments[0]["category"] == "violence"


def test_skp_parenthetical_comment_does_not_change_the_action():
    """'(male, from behind)' contains 'bla' but must not select the blank action."""
    segments = skp.parse("0:00:10 --> 0:00:20\nNudity 2 (male, from behind)\n")

    assert segments[0]["action"] == "skip"


def test_skp_missing_severity_digit_defaults_to_low():
    segments = skp.parse("0:00:10 --> 0:00:20\nnudity\n")

    assert segments[0]["severity"] == "low"


def test_skp_rejects_a_file_with_no_cues():
    with pytest.raises(ParseError, match="No skip cues"):
        skp.parse("this is not a skip file at all")


def test_skp_segments_are_time_ordered():
    text = "0:01:00 --> 0:01:10\nnudity 3\n\n0:00:10 --> 0:00:20\nviolence 3\n"
    segments = skp.parse(text)

    assert [s["start_ms"] for s in segments] == [10000, 60000]


def test_skp_keeps_valid_cues_when_one_range_is_inverted():
    """Regression: GoT S2E6 on the Exchange contains 28:21.99 --> 26:52.19.

    One authored typo used to raise out of parse() and discard the other 12
    valid cues in the file.
    """
    text = (
        "0:00:10 --> 0:00:20\nnudity 3\n\n"
        "0:28:21.99 --> 0:26:52.19\nviolence 2\n\n"
        "0:30:00 --> 0:30:10\nprofane word 1\n"
    )
    segments = skp.parse(text)

    assert [s["start_ms"] for s in segments] == [10000, 1800000]
    assert segments[0]["category"] == "nudity"


def test_skp_missing_label_does_not_swallow_the_next_cue():
    """A stray cue line must not be consumed as the previous cue's label.

    That used to merge two cues into one segment whose category/severity were
    invented from the timestamp digits.
    """
    text = "0:00:10 --> 0:00:20\n0:00:30 --> 0:00:40\nviolence 3\n"
    segments = skp.parse(text)

    assert len(segments) == 1
    assert segments[0]["start_ms"] == 30000
    assert segments[0]["category"] == "violence"


def test_skp_blank_line_before_label_is_tolerated():
    text = "0:00:10 --> 0:00:20\n\nviolence 3\n"

    segments = skp.parse(text)

    assert len(segments) == 1
    assert segments[0]["labels"] == "violence 3"


def test_skp_raises_when_every_cue_is_malformed():
    text = "0:00:20 --> 0:00:10\nnudity 3\n\n0:00:40 --> 0:00:30\nviolence 3\n"

    with pytest.raises(ParseError, match="No usable skip cues"):
        skp.parse(text)


def test_validate_segment_rejects_unknown_action():
    with pytest.raises(ParseError, match="Unknown action"):
        validate_segment({"start_ms": 0, "end_ms": 1000, "action": "fast-forward"})


# ── Kodi EDL (issue #65) ───────────────────────────────────────────────────────

def test_edl_parses_float_seconds():
    segments = edl.parse("30.5 45.0 0\n")

    assert segments[0]["start_ms"] == 30500
    assert segments[0]["end_ms"] == 45000
    assert segments[0]["action"] == "skip"


def test_edl_mute_action_maps_to_mute():
    segments = edl.parse("00:01:30.500 00:01:45.000 1\n")

    assert segments[0]["start_ms"] == 90500
    assert segments[0]["action"] == "mute"
    assert segments[0]["channel"] == "audio"


def test_edl_commercial_break_is_categorised():
    segments = edl.parse("10 20 3\n")

    assert segments[0]["category"] == "commercial"
    assert segments[0]["action"] == "skip"


def test_edl_scene_markers_are_not_filters():
    segments = edl.parse("10 20 2\n30 40 0\n")

    assert len(segments) == 1
    assert segments[0]["start_ms"] == 30000


def test_edl_rejects_frame_timings():
    with pytest.raises(ParseError, match="[Ff]rame"):
        edl.parse("#100 #200 0\n")


def test_edl_rejects_short_rows():
    with pytest.raises(ParseError, match="expected 'start end action'"):
        edl.parse("30.5 45.0\n")


def test_edl_rejects_unknown_action_code():
    with pytest.raises(ParseError, match="unknown EDL action"):
        edl.parse("10 20 9\n")


def test_edl_round_trips_times_and_actions():
    original = edl.parse("30.5 45.0 0\n60.0 61.5 1\n")
    reparsed = edl.parse(edl.export(original))

    assert [(s["start_ms"], s["end_ms"], s["action"]) for s in reparsed] == \
           [(s["start_ms"], s["end_ms"], s["action"]) for s in original]


def test_edl_ignores_comment_lines_with_spaces():
    """A '#' comment containing a space used to be parsed as a data row."""
    segments = edl.parse("# skipping the intro\n30.5 45.0 0\n")

    assert len(segments) == 1
    assert segments[0]["start_ms"] == 30500


def test_edl_export_omits_actions_edl_cannot_express():
    """blank/blur must never be downgraded to a hard cut on export (§5.4)."""
    exported = edl.export([
        {"start_ms": 10_000, "end_ms": 20_000, "action": "blank", "category": "nudity"},
        {"start_ms": 30_000, "end_ms": 40_000, "action": "skip", "category": "other"},
    ])

    assert exported.strip().splitlines() == ["30.000\t40.000\t0"]


# ── MovieContentFilter .mcf (issue #66) ────────────────────────────────────────

SPEC_EXAMPLE = """WEBVTT MovieContentFilter 1.1.0

NOTE
TITLE Ozymandias
YEAR 2013
TYPE episode
SEASON 5
EPISODE 14
IMDB http://www.imdb.com/title/tt2301451/
RELEASE North America

NOTE
START 00:00:04.020
END 01:24:00.100

00:00:06.075 --> 00:00:10.500
violence=high

00:06:14.000 --> 00:06:17.581
gambling=medium # Some comment
drugs=high=video

00:58:59.118 --> 01:00:03.240
sex=low=both # Another comment

01:02:31.020 --> 01:02:49.800
fear=low
language=high=audio
"""


def test_mcf_parses_the_specification_example():
    segments = mcf.parse(SPEC_EXAMPLE)

    assert len(segments) == 6
    assert segments[0]["category"] == "violence"
    assert segments[0]["start_ms"] == 6075


def test_mcf_multi_category_cue_yields_one_segment_each():
    segments = mcf.parse(SPEC_EXAMPLE)
    cue = [s for s in segments if s["start_ms"] == 374000]

    assert len(cue) == 2
    assert {s["category"] for s in cue} == {"drugs"}  # gambling groups under drugs
    assert {s["channel"] for s in cue} == {"both", "video"}


def test_mcf_strips_trailing_comments():
    segments = mcf.parse(SPEC_EXAMPLE)

    assert all("#" not in s["category"] for s in segments)
    assert all("comment" not in s["category"] for s in segments)


def test_mcf_audio_channel_becomes_a_mute():
    segments = mcf.parse(SPEC_EXAMPLE)
    language = [s for s in segments if s["category"] == "language"][0]

    assert language["channel"] == "audio"
    assert language["action"] == "mute"


def test_mcf_specific_terms_map_to_their_group():
    segments = mcf.parse(
        "WEBVTT MovieContentFilter 1.1.0\n\n00:00:01.000 --> 00:00:02.000\ntoplessness=high\n"
    )

    assert segments[0]["category"] == "nudity"


def test_mcf_unknown_category_falls_back_to_other():
    segments = mcf.parse(
        "WEBVTT MovieContentFilter 1.1.0\n\n00:00:01.000 --> 00:00:02.000\nbanana=high\n"
    )

    assert segments[0]["category"] == "other"


def test_mcf_requires_its_header():
    with pytest.raises(ParseError, match="header"):
        mcf.parse("00:00:01.000 --> 00:00:02.000\nviolence=high\n")


def test_mcf_accepts_a_utf8_bom():
    """Windows-authored files carry a BOM; it is not whitespace to str.strip()."""
    segments = mcf.parse(
        "\ufeffWEBVTT MovieContentFilter 1.1.0\n\n00:00:01.000 --> 00:00:02.000\nviolence=high\n"
    )

    assert segments[0]["category"] == "violence"


def test_mcf_export_omits_actions_mcf_cannot_express():
    exported = mcf.export([
        {"start_ms": 10_000, "end_ms": 20_000, "action": "blank", "category": "nudity"},
        {"start_ms": 30_000, "end_ms": 40_000, "action": "skip", "category": "other"},
    ])
    reparsed = mcf.parse(exported)

    assert len(reparsed) == 1
    assert reparsed[0]["start_ms"] == 30_000


def test_mcf_round_trips():
    original = mcf.parse(SPEC_EXAMPLE)
    reparsed = mcf.parse(mcf.export(original, title="Ozymandias", year=2013))

    assert [(s["start_ms"], s["end_ms"], s["category"], s["severity"]) for s in reparsed] == \
           [(s["start_ms"], s["end_ms"], s["category"], s["severity"]) for s in original]


# ── Pasted lists (issue #67) ───────────────────────────────────────────────────

def test_paste_parses_common_hand_written_shapes():
    segments = paste.parse(
        "00:12:30 - 00:13:05 nudity\n"
        "1:02:11 to 1:02:20  violence (fight)\n"
        "12:30-13:05\n"
    )

    assert len(segments) == 3
    assert {s["category"] for s in segments} == {"nudity", "violence", "other"}
    assert sorted(s["start_ms"] for s in segments) == [750000, 750000, 3731000]


def test_paste_ignores_prose_lines():
    segments = paste.parse("Here are the timestamps:\n00:01:00 - 00:01:10 gore\nEnjoy!\n")

    assert len(segments) == 1
    assert segments[0]["category"] == "violence"


def test_paste_rejects_input_with_no_ranges():
    with pytest.raises(ParseError, match="No timestamp ranges"):
        paste.parse("nothing useful here")


def test_paste_does_not_treat_the_name_scar_as_fear():
    """Lion King's Scar is a character, not a scary scene."""
    segments = paste.parse("04:38 - 07:15 Scar Missed Presentation of Simba\n")

    assert segments[0]["category"] == "other"


def test_paste_still_treats_scary_wording_as_fear():
    segments = paste.parse("10:00 - 10:30 scary jump scene\n")

    assert segments[0]["category"] == "fear"


def test_paste_parses_a_real_lion_king_timestamp_list():
    text = (
        "04:38 - 07:15 Scar Missed Presentation of Simba\n"
        "11:25 - 13:14 Scar Chat with Samba\n"
        "18:07 - 22:54 Simba in elephant graveyard and hyenas\n"
        "25:31 - 40:56 Scar and Hyenas meeting + Stampede\n"
        "48:17 - 50:04 Scar and Hyenas with Captured Zazu\n"
        "54:04 - 55:12 Nana Chasing Pumba\n"
        "1:10:05 - 1:21:00 Simba’s Challenge of Scar and Hyenas\n"
    )
    segments = paste.parse(text)

    assert len(segments) == 7
    assert (segments[0]["start_ms"], segments[0]["end_ms"]) == (278_000, 435_000)
    assert (segments[-1]["start_ms"], segments[-1]["end_ms"]) == (4_205_000, 4_860_000)
    assert all(seg["action"] == "skip" for seg in segments)


# ── Dispatch and sidecar discovery (issue #67) ─────────────────────────────────

def test_parse_file_dispatches_on_extension(tmp_path):
    path = tmp_path / "movie.edl"
    path.write_text("30.5 45.0 0\n", encoding="utf-8")

    segments, source = importers.parse_file(path)

    assert source == "edl"
    assert segments[0]["start_ms"] == 30500


def test_parse_file_rejects_unknown_extension(tmp_path):
    path = tmp_path / "movie.srt"
    path.write_text("whatever", encoding="utf-8")

    with pytest.raises(importers.ImportError_, match="Unsupported"):
        importers.parse_file(path)


def test_find_sidecar_prefers_skp_over_edl(tmp_path):
    (tmp_path / "movie.mkv").write_text("x", encoding="utf-8")
    (tmp_path / "movie.edl").write_text("10 20 0\n", encoding="utf-8")
    (tmp_path / "movie.skp").write_text("0:00:10 --> 0:00:20\nnudity 3\n", encoding="utf-8")

    assert importers.find_sidecar(tmp_path / "movie.mkv").suffix == ".skp"


def test_find_sidecar_returns_none_when_absent(tmp_path):
    (tmp_path / "movie.mkv").write_text("x", encoding="utf-8")

    assert importers.find_sidecar(tmp_path / "movie.mkv") is None
