# Insurance Broker Portal

A Django portal for an insurance broker. It ingests MIS (Management Information System) files from insurers, maps
every policy row against the rate-card database to calculate payout, tracks rate-card health and overlaps, and
serves dashboards for the RTO, Make/Model, Pincode and Health rate masters.

- **Stack:** Django 6 + Django REST Framework, Celery (Redis) for background work, PostgreSQL (SQLite fallback),
  Cloudflare R2 for media in production, WhiteNoise for static files. Hosted on Railway.
- **One real app:** everything lives in `insurance/`. `config/` is a leftover scaffold app (see
  [docs/architecture.md](docs/architecture.md#leftovers)); `life-payout-grid/` is a retired Next.js app kept for reference.

## Quick start

```powershell
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env                                   # then fill in SECRET_KEY etc.
venv\Scripts\python.exe manage.py migrate
venv\Scripts\python.exe manage.py createcachetable      # DB-backed cache used for login throttling (required once)
venv\Scripts\python.exe manage.py runserver 127.0.0.1:8000
```

Background jobs (MIS mapping, OCR extraction, overlap scans, grid uploads) need Redis and a worker:

```powershell
venv\Scripts\celery.exe -A project worker --loglevel=info --concurrency=2
venv\Scripts\celery.exe -A project beat --loglevel=info      # the scheduled cleanup/archive jobs
```

## Tests

```powershell
venv\Scripts\python.exe manage.py test                          # everything
venv\Scripts\python.exe manage.py test insurance.test_structure_guards   # layout / routes / docs guards
venv\Scripts\python.exe manage.py test insurance.tests.UrlAuthGateTests  # every route stays gated
```

Test runs use plain static-file storage, so they do not need a prior `collectstatic`. Without Redis running you may
see broker "connection refused" tracebacks in the output from tests that exercise the queueing fallbacks; the tests
still pass.

## Where things are

| Path | What lives there |
|---|---|
| `project/` | Django settings (all secrets from the environment), root URLs, Celery app |
| `insurance/views/` | HTTP layer, one module per feature (see [docs/architecture.md](docs/architecture.md)) |
| `insurance/services/` | Business logic the views call: matching, rule checks, search, OCR processing |
| `insurance/api/` | REST API: views, serializers, URL table (`/api/v1/`), docs hook |
| `insurance/access.py` | Who may open which page (groups + decorators) |
| `insurance/mapping_engine.py` | The MIS-to-rate-card engine (RULE 1-6) |
| `insurance/templates/`, `insurance/static/` | Pages; page CSS/JS in `static/insurance/{css,js}/pages/` |
| `docs/` | Architecture, access control, API, operations, conventions |
| `CLAUDE.md` | Working notes for AI-assisted changes (business rules and gotchas) |

## Configuration

All configuration comes from environment variables; `.env.example` lists every key with a short comment. With
`DEBUG=False` the app refuses to start without `SECRET_KEY`, `ALLOWED_HOSTS` and `PARTNER_SSO_TICKET_SECRET`; that is
intentional, do not add defaults for them.

## Deployment

Railway runs the processes in `Procfile` (`release`, `web`, `worker`, `beat`). `collectstatic` has to run at build
time because static files use hashed names. See [docs/operations.md](docs/operations.md).

## Documentation

- [Architecture and where new code goes](docs/architecture.md)
- [Access control and the route table](docs/access-control.md)
- [REST API](docs/api.md)
- [Operations: processes, environment, jobs, commands](docs/operations.md)
- [Conventions: naming, errors and logging, tests](docs/conventions.md)
