# Architecture

## Layers

```
browser ──> urls.py ──> views/<feature>.py ──> services/<feature>.py ──> models.py ──> database
                │             │ HTTP only          │ rules, queries, calculations
                │             └── forms.py         └── never imports views/ or api/ (a test enforces this)
                └──> api/urls.py ──> api/views.py ──> services/  (REST, API-key or staff gated)
```

- **`views/`** is the HTTP layer: parse the request, call helpers, render a template or return JSON.
  `views/__init__.py` re-exports every name, so `urls.py` writes `views.dashboard` and older code can still do
  `from insurance.views import RateForm`. A few large view functions (the Rate Master `dashboard`, `api_upload_chunk`)
  still carry their query and row-processing logic inline: the split moved every request-free helper into
  `services/`, but untangling those bodies is future work, not done.
- **`services/`** holds what the pages and the API share: make/model matching, the Rate Master validity rules, the
  overlap helpers, payout searches, policy-document OCR processing. Functions here may take a `request` (a few log the
  user who searched) but never import from `views/` or `api/`.
- **`access.py`** is the single place that decides who may open a page; see [access-control.md](access-control.md).
- **`constants.py`** holds values two or more modules must agree on (audit action lists, the health policy-term
  table, the insurer list). If you find yourself writing "keep in sync with X" in a comment, put the value here.
- **`forms.py`** holds every Django form (moved out of the old single `views.py`).

## Module map

| Feature | View module | Service module(s) |
|---|---|---|
| Rate Master dashboard, edit, bulk update, export | `views/rate_master.py` | `services/rate_master.py`, `services/make_model.py` |
| Rate Master Health, overlap detector | `views/rate_master_health.py` | `services/rate_master_checks.py`, `services/overlap.py` |
| Health Rate Master | `views/health_rate_master.py` | |
| Motor / Health payout rates | `views/motor_payout_rates.py`, `views/health_payout_rates.py` | `services/motor_payout.py`, `services/health_payout.py`, `services/points_audit.py` |
| Policy lock checker, locked policies | `views/policy_lock.py` | `services/policy_lock.py` |
| RTO / Make-Model / Pincode masters | `views/masters.py` | `services/masters.py` |
| Make/Model mapping, missing make/model | `views/make_model_mapping.py`, `views/missing_make_model.py` | `services/make_model_mapping.py`, `services/missing_make_model.py` |
| CSV upload | `views/upload.py` | `services/rate_master.py` |
| MIS payout automation | `views/mis_payout.py` | `services/mis_payout.py`, `mapping_engine.py` |
| OCR pipeline, MIS register | `views/ocr_pipeline.py` | `services/policy_documents.py`, `utils.py` (Gemini) |
| Grid management | `views/grid_management.py` | `services/email.py`, `grid_archive.py` |
| Tickets, special rate requests | `views/tickets.py`, `views/special_rates.py` | `services/tickets.py` |
| Audit logs | `views/audit_logs.py` | `services/audit_logs.py` |
| Analysis | `views/analysis.py` | `services/analysis.py` |
| AI rulebook (field configurator) | `views/ai_rulebook.py` | |
| Users, SSO landing, home, Rate Checker | `views/user_management.py`, `views/sso_handoff.py`, `views/home.py`, `views/rate_checker.py` | `sso.py` |
| Small shared helpers (date / yes-no parsing, JSON-in-script escaping) | | `services/common.py` |

Modules deliberately left at the top level of `insurance/`: `mapping_engine.py`, `overlap_utils.py`,
`grid_archive.py`, `health_grid_utils.py`, `sso.py`, `tasks.py`, `models.py`. Celery task names, the beat schedule and
the tests' `mock.patch` targets refer to those import paths.

## Where does new code go?

1. **A new page:** view in `views/<feature>.py`, logic in `services/<feature>.py`, template in `templates/`, a route in
   `urls.py` wrapped in a gate, a group in `access.PAGE_GROUPS` plus a data migration that creates the group, and the
   sidebar link in `base.html`. Run `python manage.py show_access_matrix` to see the route gated.
2. **A calculation or query used by more than one page:** `services/`. Add a test that does not go through a view.
3. **A value two modules both need:** `constants.py`.
4. **A REST endpoint:** `api/views.py`, a route in `api/urls.py` under `api/v1/`, `HasAPIKey` for server-to-server use.
5. **Page CSS/JS:** if it needs no server data, put it in `static/insurance/{css,js}/pages/<template>.ext` and load it with
   `{% static %}`. Keep it inline only when it interpolates server data (`{% url %}`, `{{ json }}`, loops).

## Background work

`tasks.py` holds the Celery tasks: MIS mapping, policy-document OCR, rate-overlap scans, grid file saving, and the
scheduled cleanups/archive (`CELERY_BEAT_SCHEDULE` in `project/settings.py`). Tasks import their heavy dependencies
inside the task body so loading the module stays cheap.

## Data conventions

- `RateMaster.is_deleted` is the string `"YES"`/`"NO"` and `status` is `"ACTIVE"`/`"INACTIVE"`; filter on the strings.
- The Rate Master and Health Rate Master grid pages still show soft-deleted rows; the downstream pages (dashboards,
  payout lookups, the mapping engine) exclude them.
- Rate Master Pi/Po margin and rounding rules, and the MIS mapping rules (RULE 1-6, vehicle-age bands, identical-payout
  collapsing), are described in `CLAUDE.md` next to the code they govern; `overlap_utils.py` mirrors the mapping
  engine's rules exactly, so change them together.

## Static assets

Page-specific CSS and JS that used to sit inline in the templates now live in `insurance/static/insurance/css/pages/`
and `insurance/static/insurance/js/pages/`, named after the template they came from (a second block in the same
template gets `-2`). They were moved verbatim and load from the same position in the page, so cascade and script order
are unchanged. Blocks that interpolate server data (URL tags, JSON, loops) stay inline.

## Leftovers

- `config/` is an old scaffold app with an unused duplicate `RateMaster` model and a migration that created its table.
  Nothing imports it. Removing it needs a decision about that table.
- `life-payout-grid/` and the commented-out Life Payout Grid code (settings, URLs, `views/rate_checker.py`, templates)
  are kept so the integration can be revived; the service itself was decommissioned.
