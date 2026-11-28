# Design Review — Milestone `1d-ops` Component Design (Guided Setup, Headless Feed CLI, Backup OPML & Resync Fencing)

- **Date**: 2026-11-27
- **Scope**: [`docs/design/1d-ops-setup-and-cli.md`](../design/1d-ops-setup-and-cli.md) (status `In Design Review`, commit `8e20ed4` on branch `milestone/1d-ops`). The review also covers the `requirements.md` changes in the same commit (`NFR-OPS-02` and the `1d-ops` milestone row) and the existing code the design changes: `frontend/src/sync/engine.ts`, `frontend/src/db/clientMeta.ts`, `backend/src/smartrss/api/routes/feeds.py`, `backend/src/smartrss/cli.py`, `scripts/backup.sh`, `scripts/restore-drill.sh`, `docker-compose.yml`, `Makefile` and `.env.example`.
- **Specs checked against**:
  - `requirements.md` (`NFR-OPS-01`, `NFR-OPS-02`, §6);
  - `data-model.md` (`categories`, `feeds`);
  - ADRs `0004`, `0005` and `0006`;
  - `development-process.md` §2 and §7, and `review-checklist.md`;
  - the `1a` design (§7.2, §10.2) and the `1c` design (§6, §11);
  - the [`1a`–`1c` user-test review](2026-11-26-1a-1c-user-test-implementation-review.md) (`E-09`, `E-11`) and the [`1c` implementation review](2026-11-25-1c-implementation-review.md) (the `forceFullResync()` carry-forward).
- **Reviewer**: Claude Code (Claude Opus 5.5)
- **Finding prefix**: `O` (`O-01`–`O-11`)
- **Status**: `Addressed (2026-11-27) — verification found follow-ups` (`O-01-F1`, `O-02-F1`, both minor)

## Review outcome

The design is well scoped and stays close to the code it changes. Its strongest parts:

- **No schema change.** There is no migration and no LLM dependency, so the slice really is independent of `1d`.
- **One shared feed service** (`smartrss.feeds.service`) for the API and the CLI. This is the right answer to drift between the two. It keeps the existing `C1`/`C5`/`C7`/`C8`/`D2`/`D3` and `E-10-F1` rules in one place and adds the module to the critical coverage gate.
- **Secret handling for `.env`.** `_render_env.py` generates the password in-process, writes the file `0600` from creation, and never `source`s an existing `.env`. This meets `S3`.
- **A durable resync marker.** The `client_meta` row for `forceFullResync()` is the core of the carry-forward fix. Because `setSyncMeta()` writes only its own key, no in-flight pull in any tab can overwrite the request. And because the reset runs inside a fenced transaction under the lock, a stolen IDB lease aborts it atomically.
- **Backup changes don't touch restore.** The new `.opml` files can't be picked up by `restore-drill.sh`, which selects `smartrss-*.dump` (`restore-drill.sh:29`, `:35`).

**Main risks:**

1. **Cron mode never rebuilds the worker image** (`O-01`). `docker compose up --build` skips the `worker` service when `COMPOSE_PROFILES` is empty. So the idempotent setup and upgrade path, and the new systemd timer, keep running whatever `smartrss-worker` image was built first, against a newly migrated schema. This happens on this host today.
2. **A cross-tab resync can stall under the IDB-lease fallback** (`O-02`). The design's "never stranded" mitigation sets a flag that the lock-holding tab never reads, and the waiter path re-syncs only when the outbox is non-empty. A request is never *lost*, because the marker is durable, but it can wait for the 60-second poll timer or for a hidden tab to become visible.
3. **An OPML failure blocks the off-host copy of a good dump** (`O-03`). Exiting at the OPML step, before `rsync`, means a broken `api` image or a mid-upgrade window stops a verified `pg_dump` from leaving the host.

| Severity | Count | IDs |
| :--- | :--- | :--- |
| Blocker | 0 | — |
| Major | 1 | `O-01` |
| Minor | 6 | `O-02`–`O-07` |
| Nit | 4 | `O-08`–`O-11` |

**Recommendation:** fix `O-01` before approval, and settle `O-02`–`O-07` in the design text and test plan. None of them needs an architectural change. The owner also needs to approve the proposed `NFR-OPS-02` requirement and the `1d-ops` milestone row (§Owner questions).

### Claims verified vs. inferred

| Claim in design | Result |
| :--- | :--- |
| `/api/v1/health` returns `{"status":"ok","db":"ok"}` | **Verified** (`api/routes/health.py:25`; there is also a `version` field, so the check must be a subset match). |
| `.env.example` has `LAN_PORT`, `WORKER_MODE`, `COMPOSE_PROFILES`, `SSRF_BLOCKED_CIDRS`, and the `DATABASE_URL` format | **Verified**. |
| `docker compose ps --status running --services` works | **Verified** on Compose 2.40.3. |
| `OnCalendar=*:0/15` is valid | **Verified** (`systemd-analyze calendar` → `*-*-* *:00/15:00`). |
| `%t` in a user unit means `$XDG_RUNTIME_DIR` | Inferred (documented systemd specifier behaviour). |
| A worker started before `db migrate` doesn't fail `up --wait` | **Verified**: the scheduler and reaper loops catch exceptions (`worker/runner.py:634-641`). It does log errors until the migration runs (`O-10`). |
| `setSyncMeta()` writes only `key: "sync_meta"` | **Wrong key name**: it writes `key: "sync"` (`db/clientMeta.ts:38`). The conclusion still holds (`O-08`). |
| Category names are unique case-insensitively | **Verified**: `uq_categories_active_name` on `LOWER(name) WHERE deleted_at IS NULL` (`0001_milestone_1a_core.py:157-163`). This affects `O-05`. |
| The OPML export includes `paused` and `muted` feeds | **Verified** (`feeds.py:323`). This affects `O-04`. |
| The worker image is built by `make up` in cron mode | **Refuted**: with `COMPOSE_PROFILES=` the active services are `db api web` (`O-01`). |

---

## 1. Major

### O-01 — Cron mode never rebuilds the worker image, so setup re-runs and the systemd timer run stale worker code against a migrated schema

**Where:** design §4.2 step 4 (`docker compose up -d --build --wait`) and step 5 (the timer's `docker compose run --rm -T worker …`); `docker-compose.yml:52-60` (`worker` has its own `build:` and `profiles: [daemon]`); `Makefile:95-97` (`up` has the same gap, so this predates `1d-ops`, but `1d-ops` makes it the supported path).

**Problem:** the worker is a separate image (`smartrss-worker:latest`), built only when the `daemon` profile is active. With `WORKER_MODE=cron`, setup writes `COMPOSE_PROFILES=` (empty), so `up --build` builds only `api` and `web`. Reproduced: `COMPOSE_PROFILES= docker compose config --services` → `db api web`. `docker compose run worker` builds the image only if it is **missing**. After that it reuses the cached image indefinitely.

**Failure scenario:**

1. The owner runs `git pull && make setup` to upgrade, as §4.2 promises ("safe to re-run").
2. `api` is rebuilt and `db migrate` applies the new revision.
3. Every 15 minutes the timer still runs the **old** worker.

Today this would have meant the worker missing the `E-01` `SafeHttpClient` failover and exception mapping (the fetches happen in the worker), the `E-08` 304 outcome and the `E-12` canonicalization. A later migration that renames or drops a column would make every batch fail. On this host, the `smartrss-worker` image is 40 hours old, so it predates `1987f2b`. During the user test, the cron worker ran on an image that matched only by chance.

**Suggested fix:**

- Build every image regardless of mode, before `up`. Use `docker compose --profile daemon build` (or `build api worker web`), in both `setup-server.sh` and `make up`.
- A simpler structural alternative: give `api` and `worker` the same `image: smartrss-backend:local`, with `build:` only on `api`, so one build serves both services.
- Optionally add an image-age or version check: the run-once JSON already reports nothing about versions, so log `__version__` and compare it with `/api/v1/health.version` in setup's final summary.
- **Tests:** in `1d-ops-T04`, the mocked `docker` records its argv. Assert that cron mode builds the worker image (or that both services share one image tag), and that a second run rebuilds it. If you go the shared-image way, add a compose-config test that `api` and `worker` resolve to the same image.

---

## 2. Minor

### O-02 — Under the IDB-lease fallback, a cross-tab resync can wait up to 60 s (or until the tab is visible); the `MAX_DRAIN_LOOPS` mitigation is a no-op

**Where:** design §6.2 step 5 and §6.3 rows 3 and 7; `frontend/src/sync/engine.ts:208` (own broadcasts are ignored), `:216-223` (the `sync_complete` handler re-triggers only when `outbox.count() > 0`), `:509-523` (the waiter under the IDB lease sets `recheckOnSyncComplete`), and `:242-253` (the 60 s poll timer, which skips hidden tabs).

**Problem:**

1. **Step 5 doesn't work.** It sets `this.recheckOnSyncComplete = true` in the **lock-holding** tab when `loops >= MAX_DRAIN_LOOPS`. That flag is read only when a `sync_complete` arrives from *another* tab. The holder's own broadcast is filtered out at `:208`, and no other tab is completing a cycle at that moment. Nothing re-triggers.
2. **The waiter path ignores resync requests.** Take this IDB-lease interleaving:
   - Tab B is on its last drain iteration and has already evaluated `!dirty && remaining === 0 && !pendingResync`.
   - Tab A then persists the request, fails `tryRunLockedCycle(true)`, and sets `recheckOnSyncComplete`.
   - Tab B releases the lock and broadcasts `sync_complete`.
   - Tab A's handler sees an empty outbox and does **not** call `triggerSync()`.

   The same happens with no `BroadcastChannel` at all. The request is picked up only by the next 60 s poll tick in a *visible* tab.

The §6.3 claims "Tab A acquires lock right after `sync_complete`" and "Zero lost resyncs even if `BroadcastChannel` is unavailable" are true only in the weaker sense of "eventually, through the poll timer".

**Suggested fix:**

- Re-trigger in the `sync_complete` handler when `outbox.count() > 0 || (await getFullResyncRequest(db)) !== null`.
- In the holder tab, after `withSyncLock` releases, check `getFullResyncRequest()` once and call `scheduleDebouncedSync()` if a request is still pending. Drop the `recheckOnSyncComplete` assignment from step 5.
- Reword the §6.3 rows to say exactly what is guaranteed.
- **Tests:** extend `1d-ops-T10` with:
  - (a) an IDB-lease interleaving where the request lands after Tab B's last drain check, with an **empty outbox**. Assert Tab A starts a cycle on `sync_complete` without advancing fake timers.
  - (b) the `MAX_DRAIN_LOOPS` exhaustion case. Assert that a follow-up cycle runs after the lock is released.

  Both must fail against the design as written.

### O-03 — An OPML export failure exits before `rsync`, so a verified dump never leaves the host

**Where:** design §5.3 step 3 ("exit `1`") placed before step 4 (retention and `rsync`); D05.

**Problem:** the OPML step runs after `pg_dump` and `pg_restore --list` succeed, but it depends on a different component: the `api` image and the Python CLI. If that step fails, `backup.sh` exits `1` before the off-host copy. Two examples: an `api` image that fails to start during an upgrade, or `run --rm --no-deps` failing because the image isn't built yet.

**Failure scenario:** the nightly backup runs while the owner is midway through a broken upgrade. The verified dump stays only on the server disk, which is exactly the disk an off-host copy exists to protect against. The off-host copy is skipped every night until someone reads the journal.

**Suggested fix:** treat the OPML snapshot as best-effort *within* the run, but fail the exit status *at the end*:

1. dump → verify;
2. OPML (record whether it failed);
3. prune;
4. `rsync` (always, if the dump is good);
5. `exit 1` with "OPML snapshot failed" if step 2 failed.

This keeps D05's "alert, don't silently skip". Also prune `.opml` and `.dump` independently, so a missing OPML never stops a dump from being pruned.

**Test:** in `1d-ops-T05`, make the export fail while `BACKUP_RSYNC_TARGET` is set. Assert that `rsync` was still called with the new `.dump`, that no `.opml` or `.opml.tmp` is left behind, and that the exit code is non-zero.

### O-04 — The OPML snapshot loses status and per-feed settings, so "restores all feeds" overstates it, and re-importing turns muted feeds active

**Where:** design §5.1 item 5, `1d-ops-T07` ("importing into a fresh DB restores all feeds and categories"), and `NFR-OPS-02` / the `NFR-OPS-01` "OPML/config export"; `feeds.py:323` exports `active`, `paused` and `muted`.

**Problem:** the export writes `xmlUrl`, `htmlUrl`, the title and the category outline only. Status (`paused` / `muted`), `poll_interval_minutes`, `fetch_full_text`, `expiration_days_override`, `research_policy` and `curation_instructions` are lost. Import creates every new feed as `active` with defaults.

**Failure scenario:**

1. The owner restores from the OPML snapshot because the dump is unusable.
2. Muted feeds come back active and flood the reader.
3. Paused feeds start polling.
4. Per-feed full-text and curation settings, which `1d`/`1g` will rely on, are reset.

**Suggested fix:** pick one, as an owner decision (§Owner questions):

- **(a)** Document the OPML file as a *portability snapshot*: `pg_dump` is authoritative, and the file holds subscriptions and categories only. Narrow `T07` and the requirement wording to match.
- **(b)** Add namespaced attributes such as `xmlns:smartrss` with `smartrss:status`, `smartrss:pollInterval` and `smartrss:fullText`. Export writes them, the import honours them for **new** rows only, and other readers ignore them.

Either way, add a test that a `muted` feed round-trips as documented.

### O-05 — Creating a category by name in the CLI races the `LOWER(name)` unique index, and `--create-category` defaults to `True`

**Where:** design §5.1 item 3 (the `category_name` path), §5.2 (`--create-category` "default `True` for name"), and `uq_categories_active_name` (`0001_milestone_1a_core.py:157-163`).

**Problems:**

1. **The `FOR SHARE` lock doesn't prevent the race.** Two concurrent creates with a new name (`feeds add` twice, or `feeds add` alongside an OPML import or `POST /categories`) can both see the name as missing, because locking the existing rows can't stop someone inserting a new one. The second insert raises `IntegrityError` on the unique index. The design defines no mapping for it, so the CLI prints a traceback, not exit `2`. The API has an `IntegrityError` mapping in `categories.py:62`, but the shared service doesn't.
2. **A typo silently creates a category.** With creation on by default, `--category Tehc` creates "Tehc" and syncs it to every client.

**Suggested fix:**

- In `create_or_reactivate_feed`, catch `IntegrityError` on the category insert: roll back to a savepoint (`begin_nested()`), select the winner by `LOWER(name)`, and use it.
- Make creation opt-in (`--create-category`, default `False`), and fail with a clear "category not found (use `--create-category`)" message, exit `2`.
- **Test:** add a real-Postgres test with two sessions inserting the same new name (forced overlap with a barrier, `T6`). Assert that one category results, that both feeds reference it, and that both exit `0`.

### O-06 — The worker timer silently never fires on a headless server without linger, and setup can double-schedule alongside an existing crontab entry

**Where:** design §4.2 step 5 (prints only a `loginctl enable-linger` reminder) and the unit's `flock -n %t/smartrss-worker.lock`; README `E-05` crontab line (`~/.local/state/smartrss/worker.lock`).

**Problems:**

1. **No linger means no timer.** A user timer on a host where the user isn't logged in (the normal state for a home server after a reboot) doesn't run until someone logs in. Setup still ends with a success summary, so feeds quietly stop updating after the first reboot.
2. **Two schedulers, two lock files.** This host ran the crontab line from the README during the user test. Installing the timer on such a host leaves two schedulers holding *different* lock files, so they don't exclude each other. Leases keep this safe, but it doubles container start-ups and log noise.
3. **A skipped run counts as a failed unit.** `flock -n` exits `1` when the lock is busy, so every overlapping tick marks the unit failed.

**Suggested fix:**

- After enabling the timer, check `loginctl show-user "$USER" -p Linger --value`. If it isn't `yes`, print a prominent warning in the final summary, with the exact `sudo loginctl enable-linger "$USER"` command, and say that the timer won't run after a reboot until that is done.
- Detect an existing SmartRSS crontab entry (`crontab -l | grep 'smartrss worker run-once'`) and warn, or refuse, unless the user confirms. Use one shared lock path for both mechanisms.
- Use `flock -n -E 75` with `SuccessExitStatus=75` in the unit.
- **Tests:** in `T04`, use a mocked `loginctl` returning `no` and assert the warning text, and use a mocked `crontab -l` containing the README line and assert the warning.

### O-07 — The primary LAN host for the printed install command is unspecified, and the defaults can produce an origin other machines can't reach

**Where:** design §4.2 step 3 (auto-detecting the IPv4 via `ip route get` plus `hostname -s`) and step 7 (`make install-client API_ORIGIN=http://<primary-lan-host>:<LAN_PORT>`).

**Problems:**

1. **The bare hostname usually doesn't resolve.** The design adds both the IP and `hostname -s` to `ALLOWED_HOSTS` and `CORS_ALLOW_ORIGINS`, but never says which one is "primary". A bare short hostname usually doesn't resolve from other machines on a home LAN unless there is local DNS or mDNS (`<host>.local`). If it's chosen, the printed command produces a client pointing at an origin it can't reach.
2. **DHCP can change the IP.** If the server's DHCP address changes, every origin, `ALLOWED_HOSTS` and the client config break together. Setup never mentions this, and re-running it doesn't help, because the existing `.env` is preserved.

**Suggested fix:**

- Define the primary host as the first `--lan-host` if given, otherwise the detected IPv4.
- Include `<hostname>.local` only when `getent hosts <hostname>.local` resolves (that is, Avahi is running).
- Print a one-line note recommending a DHCP reservation, and add a README note on how to add a host to an existing `.env` (`ALLOWED_HOSTS`, `CORS_ALLOW_ORIGINS`, `make up`).
- **Test:** in `T01` / `T04`, assert the chosen primary host for each input combination.

---

## 3. Nit

### O-08 — The offline row in §6.3 and the `setSyncMeta` key name are inaccurate

**Where:** §6.2 step 1 ("`setSyncMeta()` only writes `key: "sync_meta"`") and the §6.3 "Detached (offline)" row, `1d-ops-T12`; `db/clientMeta.ts:38` (the actual key is `"sync"`); `engine.ts:587-603` (the probe runs only when the snapshot is already `detached` or `misconfigured`).

**Problems:**

- **The key name.** The real key is `"sync"`. The conclusion still holds, but the text should match the code.
- **The offline claim.** A tab whose snapshot still says `online` when the network silently drops skips the probe, consumes the request, and sets `sync_cursor: null`, before the bootstrap request fails and the tab goes `detached`. This is harmless, since the null cursor keeps the request alive and the next online cycle bootstraps. But "`sync_cursor` stays intact while offline" holds only for a tab that was *already* `detached`, which is how `T12` must be set up.

**Fix:** correct the wording, and state the `T12` precondition (the snapshot is `detached` before the click).

### O-09 — The "never overwrite `.env`" rule uses check-then-`os.replace()`

**Where:** §4.2 step 3 / D04.

**Problem:** `os.replace()` overwrites unconditionally. The "does it exist?" check and the replace are two steps, so a concurrent `make setup`, or a user copying `.env.example` at the same moment, can be overwritten.

**Fix:** after writing and fsyncing `<output>.tmp`, use `os.link(tmp, output)`, which fails with `EEXIST`, and then unlink the temporary file. On `EEXIST`, take the "Preserved existing .env" path. Add a `T01` case with a pre-existing target created between the check and the link (monkeypatch the check).

### O-10 — On first setup in daemon mode, the worker starts before the migration; the user unit has a no-op `After=network-online.target`

**Where:** §4.2 step 4 and the step 5 unit text.

**Problems:**

- **Error noise before migration.** `up -d --build --wait` starts the worker before `db migrate`. Its scheduler loop then logs an `UndefinedTable` traceback every tick until the migration runs (`runner.py:634-641`). It's harmless, but it is the first thing a new user sees in `docker compose logs`.
- **A no-op ordering line.** User managers can't order against the system `network-online.target`, so `After=network-online.target` has no effect.

**Fix:** run `up -d --build --wait db api`, then migrate, then `up -d --wait`, which starts the rest. Drop the `After=` line, or note that it has no effect.

### O-11 — With `feeds add --if-not-exists`, an existing `paused` or `muted` feed exits `0` and stays paused

**Where:** §5.2 `feeds add`.

**Problem:** a script calling `feeds add --if-not-exists URL` expects the feed to be live afterwards. A paused or muted match exits `0` silently.

**Fix:** print the existing status, for example `already subscribed (status=paused)`, and in `--json` include `"status"` and `"created": false`. Optionally return a distinct exit code (for example `3`) for "exists but not active". Add a `T06` case.

---

## Owner questions / trade-offs

- **Q-1 — `NFR-OPS-02` and the `1d-ops` milestone row** were added to the approved `requirements.md` in commit `8e20ed4` and marked "proposed". Per the spec-first rule, the owner needs to approve the requirement text and the decision to split `1d` into `1d-ops` and `1d-llm` before this design can be `Approved`.
- **Q-2 — OPML fidelity (`O-04`):** should the backup OPML be a portability-only snapshot (a), or carry SmartRSS extension attributes so that an OPML-only restore keeps status and per-feed settings (b)?
- **Q-3 — Cron-mode scheduling default:** should setup install the systemd user timer by default in cron mode, which needs linger on a headless host (`O-06`), or keep documenting the crontab line? A timer gives journald logging and `Persistent=true` catch-up. A crontab works without linger.
- **Q-4 — Shared backend image (`O-01`):** should `api` and `worker` share one image tag? That removes this whole class of version skew, but changes `docker-compose.yml` outside the design's stated file list.

---

## Resolutions

*(Addressed 2026-11-27 on `milestone/1d-ops`; awaiting Claude Code verification pass and project owner decisions on `Q-1`–`Q-4`.)*

- **O-01** — Fixed. Adopted shared `image: smartrss-backend:local` on both `api` (with `build: { context: ./backend }`) and `worker` in `docker-compose.yml` (`1d-ops-D07`, added `docker-compose.yml` to §3), and updated both `scripts/setup-server.sh` (§4.2 step 4) and `Makefile` `up` (§4.1) to run `docker compose build api web` explicitly before starting services so cron mode always upgrades the worker image. Added compose-config shared-image assertion and cron-mode rebuild assertions to `1d-ops-T04`. *(Owner confirmation requested under `Q-4`.)*
- **O-02** — Fixed. Updated `sync_complete` handler in §6.2 step 2 to re-trigger when `outbox.count() > 0 || (await getFullResyncRequest(this.db)) !== null`. Replaced the broken `recheckOnSyncComplete` assignment in the holder tab (§6.2 step 5) with a post-release `getFullResyncRequest()` check calling `scheduleDebouncedSync(0)`. Reworded §6.3 interleaving rows accurately and extended `1d-ops-T10` with both (a) empty-outbox IDB-lease `sync_complete` re-trigger without advancing fake timers and (b) `MAX_DRAIN_LOOPS` exhaustion follow-up.
- **O-03** — Fixed. Reordered `scripts/backup.sh` (§5.3, `1d-ops-D05`) so OPML export is best-effort *within* the run (logging error and recording `OPML_FAILED=1` without exiting early), local & remote `.dump` and `.opml` pruning run independently, off-host `rsync` always runs for a verified dump, and `backup.sh` exits `1` at the very end if OPML export failed. Extended `1d-ops-T05` with the OPML-failure + `BACKUP_RSYNC_TARGET` test case.
- **O-04** — Fixed (option (a) proposed; *owner confirmation requested under `Q-2`*). Narrowed `NFR-OPS-02` (`docs/requirements.md`), `1d-ops-D05`, §5.1 item 5, §5.3, and `1d-ops-T07` to explicitly define the backup OPML file as a standard OPML 2.0 **portability snapshot** (subscriptions + categories only; `pg_dump` is authoritative for feed `status` and per-feed settings). Extended `1d-ops-T07` to test `muted`/`paused` feed behavior on both same-DB re-import and fresh-DB import.
- **O-05** — Fixed. Changed `--create-category` default to `False` (opt-in) in `smartrss feeds add --category <name>` (§5.2), exiting `2` with a `--create-category` hint when missing. Wrapped new-category `INSERT` in `create_or_reactivate_feed` and `import_opml_subscriptions` (§5.1 items 3–4) in a savepoint (`session.begin_nested()`) catching `IntegrityError` on `uq_categories_active_name` and re-selecting the winning active category `FOR SHARE`. Added forced-overlap real-Postgres concurrency test (`T6` barrier) to `1d-ops-T06`.
- **O-06** — Fixed. Added post-enable `loginctl show-user "$USER" -p Linger --value` check with prominent `WARNING` + exact `sudo loginctl enable-linger "$USER"` command in Step 7 (§4.2 step 5 & 7); added existing `crontab -l` detection for `smartrss worker run-once` with warning/confirmation; unified both the systemd unit and README crontab on the shared lock path `%h/.local/state/smartrss/worker.lock` (`~/.local/state/smartrss/worker.lock`); switched the unit to `flock -n -E 75` + `SuccessExitStatus=75`. Extended `1d-ops-T04` with mocked `loginctl` (`no`) and mocked `crontab -l` assertions. *(Owner confirmation on cron-mode timer default requested under `Q-3`.)*
- **O-07** — Fixed. Defined primary LAN host deterministically in §4.2 step 3 (first `--lan-host` if provided, else detected IPv4, never bare short hostname), gated `<hostname>.local` inclusion on `getent hosts "$(hostname -s).local"` resolving, added a DHCP reservation reminder to Step 7, added existing-`.env` IP-change instructions to §9 (`README.md`), and added primary-host selection cases to `1d-ops-T01` / `1d-ops-T04`.
- **O-08** — Fixed. Corrected `setSyncMeta()` key name from `"sync_meta"` to `"sync"` (`clientMeta.ts:38`) in §2 (`1d-ops-D06`), §6.1, and §6.2. Clarified §6.3 "Already `detached`" row and `1d-ops-T12` precondition (snapshot is already `detached` before clicking `forceFullResync()`; also documents the silent-drop-while-`online` fallback).
- **O-09** — Fixed. Updated `_render_env.py` (`1d-ops-D04`, §4.2 step 3) to open `<output>.tmp.<pid>` with `O_WRONLY | O_CREAT | O_EXCL` (`0o600`), fsync, and commit via `os.link(tmp_path, output_path)` (handling `FileExistsError` cleanly as `"Preserved existing .env"`) before unlinking `tmp_path` in `finally`. Added concurrent pre-existing-target test case to `1d-ops-T01`.
- **O-10** — Fixed. Updated §4.2 step 4 startup order to `docker compose build api web` → `docker compose up -d --wait db api` → `docker compose exec -T api smartrss db migrate` → `docker compose up -d --wait` (no pre-migration worker errors), made `/api/v1/health` JSON check a subset match (`status == "ok"` and `db == "ok"`), and dropped the no-op `After=network-online.target` line from `deploy/systemd/smartrss-worker.service` (§4.2 step 5).
- **O-11** — Fixed. Specified `smartrss feeds add --if-not-exists` (§5.1 item 1, §5.2): prints existing status (e.g., `Already subscribed (status=paused)`), emits `"status"` and `"created": false` in `--json`, and returns exit `0` when `active` vs. exit `3` when `paused` or `muted`. Added both cases to `1d-ops-T06`.

### Owner decisions recorded (2026-11-27)

- **`Q-1`:** Approved `NFR-OPS-02` and the `1d-ops` milestone split in `docs/requirements.md`.
- **`Q-2` (`O-04`):** Approved option **(a)** — portability-only OPML 2.0 snapshot (subscriptions + categories; `pg_dump` authoritative for feed status and per-feed settings).
- **`Q-3` (`O-06`):** Approved opt-in `systemd` user timer in `make setup` (with `loginctl` linger check + prominent warning, crontab duplicate detection, shared lock path, and `flock -n -E 75` / `SuccessExitStatus=75`) while keeping crontab documented in `README.md`.
- **`Q-4` (`O-01`):** Approved sharing `image: smartrss-backend:local` between `api` and `worker` in `docker-compose.yml` (`1d-ops-D07`).

### Design-only check summary

- No application code changed; `make check` verified green (`205` backend passed, `91.86%` total, all 16 critical modules >= 90%; `32` runner passed, `94.87%`; `57` frontend passed, all gates OK). Ready for Claude Code verification pass.

## Verification pass (2026-11-27)

**Verifier:** Claude Code, on `milestone/1d-ops` at `8b947b0`. The resolutions are in `a857dcc` and the owner decisions in `8b947b0`.

**Scope:** both commits change the design only. There are no code changes, so there was nothing to revert. Each finding was checked against the revised text of `1d-ops-setup-and-cli.md`, the `requirements.md` diff and the test plan. Where a fix rests on tool behaviour, I tested the behaviour directly.

**`make check`:** green. Backend: 205 passed, 91.83% coverage, and all 16 critical modules ≥ 90%. Runner: 32 passed, 94.87%. Frontend: 57 passed. These match the resolution notes; the 0.03-point coverage difference is run-to-run variation.

| Finding | Verdict | Evidence |
| :--- | :--- | :--- |
| `O-01` (Major) | ⚠️ partially resolved → `O-01-F1` | `1d-ops-D07` adds a shared `image: smartrss-backend:local`, and `make up` and setup both run `build api web` before `up`. That fixes the reported skew: every `up` or setup re-run now rebuilds the image the cron worker uses. **New gap:** the worker no longer has a `build:` section. I tested this with a throwaway Compose project of the same shape (`api` with `image:` + `build:`; `worker` with the same `image:`, profile-gated). `docker compose up` builds through `api` correctly. But `docker compose run --rm worker …`, which is exactly what the timer and the crontab run, tries to **pull** the image when it is absent locally and fails: `pull access denied for … repository does not exist`. See `O-01-F1`. |
| `O-02` | ⚠️ partially resolved → `O-02-F1` | The `sync_complete` handler now re-triggers on `outbox > 0 \|\| resyncReq !== null`. The `recheckOnSyncComplete` assignment in the holder tab is gone and replaced by a post-release check plus `scheduleDebouncedSync(0)`, and `T10` (a) and (b) are added. That covers the empty-outbox interleaving and the `MAX_DRAIN_LOOPS` case. **New gap:** the post-release check can spin without delay while detached. See `O-02-F1`. |
| `O-03` | ✅ | §5.3 step order: the dump is fatal on error; the OPML export is best-effort with `OPML_FAILED`; `.dump` and `.opml` are pruned independently, locally and remotely; `rsync` always runs for a verified dump; the deferred `exit 1` comes last. `T05` (d) asserts that `rsync` is still called and nothing is left behind. *Implementation note:* `backup.sh` runs under `set -euo pipefail`, so the export pipeline must be wrapped (`if ! …; then OPML_FAILED=1; fi`). Otherwise `set -e` exits before `rsync` and reintroduces `O-03`. `T05` (d) would catch this. |
| `O-04` | ✅ | The owner chose option (a) under `Q-2`. The portability contract is now stated in §5.1 item 5, D05 and `NFR-OPS-02`. `T07` now covers re-importing `muted` / `paused` feeds into the same DB, and the fresh-DB "`active` with defaults" behaviour. |
| `O-05` | ✅ | `--create-category` defaults to `False` and exits `2` with a hint when the category is missing. The category insert runs in a savepoint (`begin_nested()`) on both `create_or_reactivate_feed` and `import_opml_subscriptions`. On `IntegrityError` it re-selects the winner `WHERE LOWER(name) = LOWER(:name)`, and a `category` upsert is emitted only by the creating transaction. Under READ COMMITTED, the unique index makes the second inserter wait for the first to commit, then raise; the re-select then sees the committed row, so the protocol is sound. The concurrency test is `T06` (b), a barrier-forced real-PG overlap. |
| `O-06` | ✅ | The `loginctl … Linger` check is followed by a summary `WARNING` with the exact command. Setup detects an existing `smartrss worker run-once` crontab entry, warning when interactive and printing a `WARNING` when non-interactive. The unit and the README crontab now share the lock `%h/.local/state/smartrss/worker.lock`, with `ExecStartPre=mkdir -p`. The unit uses `flock -n -E 75` + `SuccessExitStatus=75` (I confirmed `-E` exists in util-linux `flock`). `T04` adds mocked `loginctl` / `crontab`. The owner chose an opt-in timer under `Q-3`. |
| `O-07` | ✅ | The primary host is the first `--lan-host`, otherwise the detected IPv4, never a bare short hostname. `<hostname>.local` is added only when `getent` resolves it. The summary includes a DHCP-reservation note, and §9 adds the README instructions for an IP change. Cases are added in `T01` / `T04`. |
| `O-08` | ✅ | The key is now `"sync"` (`clientMeta.ts:38`) in D06, §6.1 and §6.2. The §6.3 row is now "Already `detached`", and also describes the case where the network drops while the snapshot still reads `online`. The `T12` precondition is stated. |
| `O-09` | ✅ | A temp file opened `O_EXCL` at `0o600` → `fsync` → `os.link(tmp, output)`. On `FileExistsError`, setup takes the dedicated "preserved" exit path, and the temp file is unlinked in `finally`. The `T01` concurrent-creation case is added. |
| `O-10` | ✅ | The startup order is now `build api web` → `up -d --wait db api` → `db migrate` → `up -d --wait`. The health check is a subset match that tolerates `version`. `After=network-online.target` has been removed from the unit. |
| `O-11` | ✅ | `--if-not-exists` on a `paused` / `muted` match prints the status, puts `"status"` and `"created": false` in the JSON, and exits `3`. `FeedAlreadySubscribedError` carries `existing_status`. Cases are added in `T06`. |

### Follow-ups

- **`O-01-F1` (Minor): `docker compose run worker` fails when the shared image is missing locally, because the worker no longer has a `build:` section.**
  - **Failure scenario:** in cron mode, the owner runs `docker image prune -a` or `docker system prune -a`, or `make down` gets a `--rmi` option. Every 15 minutes the timer then fails with `pull access denied for smartrss-backend`, until someone runs `make up` or `make setup`. The `SuccessExitStatus=75` change hides overlap skips, but not this, so the unit shows as failed. Before `D07`, `run` would simply have built the image.
  - **Expected fix:** give `worker` the same `build: { context: ./backend }` as `api`, keeping the shared `image:` tag, so `run` can build on demand. With BuildKit caching, a second build in daemon mode is close to free; confirm Compose doesn't build twice, or accept a cached second build. Alternatively, add `pull_policy: never` to `worker` and a pre-check in the unit (`ExecStartPre=` `docker image inspect smartrss-backend:local` with a clear message). That fails less confusingly but doesn't self-heal.
  - **Test:** extend `T04` (a) with a compose-config assertion that the `worker` service can build (has `build:`) or has `pull_policy: never`, and a mocked or real `docker compose run` with the image absent that doesn't attempt a registry pull.
- **`O-02-F1` (Minor): the holder tab's post-release check can spin without delay while detached.** §6.2 step 5 reschedules with `scheduleDebouncedSync(0)` when a request is pending after release, "if `loops >= MAX_DRAIN_LOOPS` (or if another request arrived between the final drain check and lock release)". The parenthetical can only be implemented as "a request is pending after release". But the "Already `detached`" row in §6.3 deliberately leaves the request pending, because the probe returns `detached` before consumption.
  - **Failure scenario:**
    1. The tab is detached with a pending request.
    2. A cycle starts, the probe returns `detached`, and the lock is released.
    3. The post-release check sees the request and calls `scheduleDebouncedSync(0)`.
    4. The next cycle probes and fails again, and so on.

    The result is a tight loop of reachability probes, with CPU and battery cost, for as long as the laptop is offline. The same happens with a `misconfigured` origin, which also returns before consumption.
  - **Expected fix:** reschedule only when the request was **not** observed by the last `executeSingleCycle()`. For example, record the `request_id` that cycle read (or "none"), and compare it after release. Alternatively, reschedule only when the last cycle ended with `progressed === true`. Leave the detached, misconfigured and auth-revoked cases to the existing 60 s poll timer and the `online` / `visibilitychange` triggers. Reword step 5 without the open-ended parenthetical.
  - **Test:** add a `T12` (or `T10` (c)) case with a pending request while `detached` under fake timers. Assert that the number of probe calls stays bounded (≤ 1 per poll tick) as the clock advances, and that the resync still runs once a probe succeeds.

### Observations (no action required)

- After `D07`, the old `smartrss-api:latest` and `smartrss-worker:latest` images stay as unused local images (about 330 MB each on this host). Consider a one-line README note (`docker image rm smartrss-api smartrss-worker`) in the §9 sync.

### Repo-wide drift

- `docs/README.md` (requirements and component-design rows), `docs/design/README.md` item 4, the design header and `AGENTS.md` all say `1d-ops` is `In Design Review`, so they agree.
- `requirements.md` records `NFR-OPS-02` and the `1d-ops` split as approved by the owner on 2026-11-27 (`Q-1`).
- There is one review row, with no duplicates. No untracked files.
- *Process note:* `a857dcc` and `8b947b0` are local; `milestone/1d-ops` is 2 commits ahead of `origin`.

**Status:** left at `Addressed — verification found follow-ups`. Handed back to Antigravity (`address-review-findings`) for `O-01-F1` and `O-02-F1`.
