<p align="center">
  <img src="frontend/public/logo-256.png" width="88" height="88" alt="cleanplex logo" />
</p>

# cleanplex

[![Tests](https://github.com/loukaniko85/Cleanplex/actions/workflows/tests.yml/badge.svg)](https://github.com/loukaniko85/Cleanplex/actions/workflows/tests.yml)
[![Build container](https://github.com/loukaniko85/Cleanplex/actions/workflows/docker.yml/badge.svg)](https://github.com/loukaniko85/Cleanplex/actions/workflows/docker.yml)

A home server service that monitors your Plex instance for active streams and automatically skips past inappropriate content for configured user accounts. Nudity is detected locally with NudeNet, profanity is muted from subtitles, and violence, gore, drugs and frightening scenes come from human-graded skip files — including one-click lookups against the free [VideoSkip Exchange](https://videoskip.org/exchange/).

## How It Works

### 1. Pre-scan (Background Analysis)
When videos are added to your Plex library, Cleanplex queues them for background analysis:
- Extracts frames from the video every 10 seconds using `ffmpeg`
- Runs each frame through [NudeNet](https://github.com/notAI-tech/NudeNet) (lightweight, CPU-friendly ONNX model running fully locally)
- Each detected scene is **expanded by 5 seconds before and after** to catch any leading/trailing content the detector might have missed
- Flagged frames are clustered into skip segments and stored in a local SQLite database
- A 2-hour movie takes ~20–30 minutes to scan on a Raspberry Pi 4

### 2. Real-time Playback Monitoring
While someone watches, the service polls Plex every few seconds:
- Checks if a filtered user's playback position enters a flagged segment
- The monitor uses a **5-second lookahead** before each segment start to compensate for polling latency
- When triggered, automatically sends a seek command to jump **past the entire segment** (including the 5-second buffers)
- This is just a database lookup — no ML at playback time

### 3. Web UI Dashboard
Full browser interface at `http://your-server:7979` for:
- Configuring Plex connection and scan settings
- Reviewing and editing detected segments
- Monitoring active scans with live progress indicators
- Viewing recent skip events

## Requirements

- Python 3.11+
- `ffmpeg` and `ffprobe` installed on the server
- Plex Media Server on the same network
- A Plex authentication token

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/loukaniko85/Cleanplex.git
cd Cleanplex
```

### 2. Install Python dependencies

```bash
pip install -e .
```

### 3. Build the frontend (optional — required for the web UI)

```bash
cd frontend
npm install
npm run build
cd ..
```

### 4. Run

```bash
python -m cleanplex
```

Open `http://localhost:7979` in your browser and configure your Plex connection in **Settings**.

### 5. Install as a systemd service (Linux)

```bash
sudo cp cleanplex.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cleanplex
sudo journalctl -u cleanplex -f   # View logs
```

### 6. Run with Docker

A prebuilt image is published to GHCR on every push to `main`:

```bash
docker pull ghcr.io/loukaniko85/cleanplex:latest
docker run -d --name cleanplex \
  -p 7979:7979 \
  -v cleanplex-data:/data \
  --restart unless-stopped \
  ghcr.io/loukaniko85/cleanplex:latest
```

Or build it yourself with Compose:

```bash
docker compose up -d --build
```

The UI is on `http://localhost:7979`. Everything persistent lives under **`/data`**:

| Path | Contents |
|---|---|
| `/data/cleanplex.db` | SQLite database — Plex token, settings, segments, queue |
| `/data/posters/` | Plex artwork cache |
| `/data/thumbnails/` | Segment preview thumbnails |
| `/data/models/` | Downloaded NudeNet 640m model |

### Media mounts (required for local scanning)

The skip/skip-and-mute engine needs no media access — it only sends seek/mute
commands to Plex clients. **Local NudeNet scanning does**: the scanner runs ffmpeg
on the media file, so the file must exist inside the container at the exact path
Plex reports.

Find those paths in Plex (item → ⋯ → **Get Info** → **View XML** → `Part file="/..."`)
and mount each prefix read-only:

```bash
docker run -d --name cleanplex \
  -p 7979:7979 \
  -v cleanplex-data:/data \
  -v /mnt/user/media/tv:/tv:ro \
  -v /mnt/user/media/movies:/movies:ro \
  --restart unless-stopped \
  ghcr.io/loukaniko85/cleanplex:latest
```

If the paths do not match, every scan fails with `File not found: /tv/...` and the
NudeNet coverage panel stays at zero. The panel and the Analytics page now show
that failure count and the last error; after fixing the mounts, use **Settings →
Local scanning → Retry failed scans** instead of rescanning by hand.

Map exactly one host folder (or named volume) to `/data`. **Do not map
`/home/cleanplex`** — that is only the container user's home directory and is not
used for configuration.

For a bind mount, the container runs as UID 1000, so the host folder must be
writable by that user:

```bash
mkdir -p /home/loukaniko/cleanplex-data
sudo chown -R 1000:1000 /home/loukaniko/cleanplex-data

docker run -d --name cleanplex \
  -p 7979:7979 \
  -v /home/loukaniko/cleanplex-data:/data \
  --restart unless-stopped \
  ghcr.io/loukaniko85/cleanplex:latest
```

A named volume avoids the permission step entirely because Docker copies the
image's `/data` ownership into an empty volume:

```bash
docker run -d --name cleanplex \
  -p 7979:7979 \
  -v cleanplex-data:/data \
  --restart unless-stopped \
  ghcr.io/loukaniko85/cleanplex:latest
```

If you want NudeNet frame scanning inside the container, mount your media
read-only and make sure the paths inside Plex match the container paths
(e.g. `/media`). Imported skip files and the skip engine do not need a media
mount.

## Data Directory

All persistent state lives under **`CLEANPLEX_DATA`** (default `~/.cleanplex`; `/data`
in the container):

| Path | Contents |
|---|---|
| `cleanplex.db` | SQLite database — Plex token, settings, segments, queue |
| `posters/` | Plex artwork cache |
| `thumbnails/` | Segment preview thumbnails |
| `models/` | Downloaded NudeNet 640m model |

There is no separate config file. Plex credentials and every preference are stored
in the database and edited through the web UI. Keep this directory on local disk;
SQLite locking is unreliable on some network shares.

## Configuration

All configuration is done via the **web UI** at `http://your-server:7979/settings`. No config files to edit.

### Optional authentication

Cleanplex is designed for a trusted LAN. If it is reachable from anywhere else, set
both of these environment variables to require an HTTP Basic prompt for the UI and
API:

```bash
CLEANPLEX_AUTH_USER=admin
CLEANPLEX_AUTH_PASS=choose-a-long-password
```

`/api/health` stays public so container health checks and reverse proxies can probe
it without credentials. With the variables unset, behaviour is unchanged (no auth).

| Setting | Default | Description |
|---|---|---|
| Plex Server URL | — | e.g. `http://192.168.1.10:32400` |
| Plex Token | — | Your Plex authentication token |
| Poll Interval | 5s | How often to check active streams |
| Confidence Threshold | 0.6 | NudeNet score threshold (0–1). Lower = more sensitive. |
| Pre Buffer | 3000ms | Milliseconds to start the skip *before* a segment, so the flagged content is never briefly shown |
| Post Buffer | 3000ms | Milliseconds to hold past the end of a segment before the filter can re-trigger |
| Profanity Wordlist | built-in | JSON array of words the subtitle scanner mutes. Empty uses the bundled list |
| Skip Event Retention | 90 days | How long skip history is kept before being pruned at startup |
| Scan Window | 23:00–06:00 | Time window when background scanning runs |
| Scanner Workers | 2 | Number of parallel scans (higher = more CPU/memory usage) |
| Detector Labels | — | Select which nudity types to detect (and skip). Only selected labels trigger skips |
| Content Ratings | — | Only scan and filter titles matching your selected ratings |

## Segment Expansion & Skip Logic

**Default behavior:** When a segment is detected (e.g., 30s–60s), Cleanplex automatically:
1. **Widens the segment** by the Pre and Post Buffer settings → 27s–63s at the 3000ms defaults
2. **Monitors with a lookahead** of one poll interval — the filter triggers before the widened start to absorb polling latency
3. **Skips to 63s** (the widened end) — past the flagged content entirely — and holds the block until playback reaches 63s

Seeking to the *start* of a flagged span lands the viewer inside it; the target is always the
widened end, so a skip never plays through the content it was meant to remove.

Polling tightens automatically as a stream approaches a segment, so skips land accurately without
polling Plex hard the rest of the time. Every skip is verified on the next tick: a client that
accepts the command without actually moving is recorded as a failure and re-probed.

### Categories, severity and actions

Segments carry a **category** (nudity, sex, violence, language, drugs, fear, commercial, …), a
**severity** (low/medium/high) and an **action**:

| Action | Behaviour |
|---|---|
| `skip` | Seeks past the segment |
| `mute` | Drops the volume for the segment and restores it after — used for profanity |

Each user gets a 0–3 level per category. A segment fires when *level + severity* exceeds 3, so
level 3 filters everything in that category and level 0 filters nothing. Users with no saved
preferences have everything filtered, matching the previous behaviour.

`blank` and `blur` appear in some imported files but are **not supported**: Plex exposes playback
position and volume, not the video stream. Those segments are logged and ignored rather than
misapplied as skips.

## Importing Skip Files

Cleanplex reads skip files produced by other tools. **A title with an imported file is never
ML-scanned**, so this is by far the cheapest way to fill your library.

| Format | Extension | Notes |
|---|---|---|
| VideoSkip | `.skp` | Categories, 1–3 severity, and skip/mute/blank handling. The VideoSkip Exchange has ~563 movies and ~110 series, free and without an account |
| Kodi / MPlayer EDL | `.edl` | `start end action`; 0 cut, 1 mute, 2 scene marker, 3 commercial. Also exported |
| MovieContentFilter | `.mcf` | Spec 1.1.0, ~130 hierarchical categories. Also exported |
| Pasted list | `.txt` | Loose `00:12:30 - 00:13:05 nudity` lines, for lists that have no machine format |

EDL has no category or severity fields. Imports are therefore treated as high-severity
`skip` cuts, and `blank`/`blur` actions cannot be represented by the format.

Three ways in:

- **Sidecar** — drop `movie.skp` (or `.edl` / `.mcf`) next to `movie.mkv`. It is imported when the
  title is first discovered and the scan is skipped entirely.
- **Library page** — open a title (the download icon on every row) and upload a file, paste a list,
  or use the **VideoSkip** tab.
- **VideoSkip Exchange** — search the free community catalogue from inside Cleanplex, pick a file,
  and apply it in one click. Select several titles and use **Fetch VideoSkip** to do a bounded,
  rate-limited batch.

Matching uses the title's IMDb ID when Plex supplies one, otherwise title + year (movies) or show +
season/episode (TV). A title with an imported file is never ML-scanned. Cleanplex does not bundle or
mirror the Exchange: each file is fetched on demand from the service, at the user's request. The
Exchange is a volunteer service, so lookups are serialized and can fail if it is down.

### Automatic VideoSkip queue

Settings → **VideoSkip Exchange** can run lookups in the background. It is **off by default**.

- **Trigger** — new titles are queued as they are discovered; an optional sweep queues existing
  titles that have no VideoSkip data (default: every 60 minutes).
- **Limits** — one worker, a configurable delay between titles, a daily ceiling, and a per-title
  attempt limit. The Exchange client itself still serializes to roughly one request per second.
- **Persistence** — the queue lives in SQLite and survives restarts. Running jobs are requeued on
  startup.
- **Control** — Settings shows queued/running/done/no-match/failed counts, the current title, and
  today's usage. Pause, resume, retry failed, queue missing, and clear finished are available there.
- **Non-destructive** — auto-fetch adds VideoSkip segments alongside any existing scanner segments;
  it never replaces them. Manual queueing works even when the automatic toggle is off.

The queue targets titles with **no VideoSkip-sourced segments**, not titles with no segments at all,
so a NudeNet-only scan does not hide a title from human-graded coverage.

### Jump scares (WhenJumpScare)

Films can also pull human-timed **fear** markers from
[WhenJumpScare](https://www.whenjumpscare.com/): open a title in the Library, choose the
**Jump scares** tab, and fetch. Lookups are on demand and rate-limited; the service's
terms allow per-title lookups but not bulk redistribution, so Cleanplex never mirrors its
catalogue. TV episodes are not covered (the site indexes films only).

Each scare becomes a short `fear`/`skip` segment centered on the moment (minor scares are
medium severity, major ones high) and replaces any previous WhenJumpScare data for that
title. It is additive to NudeNet and file imports.

### Plex webhook (optional, Plex Pass)

The library watcher already discovers new titles within about a minute. If you want
an instant enqueue, point Plex at:

```text
http://your-server:7979/api/webhooks/plex
```

Only `library.new` triggers work, and only while auto-fetch is enabled. If you set
`CLEANPLEX_WEBHOOK_TOKEN`, include it as `?token=...` in the URL. When HTTP Basic auth
is enabled, the token is **mandatory** — the receiver returns 503 rather than exposing an
unauthenticated write path. Plex sends the payload as multipart form data; no extra
configuration is needed.

**Verifying it:**

- Settings → VideoSkip Exchange → **Plex webhook** shows the total deliveries, the
  last event/time, and the result (`queued`, `ignored`, `invalid`, …).
- `curl http://your-server:7979/api/webhooks/plex/status`
- `docker logs -f cleanplex | grep -i webhook`
- Manual delivery:

  ```bash
  curl -X POST http://localhost:7979/api/webhooks/plex \
    -F 'payload={"event":"library.new","Metadata":{"ratingKey":"12345","title":"Test"}}'
  ```

  A `200` with `{"ignored": "auto-fetch off"}` means the endpoint is reachable but
  auto-fetch is disabled. `library.new` fires when media is **added**, not when you
  press play. If Plex runs in a container, `localhost` in Plex's webhook URL refers
  to that container — use the host IP or a shared Docker network.

Imported timings are authored against a specific cut of a film. Cleanplex checks them against the
title's runtime and warns when they do not line up, since an extended edition will throw every
timestamp out. Auto-fetch is stricter than manual import: a file whose span is far off the
title's runtime (or a wrong-title match) is rejected rather than silently applied.

Malformed cues are handled per cue, not per file: if a `.skp` file contains one inverted range,
that cue is skipped with a log warning and the rest of the file is imported. EDL comments
(`# text`), MCF files with a UTF-8 BOM, and blank lines between a cue and its label are all
accepted.

Plex itself does not read `.edl` files, so EDL/MCF export is for Kodi, Jellyfin and mpv. Those
formats cannot express `blank`/`blur`/`fast`, so those segments are omitted from exports (and
logged) rather than downgraded to hard cuts.

## Profanity Filtering

Subtitles are scanned against a configurable wordlist and matching lines become **muted** segments —
no frames, no inference, seconds per title. Matching uses word boundaries with suffix handling, so
"shitting" is caught while "classic" is not.

Because the timings come from one audio track, these segments are tagged with that track's language
and only fire when it is the one playing; a dubbed track is left alone rather than muted in the
wrong places.

Violence, gore, drugs and frightening content have **no local detector** — NudeNet covers nudity
only. Those categories are populated by importing skip files, where humans have already graded them.

## Finding Your Plex Token

1. Sign in to Plex Web
2. Browse to any media item
3. Click the three-dot menu → **Get Info** → **View XML**
4. The URL will contain `?X-Plex-Token=XXXXXX`

## Web UI Pages

#### Dashboard
- **Active streams** — Shows all users currently watching, with playback position and controllability status
- **Plex clients** — Every discovered client with its advertised capabilities and whether Cleanplex can control it (Plex Web is shown as not controllable, with the reason)
- **Scanner status** — Number of workers active, queue size, and which titles are currently being scanned
- **Recent skip events** — Log of the last 50 skips with timestamp, title, user, and client

#### VideoSkip
- **Coverage** — titles with any segments, titles with VideoSkip data, and titles still missing human-graded coverage, overall and per library
- **Queue** — live depth, current title, daily usage, recent successes/failures with error reasons, pause/resume/retry/clear
- **Exchange search** — discovery by title; apply files from a Plex title’s VideoSkip tab
- **Test Exchange** — one-click reachability and latency check

#### Library
- **Import skip files** — Upload `.skp` / `.edl` / `.mcf`, paste a timestamp list, export segments as EDL/MCF, or fetch from the VideoSkip Exchange
- **Jump scares** — Films with a TMDB id can fetch human-timed fear markers from WhenJumpScare (on demand; nothing is bundled or mirrored)
- **VideoSkip lookup** — The download icon on every title opens the import panel; the VideoSkip tab shows each candidate file's author, filter description and download count, with a recommended pick
- **Bulk fetch** — Select titles and use **Fetch VideoSkip** to match the whole selection in one bounded run
- **Browse all titles** with scan status icons
- **Trigger scans** per title or entire library
- **"Scan Now"** to prioritize a title — moves it to the front of the scanning queue for immediate processing
- **Sort & filter** — filters by ratings and segment count; defaults to hiding ignored titles and sorting by date added (newest first)
- **Scan completion timestamps** — shows when each title finished scanning

#### Segments
- **Three-panel browser**: Library tree → Select title → Browse its segments
- **Live scanner banner** — shows which titles are currently being scanned with progress bars
- **Segment details** — Timestamp range, confidence score, and a thumbnail from the detected scene
- **Delete false positives** — Remove incorrectly flagged segments
- **Preview video** — In-app player to review the segment before deletion
- **Jump to segment** — Send Plex seek command to that spot in the video
- **Scan completion info** — Timestamp when the title finished scanning

#### Users
- **Toggle filtering per Plex account** — Turn skipping on/off for specific user accounts
- **Filter profiles** — Apply a Kids, Teens or Adults starter profile (per-category levels with profanity muted), then fine-tune
- **Category preferences** — Expand a user to set a 0–3 strictness level per content category, with an optional skip/mute override

#### Settings
- **Plex connection** — Server URL and authentication token
- **Scanner tuning** — Frame extraction interval, confidence threshold, parallel workers
- **Rating filter** — Only scan titles matching your selected content ratings (exact match, "Unrated" is explicit)
- **Detector labels** — Checkboxes to select which nudity types trigger skips (e.g., "female genitalia", "male genitalia", "breast", "butt", "anus")
- **Skip behavior** — Pre/post buffers around segments, scan window, segment merge gap, minimum hits per segment
- **Profanity wordlist** — Words the subtitle scanner mutes
- **Local scanning coverage** — NudeNet titles scanned, titles with ML segments, and titles still missing local coverage, overall and per library
- **VideoSkip coverage** — Title/segment counts overall and per library, plus the auto-fetch toggle, queue limits, live queue status, and pause/resume/retry/clear controls. The queue is off by default and processes one title at a time.

## Client Compatibility

The seek command is sent via the Plex Player Control API and works with most modern Plex clients:

| Client | Supported |
|---|---|
| Plex Web | ✅ Fully supported |
| Plex for iOS / Android | ✅ Fully supported |
| Plex HTPC | ✅ Fully supported |
| Plex Media Player (desktop) | ✅ Fully supported |
| Apple TV | ✅ Fully supported |
| Roku | ⚠️ Limited support |
| Some Smart TV apps | ⚠️ Limited support |

The Dashboard shows a **Controllable** badge per stream so you can see which clients support seeking.

**Remote Device Access:** The app proxies Plex images through its own server, so posters and artwork load correctly on remote clients (not just localhost).

## Scanner Queue & Priority

The scanner maintains two independent queues:

- **Normal queue** — Regular background scans of newly added titles, processed in order (FIFO)
- **Force queue** — High-priority scans from "Scan Now" clicks, always processed before normal queue items

When you click **"Scan Now"**:
1. Title is removed from the normal queue (if present) to avoid duplicate processing
2. Added to the force queue with the force-scan flag set in the database
3. A scanner worker picks it up immediately (workers check force queue first)
4. Scans regardless of the configured scan window time restrictions

## Ignored Titles

Titles can be marked as **ignored** to skip them during background scans:
- Ignored titles are still queued but the scanner immediately skips them
- You can still run a "Scan Now" on an ignored title — it will be scanned regardless
- The Library view defaults to hiding ignored titles (toggle with the "Show Ignored" checkbox)

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `CLEANPLEX_DATA` | `~/.cleanplex` | Data directory for the database, posters, thumbnails and models (`/data` in Docker) |
| `CLEANPLEX_PORT` | `7979` | Web UI port |
| `CLEANPLEX_HOST` | `0.0.0.0` | Bind address |
| `CLEANPLEX_VIDEOSKIP_URL` | `https://videoskip.herokuapp.com/exchange` | Exchange base URL; override for a mirror. `videoskip.org` currently loses deep links on redirect, so the application host is used directly |
| `CLEANPLEX_WHENJUMPSCARE_URL` | `https://www.whenjumpscare.com` | WhenJumpScare base URL; override for a proxy |
| `CLEANPLEX_AUTH_USER` | unset | Enables HTTP Basic auth for the UI/API when set with `CLEANPLEX_AUTH_PASS` |
| `CLEANPLEX_AUTH_PASS` | unset | Password for the optional Basic auth gate |
| `CLEANPLEX_WEBHOOK_TOKEN` | unset | Shared secret for `/api/webhooks/plex?token=...`. **Required when Basic auth is enabled** |
| `CLEANPLEX_CORS_ORIGINS` | unset | Comma-separated browser origins allowed to call the API. Unset means same-origin only |

## Troubleshooting

**NudeNet coverage stays at zero / every scan says "File not found"?**
- The Cleanplex process cannot see the media. In Docker, mount the media at the same
  absolute paths Plex reports (`-v /mnt/user/media/tv:/tv:ro`), then use
  **Settings → Local scanning → Retry failed scans**. Outside Docker, check that the
  service account can read the files and that the paths in Plex match the host.
- The Analytics page's **NudeNet Detections** section shows the failed count and the
  last scanner error, so this is visible without reading logs.

**Segments are detected but nothing is skipped?**
- Check the client. **Plex Web (a browser tab) cannot be controlled remotely** — it
  accepts the API request and ignores it. Cleanplex probes each active client with a
  harmless command and marks it "Not controllable" (with the probe error) when it
  refuses. Play through a Plex app on a TV, phone or tablet instead.
- In the Plex app, enable **Settings → Advanced → Advertise as player** so the server
  can send seek commands to it. Some mobile builds do not appear in Plex's client
  registry until this is on; Cleanplex probes unknown clients directly, so the
  Dashboard will say whether control works.
- Settings → Plex diagnostics shows `last_control_error` when a command was attempted
  and rejected; the Dashboard's recent skip events record `success: 0` for those.

**Posters not loading on remote devices?**
- The app automatically proxies all Plex images to work around localhost URL issues. Try hard-refreshing the browser (Ctrl+F5).

**"Scan Now" not prioritizing titles?**
- Run the latest version. Recent fixes ensure force-scanned titles are actually moved to the priority queue.

**Detector labels not filtering correctly?**
- Ensure the labels are selected in Settings → Detector Labels. Previously stored segments are filtered at API response time to respect your current settings.

**Some rated titles still appear in the library?**
- Check Settings → Content Ratings. The filter uses exact matching (e.g., "PG" ≠ "PG-13"). "Unrated" is an explicit checkbox option, not the same as empty rating.

**Active Streams is empty while something is playing?**
- Open `http://your-server:7979/api/sessions`. An `error` field means Cleanplex could
  not read Plex's `/status/sessions`; otherwise Plex itself reported zero sessions.
- Check `http://your-server:7979/api/settings/plex-diagnostics` for the server name,
  session snapshot and last error.
- Confirm the configured URL points at the Plex server that is actually playing (not
  plex.tv or another server), and that the token belongs to the **server owner**. A
  shared/managed-user token can browse libraries without seeing all sessions.
- Compare with Plex directly:
  `curl -s "http://plex-ip:32400/status/sessions?X-Plex-Token=TOKEN" | head -c 500`
- Plex's own streaming-service playback (free movies) is not a session on your server.

**VideoSkip says "No Exchange match"?**
- The Exchange is matched by IMDb ID when Plex provides one, then title + year (movies) or show + season/episode (TV). Re-sync the library after upgrading so season/episode and external IDs are stored. A `502` means the Exchange itself was unreachable; retry later or upload the `.skp` manually.

## Logo

| File | Use |
|---|---|
| `frontend/public/logo.png` | 1024×1024 tile, for docs and avatars |
| `frontend/public/logo-512.png` | 512×512 tile |
| `frontend/public/logo-256.png` | 256×256 tile, also the Apple touch icon |
| `frontend/public/logo-transparent.png` | 1024×1024 transparent mark |
| `frontend/public/favicon.svg` | Vector source and browser favicon |

## License

GNU General Public License v3.0 — see [LICENSE](LICENSE).
