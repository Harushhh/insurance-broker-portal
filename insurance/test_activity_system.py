"""Team Activity: events with nobody signed in (System), and the source path of every event.

System = a search made through the API with an API key, an audit entry written by a management command. Before this they
were on the Points Logs page but missing from /team-activity/. They are filed under the System pseudo-person (no actor,
username "(system)"), only ADMIN can read them, and, like every event, they say where they came from (`source_path`:
the URL path of the request, or `celery:<task name>`; never the query string).
"""
import importlib
import io
from datetime import timedelta
from io import StringIO

from celery import shared_task
from django.apps import apps as django_apps
from django.contrib.auth.models import Group, User
from django.contrib.auth.validators import UnicodeUsernameValidator
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from openpyxl import load_workbook
from rest_framework_api_key.models import APIKey

from insurance.models import ActivityEvent, AuditLog, FuelTypeMaster, ProductMaster, RateGroup, RateMaster
from insurance.services.activity import (
    SYSTEM_NAME, SYSTEM_USERNAME, activity_source, current_source, record_activity, record_system_activity,
    retention_cutoff,
)
from insurance.test_team_activity import PageTestMixin


@shared_task(name="insurance.test_activity_system.probe_source")
def probe_source():
    """A real Celery task (run eagerly in the tests below) that reports where an event recorded inside it would say it
    came from."""
    return current_source()


# -------------------------
# The writer
# -------------------------
class SystemEventWriterTests(TestCase):
    def test_a_system_event_has_no_actor_the_system_name_and_its_source(self):
        event = record_system_activity(
            "TEST", "policy_lock.checked", "Ran a Policy Lock Checker search through the API",
            details={"page": "Policy Lock Checker (API)", "parameters": {"vehicle_no": "MH12AB1234"}},
            source="/api/v1/policy-lock-checker/",
        )
        event.refresh_from_db()
        self.assertTrue(event.is_system)
        self.assertEqual((event.actor, event.actor_username, event.actor_name), (None, SYSTEM_USERNAME, SYSTEM_NAME))
        self.assertEqual((event.category, event.action), ("TEST", "policy_lock.checked"))
        self.assertEqual(event.source_path, "/api/v1/policy-lock-checker/")
        self.assertEqual(event.details["parameters"], {"vehicle_no": "MH12AB1234"})

    def test_the_reserved_name_cannot_be_a_real_username(self):
        with self.assertRaises(ValidationError):
            UnicodeUsernameValidator()(SYSTEM_USERNAME)

    def test_a_deleted_accounts_events_are_not_system_events(self):
        user = User.objects.create(username="gone")
        event = record_activity(user, "RUN", "x.run", "Ran")
        user.delete()
        event.refresh_from_db()
        self.assertIsNone(event.actor)
        self.assertEqual(event.actor_username, "gone")
        self.assertFalse(event.is_system)

    def test_recording_for_nobody_through_the_person_function_still_records_nothing(self):
        self.assertIsNone(record_activity(None, "RUN", "x.run", "Ran"))
        self.assertEqual(ActivityEvent.objects.count(), 0)

    def test_an_unknown_category_is_logged_and_does_not_raise(self):
        with self.assertLogs("insurance.services.activity", level="ERROR"):
            self.assertIsNone(record_system_activity("NOT_A_CATEGORY", "x.run", "Ran"))
        self.assertEqual(ActivityEvent.objects.count(), 0)


# -------------------------
# Where an event came from
# -------------------------
class SourceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username="rita")

    def test_inside_a_block_every_event_says_where_it_came_from(self):
        with activity_source("/motor-payout-rates/"):
            person = record_activity(self.user, "TEST", "x.test", "Ran")
            system = record_system_activity("TEST", "x.test", "Ran")
        self.assertEqual((person.source_path, system.source_path), ("/motor-payout-rates/", "/motor-payout-rates/"))

    def test_outside_any_request_or_task_it_is_empty(self):
        self.assertEqual(current_source(), "")
        self.assertEqual(record_activity(self.user, "RUN", "x.run", "Ran").source_path, "")

    def test_an_explicit_source_wins_over_the_current_one(self):
        with activity_source("/a/"):
            self.assertEqual(record_activity(self.user, "RUN", "x.run", "Ran", source="/b/").source_path, "/b/")
            self.assertEqual(record_activity(self.user, "RUN", "x.run", "Ran", source="").source_path, "")

    def test_blocks_nest_and_restore_what_was_there(self):
        with activity_source("/outer/"):
            with activity_source("/inner/"):
                self.assertEqual(current_source(), "/inner/")
            self.assertEqual(current_source(), "/outer/")
        self.assertEqual(current_source(), "")

    def test_a_path_longer_than_the_column_is_cut_instead_of_failing(self):
        event = record_activity(self.user, "RUN", "x.run", "Ran", source="/x" * 200)
        self.assertEqual(len(event.source_path), 200)

    def test_inside_a_celery_task_the_task_is_the_source_even_during_a_request(self):
        self.assertEqual(probe_source.apply().get(), "celery:insurance.test_activity_system.probe_source")
        with activity_source("/upload/"):
            self.assertEqual(probe_source.apply().get(), "celery:insurance.test_activity_system.probe_source")
            self.assertEqual(current_source(), "/upload/")             # and outside the task, the request again


class SourcePathEndToEndTests(TestCase):
    """The middleware, the checker hooks and the API, as they run for real."""

    def sign_in(self, *groups):
        user = User.objects.create(username="tester")
        for name in groups:
            user.groups.add(Group.objects.get_or_create(name=name)[0])
        self.client.force_login(user)
        return user

    def test_a_page_search_records_the_page_path_and_never_the_query_string(self):
        self.sign_in("Can_View_Motor_Payout_Rates")
        self.client.get(reverse("motor_payout_rates"), {"vehicle_no": "MH12AB1234"})
        event = ActivityEvent.objects.get(action="motor.eligibility.checked")
        self.assertEqual(event.source_path, reverse("motor_payout_rates"))
        self.assertNotIn("MH12AB1234", event.source_path)

    def test_an_edit_made_through_a_page_says_which_page(self):
        self.sign_in("Can_View_Missing_Make_Model")
        self.client.post(reverse("mark_missing_make_model_resolved"),
                         {"mm_mark_value": "Hero Splendor", "mm_mark_product": "Two Wheeler"})
        event = ActivityEvent.objects.get(action="missing_make_model.marked_resolved")
        self.assertEqual(event.source_path, reverse("mark_missing_make_model_resolved"))

    def test_an_api_key_health_search_is_filed_under_system_with_the_url_that_was_called(self):
        _, key = APIKey.objects.create_key(name="partner")
        for path in ("/api/v1/health-payout-rates/", "/api/health-payout-rates/"):
            with self.subTest(path=path):
                ActivityEvent.objects.all().delete()
                response = self.client.get(path, {"policy_term": "1"}, HTTP_AUTHORIZATION=f"Api-Key {key}")
                self.assertEqual(response.status_code, 200)
                event = ActivityEvent.objects.get()                   # exactly one: the audit row is not mirrored too
                self.assertTrue(event.is_system)
                self.assertEqual((event.category, event.action, event.source_path), ("TEST", "health.eligibility.checked", path))
                self.assertEqual(event.details["page"], "Health Payout Rates (API)")
                self.assertEqual(event.details["parameters"], {"policy_term": "1"})
                self.assertEqual(event.legacy_audit_id, AuditLog.objects.filter(action="HEALTH_POINTS_SEARCH").latest("id").pk)

    def test_an_api_key_policy_lock_search_is_filed_under_system_too(self):
        _, key = APIKey.objects.create_key(name="partner")
        for path in ("/api/v1/policy-lock-checker/", "/api/policy-lock-checker/"):
            with self.subTest(path=path):
                ActivityEvent.objects.all().delete()
                response = self.client.get(path, {"vehicle_no": "MH12AB1234"}, HTTP_AUTHORIZATION=f"Api-Key {key}")
                self.assertEqual(response.status_code, 200)
                event = ActivityEvent.objects.get()
                self.assertTrue(event.is_system)
                self.assertEqual((event.action, event.source_path), ("policy_lock.checked", path))
                self.assertEqual(event.details["page"], "Policy Lock Checker (API)")
                self.assertEqual(event.details["parameters"], {"vehicle_no": "MH12AB1234"})
                self.assertNotIn("MH12AB1234", event.summary + event.source_path)

    def test_an_api_call_with_no_search_values_records_nothing(self):
        _, key = APIKey.objects.create_key(name="partner")
        self.client.get("/api/v1/health-payout-rates/", HTTP_AUTHORIZATION=f"Api-Key {key}")
        self.assertEqual(ActivityEvent.objects.count(), 0)

    def test_a_signed_in_search_still_matches_the_person_and_links_to_its_points_log_row(self):
        user = self.sign_in("Can_View_Health_Payout_Rates")
        self.client.get(reverse("health_payout_rates"), {"policy_term": "1"})
        event = ActivityEvent.objects.get()
        self.assertEqual((event.actor, event.source_path), (user, reverse("health_payout_rates")))
        self.assertEqual(event.legacy_audit_id, AuditLog.objects.get(action="HEALTH_POINTS_SEARCH").pk)

    def test_a_management_command_audit_entry_is_a_system_event_sourced_to_the_command(self):
        product = ProductMaster.objects.create(name="Private Car")
        group = RateGroup.objects.create(key_hash="system-purge")
        RateMaster.objects.create(group=group, product=product, insurance_company="Acme General",
                                  status="ACTIVE", is_deleted="YES")
        call_command("purge_deleted_rate_master", apply=True, stdout=StringIO())
        event = ActivityEvent.objects.get(action="BULK PERMANENT DELETE")
        self.assertTrue(event.is_system)
        self.assertEqual((event.category, event.source_path), ("EDIT", "manage.py purge_deleted_rate_master"))
        self.assertEqual(event.legacy_audit_id, AuditLog.objects.get(action="BULK PERMANENT DELETE").pk)


# -------------------------
# The one-time copy of what the Points Logs page shows
# -------------------------
class PointsLogBackfillTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username="rita", first_name="Rita", last_name="Shah")
        self.migration = importlib.import_module("insurance.migrations.0065_backfill_points_logs_into_activity")
        self.cutoff = retention_cutoff()
        self.inside = self.cutoff + timedelta(days=5)

    def old_row(self, when, action="MOTOR_POINTS_SEARCH", details="{'vehicle_no': 'MH12AB1234'}", user=None):
        """An audit row as it was before this feature: written at `when`, and with no event for it on the feed."""
        entry = AuditLog.objects.create(user=user, action=action, details=details)
        AuditLog.objects.filter(pk=entry.pk).update(timestamp=when)
        ActivityEvent.objects.all().delete()
        return entry

    def checker_event_at(self, when):
        """The first event the hook for a signed-in person's search wrote, `when`."""
        event = record_activity(self.user, "TEST", "motor.eligibility.checked", "Ran a Motor eligibility check")
        ActivityEvent.objects.filter(pk=event.pk).update(occurred_at=when)

    def copy(self):
        self.migration.copy_system_and_points_logs_into_team_activity(django_apps, None)

    def test_a_system_search_becomes_a_system_test_event_with_its_values_by_name(self):
        product = ProductMaster.objects.create(name="Private Car")
        petrol = FuelTypeMaster.objects.create(name="Petrol")
        text = str({"vehicle_no": "MH12AB1234", "product": str(product.pk), "fuel": str(petrol.pk), "cc": "1197"})
        row = self.old_row(self.inside, details=text)
        self.copy()
        event = ActivityEvent.objects.get()
        self.assertTrue(event.is_system)
        self.assertEqual((event.category, event.action, event.summary),
                         ("TEST", "motor.eligibility.checked", "Ran a Motor eligibility check"))
        self.assertEqual(event.details, {"page": "Motor eligibility check", "parameters": {
            "vehicle_no": "MH12AB1234", "product": "Private Car", "fuel": "Petrol", "cc": "1197"}})
        self.assertEqual(event.legacy_audit_id, row.pk)
        self.assertEqual(event.occurred_at, AuditLog.objects.get(pk=row.pk).timestamp)
        self.assertEqual(event.source_path, "")                      # the old rows never knew which URL

    def test_a_health_search_and_the_older_text_prefix_are_read_too(self):
        self.old_row(self.inside, action="HEALTH_POINTS_SEARCH",
                     details="Eligibility Check Parameters: {'policy_term': '1', 'insurance_company': 'Acme, Beta'}")
        self.copy()
        event = ActivityEvent.objects.get()
        self.assertEqual((event.action, event.details["page"]), ("health.eligibility.checked", "Health eligibility check"))
        self.assertEqual(event.details["parameters"], {"policy_term": "1", "insurance_company": "Acme, Beta"})

    def test_text_that_cannot_be_read_as_values_is_kept_whole(self):
        self.old_row(self.inside, details="not a dict at all")
        self.copy()
        self.assertEqual(ActivityEvent.objects.get().details, {"entry": "not a dict at all"})

    def test_a_signed_in_persons_search_from_before_the_hook_is_copied_and_later_ones_are_not(self):
        before = self.old_row(self.inside - timedelta(days=1), user=self.user)
        after = self.old_row(self.inside + timedelta(days=1), user=self.user)
        system_after = self.old_row(self.inside + timedelta(days=1))                # System rows have no event of their own
        self.checker_event_at(self.inside)                                          # the hook shipped here
        self.copy()
        copied = {event.legacy_audit_id: event for event in ActivityEvent.objects.exclude(legacy_audit_id=None)}
        self.assertEqual(set(copied), {before.pk, system_after.pk})
        self.assertNotIn(after.pk, copied)
        self.assertEqual((copied[before.pk].actor, copied[before.pk].actor_name), (self.user, "Rita Shah"))
        self.assertTrue(copied[system_after.pk].is_system)

    def test_with_no_hook_event_yet_every_signed_in_search_is_copied(self):
        row = self.old_row(self.inside, user=self.user)
        self.copy()
        self.assertEqual(ActivityEvent.objects.get().legacy_audit_id, row.pk)

    def test_other_entries_with_no_user_become_system_edit_events_and_nothing_else_is_touched(self):
        system = self.old_row(self.inside, action="BULK DEDUPE", details="dedupe_rate_master: 12 rows")
        self.old_row(self.inside, action="sso_ticket_issued", details="partner", user=self.user)
        self.old_row(self.inside, action="sso_ticket_consumed", details="landing=home")
        self.old_row(self.inside, action="MANUAL EDIT", details="edited by a person", user=self.user)   # migration 0063's
        self.copy()
        event = ActivityEvent.objects.get()
        self.assertEqual((event.legacy_audit_id, event.is_system, event.category, event.action),
                         (system.pk, True, "EDIT", "BULK DEDUPE"))
        self.assertEqual(event.details, {"entry": "dedupe_rate_master: 12 rows"})

    def test_rows_before_the_retention_window_are_not_copied(self):
        self.old_row(self.cutoff - timedelta(hours=1))
        self.copy()
        self.assertEqual(ActivityEvent.objects.count(), 0)

    def test_running_it_twice_copies_each_row_once_and_leaves_a_row_that_has_its_event_alone(self):
        self.old_row(self.inside)
        mirrored = AuditLog.objects.create(user=None, action="BULK DEDUPE", details="written after the mirror existed")
        self.assertEqual(ActivityEvent.objects.filter(legacy_audit_id=mirrored.pk).count(), 1)
        self.copy()
        self.copy()
        self.assertEqual(ActivityEvent.objects.count(), 2)


# -------------------------
# The page and the export
# -------------------------
class SystemOnThePageTests(PageTestMixin, TestCase):
    def setUp(self):
        self.build_page_org()
        self.admin = self.make_admin()
        self.api_event = record_system_activity(
            "TEST", "policy_lock.checked", "Ran a Policy Lock Checker search through the API",
            details={"page": "Policy Lock Checker (API)", "parameters": {"vehicle_no": "KA01ZZ9999"}},
            source="/api/v1/policy-lock-checker/",
        )
        self.old_event = record_system_activity(
            "TEST", "health.eligibility.checked", "Ran a Health eligibility check", source="")
        with activity_source("/motor-payout-rates/"):
            record_activity(self.a1, "TEST", "motor.eligibility.checked", "Ran a Motor eligibility check")

    @staticmethod
    def workbook_text(response):
        wb = load_workbook(io.BytesIO(response.content))
        return {str(value) for ws in wb for row in ws.iter_rows(values_only=True) for value in row if value is not None}

    def test_an_admin_sees_system_events_named_system_with_their_source_path(self):
        response = self.get(self.admin)
        system = [event for event in response.context["events"] if event.is_system]
        self.assertEqual({event.pk for event in system}, {self.api_event.pk, self.old_event.pk})
        self.assertContains(response, "<th>Source path</th>")
        self.assertContains(response, "no signed-in user")
        self.assertContains(response, "<code")                              # a recorded path is shown as code
        self.assertContains(response, "/api/v1/policy-lock-checker/")
        self.assertContains(response, "KA01ZZ9999")                         # and its details, like any event
        self.assertContains(response, "not recorded")                       # the event that never knew its path

    def test_a_signed_in_persons_event_shows_its_page_path_too(self):
        self.assertContains(self.get(self.admin), "/motor-payout-rates/")
        self.assertContains(self.get(self.lead1), "/motor-payout-rates/")

    def test_no_manager_ever_sees_a_system_event(self):
        for viewer in (self.boss, self.lead1, self.lead2, self.a2, self.other_boss):
            for params in ({}, {"person": "system"}, {"q": "api"}, {"q": "KA01ZZ9999"}, {"date_range": ""}):
                with self.subTest(viewer=viewer.username, params=params):
                    response = self.get(viewer, params)
                    self.assertEqual([e for e in response.context["events"] if e.is_system], [])
                    self.assertNotContains(response, "Policy Lock Checker (API)")           # its details
                    self.assertNotContains(response, "/api/v1/policy-lock-checker/")
                    if params.get("q") == "KA01ZZ9999":
                        # The search box echoes what was typed, so look at what came back instead.
                        self.assertEqual(response.context["total"], 0)
                    else:
                        self.assertNotContains(response, "KA01ZZ9999")
            export = self.get(viewer, name="export_team_activity_xlsx")
            texts = self.workbook_text(export)
            self.assertFalse({SYSTEM_USERNAME, "KA01ZZ9999", "/api/v1/policy-lock-checker/"} & texts)

    def test_system_counts_in_the_admins_charts_and_never_in_a_managers(self):
        admin_total = self.get(self.admin).context["category_chart"]["total"]
        manager_total = self.get(self.lead1).context["category_chart"]["total"]
        everyone_else = ActivityEvent.objects.exclude(actor__isnull=True).count()        # 10 from the org + a1's search
        self.assertEqual(admin_total, everyone_else + 2)
        self.assertEqual(manager_total, 3 + 1)                                           # a1, a2, junior + a1's search

    def test_the_person_filter_offers_system_to_an_admin_and_to_nobody_else(self):
        self.assertContains(self.get(self.admin), '<option value="system"')
        self.assertNotContains(self.get(self.lead1), '<option value="system"')

    def test_picking_system_narrows_the_feed_and_the_charts_to_it(self):
        response = self.get(self.admin, {"person": "system"})
        self.assertEqual({e.pk for e in response.context["events"]}, {self.api_event.pk, self.old_event.pk})
        self.assertEqual(response.context["total"], 2)
        self.assertEqual((response.context["category_chart"]["scope"], response.context["category_chart"]["total"]),
                         ("System", 2))
        self.assertContains(response, '<option value="system" selected>')

    def test_the_people_bars_have_a_system_bar_for_an_admin(self):
        rows = self.get(self.admin).context["people_chart"]["rows"]
        system = next(row for row in rows if row["username"] == SYSTEM_USERNAME)
        self.assertEqual((system["name"], system["n"], system["id"]), ("System", 2, "system"))
        self.assertEqual(system["url"].count("person=system"), 1)                        # a click picks System
        picked = self.get(self.admin, {"person": "system"}).context["people_chart"]["rows"]
        self.assertEqual([row["username"] for row in picked if row["active"]], [SYSTEM_USERNAME])
        manager_rows = self.get(self.lead1).context["people_chart"]["rows"]
        self.assertNotIn(SYSTEM_USERNAME, {row["username"] for row in manager_rows})

    def test_a_team_filter_leaves_system_out(self):
        response = self.get(self.admin, {"team": self.lead1.pk})
        self.assertEqual([e for e in response.context["events"] if e.is_system], [])
        self.assertNotIn(SYSTEM_USERNAME, {row["username"] for row in response.context["people_chart"]["rows"]})

    def test_a_deleted_accounts_events_are_not_called_system(self):
        gone = User.objects.create(username="gone_user")
        record_activity(gone, "RUN", "x.run", "Ran something")
        gone.delete()
        response = self.get(self.admin, {"q": "gone_user"})
        (event,) = response.context["events"]
        self.assertFalse(event.is_system)
        self.assertContains(response, "account removed")

    def test_the_search_box_finds_a_source_path(self):
        found = self.get(self.admin, {"q": "policy-lock-checker"})
        self.assertEqual([e.pk for e in found.context["events"]], [self.api_event.pk])
        self.assertEqual(self.get(self.lead1, {"q": "motor-payout-rates"}).context["total"], 1)

    def test_the_export_has_a_source_path_column_and_the_system_rows_for_an_admin(self):
        wb = load_workbook(io.BytesIO(self.get(self.admin, name="export_team_activity_xlsx").content))
        rows = list(wb["Team Activity"].iter_rows(values_only=True))
        header = list(rows[0])
        self.assertEqual(header[8:], ["Source path", "Details"])
        system = [row for row in rows[1:] if row[1] == SYSTEM_USERNAME]
        self.assertEqual({(row[2], row[4], row[8]) for row in system},
                         {("System", "policy_lock.checked", "/api/v1/policy-lock-checker/"),
                          ("System", "health.eligibility.checked", None)})
        a1 = next(row for row in rows[1:] if row[1] == "a1" and row[4] == "motor.eligibility.checked")
        self.assertEqual(a1[8], "/motor-payout-rates/")
        pivot = list(wb["Pivot by category"].iter_rows(values_only=True))
        self.assertIn(("System", SYSTEM_USERNAME), {(row[0], row[1]) for row in pivot if row and row[0]})

    def test_a_source_path_that_would_run_as_a_formula_is_defused_in_the_export(self):
        record_system_activity("TEST", "x.test", "Ran", source="=HYPERLINK(\"http://example.test\")")
        wb = load_workbook(io.BytesIO(self.get(self.admin, name="export_team_activity_xlsx").content))
        rows = list(wb["Team Activity"].iter_rows(values_only=True))
        self.assertIn('\'=HYPERLINK("http://example.test")', {row[8] for row in rows[1:]})
