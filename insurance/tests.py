import json
from unittest import mock

from django.contrib.auth.models import Group, User
from django.test import TestCase, Client, override_settings
from django.urls import reverse
from django.urls.converters import get_converters
from rest_framework_api_key.models import APIKey

from insurance import sso
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
    # Inbound partner-portal SSO handoff (insurance/sso.py): issue-ticket is
    # gated by its own HasAPIKey permission instead of a session, and
    # consume is the landing page a not-yet-logged-in browser is redirected
    # to -- both are meant to be reachable while logged out.
    "sso_issue_ticket",
    "sso_consume",
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


@override_settings(
    PARTNER_SSO_TICKET_SECRET="test-secret-only-used-by-this-test-class",
    # DatabaseCache (the real backend, see project/settings.py) needs
    # `manage.py createcachetable`, which isn't run against the test
    # database -- swap in an in-memory cache so the single-use check below
    # exercises real cache behavior instead of failing closed for an
    # unrelated reason.
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class SSOTicketTests(TestCase):
    """Unit tests for insurance/sso.py's partner-portal ticket mint/verify round trip."""

    def setUp(self):
        self.user = User.objects.create_user(username="partner.user@example.com")

    def test_valid_ticket_round_trips_once(self):
        ticket = sso.mint_ticket(self.user, "policy_lock_checker", "jti-1")
        result = sso.verify_and_consume_ticket(ticket)
        self.assertIsNotNone(result)
        user, landing_page = result
        self.assertEqual(user, self.user)
        self.assertEqual(landing_page, "policy_lock_checker")

    def test_replayed_ticket_is_rejected(self):
        ticket = sso.mint_ticket(self.user, "policy_lock_checker", "jti-2")
        self.assertIsNotNone(sso.verify_and_consume_ticket(ticket))
        # Second use of the exact same ticket must fail even though it
        # hasn't expired yet.
        self.assertIsNone(sso.verify_and_consume_ticket(ticket))

    def test_expired_ticket_is_rejected(self):
        with mock.patch("insurance.sso.time.time", return_value=1_000_000.0):
            ticket = sso.mint_ticket(self.user, "policy_lock_checker", "jti-3")
        with mock.patch("insurance.sso.time.time", return_value=1_000_000.0 + sso.TICKET_TTL_SECONDS + 1):
            self.assertIsNone(sso.verify_and_consume_ticket(ticket))

    def test_tampered_signature_is_rejected(self):
        ticket = sso.mint_ticket(self.user, "policy_lock_checker", "jti-4")
        payload, _, _sig = ticket.rpartition(".")
        forged = f"{payload}.{'0' * 64}"
        self.assertIsNone(sso.verify_and_consume_ticket(forged))

    def test_wrong_audience_is_rejected(self):
        payload = f"{self.user.username}:{int(sso.time.time()) + 60}:policy_lock_checker:some-other-audience:jti-5"
        forged = f"{payload}.{sso._sign(payload)}"
        self.assertIsNone(sso.verify_and_consume_ticket(forged))

    def test_unknown_user_is_rejected(self):
        payload = f"nobody@example.com:{int(sso.time.time()) + 60}:policy_lock_checker:{sso.AUDIENCE}:jti-6"
        forged = f"{payload}.{sso._sign(payload)}"
        self.assertIsNone(sso.verify_and_consume_ticket(forged))


@override_settings(
    PARTNER_SSO_TICKET_SECRET="test-secret-only-used-by-this-test-class",
    # See the comment on SSOTicketTests above -- DatabaseCache needs a table
    # this test database doesn't have.
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    # ManifestStaticFilesStorage (the real "staticfiles" backend, see
    # project/settings.py) needs `manage.py collectstatic` to have been run
    # to generate its manifest -- not something a fresh test run can rely
    # on. Swap in the plain (non-hashed) storage just for this class, which
    # actually renders full pages (policy_lock_checker.html, via base.html).
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class SSOHandoffViewTests(TestCase):
    """
    End-to-end tests for the two views that make up the partner-portal SSO
    handoff: IssueSSOTicketAPIView (server-to-server) and sso_consume_view
    (the browser-redirect landing page).
    """

    def setUp(self):
        self.client = Client()
        _, self.api_key = APIKey.objects.create_key(name="test-partner")
        for name in ("Can_View_Policy_Locker", "Can_View_Health_Payout_Rates", "Can_View_Life_Payout_Grid"):
            Group.objects.get_or_create(name=name)

    def _issue(self, **overrides):
        payload = {
            "email": "agent@arhamsecure.com",
            "full_name": "Test Agent",
            "requested_pages": ["Rate_Checker"],
        }
        payload.update(overrides)
        return self.client.post(
            reverse("sso_issue_ticket"),
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Api-Key {self.api_key}",
        )

    @staticmethod
    def _path_from_redirect_url(redirect_url):
        # redirect_url is absolute (http://host/sso/consume/?ticket=...) --
        # the test client wants a path.
        return "/" + redirect_url.split("://", 1)[1].split("/", 1)[1]

    def test_issue_ticket_requires_api_key(self):
        response = self.client.post(
            reverse("sso_issue_ticket"),
            data=json.dumps({"email": "agent@arhamsecure.com"}),
            content_type="application/json",
        )
        self.assertIn(response.status_code, (401, 403))

    def test_full_handoff_logs_user_in_with_scoped_access(self):
        response = self._issue()
        self.assertEqual(response.status_code, 200)

        user = User.objects.get(username="agent@arhamsecure.com")
        self.assertTrue(user.is_active)
        self.assertFalse(user.has_usable_password())
        self.assertEqual(
            set(user.groups.values_list("name", flat=True)),
            {"Can_View_Policy_Locker", "Can_View_Health_Payout_Rates", "Can_View_Life_Payout_Grid"},
        )

        # Default landing page is the Rate Checker hub, not a specific tab...
        consume_path = self._path_from_redirect_url(response.json()["redirect_url"])
        consume_response = self.client.get(consume_path)
        self.assertEqual(consume_response.status_code, 302)
        self.assertEqual(consume_response.url, reverse("rate_checker"))

        # ...which itself redirects to the highest-priority accessible tab (Motor).
        hub_response = self.client.get(reverse("rate_checker"))
        self.assertEqual(hub_response.url, reverse("policy_lock_checker"))

        # Session is now live: a granted page loads directly...
        self.assertEqual(self.client.get(reverse("policy_lock_checker")).status_code, 200)
        self.assertEqual(self.client.get(reverse("health_payout_rates")).status_code, 200)
        # ...but a page outside the granted set is still forbidden.
        self.assertEqual(self.client.get(reverse("audit_logs")).status_code, 403)

    def test_explicit_landing_page_still_lands_directly_on_a_tab(self):
        response = self._issue(landing_page="health_payout_rates")
        consume_path = self._path_from_redirect_url(response.json()["redirect_url"])
        consume_response = self.client.get(consume_path)
        self.assertEqual(consume_response.url, reverse("health_payout_rates"))

    def test_ticket_cannot_be_replayed(self):
        response = self._issue()
        consume_path = self._path_from_redirect_url(response.json()["redirect_url"])

        first = self.client.get(consume_path)
        self.assertEqual(first.url, reverse("rate_checker"))

        self.client.logout()
        second = self.client.get(consume_path)
        self.assertEqual(second.url, reverse("login"))

    def test_scope_is_clamped_server_side(self):
        response = self._issue(requested_pages=["Can_View_Policy_Locker", "ADMIN", "Can_View_Audit_Log"])
        self.assertEqual(response.status_code, 200)
        user = User.objects.get(username="agent@arhamsecure.com")
        self.assertEqual(set(user.groups.values_list("name", flat=True)), {"Can_View_Policy_Locker"})

    def test_previously_grantable_pages_no_longer_granted(self):
        """
        Rate Checker replaced the old allow-list (Policy Locker + Locked
        Policies + Motor Points Logs from the first version of this
        handoff) -- Locked Policies / Motor Points Logs are no longer
        requestable through this integration.
        """
        response = self._issue(requested_pages=[
            "Can_View_Policy_Locker", "Can_View_Locked_Policies", "Can_View_Motor_Points_Logs",
        ])
        self.assertEqual(response.status_code, 200)
        user = User.objects.get(username="agent@arhamsecure.com")
        self.assertEqual(set(user.groups.values_list("name", flat=True)), {"Can_View_Policy_Locker"})

    def test_direct_visit_with_no_ticket_falls_back_to_login(self):
        response = self.client.get(reverse("sso_consume"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("login"))


@override_settings(
    # See the comment on SSOHandoffViewTests above -- the 403 page extends
    # base.html, which needs this when ManifestStaticFilesStorage's
    # manifest hasn't been generated (no `collectstatic` in this test run).
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class RateCheckerEntryTests(TestCase):
    """Tests for the plain (non-SSO) Rate Checker sidebar entry point."""

    def setUp(self):
        self.client = Client()
        for name in ("Can_View_Policy_Locker", "Can_View_Health_Payout_Rates", "Can_View_Life_Payout_Grid"):
            Group.objects.get_or_create(name=name)
        self.user = User.objects.create_user(username="broker", password="a-strong-test-password-1")
        self.client.force_login(self.user)

    def test_redirects_to_highest_priority_accessible_tab(self):
        self.user.groups.add(Group.objects.get(name="Can_View_Health_Payout_Rates"))
        self.user.groups.add(Group.objects.get(name="Can_View_Life_Payout_Grid"))
        response = self.client.get(reverse("rate_checker"))
        # Motor isn't granted, so Health (next in priority) wins.
        self.assertEqual(response.url, reverse("health_payout_rates"))

    def test_falls_through_to_life_if_thats_all_thats_granted(self):
        self.user.groups.add(Group.objects.get(name="Can_View_Life_Payout_Grid"))
        response = self.client.get(reverse("rate_checker"))
        self.assertEqual(response.url, reverse("life_payout_grid_redirect"))

    def test_forbidden_with_none_of_the_three_groups(self):
        response = self.client.get(reverse("rate_checker"))
        self.assertEqual(response.status_code, 403)
