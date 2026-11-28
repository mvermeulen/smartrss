# User-Test Review — Milestones `1a`–`1c` on a Live Deployment

- **Date**: 2026-11-26 (opened 02:40 MST; soak check-ins 03:51 and 07:39 MST); soak closed 2026-11-27 13:58 MST after a 30 h unattended run (about 35.5 h in total), followed by the backup and restore drill
- **Scope**: `main` at `87bba29` (Merge PR #2, `milestone/1c`), deployed with `make up` / `make migrate` on a single Ubuntu 26.04 host. Five live feeds, worker in `WORKER_MODE=cron` driven by a host crontab every 15 minutes, a UI paired through the localhost runner (`make install-client`, `http://localhost:4180`), and the LAN `web` container (`http://localhost:8080`). This covers the running system, the setup scripts and README, and the SSRF HTTP stack. It is not a full code re-review.
- **Checked against**: [`1a`](../design/1a-backend-ingestion-and-security.md), [`1c`](../design/1c-client-ui-and-offline-shell.md), [ADR 0006](../decisions/0006-lan-security-ssrf-and-content-isolation.md), [`README.md`](../../README.md), [`review-checklist.md`](../review-checklist.md)
- **Reviewer**: Claude Code (at the project owner's request)
- **Finding prefix**: `E-`
- **Status**: `Addressed (pass 2, 2026-11-27) — awaiting verification`

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
- URL canonicalization that keeps BBC's `at_medium` / `at_campaign` tracking parameters (`E-12`). This undermines `1e` exact canonical-URL deduplication;
- unbounded container and worker logs, with the `api` log almost entirely healthcheck lines (`E-13`).

**Soak check-in (03:51, about 95 minutes in):** six cron runs at 15-minute intervals, all exiting `0` or `2` (`partial`). Every feed was re-polled within one cron tick of its `next_fetch_at`. One Hacker News poll failed live through the `E-01` path, was retried, and succeeded on the next run. All feeds are `healthy` with `consecutive_errors = 0`. No duplicate items by `(feed_id, guid)` or `canonical_url`. 0 dead-lettered jobs. The API logged no 5xx responses or tracebacks across 71 pulls, 53 pushes, and the System Health / Settings views.

**Soak check-in 2 (07:39, about 5 h 10 min in, 10 feeds):** 21 cron runs: 19 `completed`, 2 `partial`. Both partial runs were Hacker News polls failing through the `E-01` unhandled path (03:30 `ConnectError` at TLS, 05:15 `ConnectTimeout`), and both recovered on retry. All 10 feeds are `healthy` with `consecutive_errors = 0`. 0 duplicates by `(feed_id, guid)`, `canonical_url`, or `(feed_id, content_hash)`. 0 non-`done` jobs and 0 dead letters. API: 350 pulls, 101 pushes, 5 feed creates, 2 PATCHes, 2 OPML exports and 1 search, with 0 5xx responses and 0 tracebacks. The client cursor (`since_seq=1390`) equals `sync_state.current_seq`. Poll latency p50 is 185–750 ms for every feed except Hacker News (p50 8.9 s, max 10.1 s, 2 of 7 failed). Diagnosis in the `api` container: `hnrss.org` has a single A record (TCP connect 58 ms), and a cold fetch took 2.6 s against about 0.1 s warm, so the publisher is slow to generate the feed. These are genuine transient failures, not a failover case, but they show how often `E-01`'s misclassification happens in practice: 2 of the 2 poll failures seen so far.

**Final soak report — 30 h unattended (2026-11-26 07:45 → 2026-11-27 13:45 MST, 10 feeds, Hacker News unsubscribed at 07:34):**

- **Cron and worker.** 122 cron runs, every one `completed` with exit `0`. No 15-minute slot was missed: the gap between consecutive `pipeline_runs` was never over 16 min after the first half hour. Over the whole soak there were 142 logged runs, 140 `completed` and 2 `partial`. Both partials came before the unattended window: the two Hacker News polls under `E-01`, which succeeded on retry. The median run took 0.66 s, and only the Hugging Face backfill run was long (61 s). `jobs` holds 609 rows, all `done`, with 0 dead letters, 0 reaped leases and 0 rows still carrying `last_error`.
- **Polling.** Each active feed was polled 26–28 times in the 30 h. All 10 are `healthy` with `consecutive_errors = 0`, and none recorded an error the whole time. The cadence is slightly below one per hour; see §3a ("cron quantization").
- **Unsubscribe.** Hacker News has had no `poll_feed` job since 07:15, the last poll before the 07:34 unsubscribe. Its stale `next_fetch_at` (11-26 08:11) was ignored as intended, and its 25 items were kept.
- **Ingestion and updates.** 38 new items arrived in the last 24 h, 1,164 items in total. The update path worked on live data: 10 BBC items were updated in place, and 9 BBC GUID revisions (`…#0` → `#1`/`#2`/`#3`, and one `#1` → `#0`) were matched to their existing rows by the guarded `N-01` canonical-URL fallback. There were 0 duplicates by `(feed_id, guid)`, but one BBC video was stored twice under GUID fragments `#0` and `#2`. That residual is consistent with the guarded design (§3a). Separately, every BBC `canonical_url` still carries tracking parameters (`E-12`). Extraction: 164 items `ok`. One LWN subscriber-only article (`[$]`) fell back to `fallback_rss`, as intended.
- **Expiration and retention.** `expire_items` ran in all 144 runs, and none of them had anything to expire, because the oldest unread items are 1.5 days old against the 30-day expiration window. `retention_cleanup` ran once per day, at 11-26 02:16 and 11-27 02:30.
- **Sync.** `sync_state.current_seq = 1511` with an unchanged `instance_epoch`. The `sync_mutations` outcomes are 102 `item_set_state`, 4 `item_mark_bulk_read`, 3 `item_set_starred` and 1 `feed_set_field`, all `applied`, with 0 `superseded` or `rejected`. The UI was mostly closed during the unattended window: 13 pulls and 10 pushes, all `200`. The device `Ubuntu Test` was last seen at 13:58.
- **API and containers.** No 5xx responses and no tracebacks in the `api` or `web` logs. `db` and `api` reported `healthy` throughout, and all three containers have been up for 36 h with no restarts. 10,589 of the roughly 10,640 `api` log lines are the container's own `/health` probe (`E-13`).
- **Backup and restore.** Both passed against the live data (§4).
- **Resources.** The database dump is 2.6 MB, `worker.log` is 58 KB, and 3.4 TB of disk is free.

The soak found nothing new at Major or Blocker severity. `E-01` remains the only Major, and it showed up as 2 of the 2 live poll failures (3 occurrences including the first manual run). The unattended window added `E-12` (Minor) and `E-13` (Nit).

**Recommendation:** fix `E-01` with regression tests that use the real `httpcore` backend's exception types. `E-02`–`E-04` are small script and doc fixes and should go in the same pass. `E-12` should land before `1e` relies on canonical URLs for exact deduplication, and it needs a re-canonicalization step for existing rows. `E-05`–`E-07` and `E-13` are optional. `E-08` needs the `data-model.md` §4.9 update before code; it can share a pass with `E-06`. `E-09` needs a requirement and a component design first; the owner should choose which milestone carries it.

| Severity | Findings |
| :--- | :--- |
| Blocker | — |
| Major | `E-01` |
| Minor | `E-02`, `E-03`, `E-04`, `E-10`, `E-12`, `E-08` (owner-requested), `E-09` (owner-requested, future design) |
| Nit | `E-05`, `E-06`, `E-07`, `E-13`, `E-11` (owner feedback, amended) |

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


### E-12 — URL canonicalization keeps BBC's `at_medium` / `at_campaign` tracking parameters

**Where:** `backend/src/smartrss/domain/url_canon.py:9-35` (`TRACKING_PARAM_PREFIXES` = `utm_`, `mtm_`, `pk_` plus a fixed name list); used by `poll_feed` for `items.canonical_url`.

**What happens:** BBC appends AT Internet / Piano Analytics campaign tags to every item link (`…/news/articles/c3pw4yrr8kz9o?at_medium=RSS&at_campaign=rss`). Neither parameter is on the strip list, so **all 70 BBC `canonical_url` values** keep them (sorted to `?at_campaign=rss&at_medium=RSS`). Two consequences:

- `1e` exact canonical-URL deduplication (`FR-LLM-03`, using the `ix_items_canonical_url` index from `1a`) won't match a BBC story against the same article linked from another feed or aggregator without the tags. BBC is one of the largest cross-posted sources.
- The "canonical" URL that clients show and open still carries tracking tags. That works against the point of canonicalizing.

**Suggested fix:**

- Add `at_medium` and `at_campaign` to `TRACKING_PARAM_NAMES`. Consider the other documented AT/Piano parameters (`at_custom1`–`at_custom4`, `at_format`, `at_link`, …) as well. Don't strip the bare `at_` prefix without a survey, because it could remove meaningful parameters on other sites.
- Because `poll_feed` compares stored and incoming `canonical_url` (the `N-01` fallback and the `1e` URL deduplication), a strip-list change alters keys for rows already stored. Add a one-off data step (an Alembic data migration or a `smartrss db recanonicalize` command) that recomputes `canonical_url` for existing items and emits `item` `upsert`s. Otherwise the next GUID rotation of an already-stored BBC item misses the fallback once and creates a duplicate.
- Record the strip list's scope in `1a` §5 or `data-model.md` (`canonical_url`).
- **Regression tests:** a `url_canon` unit test with the exact BBC link from this soak; a `poll_feed` integration test where a stored item with the old canonical form, followed by a GUID-rotated entry, still matches after the migration.

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


### E-13 — Container and worker logs are unbounded, and the `api` log is almost all healthcheck lines

**Where:** `docker-compose.yml`, where no service sets a `logging:` block. The host has no `/etc/docker/daemon.json`, so Docker's default `json-file` driver runs with no `max-size`. Also the `api` healthcheck (`docker-compose.yml:37-41`, every 10 s against `/api/v1/health`), and the README cron example that appends to a log file (`E-05`).

**What happens:** over 30 h, 10,589 of the roughly 10,640 `api` access-log lines were `127.0.0.1 "GET /api/v1/health" 200` from the container's own probe. That is about 8,600 lines and 0.6 MB a day of noise, which buries the 23 real sync requests from the same window when reading `docker compose logs api`. Nothing rotates the container logs or the cron `worker.log` (58 KB after 35 h). The growth is slow (tens of MB a month), but on an always-on home server it never stops.

**Suggested fix:**

- Add a shared `x-logging: &default-logging` block (`driver: json-file`, `max-size: "10m"`, `max-file: "3"`) to every service.
- Stop logging the healthcheck: filter `/api/v1/health` from the uvicorn access log with a `logging.Filter`, or have the healthcheck call a path that isn't logged. Relaxing the interval to 30 s would also cut the volume threefold.
- Document a `logrotate` stanza (or the `systemd` timer from `E-05`, which logs to the journal) for `worker.log`.
- **Tests:** a compose-config test (`docker compose config` parsed in pytest, or a shell check in `make check`) that asserts every service has bounded logging. If the filter is added, a unit test that a `/api/v1/health` access record is dropped and a `/api/v1/sync/pull` record is kept.

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
- **A residual same-feed duplicate (BBC, guarded by design).** BBC revises its item GUIDs with a `#N` fragment (`…/videos/cq808d704m22o#0` → `#2`). The `N-01` fallback matched 9 such revisions correctly, but one video was stored twice: `#0` at 11-26 14:00 and `#2` at 11-27 00:00, with identical `canonical_url`, `entry_hash` and `content_hash`. The most likely cause is that both GUIDs appeared in the same response. Then guard (b) (exactly one batch entry per `canonical_url`) or guard (c) (the old GUID must be absent from the batch) correctly refuses the match, as `1a` specifies to protect shared-link feeds. This is not raised as a finding: `1e` exact-URL clustering is the intended backstop and would group the pair. Worth keeping as a `1e` test case: same feed, same `canonical_url` and `content_hash`, different GUID.
- **Cron quantization lengthens the effective poll interval.** With a 60-minute `poll_interval_minutes` and a 15-minute cron, a feed is due at `last + 60 min ± jitter` but polled only at the next cron tick. Ars Technica, for example, succeeded at 12:45, became due at 13:47 and was polled at 14:00, 75 minutes later. Each feed got 26–28 polls in 30 h, not about 30. That is harmless and arguably polite. If exact cadence ever matters, set `next_fetch_at` from the scheduled time rather than the completion time, or document the effective `interval + cron period` ceiling for cron mode.
- **The first failure's error text isn't kept after a successful retry.** Jobs 72 and 146 (`E-01`) are `done` with `attempts = 2` and `last_error = NULL`, and their `stage_metrics` rows have `status = 'failed'` with no error column. The exception text survives only in `worker.log`, which isn't rotated or collected (`E-13`). Consider storing a short error class or message on failed `stage_metrics` rows (it can share the `E-08` column migration) so System Health can show recent transient failures.
- **Backfill-as-expired is surprising on first look.** 62 of 112 items showed up as `expired` minutes after subscribing. That is per spec (`FR-FEED-05`). Consider whether the UI or feed-add response should say "N older items archived" so it doesn't look like data loss.

## 4. Verification performed

- `make check` on `87bba29`: **pass** (Python 3.12.16 via `uv`, Node 22.22.1, Docker 29.1.3).
- `make up` + `make migrate` (migrations `0001`, `0002`), `GET /api/v1/health` → `{"status":"ok","db":"ok"}`.
- 5 feeds added through `POST /api/v1/feeds` (all `201`): Hacker News front page, Ars Technica, LWN, Simon Willison's Weblog, BBC World.
- Manual `run-once`: `status=partial exit=2`, 112 polled, 48 extracted, 1 failed (`E-01`), 1 deferred. Later DB state: all 50 `extract_content` jobs `done`, 0 dead-lettered.
- Cron run 02:30: `completed`, exit `0`.
- Sync traced through API logs and the `sync_changes` / `sync_mutations` / `sync_state` tables (see the Review outcome section).
- Reproductions: `E-01` (script run in the `api` container, plus an IPv4-only control), `E-02` (suite under 3.14), `E-04` (`install-client.sh --api-origin`).
- **Unattended soak (2026-11-26 07:45 → 2026-11-27 13:45 MST):** analyzed `worker.log` (142 JSON summaries), `pipeline_runs` (cadence gaps), `jobs`, `stage_metrics`, `items`, `sync_changes`, `sync_mutations`, `sync_state`, `devices`, and 30 h of `api` / `web` logs. I also fetched the live BBC feed (`feeds.bbci.co.uk/news/world/rss.xml`, 32 entries) and compared its GUIDs with the stored ones (21 exact matches, 9 revised GUIDs matched to existing rows, 2 not yet ingested).
- **`make backup`** (2026-11-27 13:58 MST): `backups/smartrss-20261127T205831Z.dump`, 2.6 MB, mode `0600`, in a `0700` directory (gitignored). Integrity was verified by `pg_restore --list`. The run took 0.5 s.
- **`make restore-drill`** (immediately after, using that dump): **PASS** in 3.6 s. The restore went into `smartrss_restore_drill`; the drill database's `instance_epoch` rotated `9dfefb19…` → `5095ca93…` while production's stayed at `9dfefb19…`. The Alembic head is `0002_milestone_1b_sync_search` (it matches `alembic heads`), and the row counts `feeds=10 items=1164 devices=2` match the source. Afterwards `pg_database` showed the drill database dropped, and production was untouched. One detail: the drill reused the just-written dump rather than taking its own, so the script's strict count-equality check, which only runs for fresh dumps, was skipped. The counts matched anyway. The nightly `deploy/systemd/smartrss-backup.timer` was **not** installed for this test, so scheduled backups and rotation were not exercised.
- **Ruled out:** the `instance_epoch` value is random (`gen_random_uuid()`); the pre-pairing `401`s on `GET /api/v1/devices` are the intentional `ALLOWED_HOSTS` probe (`frontend/src/sync/probe.ts:87`, `U-17-F1`); CORS preflight responses carry `access-control-max-age: 600`.

### Soak-test checklist (to append results)

- [x] Steady-state polls at each feed's 60-minute interval (03:51: all 5 re-polled within one 15-minute cron tick of `next_fetch_at`; 07:39: all 10 on schedule over 21 runs; 30 h unattended: 26–28 polls per feed, 0 missed cron slots, see §3a "cron quantization"). Conditional GET is only partly verifiable (§3a, 304 visibility → `E-08`).
- [x] No duplicate items by GUID (03:51; 07:39; final, 1,164 items). One same-feed BBC duplicate by canonical URL was found and analyzed as a guarded residual (§3a). **Updated entries observed:** 10 BBC items updated in place, and 9 BBC GUID revisions matched by the `N-01` fallback.
- [x] Feed health stays `healthy` (03:51; 07:39; final, all 10 with 0 errors in the unattended window). Both transient Hacker News failures (`E-01`) recovered on retry.
- [x] `worker.log`: no `failed` runs (142 runs: 140 `completed`, 2 `partial`, both before the unattended window). Both tracebacks are recorded under `E-01`.
- [x] Unsubscribe stops polling: Hacker News has had no `poll_feed` since 07:15 and its items were kept.
- [~] Client: star and read round-trips are covered (110 mutations applied, 0 rejected, cursor matches `current_seq`). **Not exercised:** offline reload and reconnect, and cross-client consistency between the localhost runner and the LAN `web` client, because only one UI device (`Ubuntu Test`) was paired. These are carried forward to the next user test.
- [x] `retention_cleanup` ran daily (11-26 02:16 and 11-27 02:30). `expire_items` ran 144 times with nothing due (the window is shorter than the 30-day expiration). Growth: `pipeline_runs` +144 rows, `stage_metrics` 612 rows, `jobs` 609 rows, all small.
- [x] `make backup` and `make restore-drill` against the live data: both pass (§4). The nightly backup timer was not installed.

## Resolutions

*(Addressed 2026-11-27 on `review/1a-1c-user-test`; awaiting Claude Code verification pass.)*

- **E-01** — Fixed.
  - `SafeNetworkBackend.connect_tcp` (`backend/src/smartrss/security/safe_http.py:263-288`) now catches `(OSError, httpcore.ConnectError, httpcore.ConnectTimeout)` per IP so failover works with real `httpcore.AnyIOBackend`, and includes the last exception class/detail in the final `SSRFConnectionError` (chained via `from last_exc`).
  - Added `SafeHttpTimeoutError` (`safe_http.py:57-58`) and `_map_httpcore_exceptions()` (`safe_http.py:237-249`) applied across `SafeTransport.handle_async_request` (`safe_http.py:345-346`), `SafeTransport.aclose` (`safe_http.py:356-357`), `_PinnedStream.__aiter__` (`safe_http.py:306-308`), and `_PinnedStream.aclose` (`safe_http.py:311-312`) so raw `httpcore` errors never escape `SafeHttpClient.fetch()`.
  - Updated `run_extract_content` (`backend/src/smartrss/worker/stages/extract_content.py:176-177`) to catch `SafeHttpError` (after permanent-fallback subclasses) so `SafeHttpTimeoutError`, `SSRFConnectionError`, and protocol errors all raise `TransientExtractionError` instead of reaching the worker catch-all.
  - Also fixed `backend/alembic/env.py:17` (`disable_existing_loggers=False`) so Alembic migrations during tests do not silently disable `smartrss.*` loggers.
  - Added checklist row `T7` in `docs/review-checklist.md` and updated `docs/design/1a-backend-ingestion-and-security.md` §7.1.
  - Tests:
    - `test_e01_connect_tcp_failover_httpcore_errors_and_real_anyio` (`backend/tests/boundary/test_safe_http_boundary.py`) — fake inner backend raising `httpcore.ConnectError`/`ConnectTimeout` plus real `httpcore.AnyIOBackend` failing over from refused `[::1]` to listening `127.0.0.1`.
    - `test_e01_all_ips_fail_with_httpcore_errors_raises_ssrf_connection_error` (`backend/tests/boundary/test_safe_http_boundary.py`) — all-IPs-fail with real `httpcore` exceptions and real `AnyIOBackend` against a closed port.
    - `test_e01_safe_transport_and_pinned_stream_map_httpcore_exceptions` (`backend/tests/boundary/test_safe_http_boundary.py`) — TLS `start_tls` `ConnectError`, header `ReadTimeout`, `RemoteProtocolError`, mid-body `ReadTimeout` in `_PinnedStream.__aiter__`, and `ReadError` in `_PinnedStream.aclose`.
    - `test_e01_poll_feed_and_extract_content_typed_paths_on_httpcore_errors` (`backend/tests/integration/test_user_test_regressions.py`) — asserts zero `Unhandled error` logs across `poll_feed` (TLS `ConnectError`, `ConnectTimeout`) and `extract_content` (`ConnectError`, body `ReadTimeout`).
  - Evidence (each path reverted individually):
    - Reverted `connect_tcp` catch to `except OSError`: `test_e01_connect_tcp_failover_httpcore_errors_and_real_anyio` & `test_e01_all_ips_fail_with_httpcore_errors_raises_ssrf_connection_error` failed (`httpcore.ConnectError: All connection attempts failed`); restored, passed.
    - Reverted `SafeTransport.handle_async_request` `_map_httpcore_exceptions()`: `test_e01_safe_transport_and_pinned_stream_map_httpcore_exceptions` & `test_e01_poll_feed_and_extract_content_typed_paths_on_httpcore_errors` failed (`httpcore.ConnectError: TLS handshake failed`); restored, passed.
    - Reverted `_PinnedStream.__aiter__` `_map_httpcore_exceptions()`: `test_e01_safe_transport_and_pinned_stream_map_httpcore_exceptions` & `test_e01_poll_feed_and_extract_content_typed_paths_on_httpcore_errors` failed (`httpcore.ReadTimeout: body timeout`); restored, passed.
    - Reverted `_PinnedStream.aclose` `_map_httpcore_exceptions()`: `test_e01_safe_transport_and_pinned_stream_map_httpcore_exceptions` failed (`httpcore.ReadError: close error`); restored, passed.
    - Reverted `extract_content.py` catch from `SafeHttpError` to `SSRFConnectionError`: `test_e01_poll_feed_and_extract_content_typed_paths_on_httpcore_errors` failed (`Unexpected runner.logger.exception calls: ['Unhandled error in job 4 (extract_content)']`); restored, passed.
    - All fixed lines in `safe_http.py` (92.32%) and `extract_content.py` (90%) are covered.

- **E-02** — Fixed. Added `backend/.python-version` (`3.12`), bounded `requires-python = ">=3.12,<3.14"` in `backend/pyproject.toml:9` (verified `uv --directory backend lock --check` passes), and documented in `README.md` that `uv` provisions Python 3.12 automatically.
  - Test: `test_e02_python_version_pin_present` (`backend/tests/unit/test_user_test_ops.py`).
  - Evidence: temporarily removed `backend/.python-version` → failed (`assert False`); reverted `requires-python` in `pyproject.toml` → failed (`assert 'requires-python = ">=3.12,<3.14"' in pyproject`); restored, passed.

- **E-03** — Fixed. Added `docker info >/dev/null 2>&1` check to `check_docker()` in `scripts/check-prereqs.sh:74-79` with actionable `usermod -aG docker` / `util-linux-extra && newgrp docker` hints.
  - Test: `test_e03_check_prereqs_rejects_inaccessible_docker_daemon` (`backend/tests/unit/test_user_test_ops.py`).
  - Evidence: disabled `elif ! docker info` branch → test failed (`assert 0 == 1`); restored, passed.

- **E-04** — Fixed. Implemented `--api-origin URL` / `--api-origin=URL` in `scripts/install-client.sh:32-47,85-95,146-165` (validated via `smartrss_client_runner.validate_api_origin`, updating `SMARTRSS_API_ORIGIN` in `config.env` in place without clobbering on invalid input). Synced `README.md:5,123,134` (`1a`–`1c` `Implemented & Approved`, `~/.local/share/smartrss/` with `mv` directory swap, Node.js 22+).
  - Test: `test_e04_install_client_api_origin_flag` (`backend/tests/unit/test_user_test_ops.py`).
  - Evidence: disabled `--api-origin` case in `install-client.sh` → test failed (`Unknown option: --api-origin`); restored, passed.

- **E-05** — Fixed. Updated `README.md` "Worker Execution Modes" with the complete crontab line (`flock -n`, `docker compose run --rm -T`, `--max-duration 840`, `--json`, log redirect), a note on `--json` stderr interleaving, and a `logrotate` stanza (or `systemd` timer / `journald` alternative).

- **E-06** — Fixed (together with `E-08`). Added `feeds_polled` and `feeds_not_modified` columns to `pipeline_runs` (`backend/alembic/versions/0003_user_test_metrics_and_canon.py`, `backend/src/smartrss/db/models.py:240-241`, `backend/src/smartrss/api/schemas.py:194-195`), populated in `worker/runner.py:183-189,574-592` and `jobs/metrics.py:73-88`. Added `items_ingested` alongside backwards-compatible `items_polled` on `RunOnceSummary`, updated CLI text summary (`backend/src/smartrss/cli.py:220-222`), updated System Health UI (`frontend/src/components/health/SystemHealthView.tsx`, `frontend/src/types/api.ts`), and documented all fields in `docs/data-model.md`, `docs/design/1a-backend-ingestion-and-security.md`, and `docs/design/1c-client-ui-and-offline-shell.md`.
  - Tests: `test_e08_e06_304_outcome_metrics_rollup_and_run_once_counters` (`backend/tests/integration/test_user_test_regressions.py`), `test_e06_e12_cli_text_summary_and_recanonicalize` (`backend/tests/unit/test_user_test_ops.py`).
  - Evidence: disabled `counters.feeds_polled += 1` in `runner.py` → `test_e08_e06_...` failed (`assert 0 == 2`); reverted `cli.py` text summary → `test_e06_e12_cli_...` failed (`assert 'feeds_polled=3' in ...`); restored, passed. Fixed lines covered.

- **E-07** — Fixed. Added `NoStoreResponseHeaderMiddleware` (`backend/src/smartrss/security/middleware.py:35-56`, registered in `backend/src/smartrss/api/app.py:81`) so all API responses send `Cache-Control: no-store` (preserving any route-specific `Cache-Control`), and dropped request-side `cache: "no-store"` on authenticated JSON calls in `frontend/src/api/client.ts:196-205` (keeping `cache: "no-store"` on `/client-config.json` and reachability probes) so browsers can reuse the `Access-Control-Max-Age: 600` CORS preflight cache. Updated `docs/design/1c-client-ui-and-offline-shell.md`.
  - Tests: `test_e07_api_responses_include_cache_control_no_store` (`backend/tests/integration/test_user_test_regressions.py`), `test_e07_no_store_middleware_preserves_existing_header_and_non_http_scope` (`backend/tests/unit/test_user_test_ops.py`), `test_e07_api_client_omits_request_cache_no_store_while_config_keeps_it` (`frontend/src/test/unit/review_regressions.test.ts`).
  - Evidence: removed `NoStoreResponseHeaderMiddleware` from `app.py` → backend test failed (`assert None == 'no-store'`); re-added `cache: "no-store"` in `frontend/src/api/client.ts` → vitest test failed; restored, passed. All middleware lines covered (`middleware.py` 92.66%).

- **E-08** — Fixed (owner-requested). Updated `docs/data-model.md` §4.9, `docs/design/1a-backend-ingestion-and-security.md`, and `docs/design/1c-client-ui-and-offline-shell.md`. Added Alembic migration `0003_user_test_metrics_canon` (`backend/alembic/versions/0003_user_test_metrics_and_canon.py`) adding nullable `stage_metrics.outcome TEXT CHECK (outcome IN ('fetched', 'not_modified'))` and `stage_metrics.http_status SMALLINT NULL` (plus `pipeline_runs.feeds_polled` / `feeds_not_modified`). Updated `poll_feed.py` (`outcome="not_modified"`, `http_status=304` while keeping `status="ok"`; `outcome="fetched"`, `http_status=2xx` on full fetch; `http_status` on HTTP error branches), `jobs/queue.py` (`complete_job`), `jobs/metrics.py` (`not_modified_count` in SQL rollup while `ok_count` still includes 304s), and `SystemHealthView.tsx`.
  - Tests: `test_e08_e06_304_outcome_metrics_rollup_and_run_once_counters` and `test_e08_e12_migration_0003_upgrade_downgrade_on_populated_db` (`backend/tests/integration/test_user_test_regressions.py`).
  - Evidence: reverted `poll_feed.py` 304 `outcome`/`http_status` → failed (`assert 0 == 1`); reverted `metrics.py` `not_modified_count` SQL → failed (`assert 0 == 1`); restored, passed. Migration 0003 upgrade, check constraint enforcement, and downgrade on populated 0002 DB verified. Fixed lines covered.

- **E-09** — Deferred → future milestone (requires a new requirement such as `NFR-OPS-02` and a component design before implementation, per review and `AGENTS.md`). → *Owner decision needed on which milestone carries `E-09` (e.g., alongside `1d`).*

- **E-10** — Fixed. Extended shared `SSRFPolicy.validate_url()` (`backend/src/smartrss/security/safe_http.py:41-42,110-137`) to reject unescaped RFC 3986-disallowed characters (space, `<>"{}|\^\``), unescaped `[`/`]` outside IPv6 host literals, and embedded `http(s)://` schemes in path/query/fragment — enforced uniformly across `POST /api/v1/feeds`, `PATCH /api/v1/feeds/{id}`, `POST /api/v1/feeds/import-opml`, and `SafeHttpClient.fetch()`. Added optional UI paste cleanup helper `cleanPastedFeedUrl` (`frontend/src/components/feeds/feedUrlInput.ts`, wired into add/edit inputs in `FeedManagerView.tsx`) for well-formed `[label](url)` and `<url>` pastes while leaving malformed partial pastes untouched for server `422` rejection. Updated `docs/design/1a-backend-ingestion-and-security.md` §7.2.
  - Tests: `test_e10_validate_url_rejects_markdown_and_rfc3986_disallowed_chars` & `test_e10_validate_url_accepts_percent_encoded_and_ipv6_hosts` (`backend/tests/unit/test_ssrf_policy.py`), `test_e10_feed_url_validation_all_three_paths` (`backend/tests/integration/test_user_test_regressions.py`), `test_e10_clean_pasted_feed_url_extracts_markdown_and_angle_brackets` (`frontend/src/test/unit/review_regressions.test.ts`).
  - Evidence: reverted bracket/embedded-scheme checks in `safe_http.py` → unit and all-3-paths API tests failed (`DID NOT RAISE SSRFBlockedError`); reverted `_FORBIDDEN_URL_CHARS` check → unit tests failed; disabled Markdown extraction in `feedUrlInput.ts` → vitest failed; restored, passed. Fixed lines covered.

- **E-11** — Deferred → alongside `E-09` / `1d` (owner amended finding: interactive **Export OPML** button in **Feeds & OPML** is sufficient; optional headless `smartrss feeds export-opml` CLI and backup OPML copy are best grouped with `E-09` ops/CLI work).

- **E-12** — Fixed. Added explicit AT Internet / Piano Analytics parameter names (`at_medium`, `at_campaign`, `at_campaign_type`, `at_custom1`–`at_custom4`, `at_format`, `at_link`, `at_link_id`, `at_link_type`, `at_link_origin`, `at_ptr_name`, `at_bbc_team`, `at_creation`, `at_variant` — explicit names only, never bare `at_`) to `TRACKING_PARAM_NAMES` (`backend/src/smartrss/domain/url_canon.py:34-51`). Added `backend/src/smartrss/db/recanonicalize.py`, CLI command `smartrss db recanonicalize` (`backend/src/smartrss/cli.py:393-409`), and automatic batch re-canonicalization in migration `0003` (`backend/alembic/versions/0003_user_test_metrics_and_canon.py:59-145`) emitting `item` `upsert`s into `sync_changes` under the serialized `sync_state` lock. Documented strip-list scope in `docs/data-model.md` and `smartrss db recanonicalize` in `docs/design/1a-backend-ingestion-and-security.md`.
  - Tests: `test_e12_canonicalize_strips_bbc_at_tracking_params_and_preserves_other_at_params` (`backend/tests/unit/test_url_canon.py`), `test_e12_compute_updated_canonical_url_fallbacks` & `test_e06_e12_cli_text_summary_and_recanonicalize` (`backend/tests/unit/test_user_test_ops.py`), `test_e12_recanonicalize_and_poll_feed_guid_rotation_matches` & `test_e08_e12_migration_0003_upgrade_downgrade_on_populated_db` (`backend/tests/integration/test_user_test_regressions.py`).
  - Evidence: removed `at_medium`/`at_campaign` from `url_canon.py` → unit and integration tests failed (`bbc.com/...?at_campaign=rss&at_medium=RSS` not stripped); disabled data step in migration `0003` → migration test failed; disabled `compute_updated_canonical_url` call in `recanonicalize_all_items` → `test_e12_recanonicalize_...` failed (`assert 0 == 1`); restored, passed. `url_canon.py` (100%) and `recanonicalize.py` (100%) covered.

- **E-13** — Fixed. Added shared `x-logging: &default-logging` (`driver: json-file`, `max-size: "10m"`, `max-file: "3"`) to every service in `docker-compose.yml` (`db`, `api`, `worker`, `web`, `db-test`), relaxed `api` healthcheck interval from `10s` to `30s` (with `start_period: 10s`), added `HealthCheckAccessFilter` (`backend/src/smartrss/logging.py:58-82`) attached idempotently to `uvicorn.access` in `configure_logging()` to drop `GET /api/v1/health` probe lines while keeping `/api/v1/system/health` and all other routes, and documented `logrotate` for `worker.log` in `README.md`.
  - Tests: `test_e13_healthcheck_access_log_filter` and `test_e13_compose_services_have_bounded_logging` (`backend/tests/unit/test_user_test_ops.py`).
  - Evidence: bypassed `HealthCheckAccessFilter.filter()` (`return True`) → `test_e13_healthcheck_access_log_filter` failed (`assert True is False`); removed `logging: *default-logging` from `api` in `docker-compose.yml` → `test_e13_compose_services_have_bounded_logging` failed (`Service 'api' missing logging block`); restored, passed. All fixed lines in `logging.py` (96%) covered.

### Verification summary (`make check`)

- Backend: `ruff check` + `ruff format --check` clean; `mypy src` clean (54 source files); **200 passed**, **91.78%** total branch coverage; all 16 critical core modules **>= 90%** (`safe_http.py` 92.32%, `middleware.py` 92.66%, `url_canon.py` 100.00%, `poll_feed.py` 91.98%, `queue.py` 95.12%).
- Client runner: **32 passed**, **94.87%** branch coverage (>= 90% gate).
- Frontend: `eslint` + `prettier --check` clean; `tsc --noEmit` clean; **57 passed** across 6 test files, **93.13%** total line coverage; all critical (>= 90%) and core (>= 80%) coverage gates passed.

## Accepted residual risks

*(None yet.)*

## Verification pass (2026-11-27)

**Verifier:** Claude Code, in a fresh session on `review/1a-1c-user-test` at `1987f2b`.

**`make check`:** green. Backend: **200 passed**, **91.79%** branch coverage (claimed 91.78%), and all 16 critical modules ≥ 90%. Runner: **32 passed**, 94.87%. Frontend: **57 passed** (6 files), and all gates pass. The test counts match the resolution notes exactly.

**Method.** I disabled each fixed path on its own with a minimal edit, ran the cited test (or the full suite where none was cited), restored the file, and confirmed the restore byte for byte (`git status` clean afterwards). That is 33 reverts in total, scripted.

| Finding | Verdict | Evidence |
| :--- | :--- | :--- |
| `E-01` (Major) | ⚠️ resolved, with a test gap (`E-01-F1`) | Fix present on every path. `connect_tcp` catches `httpcore.ConnectError` / `ConnectTimeout` per IP, `_map_httpcore_exceptions()` wraps `handle_async_request`, `_PinnedStream.__aiter__` / `aclose` and `SafeTransport.aclose`, and `extract_content` now catches `SafeHttpError`. `poll_feed` already caught `SafeHttpError`. The sibling `SafeHttpClient` users (`cli.py` run-once / worker) all go through these two stages. Each revert failed its cited test: `connect_tcp` → both `test_e01_connect_tcp_failover…` / `test_e01_all_ips_fail…`; `handle_async_request`, `__aiter__`, `_PinnedStream.aclose` → `test_e01_safe_transport_and_pinned_stream…`; `extract_content` catch → `test_e01_poll_feed_and_extract_content_typed_paths…`. **Exception:** reverting the `SafeTransport.aclose` mapping leaves the **full suite green** (200 passed). |
| `E-02` | ✅ | `backend/.python-version` = `3.12`; `requires-python = ">=3.12,<3.14"`; `uv lock --check` is clean; README updated. `test_e02_python_version_pin_present` is a config-presence check, which is adequate here. |
| `E-03` | ✅ | `elif ! docker info` branch with `usermod` / `newgrp` hints. With the branch disabled, `test_e03_check_prereqs_rejects_inaccessible_docker_daemon` fails. |
| `E-04` | ✅ | `--api-origin URL` / `=URL` is validated with `validate_api_origin` **before** any directory or config is touched, and replaces `SMARTRSS_API_ORIGIN` in place. Disabling the case arm, or the in-place replace, fails `test_e04_install_client_api_origin_flag`. README path, `mv` swap, Node 22+ and status line all fixed. |
| `E-05` | ✅ | Docs only. README now shows the full `flock` / `-T` / `--max-duration 840` / log-redirect line, the `--json` stderr note and a `logrotate` stanza. |
| `E-06` | ✅ | Disabling `feeds_polled += 1`, `feeds_not_modified += 1`, or `run.feeds_polled = …` in `finalize_pipeline_run` fails `test_e08_e06_…`. Reverting the CLI text fails `test_e06_e12_cli_text_summary…`. `data-model.md` defines the fields. |
| `E-07` | ✅ | Removing the middleware registration fails `test_e07_api_responses_include_cache_control_no_store`. Forcing an overwrite of an existing `Cache-Control` fails `test_e07_no_store_middleware_preserves…`. Re-adding request-side `cache: "no-store"` fails the vitest `test_e07_api_client_omits…`. The `1c` CORS invariant text is updated. |
| `E-08` | ✅ | Removing the 304 `outcome`, the 200 `outcome="fetched"`, `complete_job`'s `outcome=` pass-through, or the `not_modified_count` SQL filter each fails `test_e08_e06_…`. Removing the migration's data step fails `test_e08_e12_migration_0003…`. `ok_count` still includes 304s. `data-model.md` §4.9 is updated. |
| `E-09` | ⏸ deferred | Future requirement plus design, as the finding itself proposed. The **owner still has to decide which milestone carries it** (for example alongside `1d`). |
| `E-10` | ⚠️ partially resolved → **`E-10-F1` (Major, regression)** | The soak string is rejected on create, PATCH and OPML import, and the UI clean-up works. Every revert fails its cited test (`_FORBIDDEN_URL_CHARS`, bracket and embedded-scheme checks → `test_ssrf_policy.py` / `test_e10_feed_url_validation_all_three_paths`; Markdown and `<url>` clean-up → vitest). **But** the new checks were put in `SSRFPolicy.validate_url()`, which `SafeHttpClient.fetch()` runs on **every hop of every fetch**, so they also reject valid URLs. See `E-10-F1`. |
| `E-11` | ⏸ deferred | The owner amended this finding (the UI export is sufficient). The headless CLI and the OPML copy in backups go with `E-09`. |
| `E-12` | ✅ | Removing `at_medium` / `at_campaign` fails `test_url_canon.py` and `test_e12_recanonicalize…`. Disabling `compute_updated_canonical_url` or `sync_log.record` in `recanonicalize_all_items` fails `test_e12_recanonicalize…`. Disabling the migration data step fails `test_e08_e12_migration…`. Only explicit `at_*` names are stripped, never the bare prefix. `data-model.md` lists the strip set. |
| `E-13` | ✅ | Every service uses `logging: *default-logging`; dropping it from `api` fails `test_e13_compose_services…`. The api healthcheck is now every 30 s. Not attaching `HealthCheckAccessFilter` fails a test in the full suite. I also checked it **under real uvicorn** (`--factory`, the same startup order as the `api` container): `GET /api/v1/health` is dropped while `GET /` and `/api/v1/sync/pull` are still logged. (Reverting only the `record.args` branch stays green, because the message-text fallback covers the same case. That is a redundant guard, not a gap.) |

### Follow-ups

- **`E-10-F1` (Major; a regression introduced by the `E-10` fix): the strict paste-error checks run on the fetch path, not just at feed entry.** `_FORBIDDEN_URL_CHARS`, the bracket check and `_EMBEDDED_SCHEME_RE` are in `SSRFPolicy.validate_url()`. `SafeHttpClient.fetch()` calls that for the initial URL and every redirect `Location` (`safe_http.py:566`), and `poll_feed` / `extract_content` both use it. Reproduced against `validate_url` directly; all of these were accepted before `1987f2b` and are rejected now:
  - `https://rss-bridge.example.org/?action=display&bridge=Mastodon&url=https://mastodon.social/@x&format=Atom`: "embedded http(s):// scheme". RSS-Bridge, RSSHub-style proxies and `?url=` / `?ref=` parameters are common for a self-hosted reader.
  - `https://web.archive.org/web/2024/https://example.com/post`: embedded scheme in the path, but a legitimate URL.
  - `https://example.com/feed?tags[]=python` and `?filter[category]=news`: "unescaped brackets". PHP- and JSON:API-style query arrays, which browsers and httpx send as they are.
  - `https://example.com/a?x=1|2`: "disallowed unescaped characters".

  **Impact:** after upgrading, an **already-subscribed** feed with one of these URLs gets `SSRFBlockedError` → `_record_feed_poll_error` on every poll and ends up `degraded` / `broken`. Article URLs of these shapes, and redirects to them, silently drop to `fallback_rss` in `extract_content`. Such feeds can't be added at all. `1a` §7.2 now documents the behaviour as intended ("shared across … and `SafeHttpClient.fetch()`"). The original `E-10` suggestion ("keep it shared with `SSRFPolicy.validate_url` so they can't drift apart") was ambiguous on this point, and the reviewer shares the blame. The intent was a shared *entry-time* validator, not a stricter fetch policy.

  **Expected fix:**
  1. Restore `validate_url()` (fetch path) to its pre-`E-10` behaviour: control characters, scheme, userinfo, host and port.
  2. Move the paste-error checks into a separate entry-time validator (for example `validate_feed_url_syntax()`), used by `POST` / `PATCH /feeds` and OPML import.
  3. Narrow that validator so legitimate URLs pass. Reject whitespace and `<>"`, `[` / `]` in the **path**, and an embedded `http(s)://` in the **path** only, or only right after `](` / `)(`. That still rejects the soak string, which already fails on its `]`. Allow brackets, `|` and embedded schemes in the **query**. The lenient alternative is to percent-encode them before storing.
  4. Update `1a` §7.2.

  **Regression tests:**
  - (a) `SafeHttpClient.fetch()` of a `?url=https://…&tags[]=x` URL succeeds against a local fixture;
  - (b) a redirect whose `Location` carries such a query is followed;
  - (c) `extract_content` of such a `raw_url` does not fall back;
  - (d) `POST /feeds` with the RSS-Bridge URL → `201`, while the soak string still → `422` on all three paths.

  Each must fail against `1987f2b`.
- **`E-01-F1` (Nit, test gap):** the `_map_httpcore_exceptions()` wrapper on `SafeTransport.aclose()` (`safe_http.py:357`) is listed as fixed but no test reaches it; reverting it leaves all 200 tests green. Add a boundary case in which the pool's `aclose()` raises `httpcore.ReadError` / `ConnectError`, and assert that `SafeHttpClient.__aexit__` raises a `SafeHttpError` subclass. Alternatively, drop the wrapper and document that `aclose` errors are allowed to escape.

### Observations (no action required)

- Migration `0003`'s data step imports the live `smartrss.db.recanonicalize.compute_updated_canonical_url`, so it is not frozen. A later change to the strip list would change what `0003` does when it is replayed on an older restored database. This is acceptable because later strip-list changes are expected to ship with their own `db recanonicalize` step. If that ever matters, inline a frozen copy.
- `cleanPastedFeedUrl` runs in `onChange`, so the URL input is trimmed on every keystroke. This is harmless for URLs.

### Repo-wide drift

- `docs/README.md` has exactly one row for this review (no duplicates). The review header and that row now agree.
- `CRITICAL_MODULES` is unchanged (`1a` + `1b` lists). No new file needs adding: `recanonicalize.py` is at 100% but is not a critical module under the approved designs.
- No untracked files. Nothing in the diff looks like a secret.

**Status:** left at `Addressed — verification found follow-ups`. Handed back to Antigravity (`address-review-findings`) for `E-10-F1` and `E-01-F1`.

## Follow-up resolutions (pass 2, 2026-11-27)

*(Addressed 2026-11-27 on `review/1a-1c-user-test`; awaiting Claude Code pass-2 verification.)*

- **E-10-F1** — Fixed.
  - Restored `SSRFPolicy.validate_url()` (`backend/src/smartrss/security/safe_http.py:131-155`) to its pre-`E-10` fetch-path behavior (control characters, scheme, userinfo, host, and port only), so `SafeHttpClient.fetch()`, redirect hops, `poll_feed`, and `extract_content` accept legitimate query strings (`?url=https://…`, `?tags[]=python`, `?filter[category]=news`, `?x=1|2`) and archive paths (`/web/2024/https://…`).
  - Added a dedicated entry-time validator `validate_feed_url_syntax()` (`backend/src/smartrss/security/safe_http.py:45-64`) that rejects whitespace, `<>"`, unescaped `[`/`]` in `parts.path` (not query/host), and pasted Markdown link joins (`](http(s)://` / `)(http(s)://`), wired into all three feed-entry paths: `POST /api/v1/feeds` and `PATCH /api/v1/feeds/{id}` via `_validate_feed_url()` (`backend/src/smartrss/api/routes/feeds.py:86`) and `POST /api/v1/feeds/import-opml` (`feeds.py:204`).
  - Updated `docs/design/1a-backend-ingestion-and-security.md` §7.2 to document fetch-path `SSRFPolicy.validate_url` vs. entry-time `validate_feed_url_syntax` separately.
  - Tests:
    - `test_e10_f1_validate_feed_url_syntax_rejects_paste_artifacts` & `test_e10_f1_validate_url_and_entry_syntax_accept_legitimate_urls` (`backend/tests/unit/test_ssrf_policy.py`)
    - `test_e10_f1_fetch_and_redirect_allow_query_schemes_brackets_and_pipes` (`backend/tests/boundary/test_safe_http_boundary.py`) — covers (a) direct fetch and (b) redirect `Location` hops.
    - `test_e10_f1_extract_content_allows_query_scheme_brackets_and_pipes` (`backend/tests/integration/test_user_test_regressions.py`) — covers (c) full extraction (`extraction_status == "ok"`, no fallback).
    - `test_e10_feed_url_validation_all_three_paths` (`backend/tests/integration/test_user_test_regressions.py`) — covers (d) RSS-Bridge `POST` → `201`, query-bracket/pipe `PATCH` → `200`, archive-URL OPML import → `imported == 1`, while soak string still → `422` / `rejected_invalid == 1` on all three paths.
  - Evidence (reverted individually):
    - Re-applying `1987f2b`'s strict checks to `SSRFPolicy.validate_url()` failed all 4 regression suites: (a, b) `SSRFBlockedError: URL contains disallowed unescaped characters`; (c) `assert 'fallback_rss' == 'ok'`; (d) `assert 422 == 201`; unit `SSRFBlockedError: URL contains an embedded http(s):// scheme`.
    - Removing `validate_feed_url_syntax()` from `_validate_feed_url` (`feeds.py`) failed `test_e10_feed_url_validation_all_three_paths` (`assert 201 == 422`); removing it from `import_opml_endpoint` failed `test_e10_feed_url_validation_all_three_paths` (`assert 2 == 1`).
    - Disabling each rule inside `validate_feed_url_syntax` (whitespace/`<>"`, path brackets, Markdown join) individually failed `test_e10_f1_validate_feed_url_syntax_rejects_paste_artifacts` (`DID NOT RAISE SSRFBlockedError`).
    - Restored, all passed; all fixed lines in `safe_http.py` (92.34%) and `feeds.py` covered.

- **E-01-F1** — Fixed. Added parametrized boundary test `test_e01_f1_safe_transport_aclose_maps_httpcore_exceptions` (`backend/tests/boundary/test_safe_http_boundary.py:828-851`) verifying that `httpcore.ReadError` and `httpcore.ConnectError` from `transport._pool.aclose()` are mapped to `SSRFConnectionError` (chained via `__cause__`) when exiting `async with SafeHttpClient(...)`.
  - Evidence: removed `with _map_httpcore_exceptions():` from `SafeTransport.aclose()` → `test_e01_f1_safe_transport_aclose_maps_httpcore_exceptions` failed (`httpcore.ReadError: pool close read err`); restored, passed. Fixed lines covered.

- **Owner decisions recorded (2026-11-27):**
  - **`E-09` (guided first-time server setup)** and **`E-11` (headless feed OPML export CLI & backup OPML copy)** scheduled by the project owner alongside **Milestone `1d`** (recorded in `docs/design/README.md` item 4 alongside the `1c` `forceFullResync()` carry-forward).

### Pass-2 verification summary (`make check`)

- Backend: `ruff check` + `ruff format --check` clean; `mypy src` clean (54 source files); **205 passed**, **91.86%** total branch coverage; all 16 critical core modules **>= 90%** (`safe_http.py` 92.34%, `middleware.py` 92.66%, `url_canon.py` 100.00%, `poll_feed.py` 91.98%, `queue.py` 95.12%).
- Client runner: **32 passed**, **94.87%** branch coverage (>= 90% gate).
- Frontend: `eslint` + `prettier --check` clean; `tsc --noEmit` clean; **57 passed** across 6 test files, **93.13%** total line coverage; all critical (>= 90%) and core (>= 80%) coverage gates passed.
