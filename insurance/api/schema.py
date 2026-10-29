"""drf-spectacular hooks."""

# The unversioned routes that predate /api/v1/. They keep working for existing callers but are left out of
# the generated docs so each endpoint is documented once, under /api/v1/.
LEGACY_API_PATHS = frozenset({
    "/api/policy-lock-checker/",
    "/api/health-payout-rates/",
    "/api/make-model-master/",
    "/api/sso/issue-ticket/",
})


def hide_legacy_paths(endpoints, **kwargs):
    """Preprocessing hook: drop LEGACY_API_PATHS from the schema."""
    return [e for e in endpoints if e[0] not in LEGACY_API_PATHS]
