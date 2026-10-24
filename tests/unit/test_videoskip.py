"""Unit tests for the VideoSkip Exchange client and its parsers."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from cleanplex import videoskip
from cleanplex.importers import skp

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "videoskip"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ── Search results ────────────────────────────────────────────────────────────


def test_search_page_parses_a_movie():
    results = videoskip.parse_search_page(_fixture("search_oppenheimer.html"))

    assert len(results) == 1
    movie = results[0]
    assert movie.video_id == "805"
    assert movie.title == "Oppenheimer"
    assert movie.year == 2023
    assert not movie.is_episode


def test_search_page_parses_episodes_with_show_and_position():
    results = videoskip.parse_search_page(_fixture("search_game_of_thrones.html"))
    first = next(v for v in results if v.video_id == "435")

    assert first.series == "Game of Thrones"
    assert (first.season, first.episode) == (1, 1)
    assert first.title == "Winter Is Coming"
    assert first.year == 2011
    assert first.is_episode


# ── Video pages ───────────────────────────────────────────────────────────────


def test_video_page_parses_metadata_and_skip_files():
    video = videoskip.parse_video_page(_fixture("video_oppenheimer.html"), "805")

    assert video.title == "Oppenheimer"
    assert video.year == 2023
    assert video.imdb_id == "tt15398776"
    assert video.content_rating == "R: Restricted Under 17"
    assert len(video.skip_files) == 4

    paco = next(f for f in video.skip_files if f.skip_id == "776")
    assert paco.author == "PacoRuiz"
    assert paco.downloads == 984
    assert paco.filters == "Sex and Nudity for Teens"
    assert paco.uploaded == "Nov. 25, 2023"


def test_video_page_parses_an_episode():
    video = videoskip.parse_video_page(_fixture("video_got_s1e1.html"), "435")

    assert video.series == "Game of Thrones"
    assert (video.season, video.episode) == (1, 1)
    assert video.title == "Winter Is Coming"
    assert video.imdb_id == "tt1480055"
    assert len(video.skip_files) == 4
    # Full month names are as common as abbreviations on the Exchange.
    assert any(f.uploaded == "June 6, 2026" for f in video.skip_files)


# ── Matching ──────────────────────────────────────────────────────────────────


def test_score_match_prefers_imdb_id_and_rejects_a_different_one():
    video = videoskip.VideoRef(video_id="1", title="Oppenheimer", year=2023, imdb_id="tt15398776")

    assert videoskip.score_match(
        video, media_type="movie", title="Oppenheimer", year=2023, imdb_id="tt15398776"
    ) is not None
    assert videoskip.score_match(
        video, media_type="movie", title="Oppenheimer", year=2023, imdb_id="tt99999999"
    ) is None


def test_score_match_requires_the_exact_episode():
    video = videoskip.VideoRef(
        video_id="435",
        title="Winter Is Coming",
        series="Game of Thrones",
        season=1,
        episode=1,
        year=2011,
    )

    assert videoskip.score_match(
        video, media_type="episode", title="Game of Thrones", season=1, episode=1, year=2011
    ) is not None
    assert videoskip.score_match(
        video, media_type="episode", title="Game of Thrones", season=1, episode=2, year=2011
    ) is None


def test_episode_imdb_mismatch_still_matches_on_position():
    # Plex can expose the series IMDb ID on an episode, while the Exchange page
    # uses the episode ID. That must not veto an exact season/episode match.
    video = videoskip.VideoRef(
        video_id="435",
        title="Winter Is Coming",
        series="Game of Thrones",
        season=1,
        episode=1,
        year=2011,
        imdb_id="tt1480055",
    )

    score = videoskip.score_match(
        video,
        media_type="episode",
        title="Game of Thrones",
        season=1,
        episode=1,
        year=2011,
        imdb_id="tt0944947",
    )
    assert score is not None
    assert score > 0


def test_score_match_does_not_match_an_episode_to_a_movie():
    episode = videoskip.VideoRef(
        video_id="435", title="Winter Is Coming", series="Game of Thrones", season=1, episode=1
    )

    assert videoskip.score_match(
        episode, media_type="movie", title="Winter Is Coming", year=2011
    ) is None


def test_recommend_skip_prefers_a_stricter_audience_then_downloads():
    video = videoskip.parse_video_page(_fixture("video_oppenheimer.html"), "805")

    # Two Teens files exist; the more downloaded one wins. Adults files lose to
    # Teens even when they are older, because "Teens" is the stricter label.
    assert videoskip.recommend_skip(video).skip_id == "776"


# ── HTTP client ───────────────────────────────────────────────────────────────


def _client(handler, **kwargs) -> videoskip.VideoSkipClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return videoskip.VideoSkipClient(
        base_url="https://example.test/exchange", http=http, min_interval=0, **kwargs
    )


async def test_download_skip_returns_filename_and_parsable_text():
    body = (FIXTURES / "oppenheimer_776.skp").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/exchange/skip/776/download/"
        return httpx.Response(
            200,
            content=body,
            headers={
                "content-type": "text/plain",
                "content-disposition": 'attachment; filename="Oppenheimer_2023_local.skp"',
            },
        )

    filename, text = await _client(handler).download_skip("776")

    assert filename == "Oppenheimer_2023_local.skp"
    segments = skp.parse(text)
    assert len(segments) == 4
    assert {seg["category"] for seg in segments} == {"nudity", "sex"}


async def test_request_retries_a_server_error_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, text=_fixture("search_oppenheimer.html"))

    results = await _client(handler).search("Oppenheimer")

    assert calls["n"] == 2
    assert results[0].title == "Oppenheimer"


async def test_request_does_not_retry_a_404():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404)

    with pytest.raises(videoskip.VideoSkipNotFound):
        await _client(handler).get_video("999")
    assert calls["n"] == 1


async def test_find_candidates_filters_and_loads_matching_pages():
    pages = {
        "/exchange/search/": _fixture("search_game_of_thrones.html"),
        "/exchange/videos/435/": _fixture("video_got_s1e1.html"),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in pages:
            return httpx.Response(200, text=pages[path])
        return httpx.Response(404)

    matches = await _client(handler).find_candidates(
        media_type="episode",
        title="Game of Thrones",
        season=1,
        episode=1,
        year=2011,
        limit=2,
    )

    assert [m.video_id for m in matches] == ["435"]
    assert matches[0].skip_files


# ── Summary ───────────────────────────────────────────────────────────────────


def test_summarize_segments_counts_by_category():
    summary = videoskip.summarize_segments(
        [
            {"category": "sex"},
            {"category": "sex"},
            {"category": "violence"},
            {"category": "fear"},
        ]
    )

    assert summary == {"fear": 1, "sex": 2, "violence": 1}


def test_recommend_skip_follows_household_category_priorities():
    video = videoskip.VideoRef(
        video_id="1",
        title="Oppenheimer",
        skip_files=(
            videoskip.SkipFileRef(
                skip_id="1", filters="Sex and Nudity for Children", downloads=100
            ),
            videoskip.SkipFileRef(
                skip_id="2",
                filters="Violence and Gore for Teens; Profanity and Hate for Teens",
                downloads=10,
            ),
        ),
    )

    # Default: the stricter audience wins.
    assert videoskip.recommend_skip(video).skip_id == "1"
    # If the household filters violence, the broader file wins.
    assert videoskip.recommend_skip(video, ["violence"]).skip_id == "2"
