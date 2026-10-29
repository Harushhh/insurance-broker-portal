# REST API

The API lives in `insurance/api/`: `views.py` (DRF views), `serializers.py`, `urls.py` (the route table, spliced into
`insurance/urls.py`) and `schema.py` (docs hook). It is meant for server-to-server callers; the browser pages do not
use it.

## Endpoints

Canonical paths are versioned under `/api/v1/`. The unversioned paths that existed before versioning still work and
point at the same views, so existing callers keep working.

| Method | Canonical path | Legacy path | Auth | What it does |
|---|---|---|---|---|
| GET | `/api/v1/export-rates/` | (was already versioned) | API key **and** a signed-in user with the Dashboard page permission | Lists every non-deleted `RateMaster` row |
| GET | `/api/v1/policy-lock-checker/` | `/api/policy-lock-checker/` | API key | JSON equivalent of the Policy Lock Checker (Motor points) search |
| GET | `/api/v1/health-payout-rates/` | `/api/health-payout-rates/` | API key | JSON equivalent of the Health "Check Eligibility" search |
| GET | `/api/v1/make-model-master/` | `/api/make-model-master/` | API key | Reference data: the exact `make_names`, `make_model_class` and `product` spellings the policy-lock endpoint accepts |
| POST | `/api/v1/sso/issue-ticket/` | `/api/sso/issue-ticket/` | API key | Partner-portal backend mints a short-lived, single-use SSO ticket; the browser then lands on `/sso/consume/` |

Send the key as `Authorization: Api-Key <key>`. Keys are created in the Django admin (API keys). A request without a
valid key gets `403`.

## Interactive docs

`/api/schema/` (OpenAPI), `/api/schema/swagger-ui/` and `/api/schema/redoc/`, **staff only** (staff, superuser or the
`ADMIN` group). Only the `/api/v1/` paths are listed; the legacy aliases are hidden by
`insurance/api/schema.py::hide_legacy_paths`. Several views are plain `APIView`s without a serializer, so their request
and response bodies appear untyped, and generating the schema logs an "unable to guess serializer" warning for each.

## Versioning

- New endpoints go under `/api/v1/`. A breaking change gets a new prefix (`/api/v2/`) instead of editing v1.
- Do not remove a legacy path until its callers have confirmed they moved. To remove one: delete its line in
  `api/urls.py`, drop it from `LEGACY_API_PATHS` in `api/schema.py`, and update the expected set in
  `ApiKeyGatedRouteTests` (`insurance/tests.py`).

## Adding an endpoint

1. Write the view in `api/views.py`; put anything the browser page also needs in `services/` and call it from both.
2. Set `permission_classes = [HasAPIKey]` (server-to-server) or wrap the route like a page.
3. Add the route to `api/urls.py` under `api/v1/`.
4. Add the route name to the expected set in `ApiKeyGatedRouteTests`; `UrlAuthGateTests` then proves it refuses an
   anonymous request.
5. Add a test in `insurance/test_structure_guards.py` or next to the feature, and check the docs page renders.
