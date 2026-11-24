"""Team Activity: who may read whose events (the hierarchy scope), the event writer, the Reports To rules the
scope stands on, and the page, its Excel export and the retention job.

The tests that matter most are the leak tests: a manager must never see another team's work, however the
request is phrased (a forged ?person= or ?team=, the export, a longer chain, another team under the same boss).
"""
import datetime
import io
import math
import pathlib
import re
from io import StringIO
from unittest import mock
from urllib.parse import parse_qs

from django.conf import settings
from django.contrib.auth.models import AnonymousUser, Group, User
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import connection, transaction
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook

from insurance import tasks
from insurance.models import ActivityEvent, AuditLog, UserProfile
from insurance.services.activity import record_activity, retention_cutoff
from insurance.services.team_scope import (
    is_admin, managers_with_teams, team_user_ids, visible_events, visible_people,
)
from insurance.views.team_activity import DONUT_GAP, DONUT_RADIUS, _donut_slices, _percent_text, _safe_cell


def make_user(username, manager=None, **fields):
    """A user (no password hashing: these tests never log in with one) whose profile reports to `manager`."""
    user = User.objects.create(username=username, **fields)
    UserProfile.objects.create(user=user, reports_to=manager.profile if manager else None)
    return user


def usernames(queryset_of_events):
    return set(queryset_of_events.values_list("actor_username", flat=True))


class OrgMixin:
    """
        boss
        |- lead1
        |   |- a1
        |   |   `- junior          (three levels under boss)
        |   `- a2
        `- lead2
            `- b1

        other_boss
        `- x1                      (another team entirely)

        loner                      (an account with no profile)
    """

    def build_org(self):
        self.boss = make_user("boss")
        self.lead1 = make_user("lead1", self.boss)
        self.lead2 = make_user("lead2", self.boss)
        self.a1 = make_user("a1", self.lead1)
        self.a2 = make_user("a2", self.lead1)
        self.junior = make_user("junior", self.a1)
        self.b1 = make_user("b1", self.lead2)
        self.other_boss = make_user("other_boss")
        self.x1 = make_user("x1", self.other_boss)
        self.loner = User.objects.create(username="loner")
        self.everyone = [self.boss, self.lead1, self.lead2, self.a1, self.a2, self.junior,
                         self.b1, self.other_boss, self.x1, self.loner]
        for user in self.everyone:
            record_activity(user, "RUN", "test.run", f"{user.username} ran something")

    def make_admin(self, username="the_admin"):
        admin = User.objects.create(username=username)
        admin.groups.add(Group.objects.get_or_create(name="ADMIN")[0])
        return admin


class TeamScopeTests(OrgMixin, TestCase):
    def setUp(self):
        self.build_org()

    def ids(self, *users):
        return {u.pk for u in users}

    def test_a_manager_gets_the_whole_subtree_but_not_themselves(self):
        self.assertEqual(
            team_user_ids(self.boss),
            self.ids(self.lead1, self.lead2, self.a1, self.a2, self.junior, self.b1),
        )

    def test_a_team_lead_gets_only_their_own_side(self):
        self.assertEqual(team_user_ids(self.lead1), self.ids(self.a1, self.a2, self.junior))
        self.assertEqual(team_user_ids(self.a1), self.ids(self.junior))

    def test_a_person_with_nobody_under_them_has_an_empty_team(self):
        for user in (self.junior, self.a2, self.b1, self.x1):
            with self.subTest(user=user.username):
                self.assertEqual(team_user_ids(user), set())

    def test_an_account_with_no_profile_has_an_empty_team(self):
        self.assertEqual(team_user_ids(self.loner), set())

    def test_no_user_at_all_has_an_empty_team(self):
        self.assertEqual(team_user_ids(None), set())
        self.assertEqual(team_user_ids(AnonymousUser()), set())

    def test_a_loop_in_the_data_neither_hangs_the_walk_nor_puts_the_viewer_in_their_own_team(self):
        # Written straight to the table: the model check would refuse this, which is the point of the
        # test (old or hand-edited data must not be able to break the page).
        UserProfile.objects.filter(pk=self.boss.profile.pk).update(reports_to=self.junior.profile)
        team = team_user_ids(self.boss)
        self.assertNotIn(self.boss.pk, team)
        self.assertEqual(team, self.ids(self.lead1, self.lead2, self.a1, self.a2, self.junior, self.b1))

    def test_a_self_link_in_the_data_gives_an_empty_team(self):
        UserProfile.objects.filter(pk=self.a2.profile.pk).update(reports_to=self.a2.profile)
        self.assertEqual(team_user_ids(self.a2), set())


class VisibleEventsLeakTests(OrgMixin, TestCase):
    """The row rule itself. If any of these fail, a manager can read work that is not their team's."""

    def setUp(self):
        self.build_org()

    def test_a_manager_sees_the_events_of_the_whole_subtree_and_nobody_else(self):
        self.assertEqual(
            usernames(visible_events(self.boss)),
            {"lead1", "lead2", "a1", "a2", "junior", "b1"},
        )

    def test_a_manager_never_sees_their_own_events_or_their_bosses(self):
        seen = usernames(visible_events(self.lead1))
        self.assertEqual(seen, {"a1", "a2", "junior"})
        self.assertNotIn("lead1", seen)
        self.assertNotIn("boss", seen)

    def test_a_team_lead_never_sees_the_other_team_under_the_same_boss(self):
        seen = usernames(visible_events(self.lead1))
        self.assertNotIn("lead2", seen)
        self.assertNotIn("b1", seen)

    def test_a_manager_never_sees_an_unrelated_team(self):
        for viewer in (self.boss, self.lead1, self.lead2):
            with self.subTest(viewer=viewer.username):
                seen = usernames(visible_events(viewer))
                self.assertFalse({"other_boss", "x1", "loner"} & seen)

    def test_the_other_team_sees_only_itself_and_nothing_of_the_first(self):
        self.assertEqual(usernames(visible_events(self.other_boss)), {"x1"})

    def test_a_person_with_no_team_sees_nothing(self):
        for viewer in (self.junior, self.a2, self.b1, self.x1, self.loner):
            with self.subTest(viewer=viewer.username):
                self.assertFalse(visible_events(viewer).exists())

    def test_anonymous_and_missing_users_see_nothing(self):
        self.assertFalse(visible_events(AnonymousUser()).exists())
        self.assertFalse(visible_events(None).exists())

    def test_admin_sees_every_team(self):
        admin = self.make_admin()
        self.assertEqual(visible_events(admin).count(), len(self.everyone))

    def test_staff_or_superuser_without_the_admin_group_is_scoped_like_anyone_else(self):
        # page_access_required gives is_staff / is_superuser no free pass either.
        staff = make_user("staffer", self.other_boss, is_staff=True, is_superuser=True)
        record_activity(staff, "RUN", "test.run", "staff ran something")
        self.assertFalse(is_admin(staff))
        self.assertEqual(usernames(visible_events(staff)), set())

    def test_a_forged_person_filter_outside_the_scope_returns_nothing(self):
        # The page narrows visible_events() by ?person=<id>; a manager forging another team's id must
        # get an empty list, not that person's events.
        for forged in (self.x1, self.other_boss, self.lead2, self.b1, self.boss):
            with self.subTest(forged=forged.username):
                self.assertFalse(visible_events(self.lead1).filter(actor_id=forged.pk).exists())
        self.assertTrue(visible_events(self.lead1).filter(actor_id=self.a1.pk).exists())

    def test_moving_a_person_to_another_team_moves_their_history_with_them(self):
        # Scope follows the current hierarchy (agreed default): history is not frozen to the old team.
        UserProfile.objects.filter(pk=self.b1.profile.pk).update(reports_to=self.lead1.profile)
        self.assertIn("b1", usernames(visible_events(self.lead1)))
        self.assertNotIn("b1", usernames(visible_events(self.lead2)))

    def test_events_of_a_deleted_account_fall_out_of_every_managers_view_but_stay_for_admin(self):
        self.a2.delete()
        self.assertNotIn("a2", usernames(visible_events(self.lead1)))
        orphan = visible_events(self.make_admin()).get(actor_username="a2")
        self.assertIsNone(orphan.actor)
        self.assertEqual(orphan.summary, "a2 ran something")


class VisiblePeopleTests(OrgMixin, TestCase):
    def setUp(self):
        self.build_org()

    def test_a_manager_is_offered_their_whole_subtree_including_quiet_people(self):
        ActivityEvent.objects.filter(actor=self.a2).delete()
        self.assertEqual(
            set(visible_people(self.lead1).values_list("username", flat=True)),
            {"a1", "a2", "junior"},
        )

    def test_an_admin_is_offered_everyone_who_has_events(self):
        self.assertEqual(
            set(visible_people(self.make_admin()).values_list("username", flat=True)),
            {u.username for u in self.everyone},
        )

    def test_anonymous_is_offered_nobody(self):
        self.assertFalse(visible_people(AnonymousUser()).exists())

    def test_the_team_filter_lists_only_users_who_have_someone_reporting_to_them(self):
        self.assertEqual(
            set(managers_with_teams().values_list("username", flat=True)),
            {"boss", "lead1", "lead2", "a1", "other_boss"},
        )


class RecordActivityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username="rita", first_name="Rita", last_name="Shah")
        self.target = User.objects.create(username="the_target")

    def test_it_stores_who_what_and_about_what(self):
        event = record_activity(
            self.user, "UPLOAD", "grid.uploaded", "Uploaded a grid for ACKO",
            target=self.target, extra={"rows": 12, "status": "PENDING"},
        )
        event.refresh_from_db()
        self.assertEqual(event.actor, self.user)
        self.assertEqual((event.actor_username, event.actor_name), ("rita", "Rita Shah"))
        self.assertEqual((event.category, event.action), ("UPLOAD", "grid.uploaded"))
        self.assertEqual(event.summary, "Uploaded a grid for ACKO")
        self.assertEqual((event.target_type, event.target_id), ("user", str(self.target.pk)))
        self.assertEqual(event.extra, {"rows": 12, "status": "PENDING"})
        self.assertIsNotNone(event.occurred_at)

    def test_target_and_extra_are_optional(self):
        event = record_activity(self.user, "TEST", "x.test", "Ran a check")
        self.assertEqual((event.target_type, event.target_id, event.extra), ("", "", {}))

    def test_values_that_are_not_json_are_turned_into_text_instead_of_failing(self):
        event = record_activity(self.user, "RUN", "x.run", "Ran", extra={"when": datetime.date(2026, 11, 23)})
        self.assertEqual(event.extra, {"when": "2026-11-23"})

    def test_over_long_text_is_cut_to_the_column_instead_of_failing(self):
        event = record_activity(self.user, "RUN", "a" * 200, "s" * 1000, target=self.target)
        self.assertEqual(len(event.summary), 300)
        self.assertEqual(len(event.action), 60)

    def test_nothing_is_recorded_without_a_real_user(self):
        for nobody in (None, AnonymousUser(), User()):
            with self.subTest(user=repr(nobody)):
                self.assertIsNone(record_activity(nobody, "RUN", "x.run", "Ran"))
        self.assertEqual(ActivityEvent.objects.count(), 0)

    def test_an_unknown_category_is_logged_and_does_not_raise(self):
        with self.assertLogs("insurance.services.activity", level="ERROR"):
            self.assertIsNone(record_activity(self.user, "NOT_A_CATEGORY", "x.run", "Ran"))
        self.assertEqual(ActivityEvent.objects.count(), 0)

    def test_a_database_error_is_logged_and_does_not_break_the_callers_transaction(self):
        """A failed INSERT on Postgres aborts the whole transaction unless it ran in its own savepoint.
        The caller here keeps working afterwards, which is what a view in the middle of a request needs."""
        def failing_create(*args, **kwargs):
            with connection.cursor() as cursor:
                cursor.execute("SELECT * FROM table_that_does_not_exist_for_this_test")

        with transaction.atomic():
            with mock.patch.object(ActivityEvent.objects, "create", side_effect=failing_create):
                with self.assertLogs("insurance.services.activity", level="ERROR"):
                    self.assertIsNone(record_activity(self.user, "RUN", "x.run", "Ran"))
            # Still usable: on Postgres this raises TransactionManagementError if the savepoint is missing.
            self.assertEqual(User.objects.filter(username="rita").count(), 1)
            self.assertIsNotNone(record_activity(self.user, "RUN", "x.run", "Ran again"))


class ReportsToRuleTests(TestCase):
    """UserProfile.clean(): the hierarchy has to stay a tree, because the scope reads it as one."""

    def clean(self, profile):
        profile.full_clean(exclude=["user"])

    def test_a_person_cannot_report_to_themselves(self):
        profile = make_user("solo").profile
        profile.reports_to = profile
        with self.assertRaises(ValidationError) as caught:
            self.clean(profile)
        self.assertIn("reports_to", caught.exception.message_dict)

    def test_two_people_cannot_report_to_each_other(self):
        a = make_user("loop_a")
        b = make_user("loop_b", a)
        a.profile.reports_to = b.profile
        with self.assertRaises(ValidationError) as caught:
            self.clean(a.profile)
        self.assertIn("loop", " ".join(caught.exception.message_dict["reports_to"]))

    def test_a_longer_loop_is_refused_too(self):
        a = make_user("chain_a")
        b = make_user("chain_b", a)
        c = make_user("chain_c", b)
        a.profile.reports_to = c.profile
        with self.assertRaises(ValidationError):
            self.clean(a.profile)

    def test_ordinary_moves_are_accepted(self):
        a = make_user("ok_a")
        b = make_user("ok_b", a)
        c = make_user("ok_c", b)
        other = make_user("ok_other")
        c.profile.reports_to = a.profile          # skip a level
        self.clean(c.profile)
        c.profile.reports_to = other.profile      # another team
        self.clean(c.profile)
        c.profile.reports_to = None               # nobody
        self.clean(c.profile)
        b.profile.reports_to = a.profile          # unchanged
        self.clean(b.profile)

    def test_a_loop_that_already_exists_elsewhere_does_not_hang_the_check(self):
        x = make_user("bad_x")
        y = make_user("bad_y", x)
        UserProfile.objects.filter(pk=x.profile.pk).update(reports_to=y.profile)   # a stored loop
        newcomer = make_user("newcomer")
        newcomer.profile.reports_to = x.profile    # joins the looping pair from outside: not a loop through newcomer
        self.clean(newcomer.profile)

    def test_a_profile_that_is_not_saved_yet_cannot_be_part_of_a_loop(self):
        manager = make_user("fresh_manager")
        fresh = User.objects.create(username="fresh_report")
        profile = UserProfile(user=fresh, reports_to=manager.profile)
        self.clean(profile)


class AccessControlReportsToTests(TestCase):
    """The Reports To dropdown in Access Control, end to end.

    The dropdown posts a USER id; the field points at a USER PROFILE. Every profile here is created with an id
    that is NOT its user's id, which is exactly the situation the old code got wrong.
    """

    def setUp(self):
        self.admin = User.objects.create(username="hr_admin", is_staff=True)
        self.client.force_login(self.admin)
        self.manager = self.user_with_drifted_profile("mgr")
        self.person = self.user_with_drifted_profile("person")

    @staticmethod
    def user_with_drifted_profile(username, **fields):
        user = User.objects.create(username=username, **fields)
        UserProfile.objects.create(id=user.id + 100_000, user=user)
        return user

    def save(self, person, **fields):
        data = {"action": "update_user_access", "user_id": person.id, "email": "",
                "contact_number": "", "designation": ""}
        data.update(fields)
        return self.client.post(reverse("user_management"), data)

    def reports_to(self, user):
        user.profile.refresh_from_db()
        return user.profile.reports_to

    def test_the_dropdown_offers_user_ids(self):
        response = self.client.get(reverse("user_management"))
        offered = {row["username"]: row["id"] for row in response.context["manager_choices"]}
        self.assertEqual(offered["mgr"], self.manager.id)

    def test_choosing_a_manager_stores_that_managers_profile_not_the_user_id(self):
        self.assertNotEqual(self.manager.profile.id, self.manager.id)
        self.save(self.person, reports_to=self.manager.id)
        self.assertEqual(self.reports_to(self.person), self.manager.profile)

    def test_the_edit_form_is_sent_the_managers_user_id_to_preselect(self):
        self.save(self.person, reports_to=self.manager.id)
        response = self.client.get(reverse("user_management"))
        row = next(r for r in response.context["users"] if r["username"] == "person")
        self.assertEqual(row["reports_to"], self.manager.id)

    def test_a_manager_with_no_profile_yet_gets_one(self):
        bare = User.objects.create(username="bare_manager")
        self.assertFalse(UserProfile.objects.filter(user=bare).exists())
        self.save(self.person, reports_to=bare.id)
        self.assertEqual(self.reports_to(self.person).user, bare)

    def test_every_change_of_manager_is_put_on_the_record(self):
        self.save(self.person, reports_to=self.manager.id)
        entry = AuditLog.objects.get(action="TEAM CHANGE")
        self.assertEqual(entry.user, self.admin)
        self.assertIn("'person' now reports to mgr (was nobody)", entry.details)

        self.save(self.person, reports_to="")
        latest = AuditLog.objects.filter(action="TEAM CHANGE").first()
        self.assertIn("'person' now reports to nobody (was mgr)", latest.details)
        self.assertIsNone(self.reports_to(self.person))

    def test_saving_with_the_same_manager_writes_no_record(self):
        self.save(self.person, reports_to=self.manager.id)
        AuditLog.objects.all().delete()
        self.save(self.person, reports_to=self.manager.id, designation="Analyst")
        self.assertFalse(AuditLog.objects.filter(action="TEAM CHANGE").exists())

    def test_the_audit_page_lists_team_changes(self):
        from insurance.constants import SECURITY_AUDIT_LOG_ACTIONS
        self.assertIn("TEAM CHANGE", SECURITY_AUDIT_LOG_ACTIONS)

    def test_a_form_without_the_field_leaves_the_manager_alone(self):
        # A select whose current manager is no longer listed (deactivated) posts nothing at all.
        self.save(self.person, reports_to=self.manager.id)
        AuditLog.objects.all().delete()
        self.save(self.person)
        self.assertEqual(self.reports_to(self.person), self.manager.profile)
        self.assertFalse(AuditLog.objects.filter(action="TEAM CHANGE").exists())

    def test_nobody_can_be_their_own_manager(self):
        response = self.save(self.person, reports_to=self.person.id)
        self.assertIn("cannot report to themselves", response.context["error"])
        self.assertIsNone(self.reports_to(self.person))

    def test_a_loop_is_refused_and_nothing_changes(self):
        self.save(self.person, reports_to=self.manager.id)        # person reports to mgr
        AuditLog.objects.all().delete()
        response = self.save(self.manager, reports_to=self.person.id)   # mgr to person: a loop
        self.assertIn("loop", response.context["error"])
        self.assertIsNone(self.reports_to(self.manager))
        self.assertFalse(AuditLog.objects.filter(action="TEAM CHANGE").exists())

    def test_a_deactivated_or_unknown_manager_is_refused(self):
        gone = self.user_with_drifted_profile("gone", is_active=False)
        for bad in (gone.id, 999_999_999, "abc"):
            with self.subTest(value=bad):
                response = self.save(self.person, reports_to=bad)
                self.assertIn("does not exist or is not active", response.context["error"])
                self.assertIsNone(self.reports_to(self.person))


class CheckTeamHierarchyCommandTests(TestCase):
    def run_command(self):
        out = StringIO()
        call_command("check_team_hierarchy", stdout=out)
        return out.getvalue()

    def test_a_clean_hierarchy_reports_no_problems(self):
        boss = make_user("tidy_boss")
        make_user("tidy_report", boss)
        text = self.run_command()
        self.assertIn("tidy_report -> tidy_boss", text)
        self.assertIn("No loops, self-links or deactivated managers found.", text)

    def test_it_flags_loops_self_links_and_deactivated_managers_and_writes_nothing(self):
        a = make_user("loop_a")
        b = make_user("loop_b", a)
        UserProfile.objects.filter(pk=a.profile.pk).update(reports_to=b.profile)
        selfie = make_user("selfie")
        UserProfile.objects.filter(pk=selfie.profile.pk).update(reports_to=selfie.profile)
        gone = make_user("gone_boss", is_active=False)
        make_user("under_gone", gone)
        User.objects.create(username="bare_account")   # an active account with no profile

        before = (UserProfile.objects.count(), AuditLog.objects.count(), User.objects.count())
        text = self.run_command()
        self.assertEqual((UserProfile.objects.count(), AuditLog.objects.count(), User.objects.count()), before)

        self.assertRegex(text, r"loop_a -> loop_b .*PART OF A LOOP")
        self.assertRegex(text, r"selfie -> selfie .*REPORTS TO THEMSELVES")
        self.assertRegex(text, r"under_gone -> gone_boss .*manager is deactivated")
        self.assertIn("bare_account", text)
        self.assertIn("link(s) need a look", text)


class StructureTests(SimpleTestCase):
    def test_views_and_the_api_never_query_events_directly(self):
        """Everything that shows events starts from visible_events(); a view reaching for the table itself
        would skip the team rule."""
        app = pathlib.Path(__file__).resolve().parent
        offenders = []
        for folder in ("views", "api"):
            for path in sorted((app / folder).glob("*.py")):
                text = path.read_text(encoding="utf-8")
                if re.search(r"ActivityEvent\s*\.\s*(objects|_default_manager|_base_manager)", text):
                    offenders.append(f"{folder}/{path.name}")
        self.assertFalse(offenders, "use services.team_scope.visible_events instead: " + ", ".join(offenders))


class PageTestMixin(OrgMixin):
    """The org above with the Team Activity page group handed to the managers (and to the leaf a2, who has no team)."""

    def build_page_org(self):
        self.build_org()
        group = Group.objects.get_or_create(name="Can_View_Team_Activity")[0]
        for user in (self.boss, self.lead1, self.lead2, self.a2, self.other_boss):
            user.groups.add(group)

    def get(self, viewer, params=None, name="team_activity"):
        self.client.force_login(viewer)
        return self.client.get(reverse(name), params or {})

    @staticmethod
    def feed_names(response):
        return {event.actor_username for event in response.context["page_obj"]}

    def backdate(self, event, **delta):
        ActivityEvent.objects.filter(pk=event.pk).update(occurred_at=timezone.now() - datetime.timedelta(**delta))


class TeamActivityGateTests(PageTestMixin, TestCase):
    def setUp(self):
        self.build_page_org()

    def test_anonymous_visitors_are_sent_to_login(self):
        for name in ("team_activity", "export_team_activity_xlsx"):
            with self.subTest(route=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.url.startswith(reverse("login")))

    def test_signed_in_people_without_the_page_group_are_refused_even_with_a_team(self):
        for name in ("team_activity", "export_team_activity_xlsx"):
            with self.subTest(route=name):
                # a1 has a team (junior) but not the page group.
                self.assertEqual(self.get(self.a1, name=name).status_code, 403)
                self.assertEqual(self.get(self.junior, name=name).status_code, 403)

    def test_staff_and_superuser_flags_do_not_open_it(self):
        staff = User.objects.create(username="flag_staff", is_staff=True, is_superuser=True)
        self.assertEqual(self.get(staff).status_code, 403)

    def test_admin_and_group_members_are_let_in(self):
        for viewer in (self.make_admin(), self.boss, self.lead1):
            with self.subTest(viewer=viewer.username):
                self.assertEqual(self.get(viewer).status_code, 200)

    def test_the_sidebar_link_follows_the_page_group(self):
        link = f'href="{reverse("team_activity")}"'
        self.assertContains(self.get(self.boss, name="home_dashboard"), link)
        self.assertContains(self.get(self.make_admin("sidebar_admin"), name="home_dashboard"), link)
        self.assertNotContains(self.get(self.a1, name="home_dashboard"), link)    # a team, but no page group


class TeamActivityPageLeakTests(PageTestMixin, TestCase):
    def setUp(self):
        self.build_page_org()

    def test_each_manager_sees_only_their_own_team(self):
        expected = {
            self.boss: {"lead1", "lead2", "a1", "a2", "junior", "b1"},
            self.lead1: {"a1", "a2", "junior"},
            self.lead2: {"b1"},
            self.other_boss: {"x1"},
        }
        for viewer, names in expected.items():
            with self.subTest(viewer=viewer.username):
                response = self.get(viewer)
                self.assertEqual(self.feed_names(response), names)
                self.assertEqual(response.context["total"], len(names))
                for other in ({u.username for u in self.everyone} - names - {viewer.username}):
                    self.assertNotContains(response, f"{other} ran something")

    def test_a_forged_person_filter_shows_nothing_of_another_team(self):
        for forged in (self.x1, self.lead2, self.b1, self.boss, self.lead1):
            with self.subTest(forged=forged.username):
                response = self.get(self.lead1, {"person": forged.pk})
                self.assertEqual(response.context["total"], 0)
                self.assertNotContains(response, f"{forged.username} ran something")
        self.assertEqual(self.feed_names(self.get(self.lead1, {"person": self.a1.pk})), {"a1"})

    def test_the_team_filter_is_ignored_for_everyone_but_admins(self):
        own_team = {"a1", "a2", "junior"}
        for forged in (self.lead2, self.other_boss, self.boss):
            with self.subTest(forged=forged.username):
                response = self.get(self.lead1, {"team": forged.pk})
                self.assertEqual(self.feed_names(response), own_team)
                self.assertIsNone(response.context["selected"]["team"])

    def test_the_person_dropdown_and_the_charts_list_only_the_team(self):
        response = self.get(self.lead1)
        self.assertEqual({p.username for p in response.context["people"]}, {"a1", "a2", "junior"})
        self.assertEqual({row["username"] for row in response.context["people_chart"]["rows"]}, {"a1", "a2", "junior"})
        self.assertEqual(response.context["category_chart"]["total"], 3)          # one event each, nobody else's
        self.assertNotContains(response, "lead2")
        self.assertNotContains(response, "other_boss")

    def test_a_longer_chain_is_followed_and_a_shorter_one_is_not_widened(self):
        extra = make_user("deep", self.junior)
        record_activity(extra, "RUN", "test.run", "deep ran something")
        self.assertIn("deep", self.feed_names(self.get(self.boss)))
        self.assertIn("deep", self.feed_names(self.get(self.lead1)))
        self.assertNotIn("deep", self.feed_names(self.get(self.lead2)))

    def test_a_person_with_no_team_gets_the_empty_state_and_no_events(self):
        response = self.get(self.a2)                       # has the page group, nobody reports to them
        self.assertContains(response, "Nobody reports to you yet")
        self.assertFalse(response.context["has_team"])
        self.assertEqual(response.context["total"], 0)
        self.assertNotContains(response, "ran something")

    def test_the_export_never_includes_another_teams_rows(self):
        response = self.get(self.lead1, name="export_team_activity_xlsx")
        rows = list(load_workbook(io.BytesIO(response.content)).active.iter_rows(values_only=True))
        self.assertEqual({row[1] for row in rows[1:]}, {"a1", "a2", "junior"})

    def test_a_forged_person_or_team_in_the_export_returns_only_the_header(self):
        for params in ({"person": self.x1.pk}, {"person": self.b1.pk}):
            with self.subTest(params=params):
                response = self.get(self.lead1, params, name="export_team_activity_xlsx")
                rows = list(load_workbook(io.BytesIO(response.content)).active.iter_rows(values_only=True))
                self.assertEqual(len(rows), 1)

    def test_an_admin_sees_every_team_and_a_team_filter_narrows_it_to_that_managers_team(self):
        admin = self.make_admin()
        everyone = {u.username for u in self.everyone}
        self.assertEqual(self.feed_names(self.get(admin)), everyone)                       # loner has no profile: still renders
        self.assertEqual(self.feed_names(self.get(admin, {"team": self.lead1.pk})), {"a1", "a2", "junior"})
        self.assertEqual(self.feed_names(self.get(admin, {"team": "not-a-number"})), everyone)

    def test_an_admin_with_a_team_filter_sees_that_teams_quiet_members_under_no_activity(self):
        make_user("quiet", self.lead1)
        chart = self.get(self.make_admin(), {"team": self.lead1.pk}).context["people_chart"]
        self.assertEqual({row["username"] for row in chart["rows"]}, {"a1", "a2", "junior"})
        self.assertEqual(chart["quiet"], ["quiet"])

    def test_an_admin_without_a_team_filter_is_not_shown_the_whole_organisation_as_quiet(self):
        make_user("quiet", self.lead1)
        chart = self.get(self.make_admin()).context["people_chart"]
        self.assertEqual({row["username"] for row in chart["rows"]}, {u.username for u in self.everyone})
        self.assertEqual(chart["quiet"], [])


class TeamActivityFilterTests(PageTestMixin, TestCase):
    def setUp(self):
        self.build_page_org()

    def test_the_default_period_is_the_last_seven_days(self):
        old = ActivityEvent.objects.get(actor=self.a1)
        self.backdate(old, days=20)
        self.assertNotIn("a1", self.feed_names(self.get(self.lead1)))
        self.assertIn("a1", self.feed_names(self.get(self.lead1, {"date_range": ""})))      # cleared: no limit
        twenty = (timezone.localdate() - datetime.timedelta(days=20)).isoformat()
        only_that_day = self.get(self.lead1, {"date_range": f"{twenty} - {twenty}"})
        self.assertEqual(self.feed_names(only_that_day), {"a1"})

    def test_a_reversed_or_unreadable_range_is_handled_without_an_error(self):
        today = timezone.localdate().isoformat()
        yesterday = (timezone.localdate() - datetime.timedelta(days=1)).isoformat()
        self.assertEqual(self.get(self.lead1, {"date_range": f"{today} - {yesterday}"}).status_code, 200)
        garbage = self.get(self.lead1, {"date_range": "yesterday-ish"})
        self.assertEqual(garbage.status_code, 200)
        self.assertEqual(garbage.context["selected"]["date_range"], "")      # read as "no limit", not guessed

    def test_days_follow_india_time_not_utc(self):
        self.assertEqual(settings.TIME_ZONE, "Asia/Kolkata")
        utc = datetime.timezone.utc
        late_night = ActivityEvent.objects.get(actor=self.a1)       # 22 Nov 20:00 UTC = 23 Nov 01:30 IST
        early = ActivityEvent.objects.get(actor=self.a2)            # 23 Nov 19:00 UTC = 24 Nov 00:30 IST
        ActivityEvent.objects.filter(pk=late_night.pk).update(occurred_at=datetime.datetime(2026, 11, 22, 20, 0, tzinfo=utc))
        ActivityEvent.objects.filter(pk=early.pk).update(occurred_at=datetime.datetime(2026, 11, 23, 19, 0, tzinfo=utc))
        response = self.get(self.lead1, {"date_range": "2026-11-23 - 2026-11-23"})
        names = self.feed_names(response)
        self.assertIn("a1", names)
        self.assertNotIn("a2", names)

    def test_category_and_search_narrow_the_feed(self):
        record_activity(self.a1, "UPLOAD", "grid.uploaded", "Uploaded the grid 'Alpha.xlsx' for ACKO")
        uploads = self.get(self.lead1, {"category": "UPLOAD"})
        self.assertEqual([e.action for e in uploads.context["page_obj"]], ["grid.uploaded"])
        self.assertEqual(self.get(self.lead1, {"category": "NOT_A_CATEGORY"}).context["selected"]["category"], "")
        self.assertEqual(self.feed_names(self.get(self.lead1, {"q": "alpha"})), {"a1"})
        self.assertEqual(self.feed_names(self.get(self.lead1, {"q": "junior"})), {"junior"})   # matches the person too
        self.assertEqual(self.get(self.lead1, {"q": "zzz-nothing"}).context["total"], 0)

    def test_text_in_a_summary_is_shown_escaped(self):
        record_activity(self.a1, "UPLOAD", "grid.uploaded", "Uploaded <script>alert(1)</script>.xlsx")
        response = self.get(self.lead1)
        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertContains(response, "&lt;script&gt;alert(1)&lt;/script&gt;")

    def test_paging_and_the_rows_per_page_choice(self):
        for n in range(120):
            record_activity(self.a1, "RUN", "test.run", f"a1 run {n}")
        total = 120 + 3                                          # plus one event each for a1, a2 and junior
        first = self.get(self.lead1)
        self.assertEqual((first.context["total"], len(first.context["page_obj"]), first.context["page_obj"].paginator.num_pages),
                         (total, 50, 3))
        self.assertEqual(len(self.get(self.lead1, {"page_size": 200}).context["page_obj"]), total)
        self.assertEqual(self.get(self.lead1, {"page_size": 7}).context["page_size"], 50)       # not an offered size
        last = self.get(self.lead1, {"page": 999})
        self.assertEqual(last.context["page_obj"].number, 3)
        self.assertContains(self.get(self.lead1, {"page": 2}), "Newer")


class TeamActivityChartTests(PageTestMixin, TestCase):
    """The two charts: a donut of events by category and ranked bars of events by person. Both are filters too:
    each follows every filter except its own kind, so every slice and every bar stays on screen and can be undone."""

    def setUp(self):
        self.build_page_org()

    def chart(self, params=None, viewer=None):
        response = self.get(viewer or self.lead1, params)
        return response, response.context["category_chart"], response.context["people_chart"]

    def test_the_donut_counts_events_by_category_in_a_fixed_order(self):
        record_activity(self.a1, "UPLOAD", "grid.uploaded", "Uploaded a grid")
        record_activity(self.a1, "UPLOAD", "grid.uploaded", "Uploaded another grid")
        record_activity(self.a2, "TEST", "life.rates.searched", "Ran a Life payout rate search")
        _, donut, _ = self.chart()
        self.assertEqual([(i["code"], i["n"]) for i in donut["legend"]],
                         [("RUN", 3), ("UPLOAD", 2), ("TEST", 1), ("STATUS", 0)])
        self.assertEqual([s["code"] for s in donut["slices"]], ["RUN", "UPLOAD", "TEST"])      # nothing drawn for nothing
        self.assertEqual(donut["total"], 6)
        self.assertEqual([i["pct"] for i in donut["legend"]], ["50%", "33%", "17%", "0%"])
        self.assertEqual(donut["aria"], "Events by category, 6 in all: Run 3, Upload 2, Test 1")
        self.assertEqual(donut["scope"], "Everyone")

    def test_the_people_bars_rank_people_by_events_with_a_ratio_to_the_longest_bar(self):
        for _ in range(3):
            record_activity(self.a1, "TEST", "test.run", "a1 tested")
        record_activity(self.a2, "TEST", "test.run", "a2 tested")
        _, _, people = self.chart()
        self.assertEqual([(r["username"], r["n"]) for r in people["rows"]], [("a1", 4), ("a2", 2), ("junior", 1)])
        self.assertEqual([r["ratio"] for r in people["rows"]], ["1.0000", "0.5000", "0.2500"])
        self.assertEqual([r["share"] for r in people["rows"]], ["57%", "29%", "14%"])        # of 7 events
        self.assertEqual((people["total"], people["scope"]), (7, "All categories"))

    def test_picking_a_person_changes_the_donut_but_not_the_bars(self):
        record_activity(self.a1, "UPLOAD", "grid.uploaded", "Uploaded a grid")
        _, donut, people = self.chart({"person": self.a1.pk})
        self.assertEqual({i["code"]: i["n"] for i in donut["legend"]}, {"RUN": 1, "UPLOAD": 1, "TEST": 0, "STATUS": 0})
        self.assertEqual(donut["scope"], "a1")
        self.assertEqual({r["username"] for r in people["rows"]}, {"a1", "a2", "junior"})        # everyone still there
        self.assertEqual([r["username"] for r in people["rows"] if r["active"]], ["a1"])

    def test_picking_a_category_changes_the_bars_but_not_the_donut(self):
        record_activity(self.a1, "UPLOAD", "grid.uploaded", "Uploaded a grid")
        _, donut, people = self.chart({"category": "UPLOAD"})
        self.assertEqual({i["code"]: i["n"] for i in donut["legend"]}, {"RUN": 3, "UPLOAD": 1, "TEST": 0, "STATUS": 0})
        self.assertEqual([i["code"] for i in donut["legend"] if i["active"]], ["UPLOAD"])
        self.assertEqual([(r["username"], r["n"]) for r in people["rows"]], [("a1", 1)])
        self.assertEqual(people["quiet"], ["a2", "junior"])                       # nobody else uploaded anything
        self.assertEqual(people["scope"], "Upload only")

    def test_the_search_box_narrows_both_charts(self):
        _, donut, people = self.chart({"q": "junior"})
        self.assertEqual(donut["total"], 1)
        self.assertEqual([r["username"] for r in people["rows"]], ["junior"])

    def test_every_slice_legend_row_and_bar_links_to_the_page_with_one_more_filter(self):
        response, donut, people = self.chart({"q": "ran", "page": 2})
        window = response.context["selected"]["date_range"]
        run = next(s for s in donut["slices"] if s["code"] == "RUN")
        self.assertEqual(parse_qs(run["url"][1:]), {"date_range": [window], "q": ["ran"], "category": ["RUN"]})
        legend_run = next(i for i in donut["legend"] if i["code"] == "RUN")
        self.assertEqual(legend_run["url"], run["url"])
        first = people["rows"][0]
        self.assertEqual(parse_qs(first["url"][1:]),
                         {"date_range": [window], "q": ["ran"], "person": [str(first["id"])]})       # and never `page`
        self.assertEqual(next(i for i in donut["legend"] if i["code"] == "UPLOAD")["url"], "")     # nothing to show: no link

    def test_the_selected_slice_and_bar_link_back_to_the_page_without_that_filter(self):
        _, donut, people = self.chart({"category": "RUN", "person": self.a1.pk})
        slice_ = next(s for s in donut["slices"] if s["active"])
        self.assertNotIn("category", parse_qs(slice_["url"][1:]))
        self.assertEqual(parse_qs(slice_["url"][1:])["person"], [str(self.a1.pk)])           # the other filter stays
        bar = next(r for r in people["rows"] if r["active"])
        self.assertNotIn("person", parse_qs(bar["url"][1:]))
        self.assertEqual(parse_qs(bar["url"][1:])["category"], ["RUN"])

    def test_an_admins_team_filter_travels_with_every_link(self):
        _, donut, people = self.chart({"team": self.lead1.pk}, viewer=self.make_admin())
        self.assertEqual(parse_qs(people["rows"][0]["url"][1:])["team"], [str(self.lead1.pk)])
        self.assertEqual(parse_qs(donut["slices"][0]["url"][1:])["team"], [str(self.lead1.pk)])

    def test_a_forged_person_gives_an_empty_donut_that_does_not_say_who_that_is(self):
        response, donut, people = self.chart({"person": self.x1.pk})
        self.assertEqual((donut["total"], donut["slices"]), (0, []))
        self.assertEqual(donut["scope"], "Selected person")
        self.assertEqual({r["username"] for r in people["rows"]}, {"a1", "a2", "junior"})
        self.assertNotContains(response, "x1 ran something")

    def test_the_charts_never_mention_another_team(self):
        make_user("quiet", self.lead1)
        for viewer in (self.lead1, self.lead2):
            with self.subTest(viewer=viewer.username):
                _, _, people = self.chart(viewer=viewer)
                names = {r["username"] for r in people["rows"]} | set(people["quiet"])
                self.assertTrue(names.isdisjoint({"x1", "other_boss", "boss", "loner"}))
        _, _, lead2_people = self.chart(viewer=self.lead2)
        self.assertEqual({r["username"] for r in lead2_people["rows"]}, {"b1"})

    def test_more_people_than_bars_are_folded_into_a_count_and_the_picked_person_keeps_a_bar(self):
        extra = [make_user(f"u{i}", self.lead1) for i in range(1, 10)]
        for count, user in enumerate(extra, start=1):
            for _ in range(count):
                record_activity(user, "RUN", "test.run", f"{user.username} ran something")
        _, _, people = self.chart()                       # 12 people: u9..u1 with 9..1 events, a1, a2, junior with 1
        self.assertEqual(len(people["rows"]), 10)
        self.assertEqual(people["rows"][0]["username"], "u9")
        self.assertEqual((people["more_people"], people["more_events"]), (2, 2))
        _, _, picked = self.chart({"person": extra[0].pk})     # u1 is among the folded ones
        self.assertEqual(len(picked["rows"]), 10)
        self.assertEqual([r["username"] for r in picked["rows"] if r["active"]], ["u1"])
        self.assertEqual((picked["more_people"], picked["more_events"]), (2, 2))

    def test_quiet_people_are_listed_by_name_up_to_a_limit(self):
        for i in range(1, 11):
            make_user(f"q{i:02d}", self.lead1)
        _, _, people = self.chart()
        self.assertEqual(people["quiet"], [f"q{i:02d}" for i in range(1, 9)])
        self.assertEqual(people["quiet_more"], 2)

    def test_the_edit_category_only_gets_a_slice_once_something_is_recorded_under_it(self):
        self.assertEqual([i["code"] for i in self.chart()[1]["legend"]], ["RUN", "UPLOAD", "TEST", "STATUS"])
        record_activity(self.a1, "EDIT", "rate.edited", "Edited a rate")
        self.assertEqual([i["code"] for i in self.chart()[1]["legend"]], ["RUN", "UPLOAD", "TEST", "STATUS", "EDIT"])

    def test_the_page_draws_the_donut_its_legend_and_the_bars_and_the_old_table_is_gone(self):
        record_activity(self.a1, "UPLOAD", "grid.uploaded", "Uploaded a grid")
        response = self.get(self.lead1)
        self.assertContains(response, '<svg class="ta-donut"')
        self.assertContains(response, 'aria-label="Events by category, 4 in all: Run 3, Upload 1"')
        self.assertContains(response, "<title>Run: 3 events (75%)</title>")
        self.assertContains(response, "<title>Upload: 1 event (25%)</title>")
        self.assertContains(response, "ta-legend-row")
        self.assertContains(response, "ta-bar-row")
        self.assertContains(response, "At a glance")
        self.assertNotContains(response, "Team at a glance")

    def test_with_nothing_to_show_the_donut_is_an_empty_ring_and_the_bars_say_so(self):
        response = self.get(self.lead1, {"q": "zzz-nothing"})
        self.assertContains(response, "ta-donut-track")
        self.assertContains(response, "No events for these filters.")

    def test_names_in_the_charts_are_shown_escaped(self):
        sneaky = make_user("sneaky", self.lead1, first_name="<script>alert(1)</script>")
        record_activity(sneaky, "RUN", "test.run", "sneaky ran something")
        response = self.get(self.lead1)
        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertContains(response, "&lt;script&gt;alert(1)&lt;/script&gt;")


class DonutGeometryTests(SimpleTestCase):
    circumference = 2 * math.pi * DONUT_RADIUS

    def test_a_lone_slice_is_the_whole_ring_with_no_gap(self):
        slices = _donut_slices([("RUN", "Run", 5), ("UPLOAD", "Upload", 0)], 5)
        self.assertEqual(len(slices), 1)
        self.assertAlmostEqual(float(slices[0]["dash"].split()[0]), self.circumference, places=1)
        self.assertEqual(slices[0]["offset"], "0.00")

    def test_slices_are_their_share_of_the_ring_less_a_gap_and_start_where_the_last_ended(self):
        slices = _donut_slices([("RUN", "Run", 1), ("UPLOAD", "Upload", 1), ("TEST", "Test", 2)], 4)
        for item, share in zip(slices, (0.25, 0.25, 0.5)):
            self.assertAlmostEqual(float(item["dash"].split()[0]), self.circumference * share - DONUT_GAP, places=1)
        self.assertAlmostEqual(float(slices[0]["offset"]), -DONUT_GAP / 2, places=1)
        self.assertAlmostEqual(float(slices[1]["offset"]), -(self.circumference * 0.25 + DONUT_GAP / 2), places=1)
        self.assertAlmostEqual(float(slices[2]["offset"]), -(self.circumference * 0.5 + DONUT_GAP / 2), places=1)

    def test_a_tiny_share_still_gets_a_visible_arc(self):
        slices = _donut_slices([("RUN", "Run", 1), ("TEST", "Test", 10000)], 10001)
        self.assertGreaterEqual(float(slices[0]["dash"].split()[0]), 1.0)

    def test_nothing_to_draw_gives_no_slices_and_percentages_never_say_zero_for_something(self):
        self.assertEqual(_donut_slices([("RUN", "Run", 0)], 0), [])
        self.assertEqual(_percent_text(0, 0), "0%")
        self.assertEqual(_percent_text(1, 1000), "<1%")
        self.assertEqual(_percent_text(1, 2), "50%")


class TeamActivityExportTests(PageTestMixin, TestCase):
    def setUp(self):
        self.build_page_org()

    def rows(self, response):
        return list(load_workbook(io.BytesIO(response.content)).active.iter_rows(values_only=True))

    def test_the_file_has_a_header_the_scoped_rows_and_india_time(self):
        event = ActivityEvent.objects.get(actor=self.a1)
        ActivityEvent.objects.filter(pk=event.pk).update(occurred_at=timezone.now().replace(microsecond=0))
        event.refresh_from_db()
        response = self.get(self.lead1, name="export_team_activity_xlsx")
        self.assertEqual(response["Content-Type"], "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.assertIn("team_activity.xlsx", response["Content-Disposition"])
        rows = self.rows(response)
        self.assertEqual(rows[0], ("Time (IST)", "Username", "Name", "Category", "Action", "Summary", "About", "About id",
                                   "Source path", "Details"))
        mine = next(row for row in rows[1:] if row[1] == "a1")
        self.assertEqual(mine[0], timezone.localtime(event.occurred_at).strftime("%Y-%m-%d %H:%M:%S"))
        self.assertEqual(mine[3:6], ("Run", "test.run", "a1 ran something"))
        self.assertEqual(len(rows), 1 + 3)

    def test_the_export_applies_the_same_filters_as_the_page(self):
        record_activity(self.a1, "UPLOAD", "grid.uploaded", "Uploaded a grid")
        self.backdate(ActivityEvent.objects.get(actor=self.a2), days=30)
        rows = self.rows(self.get(self.lead1, {"category": "UPLOAD"}, name="export_team_activity_xlsx"))
        self.assertEqual([row[4] for row in rows[1:]], ["grid.uploaded"])
        default = self.rows(self.get(self.lead1, name="export_team_activity_xlsx"))
        self.assertNotIn("a2", {row[1] for row in default[1:]})                 # the 7-day default applies here too
        everything = self.rows(self.get(self.lead1, {"date_range": ""}, name="export_team_activity_xlsx"))
        self.assertIn("a2", {row[1] for row in everything[1:]})

    def test_text_that_would_run_as_a_formula_is_defused(self):
        record_activity(self.a1, "UPLOAD", "grid.uploaded", '=HYPERLINK("http://example.test","click")')
        rows = self.rows(self.get(self.lead1, {"category": "UPLOAD"}, name="export_team_activity_xlsx"))
        self.assertEqual(rows[1][5], '\'=HYPERLINK("http://example.test","click")')

    def test_the_page_links_to_an_export_that_carries_its_filters(self):
        response = self.get(self.lead1, {"category": "RUN", "q": "ran"})
        self.assertContains(response, reverse("export_team_activity_xlsx"))
        self.assertIn("category=RUN", response.context["export_query"])
        self.assertIn("q=ran", response.context["export_query"])


class TeamActivityPivotTests(PageTestMixin, TestCase):
    """The two Pivot sheets of the export (person x category, person x day), counted from the same scoped and filtered
    events as the Team Activity sheet."""

    def setUp(self):
        self.build_page_org()

    def workbook(self, viewer, params=None):
        response = self.get(viewer, params, name="export_team_activity_xlsx")
        self.assertEqual(response.status_code, 200)
        return load_workbook(io.BytesIO(response.content))

    @staticmethod
    def table(ws):
        """The sheet from its header row (the one starting with 'Person') down, as lists of cell values."""
        rows = [list(row) for row in ws.iter_rows(values_only=True)]
        start = next(i for i, row in enumerate(rows) if row and row[0] == "Person")
        return rows[start:]

    @staticmethod
    def notes(ws):
        """Everything above the header row (the title and the notes), as text."""
        out = []
        for row in ws.iter_rows(values_only=True):
            if row and row[0] == "Person":
                break
            out.extend(str(value) for value in row if value)
        return out

    def test_the_export_has_the_events_sheet_first_then_the_two_pivots(self):
        wb = self.workbook(self.lead1)
        self.assertEqual(wb.sheetnames, ["Team Activity", "Pivot by category", "Pivot by day"])
        self.assertEqual(wb.active.title, "Team Activity")

    def test_the_category_pivot_counts_each_person_by_category_and_adds_up(self):
        UserProfile.objects.filter(pk=self.a1.profile.pk).update(designation="Analyst")
        record_activity(self.a1, "UPLOAD", "grid.uploaded", "Uploaded a grid")
        record_activity(self.a1, "UPLOAD", "grid.uploaded", "Uploaded another grid")
        record_activity(self.a1, "TEST", "life.rates.searched", "Ran a Life payout rate search")
        record_activity(self.a1, "STATUS", "ticket.opened", "Opened Motor support ticket #1")
        rows = self.table(self.workbook(self.lead1)["Pivot by category"])
        self.assertEqual(rows[0], ["Person", "Username", "Designation", "Run", "Upload", "Test", "Status", "Total",
                                   "Last active (IST)"])
        latest = ActivityEvent.objects.filter(actor=self.a1).order_by("-occurred_at").first()
        self.assertEqual(rows[1], ["a1", "a1", "Analyst", 1, 2, 1, 1, 5,
                                   timezone.localtime(latest.occurred_at).strftime("%Y-%m-%d %H:%M")])
        self.assertEqual([row[1] for row in rows[2:4]], ["a2", "junior"])          # busiest first, then by name
        self.assertEqual(rows[-1], ["Total", None, None, 3, 2, 1, 1, 7, None])

    def test_the_pivots_never_include_another_team(self):
        wb = self.workbook(self.lead1)
        for name in ("Pivot by category", "Pivot by day"):
            self.assertEqual({row[1] for row in self.table(wb[name])[1:-1]}, {"a1", "a2", "junior"}, name)
        everything = {str(value) for ws in wb for row in ws.iter_rows(values_only=True) for value in row
                      if value is not None}
        self.assertTrue(everything.isdisjoint({"x1", "b1", "lead2", "boss", "other_boss", "loner", "lead1"}))

    def test_a_forged_person_gives_empty_pivots(self):
        for forged in (self.x1, self.b1):
            with self.subTest(forged=forged.username):
                wb = self.workbook(self.lead1, {"person": forged.pk})
                self.assertEqual(self.table(wb["Pivot by category"])[1:], [["Total", None, None, 0, 0, 0, 0, 0, None]])
                day_rows = self.table(wb["Pivot by day"])[1:]
                self.assertEqual(len(day_rows), 1)
                self.assertEqual(day_rows[0][0], "Total")
                self.assertEqual(sum(v for v in day_rows[0][2:] if v), 0)

    def test_quiet_teammates_get_a_row_of_zeros_unless_the_export_is_narrowed(self):
        make_user("quiet", self.lead1)

        def usernames(params=None):
            return [row[1] for row in self.table(self.workbook(self.lead1, params)["Pivot by category"])[1:-1]]

        self.assertIn("quiet", usernames())
        quiet = next(row for row in self.table(self.workbook(self.lead1)["Pivot by category"]) if row[1] == "quiet")
        self.assertEqual(quiet[3:], [0, 0, 0, 0, 0, None])
        self.assertNotIn("quiet", usernames({"category": "RUN"}))
        self.assertNotIn("quiet", usernames({"q": "ran"}))
        self.assertEqual(usernames({"person": self.a1.pk}), ["a1"])

    def test_an_admin_pivot_lists_the_people_who_were_active_and_a_team_filter_adds_the_quiet_ones(self):
        make_user("quiet", self.lead1)
        admin = self.make_admin()
        everyone = {u.username for u in self.everyone}
        listed = {row[1] for row in self.table(self.workbook(admin)["Pivot by category"])[1:-1]}
        self.assertEqual(listed, everyone)                       # loner has no profile and still gets a row
        team = self.workbook(admin, {"team": self.lead1.pk})
        self.assertEqual({row[1] for row in self.table(team["Pivot by category"])[1:-1]},
                         {"a1", "a2", "junior", "quiet"})

    def test_the_edit_column_appears_only_once_something_is_recorded_under_it(self):
        self.assertNotIn("Edit", self.table(self.workbook(self.lead1)["Pivot by category"])[0])
        record_activity(self.a1, "EDIT", "rate.edited", "Edited a rate")
        header = self.table(self.workbook(self.lead1)["Pivot by category"])[0]
        self.assertEqual(header[3:8], ["Run", "Upload", "Test", "Status", "Edit"])

    def test_the_day_pivot_counts_by_india_day_and_leaves_zeros_blank(self):
        utc = datetime.timezone.utc
        ActivityEvent.objects.filter(actor=self.a1).update(
            occurred_at=datetime.datetime(2026, 11, 22, 20, 0, tzinfo=utc))        # 23 Nov, 01:30 IST
        ActivityEvent.objects.filter(actor=self.a2).update(
            occurred_at=datetime.datetime(2026, 11, 23, 19, 0, tzinfo=utc))        # 24 Nov, 00:30 IST
        ActivityEvent.objects.filter(actor=self.junior).update(
            occurred_at=datetime.datetime(2026, 11, 23, 6, 0, tzinfo=utc))         # 23 Nov, 11:30 IST
        rows = self.table(self.workbook(self.lead1, {"date_range": "2026-11-23 - 2026-11-25"})["Pivot by day"])
        days = [v.date() if isinstance(v, datetime.datetime) else v for v in rows[0][2:-1]]
        self.assertEqual(days, [datetime.date(2026, 11, 23), datetime.date(2026, 11, 24), datetime.date(2026, 11, 25)])
        self.assertEqual({row[1]: row[2:] for row in rows[1:-1]},
                         {"a1": [1, None, None, 1], "a2": [None, 1, None, 1], "junior": [1, None, None, 1]})
        self.assertEqual(rows[-1], ["Total", None, 2, 1, 0, 3])

    def test_a_long_range_is_limited_to_the_latest_days_with_a_note(self):
        with mock.patch("insurance.views.team_activity.MAX_PIVOT_DAYS", 3):
            wb = self.workbook(self.lead1, {"date_range": "2026-11-17 - 2026-11-23"})
        days = [v.date() for v in self.table(wb["Pivot by day"])[0][2:-1]]
        self.assertEqual(days, [datetime.date(2026, 11, 21), datetime.date(2026, 11, 22), datetime.date(2026, 11, 23)])
        self.assertTrue(any("most recent 3 days" in note for note in self.notes(wb["Pivot by day"])))

    def test_with_no_date_limit_the_day_columns_span_the_days_that_have_events(self):
        utc = datetime.timezone.utc
        ActivityEvent.objects.filter(actor=self.a1).update(occurred_at=datetime.datetime(2026, 11, 3, 6, 0, tzinfo=utc))
        ActivityEvent.objects.filter(actor=self.a2).update(occurred_at=datetime.datetime(2026, 11, 5, 6, 0, tzinfo=utc))
        ActivityEvent.objects.filter(actor=self.junior).update(
            occurred_at=datetime.datetime(2026, 11, 5, 7, 0, tzinfo=utc))
        rows = self.table(self.workbook(self.lead1, {"date_range": ""})["Pivot by day"])
        days = [v.date() for v in rows[0][2:-1]]
        self.assertEqual(days, [datetime.date(2026, 11, 3), datetime.date(2026, 11, 4), datetime.date(2026, 11, 5)])

    def test_the_cap_on_event_rows_is_explained_and_the_pivots_still_count_everything(self):
        with mock.patch("insurance.views.team_activity.EXPORT_LIMIT", 2):
            wb = self.workbook(self.lead1)
        self.assertEqual(wb["Team Activity"].max_row, 1 + 2)
        for name in ("Pivot by category", "Pivot by day"):
            self.assertTrue(any("newest 2 of 3 matching events" in note for note in self.notes(wb[name])), name)
        self.assertEqual(self.table(wb["Pivot by category"])[-1][-2], 3)

    def test_the_notes_say_which_filters_were_used(self):
        wb = self.workbook(self.lead1, {"date_range": "2026-11-17 - 2026-11-23", "category": "UPLOAD", "q": "grid"})
        notes = self.notes(wb["Pivot by category"])
        self.assertIn('Same filters as the Team Activity sheet: Dates 2026-11-17 - 2026-11-23 (IST) · '
                      'Category Upload · Search "grid"', notes)
        self.assertFalse(any("newest" in note for note in notes))                 # no cap note when nothing was capped
        self.assertIn("Blank means no events that day.", self.notes(wb["Pivot by day"]))
        team = self.workbook(self.make_admin(), {"team": self.lead1.pk, "person": self.a1.pk})
        note = next(n for n in self.notes(team["Pivot by category"]) if n.startswith("Same filters"))
        self.assertIn("Team of lead1", note)
        self.assertIn("One person", note)

    def test_names_that_look_like_formulas_are_defused_in_the_pivots(self):
        sneaky = make_user("sneaky", self.lead1, first_name="=cmd", last_name="x")
        record_activity(sneaky, "RUN", "test.run", "sneaky ran something")
        wb = self.workbook(self.lead1)
        for name in ("Pivot by category", "Pivot by day"):
            row = next(r for r in self.table(wb[name]) if r[1] == "sneaky")
            self.assertEqual(row[0], "'=cmd x", name)

    def test_the_export_button_says_what_is_in_the_file(self):
        self.assertContains(self.get(self.lead1), "pivot sheets by person and category and by person and day")


class SafeCellTests(SimpleTestCase):
    def test_leading_formula_characters_get_an_apostrophe(self):
        for text in ("=1+1", "+1", "-1", "@SUM(A1)", "\t=1"):
            with self.subTest(text=text):
                self.assertEqual(_safe_cell(text), "'" + text)

    def test_ordinary_text_and_empty_values_are_unchanged(self):
        self.assertEqual(_safe_cell("Uploaded the grid 'a.xlsx'"), "Uploaded the grid 'a.xlsx'")
        self.assertEqual(_safe_cell(None), "")
        self.assertEqual(_safe_cell(12), "12")


def at(*when):
    """An aware datetime in the site's time zone (IST): at(2026, 12, 1, 2, 30) is 1 Dec 2026, 02:30 in India."""
    return timezone.make_aware(datetime.datetime(*when))


class RetentionCutoffTests(SimpleTestCase):
    """Events are kept by whole calendar month: from midnight on the 1st of LAST month. So a daily, weekly or monthly
    report for last month or this month is never cut in half the way a rolling 90 days cut it."""

    def test_the_cutoff_is_midnight_on_the_first_of_last_month(self):
        for now, first_kept in (
            (at(2026, 11, 24, 10, 0), at(2026, 10, 1)),
            (at(2026, 11, 1, 0, 0), at(2026, 10, 1)),                # the very first moment of November
            (at(2026, 11, 30, 23, 59, 59), at(2026, 10, 1)),         # the very last moment of November
            (at(2026, 12, 1, 0, 0), at(2026, 11, 1)),                # December starts: October is out
            (at(2027, 1, 1, 0, 0), at(2026, 12, 1)),                 # across the year
            (at(2027, 1, 31, 12, 0), at(2026, 12, 1)),
            (at(2027, 3, 1, 9, 0), at(2027, 2, 1)),                  # last month is a short one
            (at(2028, 3, 31, 9, 0), at(2028, 2, 1)),                 # a leap year
        ):
            with self.subTest(now=now.isoformat()):
                self.assertEqual(retention_cutoff(now), first_kept)

    def test_the_month_rolls_over_at_midnight_india_time_not_utc(self):
        utc = datetime.timezone.utc
        # 30 Nov 19:00 UTC is already 1 Dec 00:30 in India.
        self.assertEqual(retention_cutoff(datetime.datetime(2026, 11, 30, 19, 0, tzinfo=utc)), at(2026, 11, 1))
        self.assertEqual(retention_cutoff(datetime.datetime(2026, 11, 30, 18, 0, tzinfo=utc)), at(2026, 10, 1))

    def test_without_an_argument_it_is_now(self):
        cutoff = retention_cutoff()
        self.assertEqual((cutoff.day, cutoff.hour, cutoff.minute, cutoff.second), (1, 0, 0, 0))
        self.assertLess(cutoff, timezone.now())
        self.assertGreater(cutoff, timezone.now() - datetime.timedelta(days=63))      # at most two months back


class RetentionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username="keeper")

    def make(self, when):
        event = record_activity(self.user, "RUN", "test.run", f"happened {when:%d %b %Y %H:%M}")
        ActivityEvent.objects.filter(pk=event.pk).update(occurred_at=when)
        return event

    def run_on(self, now):
        """The nightly job as it would run at `now`."""
        with mock.patch("django.utils.timezone.now", return_value=now):
            return tasks.cleanup_team_activity()

    def test_last_month_and_this_month_are_kept_and_anything_older_is_deleted(self):
        before = self.make(at(2026, 9, 30, 23, 59, 59))
        first_moment = self.make(at(2026, 10, 1, 0, 0, 0))
        recent = self.make(at(2026, 11, 20, 9, 0))
        self.assertEqual(self.run_on(at(2026, 11, 24, 2, 30)), 1)
        self.assertFalse(ActivityEvent.objects.filter(pk=before.pk).exists())
        self.assertEqual(ActivityEvent.objects.filter(pk__in=[first_moment.pk, recent.pk]).count(), 2)

    def test_the_oldest_month_goes_on_the_night_the_next_month_starts(self):
        """The example that matters: October's events are kept all through November and removed on 1 December."""
        october = self.make(at(2026, 10, 31, 23, 59, 59))
        november = self.make(at(2026, 11, 1, 0, 0, 0))
        self.assertEqual(self.run_on(at(2026, 11, 30, 2, 30)), 0)         # the last night of November: nothing goes
        self.assertEqual(self.run_on(at(2026, 12, 1, 2, 30)), 1)          # the first night of December
        self.assertFalse(ActivityEvent.objects.filter(pk=october.pk).exists())
        self.assertTrue(ActivityEvent.objects.filter(pk=november.pk).exists())

    def test_running_it_again_deletes_nothing_more(self):
        self.make(at(2026, 8, 1))
        self.assertEqual(self.run_on(at(2026, 11, 24, 2, 30)), 1)
        self.assertEqual(self.run_on(at(2026, 11, 24, 2, 31)), 0)

    def test_nothing_is_a_setting_any_more(self):
        """The window is whole months, not a number of days: a leftover setting would only mislead."""
        self.assertFalse(hasattr(settings, "TEAM_ACTIVITY_RETENTION_DAYS"))

    def test_the_job_is_scheduled_and_has_a_management_command(self):
        entry = settings.CELERY_BEAT_SCHEDULE["cleanup-team-activity"]
        self.assertEqual(entry["task"], "insurance.tasks.cleanup_team_activity")
        self.assertEqual(tasks.cleanup_team_activity.name, entry["task"])
        self.make(at(2026, 9, 15, 12, 0))
        out = StringIO()
        with mock.patch("django.utils.timezone.now", return_value=at(2026, 11, 24, 2, 30)):
            call_command("cleanup_team_activity", stdout=out)
        self.assertIn("Deleted 1 Team Activity event(s) from before 01 Oct 2026.", out.getvalue())

    def test_the_audit_log_purges_do_not_touch_team_activity(self):
        self.make(at(2026, 1, 1))
        tasks.cleanup_points_search_logs()
        tasks.cleanup_security_audit_logs()
        self.assertEqual(ActivityEvent.objects.count(), 1)
