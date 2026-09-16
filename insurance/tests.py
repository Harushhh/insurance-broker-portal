import json
from unittest import mock

from django.contrib.auth.models import Group, User
from django.test import TestCase, Client, override_settings
from django.urls import reverse
from django.urls.converters import get_converters
from rest_framework_api_key.models import APIKey
from rest_framework_api_key.permissions import HasAPIKey

from insurance import sso
from insurance import urls as insurance_urls

# Routes in insurance/urls.py that are deliberately reachable while logged
# out (the password-reset flow, and the catch-all which just bounces
# everyone to login regardless of auth state). Every other name in that
# file is expected to refuse an anonymous request — this test exists so a
# newly added path() that forgets its login_required/page_access_required/
# staff_required wrapper fails loudly in CI instead of silently shipping as
# a public page.
#
# Server-to-server API routes are deliberately NOT listed here. They are
# gated by their own HasAPIKey permission rather than a session, which the
# test detects directly (see _api_key_gated) — so they are *verified* to
# reject a keyless request rather than skipped, and a new one needs no entry
# here. Only genuinely public pages belong in this set.
PUBLIC_URL_NAMES = {
    "password_reset",
    "password_reset_done",
    "password_reset_confirm",
    "password_reset_complete",
    "catch_all",
    # The landing page a not-yet-logged-in browser is redirected to by the
    # inbound partner-portal SSO handoff (insurance/sso.py) — meant to be
    # reachable while logged out. Its sibling, sso_issue_ticket, is covered
    # by the HasAPIKey path above instead.
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


def _api_key_gated(url_pattern):
    """
    Whether this route's view is gated by rest_framework_api_key's HasAPIKey
    rather than (or as well as) a session.

    DRF's as_view() stashes the view class on the function it returns as
    `.cls`, and functools.wraps -- which page_access_required and friends in
    insurance/urls.py all use -- copies the wrapped function's __dict__ onto
    the wrapper. So this still finds the class through a session wrapper,
    which matters for api-export-rates: it stacks page_access_required on top
    of a HasAPIKey view.
    """
    view_class = getattr(url_pattern.callback, "cls", None)
    return HasAPIKey in (getattr(view_class, "permission_classes", None) or ())


class UrlAuthGateTests(TestCase):
    """
    Every page in insurance/urls.py must be gated behind login_required (or
    page_access_required/staff_required, which both wrap it) unless it is
    explicitly whitelisted in PUBLIC_URL_NAMES above. An anonymous GET to a
    gated URL must redirect to the login page, never return 200 or any
    other status.

    The one exception is a server-to-server route gated by HasAPIKey: those
    refuse a keyless request with 403 rather than redirecting a browser, so
    they are held to "403 or a 302 to login" instead. They are detected
    directly rather than whitelisted -- see _api_key_gated.
    """

    def test_every_named_url_requires_login_unless_whitelisted(self):
        client = Client()
        login_path = reverse("login")
        checked = 0
        # Collected rather than asserted inline: a bare assert stops at the
        # first bad route and hides every one after it in urlpatterns order,
        # which is exactly how three unlisted HasAPIKey routes sat unnoticed
        # behind a fourth. One failure should report the whole list.
        failures = []

        for pattern in insurance_urls.urlpatterns:
            name = getattr(pattern, "name", None)
            if not name or name in PUBLIC_URL_NAMES:
                continue

            path = reverse(name, kwargs=_dummy_kwargs(pattern))
            response = client.get(path)
            checked += 1
            redirects_to_login = (
                response.status_code == 302 and response.url.startswith(login_path)
            )

            if _api_key_gated(pattern):
                # Server-to-server route: rest_framework_api_key refuses a
                # keyless request with 403 rather than redirecting a browser.
                # A 302 to login is equally fine — api-export-rates stacks
                # page_access_required on top, and that fires first. Either
                # way the route is not reachable anonymously, which is what
                # this test is actually guarding. Anything else (a 200 above
                # all, i.e. DRF's AllowAny default) is a real hole.
                if response.status_code != 403 and not redirects_to_login:
                    failures.append(
                        f"'{name}' ({path}) is HasAPIKey-gated but returned "
                        f"{response.status_code} for an anonymous, keyless request — "
                        f"expected 403 from the API-key check, or a 302 to login from "
                        f"a session wrapper stacked on top."
                    )
                continue

            if response.status_code != 302:
                # A DRF route lands here when it is neither HasAPIKey-gated nor
                # session-wrapped. DEFAULT_PERMISSION_CLASSES is IsAuthenticated
                # (project/settings.py), so it is not *open* — but this app has
                # no such route by design, and the choice between the two gates
                # should be made deliberately rather than inherited, so say that
                # instead of sending someone hunting for a missing wrapper.
                if getattr(pattern.callback, "cls", None) is not None:
                    failures.append(
                        f"'{name}' ({path}) is a DRF view that is neither HasAPIKey-gated "
                        f"nor wrapped in insurance/urls.py, so it returned "
                        f"{response.status_code} instead of redirecting to login. Add "
                        f"permission_classes = [HasAPIKey] if it is server-to-server, or "
                        f"a page_access_required wrapper if a browser session should reach it."
                    )
                else:
                    failures.append(
                        f"'{name}' ({path}) returned {response.status_code} for an "
                        f"anonymous request instead of redirecting to login — it may "
                        f"be missing a login_required/page_access_required/"
                        f"staff_required wrapper in insurance/urls.py."
                    )
            elif not redirects_to_login:
                failures.append(
                    f"'{name}' ({path}) redirected an anonymous request to "
                    f"'{response.url}' instead of the login page."
                )

        self.assertFalse(
            failures,
            f"{len(failures)} route(s) did not refuse an anonymous request:\n  "
            + "\n  ".join(failures)
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


class ApiKeyGatedRouteTests(TestCase):
    """
    The server-to-server routes UrlAuthGateTests above hands off to
    _api_key_gated. That handoff is only safe if the detection actually works
    and the gate actually refuses, so both are asserted here rather than
    assumed — otherwise a broken detector would quietly turn the auth gate
    test into a rubber stamp for every API route.
    """

    def setUp(self):
        self.client = Client()
        self.api_key_routes = [
            p for p in insurance_urls.urlpatterns
            if getattr(p, "name", None) and _api_key_gated(p)
        ]

    def test_the_api_key_routes_are_found(self):
        # If DRF ever stops setting `.cls` on as_view()'s return value, or a
        # wrapper stops using functools.wraps, this drops to zero -- and every
        # one of those routes would then be held to the strict 302 rule, which
        # fails loudly rather than silently passing. This just names the
        # breakage directly instead of leaving it to be inferred.
        found = {p.name for p in self.api_key_routes}
        self.assertEqual(
            found,
            {
                "api-export-rates",
                "api_policy_lock_checker",
                "api_health_payout_rates",
                "api_make_model_master",
                "sso_issue_ticket",
            },
            "The set of HasAPIKey-gated routes changed. If that was deliberate, update "
            "this list; if not, _api_key_gated may have stopped seeing through as_view() "
            "or a urls.py wrapper.",
        )

    def test_detection_sees_through_a_session_wrapper(self):
        # api-export-rates is the awkward one: page_access_required stacked on
        # top of a HasAPIKey view. functools.wraps copies as_view()'s `.cls`
        # onto the wrapper, which is the only reason this works.
        wrapped = next(p for p in insurance_urls.urlpatterns if p.name == "api-export-rates")
        self.assertTrue(_api_key_gated(wrapped))
        self.assertIsNot(
            wrapped.callback, wrapped.callback.cls,
            "Expected api-export-rates to be a wrapper around the DRF view, not the view itself.",
        )

    def test_every_api_key_route_refuses_a_keyless_request(self):
        refused = []
        for pattern in self.api_key_routes:
            path = reverse(pattern.name, kwargs=_dummy_kwargs(pattern))
            for method in (self.client.get, self.client.post):
                response = method(path)
                self.assertNotIn(
                    response.status_code, (200, 201),
                    f"'{pattern.name}' ({path}) served a keyless "
                    f"{method.__name__.upper()} with {response.status_code}.",
                )
            refused.append(pattern.name)
        self.assertEqual(len(refused), len(self.api_key_routes))

    def test_a_valid_api_key_gets_past_the_gate(self):
        # The mirror image of the test above: proves the 403s there come from
        # the missing key, not from the route being broken for everyone.
        _, key = APIKey.objects.create_key(name="test-gate-probe")
        pattern = next(p for p in self.api_key_routes if p.name == "api_make_model_master")
        path = reverse(pattern.name)

        self.assertIn(self.client.get(path).status_code, (401, 403))
        self.assertEqual(
            self.client.get(path, HTTP_AUTHORIZATION=f"Api-Key {key}").status_code, 200
        )


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


@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class GridSummaryDateFilterTests(TestCase):
    """Rate Master Health > Grid Summary's "Valid As Of" filter."""

    def setUp(self):
        from datetime import date
        from insurance.models import ProductMaster, RateGroup, RateMaster

        self.client = Client()
        Group.objects.get_or_create(name="Can_View_Rate_Master_Health")
        self.user = User.objects.create_user(username="ops", password="a-strong-test-password-1")
        self.user.groups.add(Group.objects.get(name="Can_View_Rate_Master_Health"))
        self.client.force_login(self.user)

        product = ProductMaster.objects.create(name="Private Car")
        RateMaster.objects.create(
            insurance_company="Acme General", product=product, status="ACTIVE", is_deleted="NO",
            group=RateGroup.objects.create(key_hash="h1"),
            from_date=date(2026, 1, 1), to_date=date(2026, 6, 30),
        )
        RateMaster.objects.create(
            insurance_company="Zenith Insurance", product=product, status="ACTIVE", is_deleted="NO",
            group=RateGroup.objects.create(key_hash="h2"),
            from_date=date(2026, 7, 1), to_date=date(2026, 12, 31),
        )

    def test_no_filter_shows_every_grid(self):
        response = self.client.get(reverse("rate_master_health"), {"view": "grid"})
        self.assertEqual(response.context["grid_summary_total_combinations"], 2)

    def test_soft_deleted_rows_are_excluded_from_the_pivot(self):
        # is_deleted="YES" is the Rate Master dashboard's own "Is Deleted?"
        # flag - a soft-deleted grid must not still count here as uploaded.
        from datetime import date
        from insurance.models import ProductMaster, RateGroup, RateMaster
        RateMaster.objects.create(
            insurance_company="Deleted Co", product=ProductMaster.objects.create(name="Two Wheeler"),
            status="ACTIVE", is_deleted="YES", group=RateGroup.objects.create(key_hash="h3"),
            from_date=date(2026, 1, 1), to_date=date(2026, 6, 30),
        )
        response = self.client.get(reverse("rate_master_health"), {"view": "grid"})
        self.assertEqual(response.context["grid_summary_total_combinations"], 2)
        rows = list(response.context["grid_summary_page_obj"])
        self.assertFalse(any(r["insurance_company"] == "Deleted Co" for r in rows))

    def test_inactive_but_not_deleted_rows_are_still_included(self):
        # Deliberately different from is_deleted: an insurer's grid going
        # INACTIVE is a real, current fact worth seeing - only a soft
        # DELETE removes it from the pivot.
        from datetime import date
        from insurance.models import ProductMaster, RateGroup, RateMaster
        RateMaster.objects.create(
            insurance_company="Inactive Co", product=ProductMaster.objects.create(name="Two Wheeler"),
            status="INACTIVE", is_deleted="NO", group=RateGroup.objects.create(key_hash="h4"),
            from_date=date(2026, 1, 1), to_date=date(2026, 6, 30),
        )
        response = self.client.get(reverse("rate_master_health"), {"view": "grid"})
        self.assertEqual(response.context["grid_summary_total_combinations"], 3)
        rows = list(response.context["grid_summary_page_obj"])
        self.assertTrue(any(r["insurance_company"] == "Inactive Co" for r in rows))

    def test_as_of_date_narrows_to_grids_valid_on_that_date(self):
        response = self.client.get(
            reverse("rate_master_health"), {"view": "grid", "grid_as_of_date": "2026-03-15"}
        )
        rows = list(response.context["grid_summary_page_obj"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["insurance_company"], "Acme General")

    def test_an_invalid_date_is_ignored_rather_than_erroring(self):
        response = self.client.get(
            reverse("rate_master_health"), {"view": "grid", "grid_as_of_date": "not-a-date"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["grid_summary_total_combinations"], 2)

    def test_export_honours_the_same_filter(self):
        response = self.client.get(
            reverse("export_grid_summary_xlsx"), {"grid_as_of_date": "2026-03-15"}
        )
        self.assertEqual(response.status_code, 200)

        import io
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(response.content))
        ws = wb.active
        data_rows = list(ws.iter_rows(min_row=2, values_only=True))
        self.assertEqual(len(data_rows), 1)
        self.assertEqual(data_rows[0][2], "Acme General")

    def test_export_with_no_filter_exports_everything(self):
        response = self.client.get(reverse("export_grid_summary_xlsx"))
        import io
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(response.content))
        ws = wb.active
        data_rows = list(ws.iter_rows(min_row=2, values_only=True))
        self.assertEqual(len(data_rows), 2)


class ApiUploadChunkRateMasterDedupTests(TestCase):
    """
    Bulk Upload -> Rate Master (api_upload_chunk) used to insert a fresh
    RateMaster row for every row it was handed with no check for an
    identical row already existing in the same group -- so a source file
    that listed the same rate line twice, the browser resending an
    already-saved chunk, or a whole file getting re-uploaded all produced
    exact-duplicate rows sharing one group_id. Confirmed in production:
    108,516 duplicate rows across 21,532 groups (~14% of the table).

    The fix makes the insert idempotent per (group, new_rto_list) -- see
    existing_rto_by_group in api_upload_chunk's rate_master branch.
    """

    def setUp(self):
        from insurance.models import RTOMaster

        self.client = Client()
        Group.objects.get_or_create(name="Can_Upload_CSV")
        self.user = User.objects.create_user(username="uploader", password="a-strong-test-password-1")
        self.user.groups.add(Group.objects.get(name="Can_Upload_CSV"))
        self.client.force_login(self.user)

        RTOMaster.objects.create(rto_name="MUMBAI")
        RTOMaster.objects.create(rto_name="PUNE")

    def _row(self, rto="MUMBAI", **overrides):
        row = {
            "insurance_company": "Acme General",
            "new_rto_list": rto,
            "from_date": "2026-01-01",
            "to_date": "2026-12-31",
            "pi_od_rate": "10",
        }
        row.update(overrides)
        return row

    def _upload(self, rows, upload_batch_id="batch-1"):
        return self.client.post(
            reverse("api_upload_chunk"),
            data=json.dumps({
                "target_table": "rate_master",
                "rows": rows,
                "upload_batch_id": upload_batch_id,
                "dry_run": False,
            }),
            content_type="application/json",
        )

    def test_same_row_repeated_in_one_chunk_is_only_saved_once(self):
        from insurance.models import RateMaster

        response = self._upload([self._row(), self._row()])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(RateMaster.objects.count(), 1)

    def test_same_chunk_resent_in_a_second_request_is_not_duplicated(self):
        # Simulates a browser/network retry resending a chunk that already
        # succeeded, or the whole file being uploaded twice -- same
        # upload_batch_id both times, since that's what a single Upload
        # click reuses across its chunks/passes (see upload.html).
        from insurance.models import RateMaster

        first = self._upload([self._row()])
        second = self._upload([self._row()])
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(RateMaster.objects.count(), 1)

    def test_genuinely_different_rtos_in_the_same_group_both_saved(self):
        # Guards against the dedup check being too broad: two rows that
        # differ only by new_rto_list are still two legitimate rows.
        from insurance.models import RateMaster

        response = self._upload([self._row(rto="MUMBAI"), self._row(rto="PUNE")])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(RateMaster.objects.count(), 2)

    def test_reuploading_content_that_is_already_active_does_not_create_a_duplicate(self):
        # A *different* upload_batch_id simulates a genuinely separate upload
        # session (e.g. exporting an existing grid and re-importing it
        # unchanged) -- not a retry of the same one. Uploads always insert as
        # INACTIVE, so "already active" here means some later, separate step
        # turned this content live; re-uploading it shouldn't add a parallel
        # copy of a rate that's already in effect.
        from insurance.models import RateMaster

        self._upload([self._row()], upload_batch_id="original-upload")
        RateMaster.objects.update(status="ACTIVE")

        response = self._upload([self._row()], upload_batch_id="a-completely-different-upload")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(RateMaster.objects.count(), 1)

    def test_reuploading_when_existing_row_is_inactive_still_creates_a_new_one(self):
        # The guard only matches ACTIVE, non-deleted rows -- content that was
        # never activated (or was deliberately turned off) doesn't block a
        # legitimate re-upload of that same content.
        from insurance.models import RateMaster

        self._upload([self._row()], upload_batch_id="original-upload")
        self.assertEqual(RateMaster.objects.filter(status="INACTIVE").count(), 1)

        response = self._upload([self._row()], upload_batch_id="a-completely-different-upload")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(RateMaster.objects.count(), 2)


class DedupeRateMasterCommandTests(TestCase):
    """
    dedupe_rate_master soft-deletes the exact-duplicate rows produced by the
    now-fixed api_upload_chunk bug, keeping the lowest id per duplicate
    cluster. Manual testing surfaced a real bug here: the duplicate
    signature originally included is_deleted, so every already-soft-deleted
    row looked like a fresh duplicate of every other already-soft-deleted
    row on the very next scan (they're identical except id) -- re-running
    the command after a cleanup pass kept finding "more" duplicates
    forever. Fixed by scoping detection to is_deleted="NO" rows only.
    """

    def setUp(self):
        from datetime import date
        from insurance.models import ProductMaster, RateGroup, RateMaster

        product = ProductMaster.objects.create(name="Private Car")
        group = RateGroup.objects.create(key_hash="dedupe-test-group")

        def make(rto):
            return RateMaster.objects.create(
                group=group, product=product, insurance_company="Acme General",
                status="ACTIVE", is_deleted="NO", new_rto_list=rto,
                from_date=date(2026, 1, 1), to_date=date(2026, 12, 31),
            )

        # Group's real content: MUMBAI once, PUNE three times over (2 extras).
        self.kept_mumbai = make("MUMBAI")
        self.kept_pune = make("PUNE")
        self.dup_pune_1 = make("PUNE")
        self.dup_pune_2 = make("PUNE")

    def test_dry_run_reports_but_does_not_modify(self):
        from io import StringIO
        from django.core.management import call_command
        from insurance.models import RateMaster

        out = StringIO()
        call_command("dedupe_rate_master", stdout=out)
        self.assertIn("2", out.getvalue())
        self.assertEqual(RateMaster.objects.filter(is_deleted="YES").count(), 0)

    def test_apply_keeps_lowest_id_per_cluster_and_logs_it(self):
        from django.core.management import call_command
        from insurance.models import AuditLog, RateMaster

        call_command("dedupe_rate_master", apply=True)

        self.kept_mumbai.refresh_from_db()
        self.kept_pune.refresh_from_db()
        self.dup_pune_1.refresh_from_db()
        self.dup_pune_2.refresh_from_db()

        self.assertEqual(self.kept_mumbai.is_deleted, "NO")
        self.assertEqual(self.kept_pune.is_deleted, "NO")
        self.assertEqual(self.dup_pune_1.is_deleted, "YES")
        self.assertEqual(self.dup_pune_2.is_deleted, "YES")

        self.assertTrue(AuditLog.objects.filter(action="BULK DEDUPE").exists())

    def test_rerunning_after_apply_finds_nothing_left(self):
        from io import StringIO
        from django.core.management import call_command

        call_command("dedupe_rate_master", apply=True)

        out = StringIO()
        call_command("dedupe_rate_master", stdout=out)
        self.assertIn("Nothing to do.", out.getvalue())


class CrossGroupDedupeTests(TestCase):
    """
    --cross-group is the second, separate duplicate category found after
    the first cleanup shipped: identical-content rows living under
    *different* group_ids (e.g. exporting a grid and re-importing it
    unchanged creates a brand new group with the same content as an
    existing one). Default-mode dedupe_rate_master never compares across
    groups, so it can't see these -- only --cross-group can.
    """

    def setUp(self):
        from datetime import date
        from insurance.models import ProductMaster, RateGroup, RateMaster

        self.product = ProductMaster.objects.create(name="Private Car")

        def make(group, status):
            return RateMaster.objects.create(
                group=group, product=self.product, insurance_company="Acme General",
                status=status, is_deleted="NO", new_rto_list="MUMBAI",
                from_date=date(2026, 1, 1), to_date=date(2026, 12, 31),
            )

        # Same exact content, two different groups -- as if re-uploaded later.
        self.original = make(RateGroup.objects.create(key_hash="original-group"), "ACTIVE")
        self.reupload = make(RateGroup.objects.create(key_hash="reupload-group"), "ACTIVE")

    def test_default_mode_does_not_see_cross_group_duplicates(self):
        from io import StringIO
        from django.core.management import call_command

        out = StringIO()
        call_command("dedupe_rate_master", stdout=out)
        self.assertIn("Nothing to do.", out.getvalue())

    def test_cross_group_mode_keeps_lowest_id_and_soft_deletes_the_rest(self):
        from django.core.management import call_command
        from insurance.models import RateMaster

        call_command("dedupe_rate_master", apply=True, cross_group=True)

        self.original.refresh_from_db()
        self.reupload.refresh_from_db()
        self.assertEqual(self.original.is_deleted, "NO")
        self.assertEqual(self.reupload.is_deleted, "YES")

    def test_cross_group_mode_ignores_inactive_matches(self):
        # Mirrors the upload-time guard: only ACTIVE rows count as "already
        # exists" for this sweep too, so an inactive row with matching
        # content is left alone rather than being silently removed.
        from django.core.management import call_command
        from insurance.models import RateGroup, RateMaster

        self.reupload.status = "INACTIVE"
        self.reupload.save()

        call_command("dedupe_rate_master", apply=True, cross_group=True)

        self.reupload.refresh_from_db()
        self.assertEqual(self.reupload.is_deleted, "NO")


class PurgeDeletedRateMasterCommandTests(TestCase):
    """
    purge_deleted_rate_master PERMANENTLY deletes every is_deleted=YES row
    -- unlike dedupe_rate_master, this is a real DELETE, not a flag flip,
    and it's scoped to *every* soft-deleted row table-wide (not just ones
    from a specific cleanup run) per explicit instruction, since is_deleted
    is also a normal, independently-used feature of the app.
    """

    def setUp(self):
        from datetime import date
        from insurance.models import ProductMaster, RateGroup, RateMaster

        product = ProductMaster.objects.create(name="Private Car")
        group = RateGroup.objects.create(key_hash="purge-test-group")

        def make(is_deleted):
            return RateMaster.objects.create(
                group=group, product=product, insurance_company="Acme General",
                status="ACTIVE", is_deleted=is_deleted, new_rto_list="MUMBAI",
                from_date=date(2026, 1, 1), to_date=date(2026, 12, 31),
            )

        self.kept_row = make("NO")
        self.deleted_row_1 = make("YES")
        self.deleted_row_2 = make("YES")

    def test_dry_run_reports_but_does_not_delete(self):
        from io import StringIO
        from django.core.management import call_command
        from insurance.models import RateMaster

        out = StringIO()
        call_command("purge_deleted_rate_master", stdout=out)
        self.assertIn("2", out.getvalue())
        self.assertEqual(RateMaster.objects.count(), 3)

    def test_apply_permanently_deletes_only_is_deleted_yes_rows(self):
        from django.core.management import call_command
        from insurance.models import AuditLog, RateMaster

        call_command("purge_deleted_rate_master", apply=True)

        remaining_ids = set(RateMaster.objects.values_list("id", flat=True))
        self.assertEqual(remaining_ids, {self.kept_row.id})
        self.assertTrue(AuditLog.objects.filter(action="BULK PERMANENT DELETE").exists())

    def test_locked_policy_referencing_a_deleted_row_is_not_broken(self):
        from django.core.management import call_command
        from insurance.models import LockedPolicy

        lock = LockedPolicy.objects.create(
            source_rate=self.deleted_row_1, vehicle_no="MH12AB1234", policy_holder_name="Test Holder",
        )

        call_command("purge_deleted_rate_master", apply=True)

        lock.refresh_from_db()
        self.assertIsNone(lock.source_rate)


class RateDecimalRoundingTests(TestCase):
    """
    Dashboard rate fields (pi_od_rate, pi_tp_rate, pi_tp_2..5, pi_net_rate,
    pi_flat_amount, pi_vli, po_od_rate, po_tp_rate, po_net_rate,
    po_flat_amount) must never be saved with more than 2 decimal places,
    whether set via the single-record Edit form or Bulk Update Selected.
    """

    def setUp(self):
        from insurance.models import ProductMaster, RateGroup, RateMaster

        self.client = Client()
        Group.objects.get_or_create(name="Can_View_Dashboard")
        self.user = User.objects.create_user(username="ops", password="a-strong-test-password-1")
        self.user.groups.add(Group.objects.get(name="Can_View_Dashboard"))
        self.client.force_login(self.user)

        self.product = ProductMaster.objects.create(name="Private Car")
        self.group = RateGroup.objects.create(key_hash="decimal-rounding-group")
        self.record = RateMaster.objects.create(
            group=self.group, product=self.product, insurance_company="Acme General",
            status="ACTIVE", is_deleted="NO", pi_od_rate=10.0, po_od_rate=3.0,
        )

    def test_bulk_update_rounds_a_targeted_rate_field_to_two_decimals(self):
        from insurance.models import RateMaster

        self.client.post(reverse("bulk_update_rates"), {
            "selected_groups": str(self.group.id),
            "update_field": "pi_od_rate",
            "update_value": "12.34567",
        })
        self.record.refresh_from_db()
        self.assertEqual(self.record.pi_od_rate, 12.35)

    def test_bulk_update_does_not_round_unrelated_numeric_fields(self):
        # tariff_min/max, cc_min/max, vehicle_age_min/max, sc_min/max are
        # numeric too but were never in the two-decimal-place requirement.
        from insurance.models import RateMaster

        self.client.post(reverse("bulk_update_rates"), {
            "selected_groups": str(self.group.id),
            "update_field": "tariff_min",
            "update_value": "12.34567",
        })
        self.record.refresh_from_db()
        self.assertEqual(self.record.tariff_min, 12.34567)

    def test_edit_form_rounds_rate_field_to_two_decimals(self):
        from insurance.views import RateForm

        form = RateForm(data={
            "insurance_company": "Acme General",
            "status": "ACTIVE",
            "is_deleted": "NO",
            "pi_od_rate": "12.3456",
            "po_od_rate": "3.005",
        }, instance=self.record, initial={"new_rto_list": [], "new_vehicle_makes": []})

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["pi_od_rate"], 12.35)
        self.assertEqual(form.cleaned_data["po_od_rate"], 3.0)


class PiPoMarginFloatPrecisionTests(TestCase):
    """
    Rate Master Health's "Pi vs Po Validation Errors" cards
    (_rate_master_pi_po_rate_violations_qs) flag a row when Pi - Po isn't
    exactly RATE_MASTER_PI_PO_MARGIN (7). pi_od_rate/po_od_rate etc. are
    plain FloatFields (IEEE-754 doubles), so e.g. 19.65 - 12.65 computes to
    6.999999999999998 in raw floating point rather than 7.0 -- a
    mathematically exact margin was being flagged as a violation purely
    from binary float representation error.
    """

    def setUp(self):
        from insurance.models import ProductMaster, RateGroup, RateMaster

        product = ProductMaster.objects.create(name="Private Car")
        group = RateGroup.objects.create(key_hash="pi-po-float-precision-group")

        # 19.65 - 12.65 == 7 mathematically, but not in raw IEEE-754 double
        # arithmetic -- this is the exact pair reported as a false positive.
        self.exact_margin_row = RateMaster.objects.create(
            group=group, product=product, insurance_company="Acme General",
            status="ACTIVE", is_deleted="NO", pi_od_rate=19.65, po_od_rate=12.65,
        )
        self.genuine_violation_row = RateMaster.objects.create(
            group=group, product=product, insurance_company="Acme General",
            status="ACTIVE", is_deleted="NO", pi_od_rate=20.0, po_od_rate=12.0,
        )

    def test_exact_margin_from_float_subtraction_is_not_flagged(self):
        from insurance.views import _rate_master_pi_po_rate_violations_qs

        flagged_ids = set(
            _rate_master_pi_po_rate_violations_qs("pi_od_rate", "po_od_rate").values_list("id", flat=True)
        )
        self.assertNotIn(self.exact_margin_row.id, flagged_ids)

    def test_a_genuinely_wrong_margin_is_still_flagged(self):
        from insurance.views import _rate_master_pi_po_rate_violations_qs

        flagged_ids = set(
            _rate_master_pi_po_rate_violations_qs("pi_od_rate", "po_od_rate").values_list("id", flat=True)
        )
        self.assertIn(self.genuine_violation_row.id, flagged_ids)
