"""Team Activity: who may read whose events (the hierarchy scope), the event writer, the Reports To rules the
scope stands on, and the page, its Excel export and the retention job.

The tests that matter most are the leak tests: a manager must never see another team's work, however the
request is phrased (a forged ?person= or ?team=, the export, a longer chain, another team under the same boss).
"""
import datetime
import io
import pathlib
import re
from io import StringIO
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import AnonymousUser, Group, User
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import connection, transaction
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook

from insurance import tasks
from insurance.models import ActivityEvent, AuditLog, UserProfile
from insurance.services.activity import record_activity
from insurance.services.team_scope import (
    is_admin, managers_with_teams, team_user_ids, visible_events, visible_people,
)
from insurance.views.team_activity import _safe_cell


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

    def test_the_person_dropdown_and_the_strip_list_only_the_team(self):
        response = self.get(self.lead1)
        self.assertEqual({p.username for p in response.context["people"]}, {"a1", "a2", "junior"})
        self.assertEqual({row["username"] for row in response.context["strip"]}, {"a1", "a2", "junior"})
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

    def test_an_admin_with_a_team_filter_sees_quiet_members_in_the_strip(self):
        make_user("quiet", self.lead1)
        response = self.get(self.make_admin(), {"team": self.lead1.pk})
        self.assertEqual({row["username"] for row in response.context["strip"]}, {"a1", "a2", "junior", "quiet"})

    def test_an_admin_without_a_team_filter_sees_only_people_who_did_something_in_the_strip(self):
        make_user("quiet", self.lead1)
        response = self.get(self.make_admin())
        names = {row["username"] for row in response.context["strip"]}
        self.assertNotIn("quiet", names)
        self.assertIn("a1", names)


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

    def test_the_strip_counts_by_category_lists_quiet_people_and_ignores_the_person_filter(self):
        make_user("quiet", self.lead1)
        record_activity(self.a1, "UPLOAD", "grid.uploaded", "Uploaded a grid")
        response = self.get(self.lead1)
        rows = {row["username"]: row for row in response.context["strip"]}
        self.assertEqual(set(rows), {"a1", "a2", "junior", "quiet"})
        self.assertEqual(rows["a1"]["total"], 2)
        self.assertEqual({cell["code"]: cell["n"] for cell in rows["a1"]["cells"]}, {"RUN": 1, "UPLOAD": 1, "TEST": 0, "STATUS": 0})
        self.assertEqual(rows["quiet"]["total"], 0)
        self.assertIsNone(rows["quiet"]["last"])
        self.assertEqual(response.context["strip"][0]["username"], "a1")          # busiest first
        narrowed = self.get(self.lead1, {"person": self.a2.pk})
        self.assertEqual({row["username"] for row in narrowed.context["strip"]}, {"a1", "a2", "junior", "quiet"})
        self.assertTrue(next(r for r in narrowed.context["strip"] if r["username"] == "a2")["active"])

    def test_the_edit_column_only_appears_once_something_is_recorded_under_it(self):
        self.assertEqual([c for c, _ in self.get(self.lead1).context["strip_columns"]], ["RUN", "UPLOAD", "TEST", "STATUS"])
        record_activity(self.a1, "EDIT", "rate.edited", "Edited a rate")
        self.assertEqual([c for c, _ in self.get(self.lead1).context["strip_columns"]],
                         ["RUN", "UPLOAD", "TEST", "STATUS", "EDIT"])

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


class TeamActivityExportTests(PageTestMixin, TestCase):
    def setUp(self):
        self.build_page_org()

    def rows(self, response):
        return list(load_workbook(io.BytesIO(response.content)).active.iter_rows(values_only=True))

    def test_the_file_has_a_header_the_scoped_rows_and_india_time(self):
        utc = datetime.timezone.utc
        event = ActivityEvent.objects.get(actor=self.a1)
        ActivityEvent.objects.filter(pk=event.pk).update(occurred_at=timezone.now().replace(microsecond=0))
        event.refresh_from_db()
        response = self.get(self.lead1, name="export_team_activity_xlsx")
        self.assertEqual(response["Content-Type"], "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.assertIn("team_activity.xlsx", response["Content-Disposition"])
        rows = self.rows(response)
        self.assertEqual(rows[0], ("Time (IST)", "Username", "Name", "Category", "Action", "Summary", "About", "About id"))
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


class RetentionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username="keeper")

    def make(self, days_old):
        event = record_activity(self.user, "RUN", "test.run", f"{days_old} days old")
        ActivityEvent.objects.filter(pk=event.pk).update(occurred_at=timezone.now() - datetime.timedelta(days=days_old))
        return event

    def test_events_older_than_the_retention_window_are_deleted_and_newer_ones_kept(self):
        old, recent = self.make(91), self.make(89)
        deleted = tasks.cleanup_team_activity()
        self.assertEqual(deleted, 1)
        self.assertFalse(ActivityEvent.objects.filter(pk=old.pk).exists())
        self.assertTrue(ActivityEvent.objects.filter(pk=recent.pk).exists())

    @override_settings(TEAM_ACTIVITY_RETENTION_DAYS=10)
    def test_the_window_is_a_setting(self):
        self.make(11)
        kept = self.make(9)
        self.assertEqual(tasks.cleanup_team_activity(), 1)
        self.assertEqual(list(ActivityEvent.objects.values_list("pk", flat=True)), [kept.pk])

    def test_the_default_window_is_ninety_days(self):
        self.assertEqual(settings.TEAM_ACTIVITY_RETENTION_DAYS, 90)

    def test_the_job_is_scheduled_and_has_a_management_command(self):
        entry = settings.CELERY_BEAT_SCHEDULE["cleanup-team-activity"]
        self.assertEqual(entry["task"], "insurance.tasks.cleanup_team_activity")
        self.assertEqual(tasks.cleanup_team_activity.name, entry["task"])
        self.make(120)
        out = StringIO()
        call_command("cleanup_team_activity", stdout=out)
        self.assertIn("Deleted 1 Team Activity event(s) older than 90 days", out.getvalue())

    def test_the_audit_log_purges_do_not_touch_team_activity(self):
        self.make(400)
        tasks.cleanup_points_search_logs()
        tasks.cleanup_security_audit_logs()
        self.assertEqual(ActivityEvent.objects.count(), 1)
