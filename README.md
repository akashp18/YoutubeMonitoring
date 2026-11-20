# ShortsWatch — YouTube Shorts availability & playback monitor

A local QA/availability monitor for your own YouTube channel. It discovers the
public Shorts on a channel, visits each one as an ordinary signed-out viewer in
Playwright, plays a randomized 60–100 % share of every Short, classifies
explicit YouTube restriction messages, stores a full history and alerts you when
a Short changes state.

This is a monitoring tool only. It never likes, subscribes, comments, shares or
otherwise interacts with engagement controls, it never reloads videos to inflate
views, and it never attempts to bypass age gates, region blocks, sign-in walls,
CAPTCHAs or other access controls.

---

## 1. Architecture overview

```
Browser (vanilla JS dashboard, /static)
        │  JSON API
        ▼
FastAPI app (app/main.py, app/api.py)  ── SQLite (WAL, data/monitoring.db)
        │                                      ▲
        │  in-process asyncio scheduler        │
        ▼                                      │
MonitoringScheduler ──► MonitoringRunner ──────┘
                            │
        ┌───────────────────┼───────────────────────────┐
        ▼                   ▼                           ▼
 ChannelResolver     ShortsDiscoveryService     YouTubeBrowserMonitor
 (HTTP → browser)    (Data API → browser →HTML)  ├─ RestrictionDetectionService
                                                 ├─ PlaybackVerificationService
                                                 └─ PlaywrightPageAdapter
                            │
                            ▼
              MonitoringHistoryService (results + evidence)
                            │
                            ▼
              StatusChangeDetector → Alerts → NotificationService
```

There was no existing application in the workspace, so the project was built as
one Python service rather than a separate frontend/backend pair. There is no
existing job scheduler, so the scheduler is a small asyncio loop on top of
durable SQLite state (no second scheduling framework was added). There is no
authentication layer because the app is a single-user local tool bound to
loopback.

### Services (all in `app/services/`)

| Service | Responsibility |
| --- | --- |
| `ChannelResolver` | Validates/normalises a channel URL, resolves it to a `UC…` channel id via HTML metadata with a rendered-page fallback. |
| `ShortsDiscoveryService` | Discovers Shorts using the YouTube Data API when configured, otherwise a scrolling Playwright session, otherwise first-page HTML. |
| `YouTubeBrowserMonitor` | One clean viewer visit per Short: navigation, settle, classification, playback check, evidence. |
| `PlaybackVerificationService` | Waits for `readyState`, checks dimensions, plays a random 60–100 % share of the Short with a real click, verifies the coverage target was reached and detects stalls/media errors. |
| `RestrictionDetectionService` | Classifies page/player signals into PASS/WARNING/RESTRICTED/FAILED/INCONCLUSIVE and a restriction type. |
| `MonitoringScheduler` | Due-channel evaluation, one job at a time, pause/resume, cancellation, restart recovery. |
| `StatusChangeDetector` | Compares consecutive results and emits meaningful change events. |
| `NotificationService` | In-app alerts plus optional webhook (generic/Slack/Discord) and SMTP delivery. |
| `MonitoringHistoryService` | Persists results, diagnostic evidence and retention cleanup. |

---

## 2. Setup

Requirements: Python 3.11+, internet access for YouTube, Chromium installed by
Playwright.

```bash
# from the project root
py -3.11 -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe -m playwright install chromium
cp .env.example .env          # optional, edit what you need
.venv/Scripts/python.exe run.py
```

Open <http://127.0.0.1:8000>. The dashboard is empty until you add a channel.

On macOS/Linux use `.venv/bin/python` instead of `.venv/Scripts/python.exe`.

### Environment variables (`.env`, never committed)

| Variable | Required | Purpose |
| --- | --- | --- |
| `YOUTUBE_API_KEY` | No | Enables official Data API discovery (recommended). |
| `YT_MON_HOST` | No | Bind address, default `127.0.0.1`. |
| `YT_MON_PORT` | No | Port, default `8000`. |
| `YT_MON_DATA_DIR` / `YT_MON_DB_PATH` | No | SQLite location. |
| `YT_MON_EVIDENCE_DIR` | No | Screenshot/evidence directory. |
| `YT_MON_SMTP_HOST`, `_PORT`, `_USERNAME`, `_PASSWORD`, `_FROM`, `_STARTTLS` | No | Optional e-mail notifications. Credentials stay out of the database. |

Runtime preferences (timeouts, concurrency, notification webhook, retention)
live in **Settings** in the dashboard and in the `app_settings` table.

---

## 3. What the monitor does

### Channel configuration

- Supported URLs: `youtube.com/@handle`, `youtube.com/channel/UC…`,
  `youtube.com/c/name`, `youtube.com/user/name` and legacy `/name`.
- The URL is validated before saving; the channel is resolved to its stable
  channel id. You can supply the channel id yourself when YouTube blocks
  automatic metadata resolution.
- Multiple channels per installation, each with its own interval, maximum
  Shorts per run and new-Short notification toggle.
- Intervals: 15 min, 30 min, 1 h, 6 h, 12 h, daily, or manual only.
- **Continuous cycles (default):** when a cycle finishes, the next one starts
  within about 10 seconds without waiting for the interval. The interval is used
  only when continuous mode is off or a cycle fails. Shorts are checked in a
  fresh random order each cycle; every eligible Short is included when it fits
  within **Max Shorts per run**, otherwise the highest-priority Shorts are
  selected first and then shuffled.

### Discovery

1. **YouTube Data API** (if a key is configured): uploads playlist, paged,
   filtered to ≤ 180 s, then each candidate is probed at `/shorts/<id>` so a
   regular ≤ 3-minute upload is not mistaken for a Short.
2. **Playwright**: opens the channel `/shorts` tab, scrolls until the grid stops
   growing or the configured scroll limit is reached, and harvests items from
   `ytInitialData`, intercepted innertube JSON and DOM links. Truncation is
   recorded so a partial listing can never be treated as "these Shorts were
   deleted".
3. **HTML fallback**: reads the first page only and is flagged as truncated.

New videos are inserted once (unique `channel_id, video_id`) and marked listed;
previously known videos missing from a *complete* listing are marked delisted.
A listing that shrinks by more than half is treated as a truncation, not as mass
deletion.

### Availability check (per Short)

1. Open `https://www.youtube.com/shorts/<id>?hl=en` once in a reused, signed-out
   browser context. No reloads inside a check.
2. Wait for the page to settle (player ready, explicit error, challenge or
   timeout).
3. Read `ytInitialPlayerResponse.playabilityStatus`, player error containers,
   page title and — only when no playable video exists — page text. That
   ordering prevents a *playing* Short from being labelled unavailable because of
   stale text elsewhere on the page.
4. If accessible: play each Short muted for a random share of its duration,
   between 60 % and 100 % by default (configurable). The player must cover that
   target for the check to pass; a Short that cannot reach it within a bounded
   window is `WARNING`, not `PASS`. Stalls, media errors and premature pauses
   fail the check. `video.play()` is used after a real click, so an autoplay
   policy is reported as `WARNING`/`NOT_VERIFIED`, never as a video restriction.
5. Persist the result, then capture evidence for non-PASS checks.

### Result categories

| Status | Meaning |
| --- | --- |
| `PASS` | Accessible and the random coverage target (60–100 %) was played. |
| `WARNING` | Accessible, but the coverage target was not reached (autoplay policy, timeout). |
| `RESTRICTED` | Explicit YouTube restriction/unavailability message. |
| `FAILED` | Page load or playback failed (media error, stall, network). |
| `INCONCLUSIVE` | CAPTCHA, consent wall, rate limit, no player and no message. |

Restriction types: `UNAVAILABLE`, `REMOVED`, `PRIVATE`, `AGE_RESTRICTED`,
`REGION_BLOCKED`, `COPYRIGHT`, `COMMUNITY_GUIDELINES`, `SIGN_IN_REQUIRED`,
`CAPTCHA`, `CONSENT_WALL`, `PLAYBACK_ERROR`, `UNKNOWN`.

Playback statuses: `VERIFIED`, `NOT_VERIFIED`, `FAILED`, `SKIPPED`.

### Scheduling and recovery

- One run at a time; a per-channel open run and a per-video re-check are also
  rejected while queued/running.
- Queue and progress are stored in `monitoring_runs`; `next_run_at` is durable.
- On startup, RUNNING runs become `INTERRUPTED` with their partial results kept,
  queued runs resume, and missing `next_run_at` values are recomputed from the
  last run.
- Pause stops new runs; the current run finishes. Resume restarts scheduling.
- Cancel is cooperative between videos and also cancels the active task.
- A failed run never stops the scheduler; repeated failures raise an alert after
  the configured threshold.

### Change detection, alerts and notifications

Alerts are generated only for meaningful changes: accessible → restricted
(`BECAME_RESTRICTED` / `BECAME_UNAVAILABLE`), accessible → playback failing
(`PLAYBACK_FAILING`), previously restricted/failed → accessible (`RECOVERED`),
new Short (`NEW_SHORT`), disappeared from the listing (`DELISTED`), significant
playback-start regression (`PLAYBACK_PERFORMANCE`) and repeated run failures
(`RUN_FAILURES`). `INCONCLUSIVE` never triggers a restriction alert.

Each open issue has one alert per Short and type; repeats are suppressed until
the issue resolves. New-Short and delisted alerts fire once. Notification
delivery is optional: generic JSON webhook, Slack, Discord, and/or SMTP e-mail.
If nothing external is configured, alerts remain visible in the dashboard and
are marked `SKIPPED`, never silently lost.

---

## 4. Database

Migrations live in `app/migrations/` and are applied once each by
`Database.migrate()` (`schema_migrations` table).

`001_initial.sql` creates:

- `youtube_channels` — URL, channel id/name/handle, monitoring settings,
  resolution state, next/last run, consecutive failures.
- `youtube_shorts` — one row per discovered video (`UNIQUE(channel_id, video_id)`),
  listing state, first/last discovery, materialised latest status.
- `monitoring_runs` — trigger, scope, status, timings, discovery and result
  counters, error message.
- `run_logs` — per-run log lines shown in the UI.
- `video_monitoring_results` — availability, playback status and coverage completion,
  restriction, diagnostics, playback start time, watched time, video duration,
  readyState, dimensions, timings, evidence link.
- `monitoring_evidence` — console/page errors, failed requests, video state,
  YouTube signals, screenshot path, browser version, expiry.
- `monitoring_alerts` — dedupe key, previous/current status, severity,
  notification status, acknowledgement/resolution.
- `app_settings` — validated key/value preferences.

Indexes cover channel id, video id, listing state, availability status, check
timestamp, run id, evidence expiry and a partial unique index on open alert
dedupe keys.

---

## 5. API endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/health` | Liveness, database, browser/API availability. |
| GET | `/api/overview` | Dashboard counters, last run, next run, scheduler, alerts. |
| GET | `/api/channels` | Channels with Short counts. |
| POST | `/api/channels` | Validate + resolve + save a channel. |
| PATCH | `/api/channels/{id}` | Update URL or monitoring settings. |
| DELETE | `/api/channels/{id}` | Remove a channel and its history. |
| POST | `/api/channels/validate` | Validate/resolve without saving. |
| POST | `/api/channels/{id}/resolve` | Re-resolve metadata. |
| POST | `/api/channels/{id}/run` | Queue a manual run. |
| GET | `/api/shorts` | Filter/search/sort/paginate Shorts. |
| GET | `/api/shorts/{id}` | Short + latest/previous result + history + alerts + evidence. |
| POST | `/api/shorts/{id}/check` | Queue a single-video re-check. |
| GET | `/api/runs`, `/api/runs/{id}` | Run list/detail with results and logs. |
| POST | `/api/runs/{id}/cancel` | Cancel a queued/running job. |
| GET | `/api/alerts` | Open/resolved/all alerts. |
| POST | `/api/alerts/{id}/acknowledge`, `/resolve` | Alert lifecycle. |
| GET | `/api/evidence/{id}`, `/api/evidence/{id}/screenshot` | Diagnostics and screenshot. |
| GET/PUT | `/api/settings` | Preferences (webhook value is masked on read). |
| POST | `/api/settings/test-notification`, `/cleanup-evidence` | Delivery test; retention cleanup. |
| GET | `/api/scheduler` | Pause state, active run, next due, queue. |
| POST | `/api/scheduler/pause`, `/resume`, `/run-due` | Scheduler controls. |

Interactive OpenAPI docs: <http://127.0.0.1:8000/docs>.

---

## 6. Dashboard

Single-page UI (`app/static/`) with six views:

- **Overview** — totals, accessible/restricted/failed/inconclusive/warning/new/
  delisted counts, last-run details, next scheduled run, 24 h check stats,
  configuration health and recent alerts.
- **Shorts** — thumbnail/title/link/id, availability and playback status,
  playback initialisation time, last check and last status change; filters (all,
  accessible, restricted, failed, inconclusive, newly published, recently
  changed, not checked, delisted), title/ID search, channel filter, sorting and
  pagination. Row actions: open on YouTube, queue a fresh check, history, and
  diagnostics.
- **Runs** — every job with counters, duration, per-video results and run log.
- **Channels** — add/edit/delete/re-resolve channels and start a run now.
- **Alerts** — deduplicated alerts with acknowledge/resolve.
- **Settings** — browser, monitoring, notifications and evidence preferences,
  test notification, cleanup.

Diagnostic evidence is shown in a drawer: screenshot, YouTube signals, console
errors, page errors, failed requests and video element state.

---

## 7. Tests

```bash
.venv/Scripts/python.exe -m pytest
```

Current result: **132 passed, 2 skipped** (the two skips are the opt-in live
YouTube tests). Coverage includes channel URL validation, resolution and its
error codes, Shorts parsing/dedup/pagination, API + browser + HTML discovery,
truncation and disappearance guards, every restriction category, CAPTCHA and
consent handling, randomised coverage playback, stall/media error/autoplay/timeouts,
navigation failures and retries, scheduler due/pause/overlap/cancel/recovery,
change detection, alert deduplication, notification delivery/failure, result and
evidence retention, incremental candidate selection, end-to-end runs, and the
JSON API including static dashboard delivery.

All browser tests use deterministic fake pages (`tests/fakes.py`); no test
depends on YouTube views or live traffic.

Live integration tests (public pages only, no engagement):

```bash
YT_LIVE_TESTS=1 YT_LIVE_CHANNEL_URL=https://www.youtube.com/@yourchannel \
  .venv/Scripts/python.exe -m pytest tests/test_live_youtube.py -v
```

---

## 8. Files

```
app/
  main.py              FastAPI wiring + lifespan
  api.py               JSON endpoints
  config.py            environment/.env loading
  db.py                SQLite helper + migration runner
  domain.py            status vocabulary
  repositories.py      data access (channels, shorts, runs, results, alerts, evidence)
  settings_store.py    validated settings with defaults
  timeutil.py          UTC helpers
  migrations/001_initial.sql
  migrations/002_full_playback.sql
  services/
    channel_url.py           URL parsing/validation
    channel_resolver.py      channel metadata resolution
    youtube_api.py           optional Data API client
    youtube_parse.py         renderer/JSON parsing
    shorts_discovery.py      discovery strategies
    browser.py               Playwright lifecycle, page adapter, discovery session
    page_scripts.py          injected probes and playback scripts
    browser_monitor.py       per-Short check orchestration
    playback_verification.py playback sampling and stall detection
    restriction_detection.py signal classification
    history.py               result/evidence persistence
    change_detection.py      transitions and performance regressions
    notifications.py         webhook/SMTP delivery
    scheduler.py             due-channel scheduling, pause/cancel/recovery
    runner.py                one run end to end
  static/                index.html, app.js, styles.css, favicon.svg
tests/                   deterministic test suite + live tests (opt-in)
run.py                   entry point
requirements.txt, pytest.ini, .env.example
```

---

## 9. Known limitations

- **YouTube blocking.** The monitor does not evade bot checks. If YouTube serves
  a CAPTCHA, consent wall or rate limit, the check is stored as `INCONCLUSIVE`
  with evidence and retried on the next schedule. A residential connection is
  usually more reliable than a datacenter IP.
- **Localised messages.** Detection is based on English YouTube wording
  (`hl=en` is requested and the locale defaults to `en-US`). Other locales can
  fall back to INCONCLUSIVE.
- **Shorts identification via the Data API** is duration-based (≤ 180 s) plus a
  `/shorts/` URL probe. The Playwright Shorts-tab strategy is the most precise.
- **Playback is real playback.** Each check watches 60–100 % of a Short, so
  YouTube can count it as a view and a full cycle takes roughly 60–100 % of the
  channel's total Shorts duration. Continuous mode chains cycles back to back;
  pause the scheduler (or turn continuous mode off in Settings) to stop after
  the current cycle. A fallback window covers players that never report a
  duration.
- **No authentication.** The server binds to loopback and assumes a single
  trusted user. Do not expose it directly to the internet.
- **One scheduler per process.** Multiple uvicorn workers would each schedule
  jobs; run a single process (the provided `run.py` does).
- **Browser settings apply to new pages**, but the headless toggle and user agent
  take effect after restarting the app (the browser is reused).
- **Delisting is inferred.** A Short missing from the listing is marked delisted,
  not necessarily deleted; the alert says so. Listings that are truncated or
  shrink drastically skip disappearance detection deliberately.
- **Evidence retention** deletes screenshots and evidence rows after the
  configured period; old results keep their diagnostic text but lose the image.
- **Screenshots and evidence are local files.** They are not uploaded anywhere.

---

## 10. License

MIT — see [LICENSE](LICENSE).
