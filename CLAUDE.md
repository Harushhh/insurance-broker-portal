# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Django portal for an insurance broker: it ingests MIS (Management Information System) files from insurers, maps each policy row against a rate-card database to calculate payout, tracks rate-card health/overlap, and exposes dashboards for RTO, Make/Model, Pincode, and Health rate masters. A separate Next.js app (`life-payout-grid/`) is linked from the sidebar for life-insurance commission grids.

## Commands

Run from the repo root with the venv active (`venv\Scripts\python.exe` on Windows, no separate activation needed if invoking that binary directly).

```bash
python manage.py runserver 127.0.0.1:8000    # dev server (also runnable via .claude/launch.json "django-dev")
python manage.py test                         # full test suite (plain Django TestCase, no pytest)
python manage.py test insurance.tests.UrlAuthGateTests   # single test class
python manage.py test insurance.tests.UrlAuthGateTests.test_something  # single test method
python manage.py test insurance.test_overlap_utils        # overlap-detector tests (separate file, not in tests.py)
python manage.py makemigrations insurance
python manage.py migrate
python manage.py createcachetable             # required once — DB-backed cache for login throttling
```

Life Payout Grid (separate Next.js app, its own `node_modules`):
```bash
npm run dev --prefix life-payout-grid    # or .claude/launch.json "life-payout-grid" (port 3000)
npm run build --prefix life-payout-grid
npm run lint --prefix life-payout-grid
```
Read `life-payout-grid/node_modules/next/dist/docs/` before writing Next.js code there — see `life-payout-grid/AGENTS.md`: this Next.js version has breaking changes vs. training-data assumptions.

Background jobs (Celery, used for MIS mapping and Gemini OCR extraction) — see `Procfile`:
```bash
celery -A project worker --loglevel=info --concurrency=2
celery -A project beat --loglevel=info    # runs the two scheduled cleanup tasks in project/settings.py
```

## Architecture

**Single real Django app: `insurance/`.** `config/` and `dashboard/` are legacy scaffold apps (a few lines each, `config` even has a duplicate unused `RateMaster` model) — not wired into `project/urls.py` beyond being in `INSTALLED_APPS`. Don't add features there; everything routes through `insurance/`.

- `project/settings.py` / `project/urls.py` — Django project config. Reads all secrets from `.env` (see `.env` keys, never commit values). `DEBUG=False` hard-fails startup if `SECRET_KEY`, `ALLOWED_HOSTS`, `LIFE_PAYOUT_GRID_AUTH_SECRET`, or `PARTNER_SSO_TICKET_SECRET` are unset — this is intentional, don't "fix" it by adding defaults.
- `insurance/models.py` — all domain models. Key ones: `RateMaster` (motor rate cards), `HealthRateMaster`, `RTOMaster`, `MakeModelMaster`, `PincodeMaster`, `MISFile`/`MISFailedRow` (uploaded MIS files and rows the mapping engine couldn't resolve), `LockedPolicy`, `SpecialRateRequest`, `RateOverlapScan`/`RateOverlapPair`, `AuditLog`, `MissingMakeModelManualResolution`.
- `insurance/views.py` (~7800 lines) — nearly the entire request-handling surface, organized into `# ====` / `# ---` banner-commented sections per feature (SSO handoff, upload/import, dashboard, rate master health, missing make/model, health rate master, motor/health payout rates, policy lock checker, business analysis, audit log, ticketing, MIS payout automation, REST API views). Grep the banner comments to navigate rather than reading linearly.
- The RTO/Make-Model/Pincode master-data dashboards (`rto_dashboard`/`make_model_dashboard`/`pincode_dashboard` in `views.py`) share one pattern: the page view renders the filter form and an empty table shell only; a sibling `<name>_dashboard_search` JSON endpoint (`?limit=N` for the default "N most recently added/updated" view, unbounded once the user clicks Apply) fills the table via JS; and a `create_<name>` JSON endpoint backs an in-page "Create New Group" modal. This replaced full server-side rendering of the whole table (600-1000+ rows) on every load. `created_at`/`updated_at` on `RTOMaster`/`MakeModelMaster`/`PincodeMaster` (mirroring `RateMaster`'s existing fields) back that ordering — any write path that does `save(update_fields=[...])` on these three models must list `"updated_at"` explicitly, since Django only touches fields named in `update_fields`. The main Rate Master dashboard (`views.dashboard`) predates this pattern and stays fully server-rendered with GET-param filters and a Django `Paginator`; it separately supports a `page_size` GET param (50/100/200, validated against `DASHBOARD_PAGE_SIZE_OPTIONS`, default 50) for rows-per-page.
- `api_upload_chunk`'s `rate_master` branch (`views.py`) processes up to 2500 rows per chunk from a client-side chunked upload (`upload.html`) — any per-row `.filter()`/`.exists()`/`.get()` added there must become a single query before the loop, cached into a dict/set, or a large file will blow past the request timeout (this has happened in production). `upload_batch_id` — fresh on every click of "Start Upload" — is deliberately part of `GROUP_FIELDS`/`build_key_hash`, so retrying an upload always creates new `RateGroup`s rather than recognizing rows an earlier, partially-successful attempt already saved; `CsvUploadAttempt` plus `api_check_duplicate_upload`/`api_complete_upload` (the browser SHA-256-hashes the raw file before parsing it) warn — but don't block — when the same file was already attempted for that target table.
- `grid_management`'s `GridDocument` uploads dispatch the actual file write to storage (Cloudflare R2 in production) to the Celery task `save_grid_document_file_task` rather than saving inline — a slow/hung storage write previously pinned a gunicorn worker for the whole request timeout, and `Procfile`'s worker/thread count is deliberately modest, so that alone could starve every other page on the site. New file-upload code should follow the same pattern.
- `insurance/urls.py` — every route wrapped in one of three access decorators defined at the top of the file: `staff_required` (is_staff/is_superuser/ADMIN group), `super_admin_required` (SUPER_ADMIN group only — reserved for minting Life Payout Grid admin tokens), `page_access_required(group_name)` (ADMIN group OR the named per-page group, checked against real Django `Group` membership set via `/user-management/`). A handful of server-to-server API routes deliberately skip these and use `HasAPIKey` instead. `insurance/tests.py::UrlAuthGateTests` enforces that every new route stays gated — update `PUBLIC_URL_NAMES` there only for genuinely public pages.
- `insurance/mapping_engine.py` — the MIS-to-RateMaster matching engine (`process_mis_mapping`), a RULE 1-6 sequential-filter chain (insurance company, product, vehicle age, make/model fuzzy match via `rapidfuzz`, RTO, etc.) that narrows a rate-card queryset down to the matching row(s) for a policy. When a rule empties the queryset, that rule's label+detail becomes the stored `MISFailedRow.failure_reason`.
- `insurance/overlap_utils.py` — the Rate Master Health overlap detector. Its predicates deliberately mirror `mapping_engine`'s RULE 1-6 semantics exactly (blank = wildcard, 0 = a real bound, YES/NO disjoint) so it flags every pair of rate rows the mapping engine could match ambiguously, before an MIS file ever hits them. Changing a rule in one file without the equivalent change in the other will desync detection from reality.
- `insurance/health_grid_utils.py` — identity-hash/parsing helpers shared between the one-off Excel import command and the web bulk-upload endpoint for `HealthRateMaster`, so both agree on what makes two rows duplicates.
- `insurance/sso.py` + `views.IssueSSOTicketAPIView` / `views.sso_consume_view` — inbound SSO handoff from an external partner portal (ArhamSecure), signed with `PARTNER_SSO_TICKET_SECRET`. Separate from `LIFE_PAYOUT_GRID_AUTH_SECRET`, which signs the *outbound* handoff into the `life-payout-grid` app (`views.life_payout_grid_admin_redirect`) — do not conflate the two secrets.
- `insurance/utils.py` — Gemini OCR extraction helpers and `auto_reconcile_mis`, which cross-checks an AI-extracted MIS record against `LockedPolicy` for payout discrepancies.
- `insurance/auth_views.py` — login/signup views with cache-backed attempt throttling (5 attempts/15 min for login, 10/hour for signup); a broken cache backend must fail open, not lock users out — this is why `createcachetable` is required setup, not optional.
- `insurance/context_processors.py` — `sidebar_access` mirrors the same ADMIN/per-page-group rules as `page_access_required`/`staff_required` in `urls.py` to decide which sidebar links render. A third place encoding access rules — keep it in sync when those decorators change.
- `insurance/tasks.py` — Celery tasks: the two scheduled log-cleanup jobs registered in `CELERY_BEAT_SCHEDULE` (also runnable ad hoc via the `cleanup_points_search_logs`/`cleanup_security_audit_logs` management commands), plus async work queued from `views.py` — `process_mis_mapping_task`, `process_policy_document_task`, `save_grid_document_file_task`, `run_rate_overlap_scan_task`.
- `insurance/management/commands/` — one-off/maintenance scripts: `dedupe_rate_master`, `find_duplicate_rate_master` (read-only duplicate scan, writes nothing; both accept `--cross-group --status ACTIVE|INACTIVE`, default `ACTIVE`, to decide which status counts as a duplicate — a repeated bulk-upload attempt lands as `INACTIVE`, so cleaning that up needs `--status INACTIVE` explicitly), `purge_deleted_rate_master`, `regroup_rate_master`, `run_overlap_scan`, `import_health_rate_master`, `migrate_media_to_object_storage`, `cleanup_points_search_logs`, `cleanup_security_audit_logs`.
- Templates live in `insurance/templates/` (mostly flat, a few in `insurance/templates/insurance/`, `partials/`, and `registration/`). `base.html` is the authenticated shell; `base_auth.html` is the login/signup shell.

### Soft delete and status conventions
- `RateMaster.is_deleted` is a `CharField` `"YES"/"NO"` (not a boolean) and `RateMaster.status` is `"ACTIVE"/"INACTIVE"` — both `db_index=True`. Filter on the string values, not truthiness.
- The Rate Master / Health Rate Master grid pages themselves still display soft-deleted rows; it's the *downstream* pages (dashboards, payout lookups, mapping engine) that must exclude `is_deleted="YES"`. When adding a new read path over these tables, check which behavior it should match.

### Access control
Permissions are real Django `Group` objects, managed at `/user-management/` (`staff_required`-gated) and enforced via `page_access_required` in `insurance/urls.py`. Group names are stable identifiers referenced by migrations (e.g. `insurance/migrations/0021_seed_page_access_groups.py`) — don't rename a permission group to "clean it up"; it silently revokes access for everyone already assigned. `insurance/context_processors.py::sidebar_access` re-derives the same rules to decide sidebar visibility — a change to who can access a page belongs in both places.

### Business-rule ambiguity
Several features here encode a general rule plus named special cases (e.g. Rate Master Pi/Po margin exemptions, tariff/cc/sc rounding scope). When a new request could combine a general rule and a special case, they combine additively (AND), not as an override — confirm this reading explicitly when a change touches one of these rule sets, and ask up front if a request is ambiguous across more than one axis (e.g. which fields *and* which condition).

## Environment

`.env` (gitignored) supplies: `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`, `DB_HOST`/`DB_NAME`/`DB_USER`/`DB_PASSWORD`/`DB_PORT` (falls back to SQLite if `DB_NAME` unset, or `DATABASE_URL` wins if set), `EMAIL_HOST`/`EMAIL_PORT`/`EMAIL_HOST_USER`/`EMAIL_HOST_PASSWORD`/`EMAIL_USE_TLS`, `GEMINI_API_KEY` (AI OCR extraction), `LIFE_PAYOUT_GRID_URL`, `LIFE_PAYOUT_GRID_AUTH_SECRET`, `PARTNER_SSO_TICKET_SECRET`. Media storage auto-switches to Cloudflare R2 (S3-compatible) when `AWS_STORAGE_BUCKET_NAME`/`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` are all set, otherwise falls back to local disk. Deployed on Railway (see `Procfile`); `vercel.json` exists but Railway is the active target.
