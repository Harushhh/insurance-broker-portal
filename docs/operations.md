# Operations

## Processes (`Procfile`)

| Process | Command | Notes |
|---|---|---|
| `release` | `manage.py migrate && manage.py createcachetable` | runs on each deploy; the cache table backs login throttling |
| `web` | `gunicorn project.wsgi` (2 workers x 2 threads, `gthread`, 60 s timeout, recycled after ~500 requests) | the worker count is modest on purpose: a slow request pins a thread, so slow work belongs in Celery |
| `worker` | `celery -A project worker --concurrency=2` | needs `REDIS_URL`; children above ~488 MB are replaced (`CELERY_WORKER_MAX_MEMORY_PER_CHILD` in settings, because the Railway service has its own start command that overrides the Procfile) |
| `beat` | `celery -A project beat` | runs the scheduled jobs below; nothing runs on schedule if this process is not deployed |

Static files use hashed names (WhiteNoise manifest storage), so `collectstatic` must run at build time. If a
`{% static %}` tag names a file that was not collected, the page fails with "Missing staticfiles manifest entry".

## Environment

`.env.example` lists every variable with a comment. Highlights:

- `DEBUG=False` refuses to start without `SECRET_KEY`, `ALLOWED_HOSTS` and `PARTNER_SSO_TICKET_SECRET`. Do not add defaults.
- `CSRF_TRUSTED_ORIGINS` (comma-separated) overrides the built-in production hostname; blank means "use the default".
- Database: `DATABASE_URL`, else `DB_*` (Postgres), else SQLite.
- Media goes to Cloudflare R2 only when all of `AWS_STORAGE_BUCKET_NAME`, `AWS_ACCESS_KEY_ID`,
  `AWS_SECRET_ACCESS_KEY` are set; otherwise local disk. `GRID_ARCHIVE_BUCKET_NAME` puts the Grid Management archive in
  its own bucket.
- `LIFE_PAYOUT_GRID_*` are no longer read (the service was decommissioned); they can be deleted from `.env` and Railway.

## Scheduled jobs (`CELERY_BEAT_SCHEDULE`)

| Job | When | Does |
|---|---|---|
| `cleanup_points_search_logs` | 02:00 daily | deletes Motor/Health points-search audit logs older than 7 days |
| `cleanup_security_audit_logs` | 02:15 daily | deletes security audit log entries older than 7 days |
| `archive_old_grid_documents` | 03:00 on the 1st | moves Grid Management uploads older than `GRID_RETENTION_MONTHS` (3) to the archive |

The archive copies each file, reads it back and compares SHA-256, writes an immutable manifest per run, and only then
deletes the rows and originals; anything that fails verification stays and is retried next run. It refuses to purge
unless R2 is configured. Details are in `CLAUDE.md`.

## Management commands

| Command | Purpose |
|---|---|
| `show_access_matrix` | prints who may open each route (feeds `docs/access-control.md`) |
| `find_duplicate_rate_master` | read-only report of exact-duplicate rate cards (`--cross-group --status ACTIVE\|INACTIVE`) |
| `dedupe_rate_master` | soft-deletes exact duplicates; a dry run unless `--apply` is passed |
| `purge_deleted_rate_master` | **permanently** deletes every `is_deleted=YES` rate card; a dry run unless `--apply` |
| `regroup_rate_master` | recomputes rate grouping under the current `GROUP_FIELDS`; only genuinely splitting groups get new ids |
| `run_overlap_scan` | runs the overlap scan in-process (needs no Redis or worker, unlike the dashboard button) |
| `import_health_rate_master` | imports / refreshes Health rate rows from a commission-grid Excel file |
| `migrate_media_to_object_storage` | one-off copy of local media files to R2; does not touch database rows |
| `cleanup_points_search_logs`, `cleanup_security_audit_logs` | the two log cleanups, run ad hoc |
| `archive_grid_documents`, `restore_grid_documents` | archive old grid uploads / restore them from R2 (`--dry-run` on both) |

## Logging

- Auth events use the `security` logger; Django request errors are forced to stdout (`django.request`).
- Everything else logs through `logging.getLogger(__name__)` under `insurance.*`, which has a console handler at INFO.
  Failures that used to be `print()`ed (MIS mapping, AI extraction, uploads, background email) now arrive with a level and
  a traceback in the Railway logs.

## Known behaviours worth knowing

- The Grid Management page filters to the current month by default, so on the 1st it looks empty until "All Months" is
  chosen. Check that before assuming data was lost.
- Uploading the same CSV twice warns but does not block (`CsvUploadAttempt`); each upload creates new rate groups.
- Two URLs answer a plain GET with a 500 instead of a 4xx: `/edit-rate/<unknown id>/` and
  `/api/update-special-rate-status/` (a POST endpoint). Behaviour left as it was.
