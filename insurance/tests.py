from django.test import TestCase, Client
from django.urls import reverse
from django.urls.converters import get_converters

from insurance import urls as insurance_urls

# Routes in insurance/urls.py that are deliberately reachable while logged
# out (the password-reset flow, and the catch-all which just bounces
# everyone to login regardless of auth state). Every other name in that
# file is expected to redirect an anonymous request straight to the login
# page — this test exists so a newly added path() that forgets its
# login_required/page_access_required/staff_required wrapper fails loudly
# in CI instead of silently shipping as a public page.
PUBLIC_URL_NAMES = {
    "password_reset",
    "password_reset_done",
    "password_reset_confirm",
    "password_reset_complete",
    "catch_all",
}

# Registered converter name (e.g. "int", "str", "uuid") -> a dummy value
# that satisfies it, so every named URL can be reversed without any real
# objects existing in the test DB.
_DUMMY_VALUE_BY_CONVERTER = {
    "int": "1",
    "str": "dummy",
    "slug": "dummy-slug",
    "uuid": "00000000-0000-0000-0000-000000000000",
    "path": "dummy/path",
}

_CONVERTER_NAME_BY_CLASS = {type(inst): name for name, inst in get_converters().items()}


def _dummy_kwargs(url_pattern):
    kwargs = {}
    for param, converter in url_pattern.pattern.converters.items():
        conv_name = _CONVERTER_NAME_BY_CLASS.get(type(converter), "str")
        kwargs[param] = _DUMMY_VALUE_BY_CONVERTER.get(conv_name, "dummy")
    return kwargs


class UrlAuthGateTests(TestCase):
    """
    Every page in insurance/urls.py must be gated behind login_required (or
    page_access_required/staff_required, which both wrap it) unless it is
    explicitly whitelisted in PUBLIC_URL_NAMES above. An anonymous GET to a
    gated URL must redirect to the login page, never return 200 or any
    other status.
    """

    def test_every_named_url_requires_login_unless_whitelisted(self):
        client = Client()
        login_path = reverse("login")
        checked = 0

        for pattern in insurance_urls.urlpatterns:
            name = getattr(pattern, "name", None)
            if not name or name in PUBLIC_URL_NAMES:
                continue

            path = reverse(name, kwargs=_dummy_kwargs(pattern))
            response = client.get(path)
            checked += 1

            self.assertEqual(
                response.status_code, 302,
                f"'{name}' ({path}) returned {response.status_code} for an "
                f"anonymous request instead of redirecting to login — it may "
                f"be missing a login_required/page_access_required/"
                f"staff_required wrapper in insurance/urls.py."
            )
            self.assertTrue(
                response.url.startswith(login_path),
                f"'{name}' ({path}) redirected an anonymous request to "
                f"'{response.url}' instead of the login page."
            )

        # Guards against this test silently checking nothing if the loop
        # above ever stops matching real entries in insurance/urls.py.
        self.assertGreater(
            checked, 20,
            "Expected to check most of insurance/urls.py's named routes — "
            "did the import path or urlpatterns structure change?"
        )

    def test_whitelisted_public_urls_still_exist(self):
        """
        Sanity check the other direction: every name in PUBLIC_URL_NAMES
        must still be a real route. Catches a stale whitelist entry left
        behind after a route is renamed or removed.
        """
        actual_names = {p.name for p in insurance_urls.urlpatterns if getattr(p, "name", None)}
        stale = PUBLIC_URL_NAMES - actual_names
        self.assertFalse(stale, f"Whitelisted public URL name(s) no longer exist: {stale}")
