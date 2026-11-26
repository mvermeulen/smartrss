# User-Test Review — Milestones `1a`–`1c` on a Live Deployment

- **Date**: 2026-11-26 (opened 02:40 MST; soak check-ins 03:51 and 07:39 MST; observation still in progress)
- **Scope**: `main` at `87bba29` (Merge PR #2, `milestone/1c`), deployed with `make up` / `make migrate` on a single Ubuntu 26.04 host. Five live feeds, worker in `WORKER_MODE=cron` driven by a host crontab every 15 minutes, a UI paired through the localhost runner (`make install-client`, `http://localhost:4180`), and the LAN `web` container (`http://localhost:8080`). This covers the running system, the setup scripts and README, and the SSRF HTTP stack. It is not a full code re-review.
- **Checked against**: [`1a`](../design/1a-backend-ingestion-and-security.md), [`1c`](../design/1c-client-ui-and-offline-shell.md), [ADR 0006](../decisions/0006-lan-security-ssrf-and-content-isolation.md), [`README.md`](../../README.md), [`review-checklist.md`](../review-checklist.md)
- **Reviewer**: Claude Code (at the project owner's request)
- **Finding prefix**: `E-`
- **Status**: `Open` — more findings may be appended as the soak test continues (see §4)

## Review outcome

The stack came up cleanly from a fresh clone, and the core pipeline works end to end on real feeds:

- **`make check` is green** on Python 3.12.16 / Node 22: 167 backend tests at 91.1% coverage, the critical-module ≥ 90% gate, the runner at 94.9%, and the frontend gates.
- **Ingestion.** The first `run-once` polled all 5 feeds, ingested 112 items, and extracted 48 articles in about 56 s. The backfill cap behaved as specified (`FR-FEED-05`, `1a` §5): exactly 10 newest items per feed are `unread` and the rest are archived as `expired` with `expired_at = fetched_at`.
- **Cron mode.** `docker compose run --rm -T worker smartrss worker run-once --json` from the host crontab works. The first scheduled run (02:30) exited `0` with nothing due.
- **Sync.** After pairing in the browser: bootstrap, a pull every 60 s, and two `item_set_state` pushes were applied (`sync_mutations.status = applied`). The client cursor advanced from 172 to 173 to 174, matching `sync_state.current_seq`.

The findings so far:

- **one Major**, reproduced in the running `api` container: `SafeHttpClient`'s per-IP connect fallback is dead code, and raw `httpcore` exceptions escape the client unmapped (`E-01`). The boundary tests miss it because their fake backend raises `OSError`, which the real backend never does.
- an unpinned Python version that makes the whole suite error on a host with Python 3.14 (`E-02`);
- a prerequisite check that passes without Docker daemon access (`E-03`);
- README drift in the client-install section (`E-04`);
- a cron-mode documentation nit (`E-05`);
- a misleading `items_polled` counter name (`E-06`);
- a CORS preflight on every API call from the client (`E-07`);
- an **owner-requested** change: record `304 Not Modified` polls separately (`E-08`);
- **owner feedback** asking for a one-command first-time server setup (`E-09`). This is future design work and doesn't block `1c`;
- feed URL validation that accepted a pasted Markdown link (`E-10`), caught live;
- **owner feedback** on feed-list export (`E-11`, amended by the owner): the UI export is fine. Only optional gaps remain: a headless CLI export and an OPML copy alongside backups.

**Soak check-in (03:51, about 95 minutes in):** six cron runs at 15-minute intervals, all exiting `0` or `2` (`partial`). Every feed was re-polled within one cron tick of its `next_fetch_at`. One Hacker News poll failed live through the `E-01` path, was retried, and succeeded on the next run. All feeds are `healthy` with `consecutive_errors = 0`. No duplicate items by `(feed_id, guid)` or `canonical_url`. 0 dead-lettered jobs. The API logged no 5xx responses or tracebacks across 71 pulls, 53 pushes, and the System Health / Settings views.

**Soak check-in 2 (07:39, about 5 h 10 min in, 10 feeds):** 21 cron runs: 19 `completed`, 2 `partial`. Both partial runs were Hacker News polls failing through the `E-01` unhandled path (03:30 `ConnectError` at TLS, 05:15 `ConnectTimeout`), and both recovered on retry. All 10 feeds are `healthy` with `consecutive_errors = 0`. 0 duplicates by `(feed_id, guid)`, `canonical_url`, or `(feed_id, content_hash)`. 0 non-`done` jobs and 0 dead letters. API: 350 pulls, 101 pushes, 5 feed creates, 2 PATCHes, 2 OPML exports and 1 search, with 0 5xx responses and 0 tracebacks. The client cursor (`since_seq=1390`) equals `sync_state.current_seq`. Poll latency p50 is 185–750 ms for every feed except Hacker News (p50 8.9 s, max 10.1 s, 2 of 7 failed). Diagnosis in the `api` container: `hnrss.org` has a single A record (TCP connect 58 ms), and a cold fetch took 2.6 s against about 0.1 s warm, so the publisher is slow to generate the feed. These are genuine transient failures, not a failover case, but they show how often `E-01`'s misclassification happens in practice: 2 of the 2 poll failures seen so far.

**Recommendation:** fix `E-01` with regression tests that use the real `httpcore` backend's exception types. `E-02`–`E-04` are small script and doc fixes and should go in the same pass. `E-05`–`E-07` are optional. `E-08` needs the `data-model.md` §4.9 update before code; it can share a pass with `E-06`. `E-09` needs a requirement and a component design first; the owner should choose which milestone carries it.

| Severity | Findings |
| :--- | :--- |
| Blocker | — |
| Major | `E-01` |
| Minor | `E-02`, `E-03`, `E-04`, `E-10`, `E-08` (owner-requested), `E-09` (owner-requested, future design) |
| Nit | `E-05`, `E-06`, `E-07`, `E-11` (owner feedback, amended) |

---

## 1. Major

### E-01 — `SafeNetworkBackend` never fails over to the next validated IP, and `SafeTransport` leaks raw `httpcore` exceptions (checklist: mock at the real boundary)

**Where:**

- `backend/src/smartrss/security/safe_http.py:236-249`: `SafeNetworkBackend.connect_tcp` loops over the validated IPs and catches only `OSError`.
- `backend/src/smartrss/security/safe_http.py:306`: `SafeTransport.handle_async_request` calls `self._pool.handle_async_request()` with no exception mapping.
- `backend/src/smartrss/worker/stages/poll_feed.py:357` and `backend/src/smartrss/worker/stages/extract_content.py:176` catch `SafeHttpError | httpx.HTTPError | TimeoutError | OSError`.
- `backend/tests/boundary/test_safe_http_boundary.py:349-400`: the failover tests use a fake inner backend that raises `OSError("connection refused")`.

**What happens:** `httpcore.AnyIOBackend.connect_tcp` (the production inner backend) wraps socket errors in `map_exceptions({TimeoutError: httpcore.ConnectTimeout, OSError: httpcore.ConnectError, …})`. It therefore raises `httpcore.ConnectError` / `httpcore.ConnectTimeout`, never `OSError`. As a result:

1. **No failover.** The first unreachable address raises out of the loop immediately. The remaining validated IPs are never tried, and the `SSRFConnectionError` at line 247 is unreachable in production.
2. **Unmapped exceptions.** `httpx.HTTPTransport` normally converts `httpcore.*` into `httpx.*` (`map_httpcore_exceptions`), but `SafeTransport` replaces that transport and does not do this. `httpcore.ConnectError`, `ConnectTimeout`, `ReadTimeout`, `RemoteProtocolError` and similar errors therefore escape `SafeHttpClient.fetch()`. They are not `SafeHttpError`, `httpx.HTTPError` or `OSError`, so the typed handlers in `poll_feed` and `extract_content` miss them. They then go to the worker's catch-all (`runner.py:249`, `logger.exception("Unhandled error in job …")`), which logs a full ERROR traceback. In `--json` mode, logging isn't configured, so the traceback goes straight to stderr and ends up mixed into the cron log. Job retry and feed-error recording still happen through the catch-all hooks, so health and backoff are not lost.

**Reproduction** (in the running `api` container; the container has no IPv6 address apart from loopback):

```python
# resolver returns [unreachable IPv6, lwn.net's real IPv4], like a dual-stack host whose AAAA sorts first
policy = SSRFPolicy(resolver=lambda h, p: [ip("2001:4860:4860::8888"), ip(real_v4)])
await SafeHttpClient(policy).fetch("https://lwn.net/headlines/rss")
# → UNMAPPED: httpcore.ConnectError All connection attempts failed
# Control (IPv4 only): 200
```

This was also seen live: the first `run-once` printed `httpcore.ConnectError: All connection attempts failed` before its JSON summary. That was an `extract_content` job for `news.ycombinator.com/item?id=50649864`, which succeeded on attempt 2.

**Second live occurrence (05:15 cron run):** `poll_feed` job 146 (Hacker News): `PollFeedUnexpectedError: ConnectTimeout` ← `httpcore.ConnectTimeout`, again through the unhandled path, and successful on retry. Connect *timeouts* are affected as well as refusals, so the mapping has to cover `httpcore.TimeoutException` subclasses.

**Seen again on the poll path (soak, 03:30 cron run):** `poll_feed` job 72 (Hacker News) failed with `Unhandled error in job 72 (poll_feed)` and a full traceback in `worker.log`. The cause was `PollFeedUnexpectedError: ConnectError` ← `httpcore.ConnectError` raised from `httpcore/_backends/anyio.py:67 start_tls`, through `safe_http.py:306` and `poll_feed.py:352`. This one came from the **TLS handshake** (`start_tls`), after `connect_tcp` had returned. So the per-IP failover fix alone doesn't cover it: the exception mapping in `SafeTransport` is needed too. The run ended `partial`. The job was retried at 03:45 and succeeded, and the feed is back to `healthy` / `consecutive_errors = 0`. A transient TLS failure belongs on the typed `_record_feed_poll_error` path, not the unhandled-crash path.

**Impact:** a feed or article host with several A/AAAA records fails whenever its first address is unreachable. Examples are a dual-stack host when the Docker bridge has no IPv6, or one dead node in a round-robin. Those feeds degrade instead of working. glibc's RFC 6724 ordering usually puts IPv4 first when there is no IPv6 source address, which hides the problem most of the time. Transient network errors are also logged as unhandled crashes.

**Suggested fix:**

- In `connect_tcp`, catch `(OSError, httpcore.ConnectError, httpcore.ConnectTimeout)` per IP and continue. Keep `SSRFConnectionError` as the final error, chained from the last exception.
- In `SafeTransport.handle_async_request`, and in `_PinnedStream` iteration/close, map `httpcore` exceptions the way httpx does. Either reuse httpx's mapping, or translate them into `SafeHttpError` subclasses such as `SSRFConnectionError` or a new `SafeHttpTimeoutError`, so that `fetch()` only raises the documented hierarchy.
- **Regression tests** (each must fail with the fix reverted, §2.3):
  - (a) boundary failover where the fake inner backend raises **`httpcore.ConnectError`** for the first IP. Better still, wrap the real `AnyIOBackend` and point it at a closed local port with a test-only permissive policy. Assert that the second IP is used.
  - (b) an all-IPs-fail test asserting `SSRFConnectionError` with the real exception type;
  - (c) a `fetch()` test asserting that a `ReadTimeout` / `RemoteProtocolError` from the pool comes out as a `SafeHttpError` subclass;
  - (d) a `poll_feed` / `extract_content` test showing that a connect failure takes the typed path (`_record_feed_poll_error` / `TransientExtractionError`) and not the "Unhandled error" path.
- **Checklist:** add a failure class saying that fakes at a library boundary must raise the exception types the real dependency raises.

---

## 2. Minor

### E-02 — No Python version pin, so on a host with Python 3.14 every backend test errors

**Where:** `backend/pyproject.toml` (`requires-python = ">=3.12"`, `filterwarnings = ["error", …]`). There is no `backend/.python-version`. `scripts/check-prereqs.sh:44-55` only checks `python3 >= 3.12`.

**What happens:** Ubuntu 26.04 ships Python 3.14. A plain `uv sync` / `uv run` picks the host interpreter. Under 3.14, pytest-asyncio calls `asyncio.get_event_loop_policy()`, which is deprecated in 3.14, and `filterwarnings = error` makes that fatal:

```
$ uv run --python 3.14 --directory backend pytest tests/unit tests/boundary
E   DeprecationWarning: 'asyncio.get_event_loop_policy' is deprecated and slated for removal in Python 3.16
107 errors in 1.71s
```

The production image is pinned to `python:3.12-slim`, so this only affects developer hosts. It does mean the first `make check` on a stock 26.04 machine fails in a confusing way. In this session it was avoided only by forcing `uv sync --python 3.12`.

**Suggested fix:** add `backend/.python-version` containing `3.12`, which `uv` honours and installs on demand. Optionally bound `requires-python = ">=3.12,<3.14"` until the suite is clean on 3.14. Longer term, upgrade pytest-asyncio or set a targeted `ignore::DeprecationWarning:pytest_asyncio` filter. Update `check-prereqs.sh` / the README to say that `uv` provisions the interpreter.

### E-03 — `check-prereqs.sh` passes without Docker daemon access

**Where:** `scripts/check-prereqs.sh:67-74` (`check_docker`) checks only `command -v docker` and `docker compose version`. Both succeed without daemon access.

**What happens:** right after `apt install docker.io`, the user isn't in the `docker` group yet, or the session predates the group change. `make check` then passes the prereq step and fails later in `db-test-up` with `permission denied while trying to connect to the docker API at unix:///var/run/docker.sock`. On Ubuntu 26.04, `sg` is gone and `newgrp` is only in `util-linux-extra`, which isn't installed by default, so the usual workaround isn't available either. The user had to reboot.

**Suggested fix:** in `check_docker`, also run `docker info >/dev/null 2>&1`. If it fails, print a hint such as: `sudo usermod -aG docker $USER`, then log out and in (or `sudo apt install util-linux-extra && newgrp docker`). Add a test case to the prereq script's test, if there is one.

### E-04 — README client-install section has drifted from `install-client.sh`

**Where:** `README.md:5`, `:109`, `:111`, `:122`

| README says | Actual |
| :--- | :--- |
| `make install-client INSTALL_CLIENT_ARGS="--api-origin http://<server-lan-ip>:8080"` (`:109`) | `scripts/install-client.sh` rejects the flag: `Unknown option: --api-origin`, exit `2` (reproduced). The origin is set with `SMARTRSS_API_ORIGIN` in `~/.config/smartrss-client/config.env` (`install-client.sh:111-116`). |
| Installs into `~/.local/share/smartrss-client/` with an "atomic symlink switch" (`:111`) | Installs into `~/.local/share/smartrss/` (`install-client.sh:11`) using `mv` directory swaps. There is no symlink. |
| "Node.js 20+" (`:122`) | `check-prereqs.sh:57-66` requires Node ≥ 22. |
| `1c` is "In Implementation Review" (`:5`) | `1c` is `Implementation Approved` (2026-11-25). |

**Impact:** anyone installing a client on a second machine, the main use of that command, fails on the first command. They then edit the wrong config path or look in the wrong directory.

**Suggested fix (`E-04`):** either implement `--api-origin` in `install-client.sh` (write or replace `SMARTRSS_API_ORIGIN` in `config.env`, validated like the runner does) or change the README to "edit `config.env`, then `systemctl --user restart smartrss-client`". Fix the path, the swap description, the Node version, and the status line. Per §4.2, include this in the same commit as any script change.

### E-10 — Feed URL validation accepts a pasted Markdown link

**Where:** `backend/src/smartrss/api/routes/feeds.py:84-88` (`_validate_feed_url`): it strips whitespace, then checks only the scheme, host and port (`policy.validate_url`) and resolves the host. The path, query and fragment aren't checked. This is used by `POST /api/v1/feeds`, `PATCH /api/v1/feeds/{id}` (feed URL edits), and presumably OPML import.

**What happened (soak, 04:27):** the owner added Latent Space in the UI by pasting a Markdown link. The API returned `201` and stored:

```
feed_url = https://www.latent.space/feed](https://www.latent.space/feed
```

The owner noticed it and corrected it with a `PATCH` at 04:29:56 (`field_updated_at['feed_url']` stamped) before the 04:30 cron run, so the bad URL was never polled. If it had been, the server would have requested the literal path `/feed](https://www.latent.space/feed`. The likely result is a 404 or HTML response, `consecutive_errors` going up, and the feed ending up `degraded` / `broken` with an unclear error message.

**Impact:** copy and paste from Markdown, chat or note apps is a normal way to add feeds. `]`, `(`, `)` and unescaped spaces make a URL invalid by RFC 3986 (a `]` is only allowed in an IPv6 host literal), and a second `scheme://` inside the path is almost always a paste error.

**Suggested fix:**

- Reject, with `422` and a clear message, any URL whose path, query or fragment contains unescaped characters that RFC 3986 doesn't allow (space, `<>"{}|\^\``, and `[` / `]` outside the host). Also reject a URL with an embedded second `http(s)://`.
- Optionally, in the UI only, pull the URL out of an obvious Markdown `[text](url)` or `<url>` paste before submitting, and show the cleaned value.
- Apply the same check on all three paths (create, PATCH, OPML import) and keep it shared with `SSRFPolicy.validate_url` so they can't drift apart.
- **Regression tests:** an API test for each path where the exact soak string gets `422`; a positive test for valid percent-encoded paths (`%5D`) and query strings; and a frontend test for the paste clean-up if it's added.

---

## 3. Nit

### E-05 — The cron-mode example is minimal

**Where:** `README.md` "Worker Execution Modes".

The example `docker compose run --rm worker smartrss worker run-once --stage all --json` works, but there are gaps for an unattended crontab:

- it has no `-T`, which matters with no TTY under cron;
- it has no log redirect;
- `--max-duration` defaults to `1800` s, longer than a typical 15-minute cadence. Overlap is safe because of leases and advisory locks, but stacked containers are wasteful.

The line used for this test is:

```cron
*/15 * * * * cd /path/to/smartrss && /usr/bin/flock -n ~/.local/state/smartrss/worker.lock /usr/bin/docker compose run --rm -T worker smartrss worker run-once --stage all --max-duration 840 --json >> ~/.local/state/smartrss/worker.log 2>&1
```

**Suggested fix:** document a complete line like this one (or a `deploy/systemd/smartrss-worker.{service,timer}` pair, like the backup units). Note that `--json` mode leaves logging unconfigured, so stderr warnings and tracebacks are mixed in with the JSON lines (see `E-01`).

### E-06 — `items_polled` counts items ingested, not feeds polled

**Where:** `backend/src/smartrss/worker/runner.py:176` (`counters.items_polled += outcome.items_ingested`); `pipeline_runs.items_polled` (`data-model.md` §4, no definition given); the CLI text summary (`cli.py:219`, `polled=…`).

**What happens:** the 03:15 cron run polled two feeds (LWN, Simon Willison; both `stage_metrics` rows `ok`) but reported `"items_polled": 0`, because neither feed had new entries. Someone reading `worker.log` or `polled=0` naturally concludes that no feeds were polled. That is how this review first misread it. Nothing in the summary counts feed polls.

**Suggested fix:** define the field in `data-model.md` ("new items ingested by `poll_feed`"). Preferably rename it to `items_ingested` (keeping `items_polled` as a JSON alias for one release) and add `feeds_polled`, plus `feeds_not_modified` once the 304 outcome is recorded (see §3a). Update the CLI text summary to match.

### E-07 — Every client API call sends a CORS preflight despite `Access-Control-Max-Age: 600`

**Where:** `frontend/src/api/client.ts:110` and `:201` (`cache: "no-store"` on every request); the API's CORS config in `backend/src/smartrss/api/app.py:78`.

**What happens:** over about 90 minutes of UI use, the API logged 72 `OPTIONS /api/v1/sync/pull` for 71 `GET`s and 53 `OPTIONS /api/v1/sync/push` for 53 `POST`s. The push URL is constant, and the preflight response carries `access-control-max-age: 600`, so the browser's preflight cache is not being used. The likely cause is `cache: "no-store"`: Chromium skips its preflight cache for requests with cache-disabling load flags. That has not been confirmed per browser. The impact is only twice the request count and latency on the LAN (each cycle is one extra round trip), but it adds up over many devices and long soak periods.

**Suggested fix:** confirm the cause in the owner's browser (DevTools → Network, with "Disable cache" off). If confirmed, keep `no-store` semantics by having the API send `Cache-Control: no-store` on responses, and drop the request-side `cache: "no-store"` for authenticated JSON calls. Alternatively, accept the extra round trip and record it as a residual risk. If the client changes, add a vitest assertion on the fetch options.

---

## 3b. Owner-requested changes

### E-08 — Record `304 Not Modified` polls separately from full fetches (owner decision, 2026-11-26)

**Severity:** Minor (an observability gap, now owner-requested; tracked like a finding).

**Where:** `backend/src/smartrss/worker/stages/poll_feed.py:379` (the 304 branch) and `:479` both return `PollFeedOutcome(metric_status="ok")`. `stage_metrics.status` is constrained to `ok | failed | skipped` (`data-model.md` §4.9; `alembic/versions/0001_milestone_1a_core.py:467`, `ck_stage_metrics_status`). The rollups in `backend/src/smartrss/jobs/metrics.py:109-111` count those three values only.

**Problem:** a poll answered with `304` can't be told apart from a full `200` download. The soak test therefore can't show from the database that conditional GET is working, how much bandwidth it saves, or which feeds never send validators (BBC sends neither `ETag` nor `Last-Modified`). See §3a for the manual replay.

**Owner decision:** record 304s separately.

**Suggested design (spec first, per §4.2):**

- **Don't add a new `status` value.** A `not_modified` status would disappear from the `ok_count` / `failed_count` / `skipped_count` rollups and from every consumer that treats `ok` as success, such as System Health success rates (`1c-T13`). Keep `status = 'ok'` for a successful 304.
- Add a nullable **`stage_metrics.outcome TEXT`** column, `CHECK (outcome IN ('fetched','not_modified'))`, set by `poll_feed` only (`NULL` for other job types), in a new Alembic migration `0003`. Optionally also add **`http_status SMALLINT NULL`** for the final response code. Document both in `data-model.md` §4.9.
- Add `not_modified_count` (`COUNT(*) FILTER (WHERE outcome = 'not_modified')`) to the rollup query and to `GET /api/v1/system/metrics`. Show it in the System Health pipeline view if that's cheap.
- Add **`feeds_polled`** and **`feeds_not_modified`** to the `run-once` JSON summary and to `pipeline_runs`, which covers `E-06` at the same time.
- Optional, for feed health: show per feed whether the publisher sends validators (`etag` / `last_modified` already stored) so "always full download" feeds are visible.

**Tests:**

- (a) integration: a `poll_feed` against a recorded fixture returning `304` writes `status='ok', outcome='not_modified'`, and a `200` writes `outcome='fetched'`;
- (b) the rollup returns the right `not_modified_count`, and `ok_count` still includes 304s;
- (c) the migration upgrades and downgrades cleanly on a populated `stage_metrics`;
- (d) the `run-once` JSON contains `feeds_polled` / `feeds_not_modified`.

### E-09 — One-command first-time server setup (owner feedback, 2026-11-26; future design)

**Severity:** Minor (owner-requested enhancement). It needs a requirement and a component design before code. It doesn't block `1c`.

**Owner feedback (verbatim, lightly trimmed):** *"It would be nice if the backend deployment was even easier, e.g. perhaps a single script. Once I have a single backend running and can create a frontend, then it becomes easier to add hosts but this first time still took some assistance."*

**What the first install actually needed.** Each of these was a manual step, or one that needed help, in this session:

| # | Step | What it took | Related |
| :--- | :--- | :--- | :--- |
| 1 | Prerequisites | Install `docker.io` + `docker-compose-v2` (sudo), add user to the `docker` group, then **reboot**, since `sg` is missing and `newgrp` isn't installed by default on 26.04. Install `uv` with `curl … \| sh`. Pin Python 3.12 by hand. | `E-02`, `E-03` |
| 2 | `.env` | Copy `.env.example`, generate a strong `POSTGRES_PASSWORD` and substitute it in **two** places (`POSTGRES_PASSWORD` and inside `DATABASE_URL`), `chmod 600`. Decide `WORKER_MODE` and set the matching `COMPOSE_PROFILES`. Work out the LAN host/IP for `ALLOWED_HOSTS` / `CORS_ALLOW_ORIGINS`. Decide whether `SSRF_BLOCKED_CIDRS` needs the WAN `/32`. | — |
| 3 | Start | `make up`, `make migrate`, then poll `/api/v1/health` until ready. | — |
| 4 | Worker scheduling | Cron mode: write a crontab line by hand (flock, `-T`, `--max-duration`, log path), because the README example isn't usable as is. | `E-05` |
| 5 | First pairing | `docker compose exec api smartrss pair create`, copy the 16-character code into the UI within 5 minutes. For headless setup, a `curl` exchange for a bearer token. | — |
| 6 | Feeds | There's no CLI. Pairing a device and running `curl POST /api/v1/feeds` was needed, unless done in the UI after step 7. | §3a Q-1 |
| 7 | Client | `make install-client`, where the README's `--api-origin` flag fails, so edit `config.env` by hand. | `E-04` |

**Suggested shape (for the designer to evaluate):**

- **`make setup`** / **`scripts/setup-server.sh`**: idempotent and safe to re-run, ending in a working server.
  1. Run `check-prereqs.sh --mode server`, including daemon access (`E-03`). On a missing prerequisite, print the exact `apt` / `usermod` commands. Don't run `sudo` without asking.
  2. Create `.env` only if absent: a random password written consistently into both variables, mode `0600`, and LAN IP/hostname detection (offered for confirmation) for `ALLOWED_HOSTS` / `CORS_ALLOW_ORIGINS`. Ask for or flag the worker mode (`daemon` default) and set `COMPOSE_PROFILES` to match. Never overwrite an existing `.env`.
  3. `docker compose up -d --build --wait`, `smartrss db migrate`, and a health check.
  4. In cron mode, optionally install a `systemd` user timer (`deploy/systemd/smartrss-worker.{service,timer}`) instead of a raw crontab.
  5. Optionally import an OPML file (`--opml feeds.opml`) through a server-side CLI, with no pairing needed (covers §3a Q-1).
  6. Finish by printing the UI URL(s) and a fresh pairing code with its expiry, plus the exact `make install-client` command for this server's origin.
- **`make install-client API_ORIGIN=http://host:8080`**: a working origin flag (`E-04`), so adding another laptop is one command plus pairing.
- Keep the advanced path (manual `.env`, `make up`) documented. Setup is a convenience layer, not a replacement.

**Placement:** add a requirement, for example a new `NFR-OPS-02 Guided first-time install`, or extend `NFR-OPS-01` with a criterion such as "fresh Ubuntu LTS host → paired UI with feeds in one command plus a pairing paste". Then design it in a component spec. It's mostly scripts and docs with no data-model impact, so the owner may schedule it alongside `1d` rather than after `1g`.

**Tests (for `E-09`, when designed):** a scripted fresh-install E2E in a clean VM or container-in-container runner. Re-running `setup` is a no-op that preserves `.env` and data. Prerequisite failure messages for each missing prerequisite, including no daemon access. A pairing code is printed and works.

### E-11 — Feed-list export: no headless export and no OPML copy in backups (owner feedback, 2026-11-26; amended)

**Severity:** Nit (optional enhancement).

**Owner feedback:** *"It would be nice to be able to export the list of feeds."* **Amended by the owner (2026-11-26):** the UI export is fine. The owner had just missed the existing **Export OPML** button, so the discoverability and relabel parts of this finding are **withdrawn**.

**What already exists, and is sufficient for interactive use:** the **Feeds & OPML** view's **Export OPML** button (`frontend/src/components/feeds/FeedManagerView.tsx:170-201`, `data-testid="export-opml-btn"`) downloads `smartrss-feeds.opml` through `GET /api/v1/feeds/export-opml` (`feeds.py:309`; `1c` §596, `1c-T14`).

**Remaining small gaps, for whenever convenient:**

1. **No headless export.** There's no `smartrss feeds export-opml` CLI, so exporting without a paired browser needs a bearer token and `curl`. This is the same gap as §3a Q-1 and `E-09` step 6, and is best done with them.
2. **No OPML copy alongside backups.** `NFR-OPS-01` lists "OPML/config export" next to the nightly `pg_dump`, but `scripts/backup.sh` writes only the dump. Either write `smartrss-feeds-<timestamp>.opml` (mode `0600`, same rotation) with each backup, or clarify in `NFR-OPS-01` that the UI/API export satisfies the requirement.

**Tests (if done):** a CLI export that round-trips through `import-opml` (no duplicates, categories kept); a backup-script check for the OPML file's presence, mode and rotation.

---

## 3a. Owner questions / observations (not findings)

- **Q-1 — No CLI for feed management.** Setup without the UI needed `pair create`, a `curl` exchange for a bearer token, then `POST /api/v1/feeds`. A `smartrss feeds add|list|import-opml` CLI (running in-container, as `pair create` does) would make headless setup and scripting easier. Is that wanted for a later milestone?
- **A `304 Not Modified` poll can't be told apart from a `200` poll.** Both return `PollFeedOutcome(metric_status="ok")` (`poll_feed.py:379`, `:479`), and `stage_metrics.status` only allows `ok` / `failed` / `skipped`, so the soak test can't confirm from the database that conditional GET is in use. Replaying the stored validators by hand returned `304` for Ars Technica and `200` for the others, which is consistent with their content having changed. BBC sends neither `ETag` nor `Last-Modified`, so it is always fully re-downloaded and relies on de-duplication alone (no duplicates so far). **Owner decision (2026-11-26): yes, record 304s separately → `E-08`.**
- **Large first-subscribe archives.** The Hugging Face blog feed returned 917 entries on its first poll (the 04:30 run ingested 987 items in 61 s). 10 are `unread` and 907 are stored as `expired` permanently (the permanent historical log). That is per spec and fine at this scale, but feeds that publish their whole archive will dominate storage and FTS size over time. Should backfill keep only the newest `K` entries beyond the unread cap (say 100), or is the permanent log meant to be complete?
- **The startup reachability probe runs twice per page load.** At each of 5 page loads (04:24, 04:34, 05:06, 05:07, 07:28 MST), Nginx logged `GET /api/v1/health` and the unauthenticated `GET /api/v1/devices` probe (`frontend/src/sync/probe.ts`) **twice within 1 ms**, followed by a single `sync/pull`. The probe in `engine.ts:588-600` runs before the cycle lock, so either two triggers fire at load (startup `triggerSync()` plus a `visibilitychange` / `online` event) or two tabs on the same origin started together. It's harmless (two extra small requests per load) and not raised as a finding. The implementer can check it when touching `engine.ts`, for example by de-duplicating the in-flight probe the way the cycle itself is de-duplicated.
- **Permanent redirects work.** TLDR (`tldrnewsletter.com/rss`) moved on its own to `tldr.tech/api/rss/tech` with no user stamp on `field_updated_at['feed_url']`, as `1a` specifies for 301/308.
- **Backfill-as-expired is surprising on first look.** 62 of 112 items showed up as `expired` minutes after subscribing. That is per spec (`FR-FEED-05`). Consider whether the UI or feed-add response should say "N older items archived" so it doesn't look like data loss.

## 4. Verification performed

- `make check` on `87bba29`: **pass** (Python 3.12.16 via `uv`, Node 22.22.1, Docker 29.1.3).
- `make up` + `make migrate` (migrations `0001`, `0002`), `GET /api/v1/health` → `{"status":"ok","db":"ok"}`.
- 5 feeds added through `POST /api/v1/feeds` (all `201`): Hacker News front page, Ars Technica, LWN, Simon Willison's Weblog, BBC World.
- Manual `run-once`: `status=partial exit=2`, 112 polled, 48 extracted, 1 failed (`E-01`), 1 deferred. Later DB state: all 50 `extract_content` jobs `done`, 0 dead-lettered.
- Cron run 02:30: `completed`, exit `0`.
- Sync traced through API logs and the `sync_changes` / `sync_mutations` / `sync_state` tables (see the Review outcome section).
- Reproductions: `E-01` (script run in the `api` container, plus an IPv4-only control), `E-02` (suite under 3.14), `E-04` (`install-client.sh --api-origin`).
- **Ruled out:** the `instance_epoch` value is random (`gen_random_uuid()`); the pre-pairing `401`s on `GET /api/v1/devices` are the intentional `ALLOWED_HOSTS` probe (`frontend/src/sync/probe.ts:87`, `U-17-F1`); CORS preflight responses carry `access-control-max-age: 600`.

### Soak-test checklist (to append results)

- [x] Steady-state polls at each feed's 60-minute interval (03:51: all 5 re-polled within one 15-minute cron tick of `next_fetch_at`; 07:39: all 10 on schedule over 21 runs); conditional GET is only partly verifiable (see §3a, 304 visibility)
- [x] No duplicate items across polls (GUID / canonical-URL / content-hash) so far (03:51; 07:39 with 10 feeds and 1,118 items); updated entries not yet observed
- [x] Feed health stays `healthy` (03:51; 07:39 for all 10); both transient Hacker News failures (`E-01`) recovered on retry
- [x] `worker.log`: no `failed` runs (03:51; 07:39 over 21 runs); both tracebacks are recorded under `E-01`
- [ ] Client: offline reload, reconnect, star/read round-trip across the localhost runner and the LAN `web` client
- [ ] `retention_cleanup` daily run; `pipeline_runs` / `stage_metrics` growth
- [ ] `make backup` and `make restore-drill` against the live data

## Resolutions

*(To be filled in by the implementer.)*

## Accepted residual risks

*(None yet.)*
