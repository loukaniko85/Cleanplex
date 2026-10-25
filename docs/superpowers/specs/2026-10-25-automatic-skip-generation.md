# Automatic skip generation — design

Date: 2026-10-25
Status: proposal (not yet approved)

## Problem

Cleanplex's filtering quality depends on segments, and segment supply is the bottleneck:

- Local NudeNet covers **nudity only**, and only for titles the scanner has processed.
- Subtitle scanning covers **language**, but only when embedded subtitles exist.
- Everything else (violence, gore, drugs, fear) depends on human-graded skip files.
- Measured on the reference library: the VideoSkip Exchange was fully harvested —
  192 of 23,276 attempted titles matched (**0.83%**), and the queue has no more
  material there. Movies: 4 of 1,257.
- No larger open corpus of multi-category human timings exists. Research confirmed:
  sensible-cinema (~85 DVD-era EDLs), MovieContentFilter (near-empty catalogue),
  CleanPlay/VidAngel/Skipit/Enjoy (closed), IMDb/kids-in-mind/BBFC (no timestamps).
  The only live, licensed, API'd alternative is **WhenJumpScare** — fear-only,
  films-only, ~1,167 titles / 9,783 timestamps.

Conclusion: additional *sources* can only be niche; **coverage has to be generated
locally**. This document specifies that generation path and the one external source
worth wiring in.

## Goals

1. Every title in the library gets a useful set of segments without a human author.
2. Generated segments carry honest category, severity, action and confidence so the
   existing threshold/preference engine keeps working unchanged.
3. No event-loop blocking, no unbounded CPU by default, and no new external service
   dependency for core functioning.
4. One genuinely new external source integrated on-demand, respecting licensing.

## Non-goals

- Redistributing third-party catalogues (CLAUDE.md §5.4).
- Real-time in-stream video modification (Plex exposes position and volume, nothing else).
- Building a violence model from scratch (out of scope for v1; see "Later").

## Part 1 — Whisper audio-track profanity muting (highest value)

**Why:** language is usually the most-filtered category, and the current subtitle
scanner only fires when the file ships subtitles. A speech-to-text pass makes profanity
muting work on every title with an audio track — including the 23k-episode reference
library, where per-episode human files will never exist.

**Design**

- New module `speech_scanner.py`, owned like `subtitle_scanner.py`.
  - Pipeline: `ffmpeg -i file -map 0:a:0 -ar 16000 -ac 1 -f wav pipe:1` into a
    bundled `faster-whisper` model (CPU, int8). Words come back with timestamps.
  - Match the configured `profanity_wordlist` against word tokens using the same
    suffix pattern as `subtitle_scanner.build_pattern` so behaviour is consistent.
  - Emit segments identical in shape to subtitle segments: `category=language`,
    `action=mute`, `channel=audio`, `source="speech"`, `language=<track tag>`,
    `severity="high"`, `confidence` from the word probability.
  - Merge hits with the same `MERGE_GAP_MS` logic. Reuse `find_hits`-style padding.
- Additive storage: `insert_segments_bulk(..., source="speech")` then
  `merge_title_segments` — same as every other importer. Auto-generation must never
  replace an existing human-graded file; only an explicit Replace does.
- Scheduling and cost control:
  - Off by default; a Settings toggle per library ("Generate profanity mutes from
    audio") and a model-size choice (tiny/base/small).
  - Run inside the existing scan window via the scanner queue, one worker.
  - Skip titles that already have language segments from any human source
    (`source` in `skp/edl/mcf/paste/manual` or `videoskip:*`), unless forced.
  - Persist a per-title flag so a title is transcribed once per file hash.
- Failure handling: no audio track → `skipped` with reason; transcription error →
  `failed` with retry ceiling, same semantics as VideoSkip jobs.

**Coverage impact (estimate):** ~100% of titles with dialogue audio vs. the current
subset with embedded subtitles. The model runs ~5–20× realtime on CPU depending on
size; a 24k-episode library must be treated as a background, months-long project —
exactly like a scan backlog. Prioritise by recently watched titles first.

**Risks:** whisper mis-timing on music/laughter, false positives on homophones —
mitigated by word-boundary matching, confidence floor (drop < 0.4) and the existing
review UI (segments are deletable).

## Part 2 — WhenJumpScare integration (one external source worth wiring)

**Why:** a real, live, human-authored timing dataset, and its legal terms explicitly
allow on-demand API lookup while forbidding bulk redistribution — the exact shape of
the existing VideoSkip integration.

- 1,167 films / 9,783 jumpscare timestamps, major/minor severity, films only.
- Public API: `GET https://www.whenjumpscare.com/api/movie/{tmdbId}` returns counts;
  per-film pages carry the timestamp list (plain text) and a warning `.SRT`.
- Matching key: TMDB id (already stored on `scan_jobs.tmdb_id`), fallback title + year.

**Design**

- New module `whenjumpscare.py`, mirroring `videoskip.py`: one serialized client,
  ≤1 request/second, bounded retries, forgiving HTML parsing with a clear error on
  shape change.
- Fetch per film → segments `category="fear"`, `severity` major→high / minor→medium,
  `action="skip"`, `source="whenjumpscare:<id>"`.
- Integrate as a second source in the VideoSkip queue: on `no_match` from the
  Exchange, try WhenJumpScare for movies with a TMDB id. Respect the same daily
  ceiling and attempt limits; add a source column to the queue history rows.
- Feature flag in Settings: "Also fetch jump-scares (WhenJumpScare)".

**Limits:** fear only, movies only, no TV. Treat it as a supplement, not a fix.

## Part 3 — Make sharing real (strategic, no new data source)

The sync infrastructure already exists (`cleanplex-segments` GitHub repo, segments
keyed by file SHA-256 — exact-rip matching) and is empty. Coverage from sharing grows
with adoption, which no single user can bootstrap alone; the cheapest credible steps:

- One-click "publish my segments" from the UI (currently a settings-level flow), with
  an explicit licence acknowledgement.
- Aggregate several community repos instead of one hard-coded default.
- Include title/IMDb metadata alongside the hash so non-identical rips can still be
  suggested (with the existing runtime warning) rather than discarded.

Out of scope until Parts 1–2 ship.

## Part 4 — Later (not now)

- Violence/gore detection: an open video classifier (e.g. MoViNet/XD-Violence-trained
  models) running at the scan step. Expensive, noisy, needs a confidence story and a
  UI for false positives. Only worth it if users actually filter violence.
- Jump-scare detection from audio loudness + flash-frame heuristics (no external data,
  works on TV). Cheaper than a violence model; candidate after Part 1.

## Rollout

1. `speech_scanner.py` + Settings toggle + tests (unit: segment shape, merging,
   skip-if-human-covered, failure semantics).
2. Soak on the dev instance (port 7980) on the Movies library only.
3. Production behind the toggle, default off.
4. WhenJumpScare fetcher behind its own flag, same queue machinery.
5. Revisit sharing once generated segments exist to share (nobody wants to publish an
   empty library, and VideoSkip-imported data must not be redistributed).

## Open questions for the owner

1. Whisper model size vs. CPU budget on the Plex host? (default `base`, int8, one worker)
2. Should generated `language` segments be additive to existing subtitle segments
   (duplicates collapsed by `merge_title_segments`) or should the audio pass replace
   subtitle-derived mutes when both exist? (Recommend additive — merge already
   dedupes identical ranges.)
3. Is fear/jump-scare coverage worth a per-title TMDB lookup and a second queue path,
   given it is films-only?
