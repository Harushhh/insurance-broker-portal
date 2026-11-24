"""Team Activity, the exact-parameters layer: what an event's `details` hold and how they read back, the mirror that put
the Security Audit page's entries on the feed, the one-time copy of what the audit log already held, and the page,
the Action filter and the Excel file that show them.

(Month-aligned retention is tested with the retention job, in test_team_activity.RetentionTests; what each page records is
tested beside the page's hook, in test_activity_hooks.)

The rule these tests keep: `summary` and `extra` stay free of personal data, `details` holds the exact values (a vehicle
number, a policy-holder name, an old and a new rate), and everything is shown only inside the manager scope.
"""
import datetime
import importlib
import io
from decimal import Decimal
from unittest import mock
from urllib.parse import parse_qs

from django import forms
from django.apps import apps as django_apps
from django.contrib.auth.models import AnonymousUser, Group, User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from openpyxl import load_workbook

from insurance.models import ActivityEvent, AuditLog, FuelTypeMaster, ProductMaster, RateGroup, RateMaster
from insurance.services.activity import (
    LISTED_ITEMS, LISTED_ROWS, MAX_DETAILS_CHARS, STATUS, SYSTEM_USERNAME, audit_change, detail_rows, form_changes,
    mirror_audit_entry, plain, previous_values, record_activity, retention_cutoff, snapshot, snapshot_changes,
)
from insurance.services.points_audit import search_parameters
from insurance.test_team_activity import PageTestMixin
from insurance.views.team_activity import EXCEL_CELL_LIMIT


# -------------------------
# Writing details
# -------------------------
class RecordDetailsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username="rita", first_name="Rita", last_name="Shah")

    def test_the_exact_parameters_are_stored_beside_a_sentence_that_does_not_carry_them(self):
        event = record_activity(
            self.user, "TEST", "motor.eligibility.checked", "Ran a Motor eligibility check",
            details={"page": "Motor Payout Rates",
                     "parameters": {"vehicle_no": "MH12AB1234", "policy_holder_name": "Ravi Kumar"}},
        )
        event.refresh_from_db()
        self.assertEqual(event.details["parameters"], {"vehicle_no": "MH12AB1234", "policy_holder_name": "Ravi Kumar"})
        self.assertNotIn("MH12AB1234", event.summary)
        self.assertEqual(event.extra, {})

    def test_details_are_optional(self):
        self.assertEqual(record_activity(self.user, "RUN", "x.run", "Ran").details, {})

    def test_dates_and_decimals_are_stored_as_text_instead_of_failing(self):
        event = record_activity(self.user, "EDIT", "x.edit", "Edited",
                                details={"on": datetime.date(2026, 11, 24), "rate": Decimal("12.50")})
        self.assertEqual(event.details, {"on": "2026-11-24", "rate": "12.50"})

    def test_a_huge_details_is_replaced_by_a_note_instead_of_filling_the_table(self):
        event = record_activity(self.user, "EDIT", "x.edit", "Bulk", details={"ids": ["x" * 100] * 3000})
        self.assertEqual(set(event.details), {"too_large", "characters"})
        self.assertTrue(event.details["too_large"])
        self.assertGreater(event.details["characters"], MAX_DETAILS_CHARS)

    def test_an_audit_row_can_be_copied_onto_the_feed_only_once(self):
        record_activity(self.user, "EDIT", "A", "x", legacy_audit_id=7)
        with self.assertLogs("insurance.services.activity", level="ERROR"):
            self.assertIsNone(record_activity(self.user, "EDIT", "A", "x", legacy_audit_id=7))
        self.assertEqual(ActivityEvent.objects.filter(legacy_audit_id=7).count(), 1)

    def test_events_with_no_audit_row_do_not_collide_with_each_other(self):
        for number in range(3):
            self.assertIsNotNone(record_activity(self.user, "RUN", "x.run", f"run {number}"))
        self.assertEqual(ActivityEvent.objects.filter(legacy_audit_id=None).count(), 3)


class SearchParametersTests(TestCase):
    def test_the_ids_the_search_form_posts_become_the_names_the_page_showed(self):
        product = ProductMaster.objects.create(name="Private Car")
        fuel = FuelTypeMaster.objects.create(name="Petrol")
        shown = search_parameters({"product": str(product.pk), "fuel": str(fuel.pk), "vehicle_no": "MH12AB1234", "cc": "1200"})
        self.assertEqual(shown, {"product": "Private Car", "fuel": "Petrol", "vehicle_no": "MH12AB1234", "cc": "1200"})

    def test_an_id_that_names_nothing_and_ordinary_text_are_kept_as_submitted(self):
        shown = search_parameters({"product": "999999", "sub_product": "3+3"})
        self.assertEqual(shown, {"product": "999999", "sub_product": "3+3"})


# -------------------------
# Turning values and forms into details
# -------------------------
class PlainTests(SimpleTestCase):
    def test_values_become_plain_json_friendly_data(self):
        value = {"a": (1, 2), "b": Decimal("1.5"), "c": datetime.date(2026, 11, 24), "d": None, "e": True, "f": {5}}
        self.assertEqual(plain(value), {"a": [1, 2], "b": "1.5", "c": "2026-11-24", "d": None, "e": True, "f": [5]})
        self.assertEqual(plain(datetime.datetime(2026, 11, 24, 10, 0)), "2026-11-24T10:00:00")

    def test_a_model_is_its_name_and_anything_else_is_text(self):
        self.assertEqual(plain(User(username="rita")), "rita")
        self.assertEqual(plain(complex(1, 2)), "(1+2j)")


class SnapshotTests(TestCase):
    def test_a_snapshot_holds_every_stored_field_with_a_foreign_key_as_its_id(self):
        user = User.objects.create(username="rita")
        entry = AuditLog.objects.create(user=user, action="X", details="y")
        shot = snapshot(entry)
        self.assertEqual((shot["user"], shot["action"], shot["details"]), (user.pk, "X", "y"))
        self.assertIn("timestamp", shot)

    def test_fields_can_be_left_out(self):
        self.assertIn("password", snapshot(User(username="rita")))
        self.assertNotIn("password", snapshot(User(username="rita"), exclude=("password",)))

    def test_snapshot_changes_lists_only_what_differs(self):
        self.assertEqual(
            snapshot_changes({"a": 1, "b": 2}, {"a": 1, "b": 3, "c": 4}),
            {"b": {"from": 2, "to": 3}, "c": {"from": None, "to": 4}},
        )


class FormChangesTests(TestCase):
    class UserForm(forms.ModelForm):
        class Meta:
            model = User
            fields = ["first_name", "email", "groups"]

    def test_a_form_reports_each_changed_field_from_and_to_with_choices_by_name(self):
        team = Group.objects.create(name="Team A")
        user = User.objects.create(username="rita", first_name="Old")
        form = self.UserForm({"first_name": "New", "email": "", "groups": [team.pk]}, instance=user)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form_changes(form), {
            "first_name": {"from": "Old", "to": "New"},
            "groups": {"from": [], "to": ["Team A"]},         # the group's name, not its id
        })

    def test_a_form_nobody_changed_reports_nothing(self):
        user = User.objects.create(username="rita", first_name="Same")
        form = self.UserForm({"first_name": "Same", "email": "", "groups": []}, instance=user)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form_changes(form), {})


class PreviousValuesTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username="rita")

    def test_it_counts_what_the_rows_hold_now_most_common_first(self):
        for action in ("A", "A", "A", "B"):
            AuditLog.objects.create(user=self.user, action=action, details="d")
        self.assertEqual(list(previous_values(AuditLog.objects.all(), "action").items()), [("A", 3), ("B", 1)])

    def test_a_foreign_key_shows_the_name_and_a_missing_value_reads_empty(self):
        AuditLog.objects.create(user=self.user, action="A", details="d")
        AuditLog.objects.create(user=self.user, action="A", details="d")
        AuditLog.objects.create(user=None, action="A", details="d")
        self.assertEqual(previous_values(AuditLog.objects.all(), "user"), {"rita": 2, "(empty)": 1})


# -------------------------
# Reading details back as rows
# -------------------------
class DetailRowsTests(SimpleTestCase):
    def test_nothing_gives_no_rows(self):
        self.assertEqual(detail_rows({}), [])
        self.assertEqual(detail_rows(None), [])

    def test_nested_values_read_outer_then_inner(self):
        rows = detail_rows({"page": "Motor Payout Rates", "parameters": {"vehicle_no": "MH12AB1234", "fuel": "Petrol"}})
        self.assertEqual(rows, [
            ("Page", "Motor Payout Rates"), ("Parameters › Vehicle no", "MH12AB1234"), ("Parameters › Fuel", "Petrol"),
        ])

    def test_a_from_to_pair_reads_old_arrow_new(self):
        rows = detail_rows({"changes": {"status": {"from": "ACTIVE", "to": "INACTIVE"}}})
        self.assertEqual(rows, [("Changes › Status", "ACTIVE → INACTIVE")])

    def test_empty_values_and_yes_no_are_spelled_out_and_zero_stays_zero(self):
        rows = dict(detail_rows({"a": "", "b": None, "c": True, "d": False, "e": 0}))
        self.assertEqual(rows, {"A": "(empty)", "B": "(empty)", "C": "Yes", "D": "No", "E": "0"})

    def test_an_empty_list_says_none(self):
        self.assertEqual(detail_rows({"ids": []}), [("Ids", "(none)")])

    def test_a_long_list_is_shortened_on_the_page_but_complete_in_the_export(self):
        everything = list(range(100))
        (label, text), = detail_rows({"ids": everything})
        self.assertEqual(label, "Ids")
        self.assertEqual(text, ", ".join(map(str, everything[:LISTED_ITEMS])) + f" … and {100 - LISTED_ITEMS} more")
        (_, full), = detail_rows({"ids": everything}, full=True)
        self.assertEqual(full, ", ".join(map(str, everything)))

    def test_a_list_of_records_gets_numbered_rows_and_a_count_of_the_rest(self):
        rows = detail_rows({"rows": [{"id": 1, "rate": 5}, {"id": 2, "rate": 6}]})
        self.assertEqual(rows, [("Rows › 1 › Id", "1"), ("Rows › 1 › Rate", "5"),
                                ("Rows › 2 › Id", "2"), ("Rows › 2 › Rate", "6")])
        many = [{"id": n} for n in range(LISTED_ITEMS + 5)]
        shortened = detail_rows({"rows": many})
        self.assertEqual(shortened[-1], ("Rows", "… and 5 more"))
        self.assertEqual(len(detail_rows({"rows": many}, full=True)), LISTED_ITEMS + 5)

    def test_a_very_long_details_is_cut_with_a_pointer_to_the_export(self):
        details = {f"k{n}": n for n in range(LISTED_ROWS + 100)}
        rows = detail_rows(details)
        self.assertEqual(len(rows), LISTED_ROWS + 1)
        self.assertEqual(rows[-1][0], "…")
        self.assertIn("100 more rows", rows[-1][1])
        self.assertEqual(len(detail_rows(details, full=True)), LISTED_ROWS + 100)


# -------------------------
# The audit-log mirror (what the Security Audit page used to be the only place for)
# -------------------------
class AuditMirrorTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username="rita", first_name="Rita", last_name="Shah")

    def test_an_audit_entry_appears_on_the_feed_as_an_edit_with_its_text_as_the_details(self):
        entry = AuditLog.objects.create(user=self.user, action="RATE UPDATE", details="Updated group 123: po 10 -> 12")
        event = ActivityEvent.objects.get()
        self.assertEqual((event.actor, event.category, event.action), (self.user, "EDIT", "RATE UPDATE"))
        self.assertEqual(event.summary, entry.details)
        self.assertEqual(event.details, {"entry": entry.details})
        self.assertEqual(event.legacy_audit_id, entry.pk)
        self.assertEqual(event.actor_name, "Rita Shah")

    def test_the_events_time_is_when_the_entry_was_written(self):
        entry = AuditLog.objects.create(user=self.user, action="X", details="d")
        self.assertAlmostEqual(
            ActivityEvent.objects.get().occurred_at.timestamp(), entry.timestamp.timestamp(), delta=5)

    def test_long_text_is_shortened_in_the_sentence_but_kept_whole_in_the_details(self):
        AuditLog.objects.create(user=self.user, action="a" * 90, details="x" * 500)
        event = ActivityEvent.objects.get()
        self.assertEqual(len(event.summary), 300)
        self.assertEqual(len(event.action), 60)
        self.assertEqual(event.details["entry"], "x" * 500)

    def test_the_points_searches_and_sso_tickets_are_not_copied_by_the_mirror(self):
        """The searches get their own Test events (with every parameter) from points_audit, and the sso_ticket rows are
        sign-in handoffs, not activity."""
        for user in (self.user, None):
            AuditLog.objects.create(user=user, action="MOTOR_POINTS_SEARCH", details="{'a': 1}")
            AuditLog.objects.create(user=user, action="HEALTH_POINTS_SEARCH", details="{'a': 1}")
            AuditLog.objects.create(user=user, action="sso_ticket_issued", details="partner")
            AuditLog.objects.create(user=user, action="SSO_TICKET_CONSUMED", details="partner")
        self.assertEqual(ActivityEvent.objects.count(), 0)

    def test_an_entry_with_no_user_is_copied_as_a_system_event(self):
        """Written by a management command, or after its writer's account was gone: nobody signed in, so System."""
        entry = AuditLog.objects.create(user=None, action="BULK DEDUPE", details="dedupe_rate_master: soft-deleted 12 rows")
        event = ActivityEvent.objects.get()
        self.assertTrue(event.is_system)
        self.assertEqual((event.actor, event.actor_username, event.actor_name), (None, SYSTEM_USERNAME, "System"))
        self.assertEqual((event.category, event.action, event.summary), ("EDIT", "BULK DEDUPE", entry.details))
        self.assertEqual(event.details, {"entry": entry.details})
        self.assertEqual(event.legacy_audit_id, entry.pk)

    def test_saving_an_entry_again_does_not_copy_it_twice(self):
        entry = AuditLog.objects.create(user=self.user, action="X", details="one")
        entry.details = "edited afterwards"
        entry.save()
        self.assertEqual(ActivityEvent.objects.count(), 1)
        self.assertIsNone(mirror_audit_entry(entry))           # asking again is harmless too
        self.assertEqual(ActivityEvent.objects.count(), 1)

    def test_a_failure_while_copying_never_breaks_the_audit_write(self):
        with mock.patch("insurance.services.activity.record_activity", side_effect=RuntimeError("boom")):
            with self.assertLogs("insurance.services.activity", level="ERROR"):
                entry = AuditLog.objects.create(user=self.user, action="X", details="one")
        self.assertTrue(AuditLog.objects.filter(pk=entry.pk).exists())
        self.assertEqual(ActivityEvent.objects.count(), 0)

    def test_an_entry_saved_without_signals_can_still_be_copied_by_hand(self):
        (entry,) = AuditLog.objects.bulk_create([AuditLog(user=self.user, action="X", details="bulk")])
        self.assertEqual(ActivityEvent.objects.count(), 0)         # bulk_create sends no signal
        self.assertIsNotNone(mirror_audit_entry(entry))
        self.assertEqual(ActivityEvent.objects.get().legacy_audit_id, entry.pk)


class AuditChangeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username="rita", first_name="Rita", last_name="Shah")

    def test_it_writes_the_audit_entry_and_exactly_one_event_that_shares_its_text(self):
        entry = audit_change(
            self.user, "RATE UPDATE", "Updated group 5", target=self.user,
            details={"changes": {"po_net_rate": {"from": 1, "to": 2}}},
        )
        self.assertEqual(AuditLog.objects.get(), entry)
        event = ActivityEvent.objects.get()                       # one, not one from here and one from the mirror
        self.assertEqual((event.category, event.action, event.summary), ("EDIT", "RATE UPDATE", "Updated group 5"))
        self.assertEqual(event.legacy_audit_id, entry.pk)
        self.assertEqual(event.details, {"entry": "Updated group 5", "changes": {"po_net_rate": {"from": 1, "to": 2}}})
        self.assertEqual((event.target_type, event.target_id), ("user", str(self.user.pk)))

    def test_how_the_event_reads_can_differ_from_the_audit_sentence(self):
        audit_change(
            self.user, "LIFE GRID UPLOAD", "Deleted an earlier upload 'Oct'26'", category=STATUS,
            code="life.grid.deleted", summary="Deleted the Life grid 'Oct'26'",
        )
        event = ActivityEvent.objects.get()
        self.assertEqual((event.category, event.action, event.summary), ("STATUS", "life.grid.deleted", "Deleted the Life grid 'Oct'26'"))
        self.assertEqual(AuditLog.objects.get().action, "LIFE GRID UPLOAD")        # the audit entry keeps its own name
        self.assertEqual(event.details["entry"], "Deleted an earlier upload 'Oct'26'")

    def test_with_no_real_user_the_audit_entry_is_still_written_and_no_event(self):
        entry = audit_change(AnonymousUser(), "X", "nobody did this")
        self.assertEqual(AuditLog.objects.get(), entry)
        self.assertIsNone(entry.user)
        self.assertEqual(ActivityEvent.objects.count(), 0)


# -------------------------
# The one-time copy of what the audit log held when this shipped
# -------------------------
class BackfillTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username="rita", first_name="Rita", last_name="Shah")
        self.migration = importlib.import_module("insurance.migrations.0063_backfill_audit_log_into_activity")
        self.cutoff = retention_cutoff()

    def old_entry(self, when, **fields):
        """An audit entry as it was before the mirror existed: written at `when`, with no event on the feed."""
        fields.setdefault("user", self.user)
        fields.setdefault("action", "RATE UPDATE")
        fields.setdefault("details", "did it")
        entry = AuditLog.objects.create(**fields)
        AuditLog.objects.filter(pk=entry.pk).update(timestamp=when)
        ActivityEvent.objects.all().delete()
        return entry

    def copy(self):
        self.migration.copy_audit_log_into_team_activity(django_apps, None)

    def test_entries_inside_the_retention_window_are_copied_as_edit_events(self):
        inside = self.old_entry(self.cutoff + datetime.timedelta(hours=1))
        self.old_entry(self.cutoff - datetime.timedelta(hours=1))            # last month's neighbour, one month too old
        self.copy()
        event = ActivityEvent.objects.get()
        self.assertEqual((event.actor, event.category, event.action, event.summary),
                         (self.user, "EDIT", "RATE UPDATE", "did it"))
        self.assertEqual(event.details, {"entry": "did it"})
        self.assertEqual(event.legacy_audit_id, inside.pk)
        self.assertEqual(event.occurred_at, AuditLog.objects.get(pk=inside.pk).timestamp)
        self.assertEqual(event.actor_name, "Rita Shah")

    def test_entries_with_no_person_points_searches_and_sso_tickets_are_not_copied(self):
        when = self.cutoff + datetime.timedelta(days=3)
        self.old_entry(when, user=None)
        self.old_entry(when, action="MOTOR_POINTS_SEARCH")
        self.old_entry(when, action="HEALTH_POINTS_SEARCH")
        self.old_entry(when, action="sso_ticket_issued")
        self.copy()
        self.assertEqual(ActivityEvent.objects.count(), 0)

    def test_running_it_twice_copies_each_entry_once(self):
        when = self.cutoff + datetime.timedelta(days=3)
        self.old_entry(when, details="first")
        self.old_entry(when, details="second")
        self.copy()
        self.copy()
        self.assertEqual(sorted(ActivityEvent.objects.values_list("summary", flat=True)), ["first", "second"])

    def test_an_entry_the_mirror_already_copied_is_left_alone(self):
        AuditLog.objects.create(user=self.user, action="X", details="written after the mirror existed")
        self.assertEqual(ActivityEvent.objects.count(), 1)
        self.copy()
        self.assertEqual(ActivityEvent.objects.count(), 1)

    def test_text_longer_than_the_columns_is_cut_where_it_has_to_be_and_kept_whole_in_the_details(self):
        self.old_entry(self.cutoff + datetime.timedelta(days=3), action="a" * 90, details="x" * 500)
        self.copy()
        event = ActivityEvent.objects.get()
        self.assertEqual((len(event.action), len(event.summary)), (60, 300))
        self.assertEqual(event.details["entry"], "x" * 500)


# -------------------------
# The page
# -------------------------
class DetailsOnThePageTests(PageTestMixin, TestCase):
    def setUp(self):
        self.build_page_org()
        record_activity(
            self.a1, "TEST", "motor.eligibility.checked", "Ran a Motor eligibility check on Motor Payout Rates",
            details={"page": "Motor Payout Rates", "parameters": {"vehicle_no": "MH12AB1234", "fuel": "Petrol"}},
        )
        record_activity(
            self.x1, "TEST", "other.team.check", "Ran another team's check",
            details={"parameters": {"vehicle_no": "KA01ZZ9999"}},
        )

    @staticmethod
    def workbook_text(response):
        wb = load_workbook(io.BytesIO(response.content))
        return {str(value) for ws in wb for row in ws.iter_rows(values_only=True) for value in row if value is not None}

    def test_a_manager_sees_the_exact_parameters_of_their_teams_events(self):
        response = self.get(self.lead1)
        self.assertContains(response, "MH12AB1234")
        self.assertContains(response, "Parameters › Vehicle no")
        self.assertContains(response, "(3 items)")                    # page, vehicle no and fuel
        event = next(e for e in response.context["events"] if e.action == "motor.eligibility.checked")
        self.assertEqual(dict(event.detail_rows),
                         {"Page": "Motor Payout Rates", "Parameters › Vehicle no": "MH12AB1234", "Parameters › Fuel": "Petrol"})

    def test_another_teams_parameters_never_reach_a_manager(self):
        for label, params in (("the feed", {}), ("a forged person", {"person": self.x1.pk}),
                              ("a forged team", {"team": self.other_boss.pk}),
                              ("a guessed action name", {"action": "other.team.check"})):
            with self.subTest(label):
                response = self.get(self.lead1, params)
                self.assertNotContains(response, "KA01ZZ9999")
                self.assertEqual([e for e in response.context["events"] if e.actor_username == "x1"], [])
        # The Action dropdown only lists what happened inside the manager's own scope.
        self.assertNotIn("other.team.check", self.get(self.lead1).context["action_choices"])
        export = self.get(self.lead1, name="export_team_activity_xlsx")
        self.assertFalse(any("KA01ZZ9999" in text for text in self.workbook_text(export)))

    def test_an_admin_sees_every_teams_parameters(self):
        admin = self.make_admin()
        self.assertContains(self.get(admin), "KA01ZZ9999")
        self.assertTrue(any("KA01ZZ9999" in text for text in self.workbook_text(
            self.get(admin, name="export_team_activity_xlsx"))))

    def test_details_are_shown_escaped(self):
        record_activity(self.a1, "EDIT", "x.edit", "Edited", details={"note": "<script>alert(1)</script>"})
        response = self.get(self.lead1)
        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertContains(response, "&lt;script&gt;alert(1)&lt;/script&gt;")

    def test_an_event_with_no_details_has_no_details_block(self):
        response = self.get(self.lead1, {"person": self.a2.pk})        # a2 only has the plain event from build_org
        self.assertEqual(response.context["total"], 1)
        self.assertNotContains(response, 'class="ta-details"')

    def test_a_long_list_says_how_many_more_the_export_has(self):
        record_activity(self.a1, "EDIT", "rate.bulk_updated", "Bulk update", details={"group_ids": list(range(1, 101))})
        self.assertContains(self.get(self.lead1), f"… and {100 - LISTED_ITEMS} more")

    def test_the_search_box_also_finds_a_value_inside_the_details_within_the_scope(self):
        found = self.get(self.lead1, {"q": "mh12ab1234"})               # any case
        self.assertEqual([e.actor_username for e in found.context["events"]], ["a1"])
        self.assertEqual(self.get(self.lead1, {"q": "KA01ZZ9999"}).context["total"], 0)      # another team's value
        self.assertEqual([e.actor_username for e in self.get(self.make_admin(), {"q": "KA01ZZ9999"}).context["events"]],
                         ["x1"])

    def test_the_page_says_which_months_are_kept_and_the_date_box_cannot_go_further_back(self):
        first = retention_cutoff().date()
        response = self.get(self.lead1)
        self.assertEqual(response.context["retention_from"], first)
        self.assertContains(response, f"everything since {first.day} {first:%b} {first.year}")
        self.assertContains(response, f'data-min="{first.isoformat()}"')
        self.assertNotContains(response, "90 days")


class ActionFilterTests(PageTestMixin, TestCase):
    def setUp(self):
        self.build_page_org()
        record_activity(self.a1, "UPLOAD", "grid.uploaded", "Uploaded a grid")
        record_activity(self.a2, "STATUS", "grid.status.changed", "Changed a grid")
        record_activity(self.x1, "UPLOAD", "other.team.action", "Another team's upload")

    def test_the_action_narrows_the_feed_the_charts_and_the_export_link(self):
        response = self.get(self.lead1, {"action": "grid.uploaded"})
        self.assertEqual([e.action for e in response.context["events"]], ["grid.uploaded"])
        self.assertEqual(response.context["total"], 1)
        self.assertEqual(response.context["category_chart"]["total"], 1)
        self.assertEqual([row["username"] for row in response.context["people_chart"]["rows"]], ["a1"])
        self.assertIn("action=grid.uploaded", response.context["export_query"])
        self.assertContains(response, 'value="grid.uploaded" selected')

    def test_the_chart_links_keep_the_action(self):
        response = self.get(self.lead1, {"action": "grid.uploaded"})
        donut = response.context["category_chart"]
        self.assertEqual(parse_qs(donut["slices"][0]["url"][1:])["action"], ["grid.uploaded"])
        self.assertEqual(parse_qs(response.context["people_chart"]["rows"][0]["url"][1:])["action"], ["grid.uploaded"])

    def test_the_dropdown_lists_only_actions_inside_the_managers_scope_and_period(self):
        self.assertEqual(self.get(self.lead1).context["action_choices"],
                         ["grid.status.changed", "grid.uploaded", "test.run"])
        old = ActivityEvent.objects.get(actor=self.a1, action="grid.uploaded")
        self.backdate(old, days=30)
        self.assertNotIn("grid.uploaded", self.get(self.lead1).context["action_choices"])
        self.assertIn("grid.uploaded", self.get(self.lead1, {"date_range": ""}).context["action_choices"])

    def test_an_action_nobody_in_scope_did_shows_nothing_and_does_not_leak(self):
        response = self.get(self.lead1, {"action": "other.team.action"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total"], 0)
        self.assertNotContains(response, "Another team's upload")

    def test_the_export_applies_the_action_and_says_so(self):
        response = self.get(self.lead1, {"action": "grid.uploaded"}, name="export_team_activity_xlsx")
        wb = load_workbook(io.BytesIO(response.content))
        events = list(wb["Team Activity"].iter_rows(values_only=True))
        self.assertEqual([row[4] for row in events[1:]], ["grid.uploaded"])
        notes = [str(row[0]) for ws in (wb["Pivot by category"], wb["Pivot by day"])
                 for row in ws.iter_rows(values_only=True) if row[0]]
        self.assertTrue(any("Action grid.uploaded" in note for note in notes))
        pivot = list(wb["Pivot by category"].iter_rows(values_only=True))
        header = next(i for i, row in enumerate(pivot) if row and row[0] == "Person")
        self.assertEqual({row[1] for row in pivot[header + 1:-1]}, {"a1"})       # no zero rows for quiet teammates


class EditEventsOnThePageTests(PageTestMixin, TestCase):
    def setUp(self):
        self.build_page_org()
        record_activity(self.a1, "EDIT", "rate.updated", "Updated a rate group", details={"group": 5})

    def test_edit_events_get_their_own_slice_and_can_be_picked(self):
        response = self.get(self.lead1)
        legend = response.context["category_chart"]["legend"]
        self.assertEqual([(item["code"], item["n"]) for item in legend],
                         [("RUN", 3), ("UPLOAD", 0), ("TEST", 0), ("STATUS", 0), ("EDIT", 1)])
        edits = self.get(self.lead1, {"category": "EDIT"})
        self.assertEqual([e.action for e in edits.context["events"]], ["rate.updated"])
        self.assertContains(edits, "ta-cat-edit")

    def test_a_period_with_no_edits_keeps_the_four_category_legend(self):
        ActivityEvent.objects.filter(category="EDIT").delete()
        legend = self.get(self.lead1).context["category_chart"]["legend"]
        self.assertEqual([item["code"] for item in legend], ["RUN", "UPLOAD", "TEST", "STATUS"])

    def test_the_category_pivot_gets_an_edit_column_only_when_something_was_edited(self):
        def header(response):
            ws = load_workbook(io.BytesIO(response.content))["Pivot by category"]
            return next(row for row in ws.iter_rows(values_only=True) if row and row[0] == "Person")

        with_edits = header(self.get(self.lead1, name="export_team_activity_xlsx"))
        self.assertEqual(with_edits[3:8], ("Run", "Upload", "Test", "Status", "Edit"))
        ActivityEvent.objects.filter(category="EDIT").delete()
        without = header(self.get(self.lead1, name="export_team_activity_xlsx"))
        self.assertNotIn("Edit", without)


# -------------------------
# The Excel file
# -------------------------
class ExportDetailsTests(PageTestMixin, TestCase):
    def setUp(self):
        self.build_page_org()

    def event_rows(self, params=None):
        response = self.get(self.lead1, params, name="export_team_activity_xlsx")
        return list(load_workbook(io.BytesIO(response.content))["Team Activity"].iter_rows(values_only=True))

    def details_cell(self, action, params=None):
        rows = self.event_rows(params)
        self.assertEqual(rows[0][9], "Details")
        return next(row for row in rows[1:] if row[4] == action)[9]

    def test_every_parameter_is_one_line_with_nested_names_old_to_new_and_the_whole_list(self):
        record_activity(
            self.a1, "EDIT", "rate.bulk_updated", "Bulk update",
            details={"entry": "Updated 60 rows", "group_ids": list(range(1, 61)),
                     "changes": {"po_net_rate": {"from": "10", "to": "12"}}},
        )
        lines = set(self.details_cell("rate.bulk_updated").split("\n"))
        self.assertEqual(lines, {
            "Entry: Updated 60 rows",
            "Group ids: " + ", ".join(map(str, range(1, 61))),             # every id: the page shortens it, the file does not
            "Changes › Po net rate: 10 → 12",
        })

    def test_an_event_with_no_details_has_an_empty_cell(self):
        self.assertIn(self.details_cell("test.run"), ("", None))

    def test_the_events_sheet_opens_readable_wrapped_details_a_frozen_header_and_wide_columns(self):
        record_activity(self.a1, "EDIT", "x.edit", "Edited", details={"one": 1, "two": 2})
        response = self.get(self.lead1, name="export_team_activity_xlsx")
        ws = load_workbook(io.BytesIO(response.content))["Team Activity"]
        self.assertEqual(ws.freeze_panes, "A2")
        self.assertTrue(ws["J2"].alignment.wrap_text)
        self.assertTrue(ws["F2"].alignment.wrap_text)
        self.assertGreaterEqual(ws.column_dimensions["J"].width, 60)
        self.assertTrue(ws["A1"].font.bold)

    def test_text_that_would_run_as_a_formula_is_defused(self):
        record_activity(self.a1, "EDIT", "x.edit", "Edited", details={"=cmd": "x"})
        self.assertEqual(self.details_cell("x.edit"), "'=cmd: x")

    def test_a_details_over_the_excel_cell_limit_is_cut_with_a_note(self):
        record_activity(self.a1, "EDIT", "x.edit", "Edited", details={"blob": "x" * (EXCEL_CELL_LIMIT + 5000)})
        cell = self.details_cell("x.edit")
        self.assertLessEqual(len(cell), 32767)
        self.assertIn("cut here", cell)


# -------------------------
# Rate Master edits: the single edit form and the bulk update
# -------------------------
class RateMasterEditEventTests(TestCase):
    def setUp(self):
        Group.objects.get_or_create(name="Can_View_Dashboard")
        self.user = User.objects.create_user(username="ops", password="a-strong-test-password-1")
        self.user.groups.add(Group.objects.get(name="Can_View_Dashboard"))
        self.client.force_login(self.user)
        self.product = ProductMaster.objects.create(name="Private Car")
        self.group = RateGroup.objects.create(key_hash="edit-events-group")
        self.rows = [
            RateMaster.objects.create(group=self.group, product=self.product, insurance_company="Acme General",
                                      status="ACTIVE", is_deleted="NO", pi_od_rate=10.0, po_od_rate=3.0)
            for _ in range(2)
        ]

    def test_a_bulk_update_records_the_field_the_old_values_the_new_value_and_every_row(self):
        self.client.post(reverse("bulk_update_rates"), {
            "selected_groups": str(self.group.id), "update_field": "pi_od_rate", "update_value": "12.34567"})
        entry = AuditLog.objects.get(action="BULK UPDATE")
        event = ActivityEvent.objects.get(action="BULK UPDATE")
        self.assertEqual((event.actor, event.category, event.legacy_audit_id), (self.user, "EDIT", entry.pk))
        self.assertEqual(event.summary, "Bulk update on Rate Master: 2 row(s), 'pi_od_rate' set to '12.34567'")
        details = event.details
        self.assertEqual(details["field"], "pi_od_rate")
        self.assertEqual((details["new_value_submitted"], details["new_value_applied"]), ("12.34567", 12.35))
        self.assertEqual(details["rows_updated"], 2)
        self.assertEqual(details["groups_selected"], [self.group.id])
        self.assertEqual(details["values_before"], {"10.0": 2})               # what the two rows held before
        self.assertEqual(sorted(details["record_ids"]), sorted(row.id for row in self.rows))
        self.assertEqual(details["entry"], entry.details)
        self.assertEqual(ActivityEvent.objects.filter(legacy_audit_id=entry.pk).count(), 1)     # once, not twice

    def test_a_refused_bulk_update_records_nothing(self):
        self.client.post(reverse("bulk_update_rates"), {
            "selected_groups": str(self.group.id), "update_field": "id", "update_value": "1"})
        self.assertEqual(ActivityEvent.objects.count(), 0)

    def test_the_edit_form_records_each_field_that_changed_from_and_to(self):
        self.client.post(reverse("edit_rate", args=[self.group.id]), {
            "insurance_company": "Acme General", "status": "ACTIVE", "is_deleted": "NO",
            "product": str(self.product.id), "pi_od_rate": "11", "po_od_rate": "3"})
        event = ActivityEvent.objects.get(action="MANUAL EDIT")
        self.assertEqual((event.actor, event.category), (self.user, "EDIT"))
        self.assertEqual(event.details["changes"], {"pi_od_rate": {"from": 10.0, "to": 11.0}})
        self.assertEqual(event.details["rows_updated"], 2)
        self.assertEqual(sorted(event.details["record_ids"]), sorted(row.id for row in self.rows))
        self.assertEqual(str(self.group.id), str(event.details["group_or_record_id"]))


# -------------------------
# What must never be written down
# -------------------------
class UserManagementDetailsTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create(username="hr_admin", is_staff=True)
        self.client.force_login(self.admin)
        self.person = User.objects.create(username="person")

    def everything_recorded(self):
        return str(list(ActivityEvent.objects.values("summary", "extra", "details", "action")))

    @staticmethod
    def fixed_password(value):
        """The view's `secrets` module replaced by one that hands out `value` (nothing else in the process is touched)."""
        return mock.patch("insurance.views.user_management.secrets", mock.Mock(token_urlsafe=lambda size: value))

    def test_a_new_users_one_time_password_is_never_recorded(self):
        with self.fixed_password("ONE-TIME-SECRET-1"):
            self.client.post(reverse("user_management"), {
                "action": "create_user", "new_username": "newbie", "full_name": "New Bie", "email": "newbie@example.test"})
        event = ActivityEvent.objects.get(action="user.created")
        self.assertEqual(event.details, {"username": "newbie", "full_name": "New Bie",
                                         "email": "newbie@example.test", "contact_number": ""})
        self.assertNotIn("ONE-TIME-SECRET-1", self.everything_recorded())

    def test_a_password_reset_records_who_was_reset_and_never_the_password(self):
        with self.fixed_password("RESET-SECRET-2"):
            self.client.post(reverse("user_management"), {"action": "reset_password", "user_id": self.person.id})
        event = ActivityEvent.objects.get(action="user.password_reset")
        self.assertEqual(event.details, {"user": "person"})
        self.assertNotIn("RESET-SECRET-2", self.everything_recorded())

    def test_pan_and_bank_details_are_logged_as_changed_but_never_with_their_values(self):
        self.client.post(reverse("user_management"), {
            "action": "update_user_access", "user_id": self.person.id, "email": "", "contact_number": "",
            "designation": "Analyst", "pan": "ABCDE1234F", "bank_account": "123456789012", "ifsc": "HDFC0001234"})
        event = ActivityEvent.objects.get(action="user.access.updated")
        changes = event.details["changes"]
        self.assertEqual(changes["designation"]["to"], "Analyst")           # an ordinary field keeps its values
        for name in ("pan", "bank_account", "ifsc"):
            self.assertEqual(changes[name], {"from": "(value not logged)", "to": "(changed; value not logged)"}, name)
        recorded = self.everything_recorded()
        for secret in ("ABCDE1234F", "123456789012", "HDFC0001234"):
            self.assertNotIn(secret, recorded)
