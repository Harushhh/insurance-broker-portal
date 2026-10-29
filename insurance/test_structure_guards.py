"""Guards for the code layout and for the routes/pages added with the restructure.

They fail loudly when the pieces drift apart again: a shared table gets edited in only one place, a service
starts importing a view, an import inside a function body points at a module that moved, the sidebar and the
page gates disagree about who may open a page, or the API docs lose their staff-only gate.
"""
import ast
import importlib
import logging
import pathlib
import re
from io import StringIO
from unittest import mock

import yaml
from django.contrib.auth.models import AnonymousUser, Group, User
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.http import Http404
from django.test import Client, RequestFactory, SimpleTestCase, TestCase
from django.urls import resolve, reverse

from insurance import access, constants, mapping_engine, tasks
from insurance.context_processors import sidebar_access
from insurance.services import make_model

APP_DIR = pathlib.Path(__file__).resolve().parent


class SharedDefinitionTests(SimpleTestCase):
    """What used to be written out twice (with a "keep in sync" comment) now has one definition."""

    def test_purge_tasks_and_pages_share_the_same_action_lists(self):
        from insurance import views
        self.assertIs(tasks.POINTS_SEARCH_ACTIONS, constants.POINTS_SEARCH_ACTIONS)
        self.assertIs(tasks.SECURITY_AUDIT_LOG_ACTIONS, constants.SECURITY_AUDIT_LOG_ACTIONS)
        self.assertIs(views.POINTS_SEARCH_ACTIONS, constants.POINTS_SEARCH_ACTIONS)
        self.assertIs(views.SECURITY_AUDIT_LOG_ACTIONS, constants.SECURITY_AUDIT_LOG_ACTIONS)

    def test_health_term_tables_agree_and_keep_their_values(self):
        self.assertIs(mapping_engine.HEALTH_TENURE_RATE_COLUMNS, constants.HEALTH_TENURE_RATE_COLUMNS)
        self.assertEqual(
            constants.HEALTH_POLICY_TERM_FIELD,
            {"1": "one_year_rate", "2": "multi_year_2_rate", "3": "multi_year_3_rate",
             "4": "multi_year_4_rate", "5": "multi_year_5_rate"},
        )
        self.assertEqual(
            constants.HEALTH_TENURE_RATE_COLUMNS,
            {1: "one_year_rate", 2: "multi_year_2_rate", 3: "multi_year_3_rate",
             4: "multi_year_4_rate", 5: "multi_year_5_rate"},
        )

    def test_the_mis_engine_uses_the_shared_cluster_matcher(self):
        self.assertIs(mapping_engine.strict_match_in_cluster, make_model.strict_match_in_cluster)

    def test_cluster_matcher_respects_word_boundaries(self):
        match = make_model.strict_match_in_cluster
        self.assertTrue(match("MH09", "MH01, MH09, MH12"))
        self.assertTrue(match("mh09", "MH01,MH09"))
        self.assertFalse(match("MH09", "MH090"))
        self.assertFalse(match("MH09", ""))

    def test_names_that_used_to_live_in_views_py_still_import_from_the_views_package(self):
        from insurance import views
        for name in ("PAGE_GROUPS", "RATE_CHECKER_TABS", "RATE_CHECKER_GROUPS", "GRID_INSURER_CHOICES",
                     "HEALTH_POLICY_TERM_FIELD", "RateForm", "GROUP_FIELDS", "build_key_hash",
                     "process_policy_document", "resolve_make_model_groups", "ExportRatesAPIView"):
            with self.subTest(name=name):
                self.assertTrue(hasattr(views, name), f"insurance.views.{name} disappeared")


class LayeringTests(SimpleTestCase):
    """services/ is the lower layer: it may use models and other services, never views or the API package."""

    def test_services_never_import_views_or_api(self):
        offenders = []
        for path in sorted((APP_DIR / "services").glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.ImportFrom):
                    continue
                target = "." * node.level + (node.module or "")
                names = [a.name for a in node.names]
                bad = re.match(r"^\.\.(views|api)(\.|$)", target) or (
                    target == ".." and any(n in ("views", "api") for n in names))
                if bad:
                    offenders.append(f"{path.name}:{node.lineno} imports {target} {names}")
        self.assertFalse(offenders, "services must not depend on views/api:\n  " + "\n  ".join(offenders))

    def test_every_import_in_the_app_resolves_even_inside_function_bodies(self):
        """A relative import inside a function only fails when that code path runs; a module that moved one
        package deeper breaks it silently. Resolve every one now."""
        problems = []
        skip = {"migrations", "__pycache__"}
        for path in sorted(APP_DIR.rglob("*.py")):
            rel = path.relative_to(APP_DIR.parent)
            if skip & set(rel.parts) or path.name.startswith("test"):
                continue
            package = list(rel.with_suffix("").parts)
            if path.name != "__init__.py":
                package = package[:-1]
            else:
                package = package[:-1]
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.ImportFrom) or not node.level:
                    continue
                if node.level - 1 > len(package):
                    problems.append(f"{rel}:{node.lineno} climbs above the top package")
                    continue
                base = package[: len(package) - (node.level - 1)]
                modname = ".".join(base + ([node.module] if node.module else []))
                try:
                    mod = importlib.import_module(modname)
                except Exception as exc:  # noqa: BLE001 - report every failure, whatever it is
                    problems.append(f"{rel}:{node.lineno} cannot import {modname} ({type(exc).__name__})")
                    continue
                for alias in node.names:
                    if alias.name != "*" and not hasattr(mod, alias.name):
                        try:
                            importlib.import_module(f"{modname}.{alias.name}")
                        except Exception:  # noqa: BLE001
                            problems.append(f"{rel}:{node.lineno} {modname} has no name {alias.name}")
        self.assertFalse(problems, "unresolvable imports:\n  " + "\n  ".join(problems))


class SidebarMatchesPageGateTests(TestCase):
    """context_processors.sidebar_access draws the sidebar links; access.py decides who may open the pages.
    The two must agree, or the sidebar shows links that 403 (or hides pages that would open)."""

    def setUp(self):
        self.groups = {name: Group.objects.get_or_create(name=name)[0]
                       for name in ("ADMIN", "SUPER_ADMIN", "Can_View_Dashboard")}
        self.nobody = User.objects.create_user("gate_nobody", password="unused-throwaway-1")
        self.one_page = User.objects.create_user("gate_one_page", password="unused-throwaway-1")
        self.one_page.groups.add(self.groups["Can_View_Dashboard"])
        self.admin = User.objects.create_user("gate_admin", password="unused-throwaway-1")
        self.admin.groups.add(self.groups["ADMIN"])
        self.staff = User.objects.create_user("gate_staff", password="unused-throwaway-1", is_staff=True)
        self.superuser = User.objects.create_user("gate_super", password="unused-throwaway-1", is_superuser=True)
        self.super_admin = User.objects.create_user("gate_super_admin", password="unused-throwaway-1")
        self.super_admin.groups.add(self.groups["SUPER_ADMIN"])
        self.everyone = [self.nobody, self.one_page, self.admin, self.staff, self.superuser, self.super_admin]

    @staticmethod
    def _request(user):
        request = RequestFactory().get("/")
        request.user = user
        return request

    def _allows(self, decorator, user):
        view = decorator(lambda request: "opened")
        try:
            return view(self._request(user)) == "opened"
        except PermissionDenied:
            return False

    def test_page_links_follow_page_access_required_for_every_group(self):
        for user in self.everyone:
            nav_pages = sidebar_access(self._request(user))["nav_pages"]
            for group_name in access.PAGE_GROUPS:
                with self.subTest(user=user.username, group=group_name):
                    self.assertEqual(group_name in nav_pages,
                                     self._allows(access.page_access_required(group_name), user))

    def test_access_control_link_follows_staff_required(self):
        for user in self.everyone:
            with self.subTest(user=user.username):
                self.assertEqual(sidebar_access(self._request(user))["nav_can_access_control"],
                                 self._allows(access.staff_required, user))

    def test_super_admin_flag_follows_super_admin_required(self):
        for user in self.everyone:
            with self.subTest(user=user.username):
                self.assertEqual(sidebar_access(self._request(user))["nav_is_super_admin"],
                                 self._allows(access.super_admin_required, user))

    def test_anonymous_users_get_no_sidebar(self):
        request = RequestFactory().get("/")
        request.user = AnonymousUser()
        self.assertEqual(sidebar_access(request), {})


class ApiVersionAliasTests(TestCase):
    """/api/v1/ is the canonical prefix; the older unversioned paths stay as aliases of the very same views."""

    PAIRS = [
        ("/api/policy-lock-checker/", "/api/v1/policy-lock-checker/"),
        ("/api/health-payout-rates/", "/api/v1/health-payout-rates/"),
        ("/api/make-model-master/", "/api/v1/make-model-master/"),
        ("/api/sso/issue-ticket/", "/api/v1/sso/issue-ticket/"),
    ]

    def test_each_alias_is_the_same_view_as_its_legacy_path(self):
        for legacy, versioned in self.PAIRS:
            with self.subTest(path=versioned):
                self.assertIs(resolve(legacy).func.cls, resolve(versioned).func.cls)

    def test_aliases_refuse_a_keyless_request_exactly_like_the_originals(self):
        client = Client()
        for legacy, versioned in self.PAIRS:
            with self.subTest(path=versioned):
                self.assertEqual(client.get(legacy).status_code, 403)
                self.assertEqual(client.get(versioned).status_code, 403)

    def test_export_rates_keeps_its_versioned_path_and_login_gate(self):
        response = Client().get("/api/v1/export-rates/")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse("login")))


class ApiDocsAccessTests(TestCase):
    """Swagger / ReDoc / the OpenAPI schema were shadowed by the login catch-all; they are reachable now,
    for staff only."""

    NAMES = ("schema", "swagger-ui", "redoc")

    def setUp(self):
        self.staff = User.objects.create_user("docs_staff", password="unused-throwaway-1", is_staff=True)
        self.plain = User.objects.create_user("docs_plain", password="unused-throwaway-1")

    def test_anonymous_visitors_are_sent_to_login_with_a_next_target(self):
        for name in self.NAMES:
            with self.subTest(route=name):
                response = Client().get(reverse(name))
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.url.startswith(reverse("login")))
                self.assertIn("next=", response.url)  # the catch-all redirect has no next=

    def test_signed_in_non_staff_users_are_refused(self):
        client = Client()
        client.force_login(self.plain)
        for name in self.NAMES:
            with self.subTest(route=name):
                self.assertEqual(client.get(reverse(name)).status_code, 403)

    def test_staff_can_open_all_three(self):
        client = Client()
        client.force_login(self.staff)
        for name in self.NAMES:
            with self.subTest(route=name):
                self.assertEqual(client.get(reverse(name)).status_code, 200)

    def test_schema_documents_the_versioned_paths_once(self):
        client = Client()
        client.force_login(self.staff)
        spec = yaml.safe_load(client.get(reverse("schema")).content)
        paths = set(spec["paths"])
        for versioned in ("/api/v1/export-rates/", "/api/v1/policy-lock-checker/",
                          "/api/v1/health-payout-rates/", "/api/v1/make-model-master/",
                          "/api/v1/sso/issue-ticket/"):
            self.assertIn(versioned, paths)
        for legacy in ("/api/policy-lock-checker/", "/api/health-payout-rates/",
                       "/api/make-model-master/", "/api/sso/issue-ticket/"):
            self.assertNotIn(legacy, paths)


class ErrorPageTests(TestCase):
    def test_404_page_matches_the_403_card(self):
        from django.views.defaults import page_not_found
        request = RequestFactory().get("/missing/")
        request.user = AnonymousUser()
        response = page_not_found(request, Http404())
        body = response.content.decode()
        self.assertEqual(response.status_code, 404)
        self.assertIn("Page Not Found", body)
        self.assertIn("Back to Dashboard", body)
        self.assertIn("denied-card", body)  # shared styling with 403.html

    def test_403_page_still_renders_its_wording_now_that_404_extends_it(self):
        user = User.objects.create_user("err_plain", password="unused-throwaway-1")
        client = Client()
        client.force_login(user)
        response = client.get(reverse("user_management"))  # staff-only page -> 403
        self.assertEqual(response.status_code, 403)
        body = response.content.decode()
        self.assertIn("Access Denied", body)
        self.assertIn("restricted to staff accounts", body)

    def test_500_page_is_self_contained(self):
        from django.views.defaults import server_error
        response = server_error(RequestFactory().get("/"))
        self.assertEqual(response.status_code, 500)
        self.assertIn("Something went wrong", response.content.decode())

    def test_an_unhandled_exception_renders_the_500_page(self):
        user = User.objects.create_user("err_admin", password="unused-throwaway-1")
        user.groups.add(Group.objects.get_or_create(name="ADMIN")[0])
        client = Client(raise_request_exception=False)
        client.force_login(user)
        with mock.patch("insurance.views.home.render", side_effect=RuntimeError("boom")):
            with self.assertLogs("django.request", level="ERROR"):  # the expected traceback, kept out of the test output
                response = client.get(reverse("home_dashboard"))
        self.assertEqual(response.status_code, 500)
        self.assertIn("Something went wrong", response.content.decode())


class AccessMatrixCommandTests(TestCase):
    """docs/access-control.md embeds the output of this command, so it has to keep reading the gates correctly."""

    def test_matrix_reports_each_kind_of_gate(self):
        out = StringIO()
        call_command("show_access_matrix", stdout=out)
        text = out.getvalue()
        expected = {
            "dashboard": "group `Can_View_Dashboard` or ADMIN",
            "user_management": "staff, superuser or ADMIN",
            "rate_checker": "any signed-in user",
            "api_policy_lock_checker": "API key",
            "api-export-rates": "group `Can_View_Dashboard` or ADMIN + API key",
            "password_reset": "public",
            "schema": "staff, superuser or ADMIN",
        }
        for name, gate in expected.items():
            with self.subTest(route=name):
                self.assertRegex(text, rf"\| `/[^`]*` \| `{re.escape(name)}` \| {re.escape(gate)} \|")

    def test_every_named_route_appears_once_per_pattern(self):
        out = StringIO()
        call_command("show_access_matrix", stdout=out)
        rows = [line for line in out.getvalue().splitlines() if line.startswith("| `/")]
        self.assertGreater(len(rows), 90)
        self.assertEqual(len(rows), len(set(rows)))


class StaticReferenceTests(SimpleTestCase):
    """Test runs use plain static storage, which does not check that a file exists -- a typo in a {% static %}
    tag would just render a page without its CSS. Check the references directly."""

    def test_every_static_file_a_template_asks_for_exists(self):
        from django.contrib.staticfiles import finders
        pattern = re.compile(r"""\{%\s*static\s+['"]([^'"]+)['"]\s*%\}""")
        missing = []
        for path in sorted((APP_DIR / "templates").rglob("*.html")):
            for ref in pattern.findall(path.read_text(encoding="utf-8")):
                if not finders.find(ref):
                    missing.append(f"{path.relative_to(APP_DIR)} -> {ref}")
        self.assertFalse(missing, "templates reference static files that do not exist:\n  " + "\n  ".join(missing))

    def test_pages_that_load_page_assets_also_load_the_static_tag_library(self):
        """A {% static %} tag only works in a template that has {% load static %} (child templates do not
        inherit it from their parent)."""
        offenders = []
        for path in sorted((APP_DIR / "templates").rglob("*.html")):
            text = path.read_text(encoding="utf-8")
            if re.search(r"\{%\s*static\s", text) and not re.search(r"\{%\s*load\s+[^%]*\bstatic\b", text):
                offenders.append(str(path.relative_to(APP_DIR)))
        self.assertFalse(offenders, f"templates use {{% static %}} without {{% load static %}}: {offenders}")


class LoggingTests(SimpleTestCase):
    def test_insurance_loggers_have_a_console_handler(self):
        logger = logging.getLogger("insurance")
        self.assertTrue(logger.handlers)
        self.assertFalse(logger.propagate)

    def test_a_failed_background_email_is_logged_and_does_not_raise(self):
        from insurance.services.email import send_email_background
        with mock.patch("insurance.services.email.send_mail", side_effect=RuntimeError("smtp down")):
            with self.assertLogs("insurance.services.email", level="ERROR") as captured:
                self.assertIsNone(send_email_background("subject", "body", ["someone@example.com"]))
        self.assertTrue(any("Background email failed" in line and "smtp down" in line for line in captured.output))

    def test_a_sent_background_email_is_logged_at_info(self):
        from insurance.services.email import send_email_background
        with mock.patch("insurance.services.email.send_mail", return_value=1):
            with self.assertLogs("insurance.services.email", level="INFO") as captured:
                send_email_background("subject", "body", ["a@example.com", "b@example.com"])
        self.assertTrue(any("2 recipient(s)" in line for line in captured.output))
