# Audit follow-ups — issue drafts (2026-10-25)

`gh` is not available in this environment, so these are ready to paste into GitHub.
Each follows the template in CLAUDE.md §2.2. Items already fixed in this branch are
listed at the end for closing/reference.

---

## 1. Gate destructive routes behind an explicit auth dependency

**Labels:** bug, reliability, high-priority, backend

### Summary
Authentication is optional and global: every route trusts middleware that is disabled
unless `CLEANPLEX_AUTH_USER/PASS` are set. With defaults the service binds `0.0.0.0`
with CORS `*`, and every destructive endpoint (segment delete, queue clear, scanner
restart, user preferences, MCP delete tool, webhook) is writable by any LAN peer.

### Evidence
- cleanplex/web/app.py:54-66 — CORS `allow_origins=["*"]`; auth middleware only installed when both env vars exist.
- cleanplex/main.py:63-64 — default bind `0.0.0.0:7979`.
- cleanplex/web/routes/segments.py — `DELETE /api/segments/{id}`, `DELETE /api/titles/{guid}/segments` have no dependency.

### Impact
Any device on the network can wipe segments, poison the queue, restart the scanner, and
change per-user filtering. The settings-token leak was the fastest takeover path; it is
fixed in this branch, but the endpoints remain open by design.

### Proposed Work
- Add a FastAPI dependency `require_auth` to every mutating router.
- Default-deny when no credentials are configured, or bind to 127.0.0.1 by default and
  require an explicit opt-in for LAN exposure.
- Restrict CORS to the configured origin.

### Acceptance Criteria
- Unauthenticated POST/PUT/DELETE returns 401/403 in the default deployment.
- Read-only GETs remain usable for the dashboard behind the same auth.

---

## 2. Bound GitHub segment-sync fan-out and report partial results

**Labels:** performance, reliability, medium-priority, backend

### Summary
`download_segment_library` issues one GitHub Contents GET per file hash (5-way
semaphore) with no rate-limit handling, and an all-or-nothing `asyncio.gather`. On a
24k-title library this is 24k requests against a 60/hr unauthenticated limit; one 403
aborts everything and `mark_sync_complete()` never runs.

### Evidence
- cleanplex/sync.py:298-301 — `_fetch_one` per hash, `raise_for_status()` with no 403/backoff handling.
- cleanplex/sync.py:330 — `asyncio.gather(*...)`; no partial-results path.

### Impact
First sync on a real library cannot complete; already-fetched data is discarded from
the run's result.

### Proposed Work
- Paginate/batch via the Git Trees API (one tree call lists all blobs).
- Handle 403/429 with exponential backoff and a ceiling.
- Return per-hash results; mark sync complete when any batch succeeds.

### Acceptance Criteria
- A sync with 1k hashes makes O(10) requests, not 1k.
- A mid-run failure returns the successful subset and records the failure.

---

## 3. Force-requeue VideoSkip lookups when a title becomes eligible again

**Labels:** bug, medium-priority, backend

### Summary
`skipped` is terminal: once a title is skipped because it was ignored or its library was
excluded, `enqueue_videoskip_job` refuses it forever — even after it is un-ignored or the
library is re-enabled.

### Evidence
- cleanplex/database.py:1337-1341 — `if status in ("done", "no_match", "skipped"): return False`.
- cleanplex/videoskip_queue.py:92-102 — skipped reasons: "Title is gone or ignored", "Library is excluded".
- cleanplex/watcher.py:195-199 — the watcher's own re-enqueue is refused by the terminal row.

### Proposed Work
- On un-ignore, call `enqueue_videoskip_job(..., force=True)` when auto-fetch is on.
- When `videoskip_auto_libraries` changes, force-requeue rows skipped with the exclusion reason.
- Consider storing the skip reason as a distinct status (`deferred`) instead of `skipped`.

### Acceptance Criteria
- Un-ignoring a skipped title queues it on the next sweep (or immediately).
- Removing a library from the exclusion list requeues its skipped titles.

---

## 4. Complete the MovieContentFilter category mapping

**Labels:** bug, low-priority, backend

### Summary
`_SPECIFIC_TO_GROUP` + `_FEAR_TERMS` cover ~89 of the spec's ~130 specific terms. Any
unmapped term silently becomes `other`, so a user's category preference never fires for
that cue.

### Evidence
- cleanplex/importers/mcf.py:36-80 — table with ~89 entries; comment claims ~130 categories.
- cleanplex/importers/mcf.py:80 — `return _SPECIFIC_TO_GROUP.get(key, "other")`.

### Proposed Work
- Enumerate the full MCF 1.1.0 vocabulary and map every term.
- Add a unit test that every category in the spec maps to a top-level group.

### Acceptance Criteria
- No spec term lands in `other` except the literal `other` group.

---

## 5. Preserve severity when importing EDL files

**Labels:** tech-debt, low-priority, backend

### Summary
EDL has no severity column, so every import lands as `high`. Since the threshold model
is `level + rank > 3`, an EDL-derived cut always fires for a user at level 1, unlike an
equivalent `.skp` cue authored as low severity. This is inherent to EDL but undocumented
in the UI and easy to mistake for a parser bug.

### Evidence
- cleanplex/importers/edl.py:27-31 — mapping has no severity field → validate_segment default `high`.

### Proposed Work
- Document the lossy round-trip in the import UI and README.
- Optionally add a user-level "EDL severity" setting (default `high`).

### Acceptance Criteria
- The import panel states that EDL carries no severity/category information.

---

## 6. Broaden the pasted-list parser

**Labels:** bug, low-priority, backend

### Summary
Pasted lists only accept `start - end text` with `:`/`,` separators. AM/PM forms and
dotted `MM.SS` are dropped silently (decimal dots are interpreted as seconds, a 60×
error), and dropped lines are not counted.

### Evidence
- cleanplex/importers/paste.py:23-26 — anchored regex without am/pm or dot-minute forms.
- cleanplex/importers/paste.py:58-61 — unmatched lines are skipped with no feedback.

### Proposed Work
- Accept am/pm and `MM.SS` forms where unambiguous; warn when a line looks like a
  timestamp but fails.
- Return a skipped-line count to the import response.

### Acceptance Criteria
- A list mixing accepted and malformed lines reports how many were ignored.

---

## 7. VideoSkip search pagination and series-page links

**Labels:** enhancement, low-priority, backend

### Summary
`search()` fetches a single `/search/?q=` page and only recognises
`/exchange/videos/<id>/` anchors. Shows whose correct episode sorts past page one can
end as `no_match`, and series links are ignored.

### Evidence
- cleanplex/videoskip.py:49-51 — single anchor shape.
- cleanplex/videoskip.py:414-420 — no pagination loop.

### Proposed Work
- Follow result pagination with the existing rate limit.
- Recognise `/exchange/series/<id>/` pages (list their episodes).

### Acceptance Criteria
- A title present beyond page one resolves without manual search.

---

## 8. Respect the daily ceiling in the bulk `/videoskip/auto` route

**Labels:** reliability, low-priority, backend

### Summary
The Library "Fetch VideoSkip" path performs up to 100 synchronous Exchange lookups per
request without consulting `videoskip_auto_daily_limit` or the queue, so it can exceed
the politeness budget that the queue enforces.

### Evidence
- cleanplex/web/routes/videoskip_routes.py:162-238 — direct `fetch_best` loop.
- cleanplex/videoskip_queue.py:49-54 — the ceiling exists only in the worker.

### Proposed Work
- Check/adjust a shared daily counter in the route, or enqueue the work and poll the
  queue from the UI.

### Acceptance Criteria
- A bulk fetch cannot exceed the configured daily allowance.

---

## 9. Batch the per-session user-filter lookup

**Labels:** performance, low-priority, backend

### Summary
`: `get_user_filter` runs once per active session per poll tick (default 5s). With a
handful of sessions this is trivial, but it is a textbook §5.2 loop-DB-call.

### Evidence
- cleanplex/watcher.py:47-48 — per-session `db.get_user_filter`.

### Proposed Work
- One `SELECT ... WHERE plex_username IN (...)` per tick, mapped in memory.

### Acceptance Criteria
- One DB query per poll tick regardless of session count.

---

## 10. Document the plex_markers rebuild migration as a policy exception

**Labels:** tech-debt, low-priority, backend

### Summary
The marker-key migration drops and recreates `plex_markers` (CLAUDE.md §5.2 requires
additive migrations). The comment argues it is a re-syncable cache; `INSERT OR IGNORE`
also silently drops colliding rows.

### Evidence
- cleanplex/database.py:324-343 — `DROP TABLE` / `RENAME` / `INSERT OR IGNORE ... SELECT *`.

### Proposed Work
- Record the exception in CLAUDE.md §5.2 with its justification, or replace the rebuild
  with a new-table copy + view swap.

### Acceptance Criteria
- The architecture rules and the code agree on the migration policy.

---

# Fixed in this branch (for commit/close references)

- **Skip seeks to the segment start** (filter_engine.py) — a skip landed *inside* the
  flagged content and then suppressed retries until past it. Now targets the widened
  end; trigger window's right edge is exclusive.
- **One bad cue discarded a whole `.skp` file** (importers/skp.py, _common.py) —
  per-cue tolerance, no more swallowing the next cue as a label, blank-line tolerance.
- **Episode matching by season/episode alone** (videoskip.py) — requires series-name
  affinity (word-level, not substring: "cia" in "special"); wrong-show files are
  rejected instead of applied.
- **Auto-fetch could apply a file for a different cut** (videoskip_service.py) — span
  <50% or >150% of runtime is rejected on the automatic path.
- **Sweep head-clog** (database.py) — terminal queue rows no longer occupy the sweep
  batch; indexes on `scan_jobs(created_at,id)` and `scan_jobs(library_id)` added.
- **EDL comments with spaces crashed the parser** (edl.py) — fixed; frame timings still
  produce the clear unsupported-timing error.
- **UTF-8 BOM broke MCF imports** (mcf.py, importers/__init__.py, import_routes.py).
- **Exports downgraded blank/blur to cuts** (edl.py, mcf.py) — omitted and logged.
- **Queue enqueue race** (database.py) — `BEGIN IMMEDIATE` + guarded UPDATE + integrity
  tolerance; merge plan/write now runs in one transaction.
- **Watcher did 24k DB queries/minute** (watcher.py) — one batched lookup per section;
  sidecar imports now write `done` (were invisible to sync uploads).
- **Library sync/scan upserts were per-row commits** (segments.py, scanner_routes.py,
  database.py) — batched `upsert_scan_jobs_bulk`.
- **Scanner hot-spun outside the scan window** (scanner.py) — deferred set instead of
  re-enqueueing; stuck `scanning` rows recovered on pool start/restart.
- **Shutdown left loops running and clients open** (main.py) — tasks cancelled, Plex and
  VideoSkip clients closed; interrupted scans reset to pending.
- **Malformed settings crash-looped the process** (config.py, settings route) —
  tolerant parsing plus 422 validation on save.
- **Subtitle scanner read files on the loop, leaked ffmpeg, mis-stamped fallback
  tracks** (subtitle_scanner.py).
- **Frame extraction could hang on full stderr pipe and mark partial scans done**
  (frame_extractor.py).
- **Plex token returned by GET /api/settings** (settings.py) — redacted; sentinel
  echo keeps the stored token on save. `test-connection` no longer leaks a client.
- **`/api/plex-image` SSRF with token exfiltration** (plex_client.py, segments.py) —
  relative paths only, must resolve to the configured Plex origin.
- **Unbounded request bodies** (import_routes.py) — streamed 2 MB cap, pasted-list
  length cap.
- **Unbounded list endpoints** (segments.py) — page cap 500, batch cap 1000.
- **Webhook conflated Plex errors with "item not found"** (webhook_routes.py,
  plex_client.py).
- **Show-art cache grew forever** (plex_client.py) — bounded at 512 entries.
- **Frontend**: VideoSkip poll abort/error handling, search race, Settings stuck
  skeleton/spinner and swallowed save errors, Library bulk delete bounded concurrency
  and visible failures.
- **NudeNet coverage panel** — new `GET /api/scan/coverage` + Settings block
  (titles/scanned/nudenet per library) mirroring the VideoSkip panel.
