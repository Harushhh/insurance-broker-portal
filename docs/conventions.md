# Conventions

## Naming

- Python: `snake_case` modules, functions and variables; `PascalCase` classes; `UPPER_SNAKE` constants; a leading
  underscore marks a helper that is not part of a module's interface (tests do import a few, e.g.
  `_run_policy_lock_checker_search`).
- One module per feature under `views/` and `services/`, named after the feature, not the layer (`policy_lock.py`, not
  `policy_lock_views.py`).
- URL paths are `kebab-case/`; URL names are `snake_case`. Two names still use hyphens, `api-export-rates` and
  `swagger-ui`; tests refer to them by name, so leave them unless you change every reference.
- Templates sit flat in `insurance/templates/` (a few in `insurance/`, `partials/`, `registration/`). Shared fragments
  live in `partials/` (for example `partials/ticket_modal.html`). Page assets are named after their template.
- Quote style: double quotes in new code.

## Never rename

Permission group names (`Can_View_...`, `ADMIN`, `SUPER_ADMIN`), URL paths and names the tests or other systems use,
database tables and columns, model names, and the import paths of `mapping_engine`, `tasks`, `overlap_utils`,
`grid_archive` and `sso` (Celery task names, the beat schedule and test patches point at them). A rename that looks
harmless silently revokes access or breaks queued jobs.

## Errors and logging

- Create a logger per module: `logger = logging.getLogger(__name__)`. Auth events keep using the `security` logger.
- Log with `logger.exception(...)` inside an `except` block so the traceback survives. Do not use `print()`.
- A handler may swallow an error on purpose (a best-effort cleanup, a lenient parse), but it must say so in a comment
  and still log at `debug` or above. Never `except:` without `Exception`.
- Keep the user-facing response of a handler as it was when you add logging; changing what a view returns is a behaviour
  change and needs its own decision.
- JSON endpoints answer `{"success": bool, "message": str}` (the upload endpoints use `{"status": ..., "message": ...}`).
  Error pages: `403.html`, `404.html` (extends the 403 card) and a self-contained `500.html`.
- Celery tasks declare `max_retries` on several tasks, but nothing in `tasks.py` calls `retry`, so they do not retry.
  Retrying a task changes behaviour (a second run of a half-finished job), so decide per task before enabling it.

## Tests

- Plain Django `TestCase` (no pytest). Run everything with `python manage.py test`.
- `insurance/test_structure_guards.py` keeps the layout honest: shared tables stay shared, services never import views
  or the API, every import (including those inside functions) resolves, the sidebar matches the page gates, the API
  aliases and staff-only docs hold, every `{% static %}` file exists.
- When you move code, run `manage.py makemigrations --check --dry-run` (it must report no changes) and the whole suite.
- Optional lint: `ruff check --select F insurance project` catches undefined names and unused imports. It does not see
  a relative import inside a function that points at the wrong place; the guard test does.

## Migrations

Group names and seed data are referenced by migrations; never edit an applied migration. Add a new data migration to
create a new permission group.
