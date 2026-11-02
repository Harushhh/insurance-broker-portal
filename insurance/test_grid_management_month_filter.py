"""Grid Management's month filter, the toggle that picks which date it is scoped to
(Upload Month vs. Work Effected Date Month), and the redirects that keep or drop those filters."""
import re
from datetime import date, datetime
from unittest import mock

from django.contrib.auth.models import Group, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from insurance.models import GridDocument


def local(year, month, day, hour=12):
    return timezone.make_aware(datetime(year, month, day, hour))


def make_doc(insurer, uploaded, effected=None, status="PENDING"):
    doc = GridDocument.objects.create(
        insurer_name=insurer, uploaded_file="", work_effected_date=effected, status=status,
    )
    # uploaded_date is auto_now_add, so it can only be backdated after the insert.
    GridDocument.objects.filter(pk=doc.pk).update(uploaded_date=uploaded)
    return doc


class GridManagementTestCase(TestCase):
    def setUp(self):
        Group.objects.get_or_create(name="Can_View_Grid_Management")
        user = User.objects.create_user(username="grids", password="a-strong-test-password-1")
        user.groups.add(Group.objects.get(name="Can_View_Grid_Management"))
        self.client.force_login(user)
        self.url = reverse("grid_management")

    def names(self, response):
        return {doc.insurer_name for doc in response.context["documents"]}


class GridManagementMonthBasisTests(GridManagementTestCase):
    """
    Four grids chosen so the two bases disagree about every one of them:

        grid      uploaded     work effected
        Alpha     2026-03-10   2026-04-01
        Bravo     2026-04-05   2026-03-20
        Charlie   2026-03-12   (none)
        Delta     2026-05-02   2026-06-15
    """

    def setUp(self):
        super().setUp()
        make_doc("Alpha", local(2026, 3, 10), date(2026, 4, 1), status="PENDING")
        make_doc("Bravo", local(2026, 4, 5), date(2026, 3, 20), status="DONE")
        make_doc("Charlie", local(2026, 3, 12), None, status="PENDING")
        make_doc("Delta", local(2026, 5, 2), date(2026, 6, 15), status="REJECTED")

    def test_upload_month_is_the_default_basis(self):
        # Links that predate the toggle carry no month_basis and must keep meaning "uploaded in".
        response = self.client.get(self.url, {"month": "2026-03"})
        self.assertEqual(response.context["selected_month_basis"], "upload")
        self.assertEqual(self.names(response), {"Alpha", "Charlie"})

    def test_work_effected_basis_filters_on_the_work_effected_date(self):
        march = self.client.get(self.url, {"month": "2026-03", "month_basis": "work_effected"})
        self.assertEqual(self.names(march), {"Bravo"})
        april = self.client.get(self.url, {"month": "2026-04", "month_basis": "work_effected"})
        self.assertEqual(self.names(april), {"Alpha"})

    def test_a_grid_with_no_work_effected_date_only_shows_under_all_months(self):
        everything = self.client.get(self.url, {"month": "", "month_basis": "work_effected"})
        self.assertEqual(self.names(everything), {"Alpha", "Bravo", "Charlie", "Delta"})
        march = self.client.get(self.url, {"month": "2026-03", "month_basis": "work_effected"})
        self.assertNotIn("Charlie", self.names(march))

    def test_an_unknown_basis_falls_back_to_upload_month(self):
        response = self.client.get(self.url, {"month": "2026-03", "month_basis": "bogus"})
        self.assertEqual(response.context["selected_month_basis"], "upload")
        self.assertEqual(self.names(response), {"Alpha", "Charlie"})

    def test_the_stat_cards_count_the_grids_in_the_chosen_month(self):
        by_upload = self.client.get(self.url, {"month": "2026-03"})
        self.assertEqual(by_upload.context["total_all"], 2)
        self.assertEqual(by_upload.context["status_counts"], {"PENDING": 2})

        by_effected = self.client.get(self.url, {"month": "2026-03", "month_basis": "work_effected"})
        self.assertEqual(by_effected.context["total_all"], 1)
        self.assertEqual(by_effected.context["status_counts"], {"DONE": 1})

    def test_the_status_filter_composes_with_the_basis(self):
        params = {"month": "2026-03", "month_basis": "work_effected"}
        self.assertEqual(self.names(self.client.get(self.url, {**params, "status": "DONE"})), {"Bravo"})
        self.assertEqual(self.names(self.client.get(self.url, {**params, "status": "PENDING"})), set())

    def test_the_dropdown_lists_the_months_of_the_chosen_basis_newest_first(self):
        current = timezone.localdate().replace(day=1)

        by_upload = self.client.get(self.url, {"month": ""})
        self.assertEqual(
            set(by_upload.context["available_months"]),
            {date(2026, 3, 1), date(2026, 4, 1), date(2026, 5, 1), current},
        )

        # Charlie has no work effected date, so it contributes no month here (and no blank entry).
        by_effected = self.client.get(self.url, {"month": "", "month_basis": "work_effected"})
        months = by_effected.context["available_months"]
        self.assertEqual(set(months), {date(2026, 3, 1), date(2026, 4, 1), date(2026, 6, 1), current})
        self.assertEqual(months, sorted(months, reverse=True))

    def test_the_selected_month_stays_in_the_dropdown_even_when_it_has_no_grids(self):
        # May has an upload but nothing taking effect in it: flipping the toggle with May selected
        # must leave the select showing May, not silently fall back to "All Months" while the
        # table is still filtered to May.
        response = self.client.get(self.url, {"month": "2026-05", "month_basis": "work_effected"})
        self.assertEqual(self.names(response), set())
        self.assertIn(date(2026, 5, 1), response.context["available_months"])
        self.assertEqual(response.context["selected_month_str"], "2026-05")

    def test_the_toggle_offers_both_bases_with_the_active_one_checked(self):
        radio = re.compile(r'<input type="radio" name="month_basis"[^>]*value="([^"]+)"([^>]*)>')
        for params, active in (
            ({}, "upload"),
            ({"month_basis": "work_effected"}, "work_effected"),
            ({"month_basis": "bogus"}, "upload"),
        ):
            with self.subTest(params=params):
                html = self.client.get(self.url, params).content.decode()
                radios = [(value, "checked" in rest.split()) for value, rest in radio.findall(html)]
                self.assertEqual(
                    radios, [("upload", active == "upload"), ("work_effected", active == "work_effected")]
                )
        self.assertContains(self.client.get(self.url), "Work Effected Date Month")

    def test_the_stat_card_links_carry_the_basis(self):
        response = self.client.get(self.url, {"month": "2026-03", "month_basis": "work_effected"})
        self.assertContains(response, 'href="?status=all&month=2026-03&month_basis=work_effected"')
        self.assertContains(response, 'href="?status=DONE&month=2026-03&month_basis=work_effected"')

    def test_clear_filters_keeps_the_basis_and_the_empty_message_names_it(self):
        response = self.client.get(self.url, {"month": "2026-05", "month_basis": "work_effected"})
        self.assertContains(response, 'href="?status=all&month=&month_basis=work_effected"')
        self.assertContains(
            response, "No grids match this filter for May 2026 (Work Effected Date Month)."
        )


class GridManagementDefaultMonthTests(GridManagementTestCase):
    def test_a_fresh_page_load_defaults_to_the_current_month_of_the_chosen_basis(self):
        make_doc("Uploaded now", timezone.now(), date(2020, 1, 1))
        make_doc("Effective now", local(2020, 1, 15), timezone.localdate())

        self.assertEqual(self.names(self.client.get(self.url)), {"Uploaded now"})
        self.assertEqual(
            self.names(self.client.get(self.url, {"month_basis": "work_effected"})), {"Effective now"}
        )


class GridManagementRedirectTests(GridManagementTestCase):
    """
    A row's status <select> lives in a form with no action=, so a browser posts it to the page's own
    URL and the page's filters ride along as the query string. The redirect must land back on them.
    """

    def post_status(self, doc, new_status, query=""):
        return self.client.post(
            f"{self.url}{query}", {"action": "update_status", "doc_id": doc.id, "status": new_status}
        )

    def test_a_status_change_returns_to_the_same_filtered_view(self):
        # The URL's status= is the filter card and the posted status is the new value: they must not
        # get mixed up.
        doc = make_doc("Alpha", local(2026, 3, 10), date(2026, 4, 1))
        query = "?status=PENDING&month_basis=work_effected&month=2026-04"
        response = self.post_status(doc, "DONE", query)
        self.assertRedirects(response, f"{self.url}{query}")
        doc.refresh_from_db()
        self.assertEqual(doc.status, "DONE")

    def test_an_explicit_all_months_survives_the_redirect(self):
        # A blank month= is the "All Months" option; leaving it out would mean "the current month".
        doc = make_doc("Alpha", local(2026, 3, 10), date(2026, 4, 1))
        query = "?month=&month_basis=work_effected"
        self.assertRedirects(self.post_status(doc, "DONE", query), f"{self.url}{query}")

    def test_with_no_filters_it_returns_to_the_bare_page(self):
        doc = make_doc("Alpha", local(2026, 3, 10), date(2026, 4, 1))
        self.assertRedirects(self.post_status(doc, "DONE"), self.url)

    @mock.patch("insurance.views.grid_management.threading")
    @mock.patch("insurance.views.grid_management.save_grid_document_file_task")
    def test_an_upload_still_lands_on_the_default_view(self, save_task, threading):
        # Deliberately unlike a status change: the new grid belongs to the current month, and a
        # filtered view (say, October by work effected date) could hide the grid just uploaded.
        response = self.client.post(
            f"{self.url}?month=2026-04&month_basis=work_effected",
            {
                "action": "upload_grid",
                "insurer_name": "Alpha General",
                "remarks": "",
                "work_effected_date": "2026-12-01",
                "uploaded_file": SimpleUploadedFile("grid.xlsx", b"x"),
            },
        )
        self.assertRedirects(response, self.url)
        self.assertEqual(GridDocument.objects.get().insurer_name, "Alpha General")
        save_task.delay.assert_called_once()
