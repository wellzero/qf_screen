# Plan: Optimize `qf-screen-web` as a Daily Quant Product Tool

## Deliverable
Write this review and phased optimization plan to `/home/mnt/quant_volumn/qf_project/qf_screen/doc/plan/PLAN_web_optimization.md` (documentation only — no code implementation in this step).

## 1. Review of Current State

`/home/quant_volumn/qf_project/qf_tools/website/qf_screen` is a clean, well-tested (216 pytest tests) read-only Flask + vanilla-TypeScript SPA wrapper over the `qf-screen` CLI. Its core engineering foundations are strong: strict flag allow-listing (`backend/commands.py`), bounded subprocess execution (`backend/runner.py`), URL-as-state routing (`frontend/src/router.ts`), zero runtime frontend dependencies, and an explicit PEP 660 import guard (`backend/config.py`).

To elevate it from a "CLI in a browser" into a fast, cohesive daily research and monitoring product, four structural gaps stand out:

1. **Latency, Concurrency & Runtime Hygiene**
   - Every view load / refresh / Back-Forward re-spawns `qf-screen` from scratch with no response caching or single-flight deduplication (notably `Briefing`, which auto-runs `report` across all presets on landing).
   - Aborting a browser `fetch` on navigation does **not** terminate the running subprocess in `backend/runner.py`, so clicking between heavy views quickly exhausts `MAX_CONCURRENT = 2` and returns `503 Busy`.
   - `qf-screen-web.service` runs Flask's built-in development server (`app.py`) rather than a production WSGI server, and lacks a lightweight `/api/health` or fast data-freshness summary.
2. **Daily Workflow & Cross-View Friction**
   - Users must type exact canonical codes (`SH600519`) everywhere—no search/autocomplete by 6-digit code (`600519`) or Chinese name (`茅台`).
   - Tables (`frontend/src/table.ts`) have no CSV export or "Copy symbols" action.
   - Views are siloed: no one-click preset switcher across `Run` / `Diff` / `History` / `Validate`, no clickable industry bars in `Run` to filter members, no row-level "Explain" or "Send to Watch" actions, and stock symbols inside CLI HTML report iframes (`Briefing`, `Validate`, `Compare`) don't link into `Inspect`.
   - `Watch` has no saved watchlists—symbols must be pasted or bookmarked in long URLs.
3. **Ad-hoc Strategy Iteration & Controlled Write Gaps**
   - Users cannot fork a built-in preset into an ad-hoc screen, click features in the reference panel to insert them, or validate DSL syntax before running a full screen.
   - Because the app is strictly read-only, users must drop to SSH/terminal to save a user preset, archive a daily snapshot (`--save-history`), or warm/prune the cache.
4. **Security & Ergonomics for Write Operations**
   - Introducing controlled `POST`/`PUT`/`DELETE` endpoints requires explicit same-origin / `X-Requested-With` CSRF protection, strict path-confinement to `user_preset_dir()`, dry-run `ScreenSpec` validation before writing YAML, and clear audit logging.

---

## 2. Proposed Architecture & Phased Plan

Keep the English UI chrome, preserve URL-as-state for shareable views, and keep `qf-screen` subprocess isolation for heavy computations while adding targeted in-process helpers and controlled write endpoints.

### Phase 1 — Speed, Cancellation & Production Hardening
1. **Fingerprint-Keyed Response Cache & Single-Flight (`backend/cache.py`, `backend/api.py`)**
   - Add a bounded in-memory LRU cache (TTL + automatic invalidation keyed on a lightweight fingerprint of `DATA_DIR` / `qf_screen` cache state + `user_preset_dir` mtime).
   - Deduplicate concurrent identical GET requests (single-flight) so two tabs or reloads share one subprocess.
   - Support `?refresh=1` to bypass cache; expose `cached: true` in the JSON envelope and `X-Screen-Cache: HIT|MISS` header on HTML pages.
2. **Subprocess Cancellation on Cancel / Disconnect (`backend/runner.py`, `backend/api.py`, `frontend/src/components.ts`)**
   - Track active subprocesses by request token (`?req_id=...` or server-assigned header) and expose `POST /qf_screen/api/cancel/<req_id>` to invoke `_kill_group(proc)` and release the semaphore slot immediately.
   - Wire `AbortController` cleanup in `frontend/src/router.ts` and a **Cancel** button inside `busyPanel` to fire cancellation when a user navigates away or cancels a slow run.
3. **Production WSGI, Health & Fast Freshness (`qf-screen-web.service`, `backend/routes.py`, `backend/api.py`, `frontend/src/shell.ts`)**
   - Switch `qf-screen-web.service` from `app.py` dev server to `gunicorn` (or `waitress` installed in `/opt/quantfree`, threaded worker model compatible with in-memory cache and semaphore).
   - Add `GET /qf_screen/api/health` (no store load) and lightweight cache/data-freshness summary in `/api/bootstrap` so the sidebar displays the latest session date without running full `data-status`.

### Phase 2 — Daily Workflow & Cross-View Cohesion (Web + Core)
4. **Stock Search & Autocomplete (`backend/symbols.py`, `backend/routes.py`, `frontend/src/components.ts`, `frontend/src/shell.ts`)**
   - Add `GET /qf_screen/api/symbols?q=` reading the lightweight universe census from `qf_screen.data.universe` (cached in-process with mtime invalidation), matching canonical symbols, 6-digit codes, and Chinese names.
   - Add a reusable autocomplete input for `Inspect`, `Run --explain`, `Watch`, plus a global `/` keyboard shortcut in the shell to jump straight to any stock's `Inspect` page.
5. **Table Export & Row Quick Actions (`frontend/src/table.ts`, `frontend/src/views/run.ts`, `frontend/src/views/diff.ts`)**
   - Add **Export CSV** (UTF-8 BOM for Excel compatibility) and **Copy symbols** buttons to `dataTable`'s toolbar.
   - Add row-level quick actions in `Run` (`Explain` opens/updates `--explain` for that stock) and a **Send to Watch** button on `Run` and `Diff` member tables.
6. **Cross-View Drill-Downs & Core HTML Report Linking (`frontend/src/views/*.ts`, `qf_screen/ui/html_report.py`, `frontend/src/reportframe.ts`)**
   - Add a compact preset context strip on `Run`, `Diff`, `History`, and `Validate` to switch between those views for the active `preset` and `as_of` in one click.
   - Make industry bars in `Run`'s Industry Mix clickable to filter the Members table by that industry (click again to clear).
   - In the sibling core repo (`qf_screen/ui/html_report.py` and `qf_screen/cli.py`), support an optional `--symbol-link-template` (or render symbols with a `data-symbol` attribute) so `reportframe.ts` intercepts clicks on symbols inside `Briefing`, `Validate`, and `Compare` iframes and navigates the parent SPA directly to `/qf_screen/inspect?symbol=...`.
7. **Saved Watchlists & Recent Runs (`frontend/src/storage.ts`, `frontend/src/views/watch.ts`, `frontend/src/shell.ts`)**
   - Persist named watchlists and recent screen runs in `localStorage` (`Watch` gets a watchlist selector + Save/Delete/Rename; sidebar gets a compact "Recent" list). URL query remains the source of truth for sharing and Back/Forward.

### Phase 3 — Ad-hoc Screening & Controlled Write Operations
8. **Interactive Ad-hoc Builder & Instant DSL Validation (`backend/api.py`, `frontend/src/views/run.ts`, `frontend/src/components.ts`)**
   - Add a **Fork to Ad-hoc** button on `Run` when viewing a preset, populating `where`, `rank`, `boards`, `top`, and `max_per_industry` into editable ad-hoc controls.
   - Add click-to-insert buttons in `featuresPanel` that insert the feature name at the cursor in the active `where` or `rank` input.
   - Add `POST /qf_screen/api/validate-dsl` using `qf_screen.rules.dsl` in-process to validate filter/rank expressions in milliseconds before submitting a full screen.
9. **Controlled User Preset Management (`backend/presets_api.py`, `backend/api.py`, `frontend/src/views/run.ts`, `frontend/src/views/system.ts`)**
   - Add `POST /qf_screen/api/presets`, `PUT /qf_screen/api/presets/<name>`, and `DELETE /qf_screen/api/presets/<name>`.
   - Enforce: `Origin`/`Referer` + `X-Requested-With` CSRF guard, `PRESET_RE` name validation, strict path confinement to `user_preset_dir()`, dry-run `ScreenSpec.from_yaml` / `ScreenSpec.from_dict` validation before atomic temp-file rename, and refusing deletion of built-ins (only user files / overrides).
   - Add "Save as user preset" in `Run` (from ad-hoc or forked rules) and "Edit / Delete" drawer in `System`, calling `reloadBoot()` and refreshing the sidebar preset list after mutations.
10. **Controlled Operational Writes (`backend/commands.py`, `backend/api.py`, `frontend/src/views/run.ts`, `frontend/src/views/history.ts`, `frontend/src/views/system.ts`)**
    - Add explicit `POST` endpoints (guarded by the same CSRF check and runner concurrency limits):
      - `POST /qf_screen/api/history/save` (`qf-screen run <preset> --save-history`) — button on `Run` and `History` (solves the empty-history onboarding dead end).
      - `POST /qf_screen/api/cache/warm` and `POST /qf_screen/api/cache/prune` — explicit confirmation buttons on `System` (invalidates the web response cache on completion).

### Phase 4 — Testing, Verification & Documentation
11. **Tests, Build & Docs (`website/qf_screen/tests/`, `qf_screen/test/`, `website/qf_screen/README.md`)**
    - Add unit and API tests covering: response cache hit/miss/invalidation/single-flight, process-group cancellation, symbol search, DSL validation, preset CRUD (including path-traversal and invalid-YAML rejection), CSRF header/origin enforcement on all `POST`/`PUT`/`DELETE` routes, and `data-symbol` rendering in `qf_screen/ui/html_report.py`.
    - Run `pytest` in both `qf_screen` and `website/qf_screen`, and run `npm run build` (`tsc --noEmit && vite build`) in `website/qf_screen/frontend`.
    - Update `website/qf_screen/README.md` to document the new controlled-write security model, caching/cancellation behavior, and WSGI deployment.

---

## 3. Notes & Considerations

- **Security boundary**: Because the app sits behind Caddy basicauth on `:8080` and binds `127.0.0.1:8505`, all new write endpoints must remain strictly allow-listed, never pass user strings to a shell, validate CSRF (`X-Requested-With: qf-screen-web` + same-origin check), and confine file writes strictly to `user_preset_dir()` or `qf-screen`'s own `--save-history` / `cache` subcommands.
- **Zero-dependency frontend**: Preserve the vanilla TypeScript + zero runtime dependency architecture of `frontend/` so builds remain fast and `dist/` stays small and self-contained.
- **Process model with in-memory cache**: Use a single multi-threaded WSGI worker (e.g., `gunicorn -w 1 --threads 8` or `waitress-serve --threads=8`) so the in-memory LRU cache, single-flight table, and `MAX_CONCURRENT` semaphore are shared accurately across all requests.

---

# Implementation record (2026-11-27)

All four phases implemented and verified live against the running
service (`systemctl restart qf-screen-web` after the backend edits;
frontend rebuilt into `dist/`).  Nothing is committed, in either repo
(qf_screen stays on branch `v0.3` with the v0.4 work; qf_tools keeps the
app untracked) — same state the v0.4 session left.

## What landed

**Phase 1.**  `backend/cache.py`: a bounded (32 entries, TTL 300 s,
`QF_SCREEN_WEB_CACHE_TTL` / `..._ENTRIES`) in-memory response cache keyed
on `(command, argv, format)` under a **state fingerprint** — a sha1 of a
millisecond walk of exactly what decides an answer: the screener's own
cache directory (merged stores, universe census, derived tables), the
user preset directory and the history archive, plus the package version.
Verified on the real box: a warm repeat run rewrites nothing there, so
the fingerprint is stable while the world is; a data update, a preset
save or a snapshot moves it.  Envelope carries `"cached": true`; pages
carry `X-Screen-Cache: HIT|MISS`; `?refresh=1` bypasses; **single-flight**
shares one subprocess between concurrent identical requests; failures
are never cached.  Cancellation: every request carries a client-made
`req_id`; `POST /api/cancel/<id>` SIGKILLs the process group and frees
the slot at once (answer 499 `cancelled` — including the broken-pipe
`OSError` that accompanies a mid-read kill); the SPA fires it on Abort
(`keepalive`) and the busy panel gained a **Cancel** button.
`app.py` now serves through **waitress 3.0.2** (installed into
/opt/quantfree; one process, 8 threads — the model the cache, the
single-flight table and the semaphore are accurate under), falling back
to Flask's dev server when absent; the unit's ExecStart is unchanged.
`GET /api/health` (no store load) and a bootstrap `freshness` block read
from the merged store's own `meta.json` (`last_complete_session`
2026-11-25, shown in the sidebar).

**Phase 2.**  `backend/symbols.py` + `GET /api/symbols?q=`: the census
(6,297 stocks) in-process with mtime invalidation — canonical symbol,
6-digit code, Chinese name, industry; a reusable `symbolPicker`
(debounced, arrow keys, Enter takes the best hit) in the sidebar (global
`/` shortcut), Inspect, Run's `--explain` and Watch's add box.  Tables
gained **Export CSV** (UTF-8 BOM, honors the filter and sort) and
**Copy symbols**, plus row actions (**Explain**, **+ Watch**) on Run's
members and Diff's entered/exited.  A **preset strip** on Run / Diff /
History / Validate keeps one preset's views one click apart; Run's
industry bars are clickable filters of the members table (click again to
clear).  The CLI now paints an inert `data-symbol` on every stock the
briefing names (`qf_screen/ui/html_report.py`: member cells + change
lists) and `reportframe.ts` turns clicks on them into navigation to
Inspect.  Watch gained named watchlists saved in `localStorage`
(selector, Save / New / Rename / Delete; the sidebar remembers recent
screens too).

**Phase 3.**  `backend/presets_api.py`: `POST /api/presets`,
`GET/PUT/DELETE /api/presets/<name>` — every mutation behind
`require_csrf()` (the SPA's `X-Requested-With: qf-screen-web` token plus
same-origin Origin/Referer), the name confined by the registry's own
pattern (path-shaped names refused), the text validated by the REAL
loader on a probe file inside the target directory, the write an atomic
rename, built-ins untouchable (deleting an override restores the
built-in).  Run grew **Fork to Ad-hoc** (with an honest note when the
preset blends several rank expressions — the ad-hoc screen takes one),
click-to-**insert** feature names at the cursor, **Check the
expressions** (`POST /api/validate-dsl`, in-process, milliseconds,
`factor(...)` accepted with its family verified at run), **Save as user
preset**, and **Save today's snapshot**; History's empty state offers
the same.  `POST /api/cache/warm` and `POST /api/cache/prune` (stale
only, `--dry-run` preview first) live on System with an edit/delete
drawer for user presets; every write drops all cached answers.

**Phase 4.**  Tests: web 249 (was 216) — `test_cache.py` (hit/miss/
refresh/fingerprint-move/page headers/failure-never-cached/TTL/eviction/
single-flight against a blocking stub) and `test_writes.py` (CSRF matrix,
the exact write argvs, preset CRUD incl. path traversal and invalid
YAML left nothing behind, DSL verdicts, process-group cancellation of a
REAL sleeping child, symbols ranking); core `test_v04_product.py` gained
the `data-symbol` markup test.  `ruff check` clean; core `pytest -m
unit` 222 passed, `-m data` 30 passed; `tsc --noEmit && vite build`
clean (73 kB bundle).  README rewritten around the new model; the unit
file's comments and `requirements.txt` updated.

## Verified live (the service on 127.0.0.1:8505)

* warm `run` 0.88 s → identical request **29 ms** from cache; report
  page `X-Screen-Cache: MISS` then `HIT`;
* `?q=茅台` → `SH600519 贵州茅台 食品饮料`; sidebar `/` search → Enter →
  Inspect lands on the stock;
* a mid-flight `validate` cancelled: slot freed immediately, the
  request answered `499 cancelled`, health shows `active: 0`;
* snapshot saved through the web (`run value_dividend --save-history`,
  30 members): History now reads 1 archived session with streaks — the
  empty-archive dead end is gone (this wrote root's archive, which is
  the account the service runs as);
* clicking a symbol inside the CLI's own briefing page navigated the SPA
  to `inspect?symbol=SH601997`;
* CSRF: writes without the token or with a foreign Origin are 403.

## Decisions that differ from the plan's letter

* **Waitress inside `app.py`**, not a new `waitress-serve` ExecStart:
  the unit's env-sourcing line is untouched, `python app.py` by hand
  still works, and the one-process/8-threads model the plan asked for is
  what waitress is configured with.  The unit's comment says so.
* **`data-symbol`, not `--symbol-link-template`**: the plan offered the
  choice; inert markup keeps the CLI's surface unchanged (the standalone
  page ignores it) and the host does the linking.  No new CLI flag.
* **Preset validation via a probe file through `ScreenSpec.from_yaml`**
  (there is no dict entry point on ScreenSpec); the probe is removed
  even on failure.
* **`req_id` is client-generated** (query param on GETs, body field on
  writes) rather than a server-assigned header — it survives the
  response-cache layer, where a HIT never starts a process to assign one.
* **Prune is stale-only** from the web (`--older-than` / `--max-gb`
  remain command-line) — the conservative reading of "explicit
  confirmation buttons".
* Cancel answers **499** (nginx's client-closed-request), not 409/503.

## Bugs found on the way (and fixed)

* a **cross-realm `instanceof`** in the frame's click handler (the
  target belongs to the iframe's realm; the closure's `Element` to the
  parent's) — every symbol click silently did nothing until duck-typed;
* the killed child's **broken pipe** (`OSError` out of `communicate`)
  escaped as a 500 instead of the 499;
* Enter in the symbol picker with no arrow selection now takes the first
  hit (the natural gesture).

## Not done (deliberately)

* no pinyin matching in symbol search (codes, names, industries — as the
  plan's letter lists);
* a forked multi-rank preset keeps only the first ranking expression in
  the ad-hoc box (the CLI's ad-hoc screen itself takes one `--rank`);
  the note on screen says what was dropped;
* the SPA shell carries no cache header (only the CLI pages do — the
  shell is `no-cache` by design);
* `data-status` is cached like any read (TTL-bounded) rather than
  fingerprinted on the raw source tree — the census file in the
  fingerprint covers the common path, and `?refresh=1` exists.
