"""Rate Master Matrix (/dashboard/): the "Rows per page" choices and what the server does with ?page_size=."""
import re

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from insurance.models import AuditLog, ProductMaster, RateGroup, RateMaster
from insurance.views.rate_master import DASHBOARD_BATCH_SIZE

# Enough rate groups that a page of 500 fills up and a short second page is left over.
GROUP_COUNT = 520


class DashboardPageSizeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Group.objects.get_or_create(name="Can_View_Dashboard")
        cls.user = User.objects.create_user(username="ops", password="a-strong-test-password-1")
        cls.user.groups.add(Group.objects.get(name="Can_View_Dashboard"))

        product = ProductMaster.objects.create(name="Private Car")
        RateGroup.objects.bulk_create([RateGroup(key_hash=f"page-size-{n}") for n in range(GROUP_COUNT)])
        RateMaster.objects.bulk_create([
            RateMaster(
                insurance_company="Acme General", product=product, status="ACTIVE", is_deleted="NO", group=group,
            )
            for group in RateGroup.objects.filter(key_hash__startswith="page-size-")
        ])

    def setUp(self):
        self.client.force_login(self.user)

    def get(self, **params):
        # dashboard() skips its real query until two filters are set, so these two are always sent.
        return self.client.get(reverse("dashboard"), {"status": "ACTIVE", "is_deleted": "NO", **params})

    def page_size_select(self, response):
        match = re.search(r'<select id="pageSizeSelect".*?</select>', response.content.decode(), re.S)
        self.assertIsNotNone(match, "the Rows per page dropdown is not on the page")
        return match.group(0)

    def test_the_dropdown_offers_50_100_200_and_500(self):
        response = self.get()
        self.assertEqual(re.findall(r'<option value="(\d+)"', self.page_size_select(response)),
                         ["50", "100", "200", "500"])

    def test_page_size_500_returns_a_page_of_500_and_the_rest_on_page_2(self):
        first = self.get(page_size=500)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(len(first.context["data"]), 500)
        self.assertEqual(first.context["total"], GROUP_COUNT)
        self.assertEqual(first.context["page_obj"].paginator.num_pages, 2)
        self.assertEqual(first.context["selected"]["page_size"], 500)
        self.assertIn('<option value="500" selected>500</option>', self.page_size_select(first))

        second = self.get(page_size=500, page=2)
        self.assertEqual(len(second.context["data"]), GROUP_COUNT - 500)

    def test_the_pager_counts_pages_of_500_and_keeps_the_size_when_moving_on(self):
        html = self.get(page_size=500).content.decode()
        next_link = re.search(r'<a href="([^"]*)">Next', html)
        last_link = re.search(r'<a href="([^"]*)">Last', html)
        self.assertIsNotNone(next_link)
        self.assertIsNotNone(last_link)
        # 520 groups at 500 a page is two pages (at 50 a page "Last" would say page=11)...
        self.assertTrue(next_link.group(1).endswith("page=2"))
        self.assertTrue(last_link.group(1).endswith("page=2"))
        # ...and both links keep the size, so page 2 is not silently back to 50 a page.
        self.assertIn("page_size=500", next_link.group(1))
        self.assertIn("page_size=500", last_link.group(1))

    def test_the_default_is_still_50(self):
        self.assertEqual(DASHBOARD_BATCH_SIZE, 50)
        for params in ({}, {"page_size": ""}):
            with self.subTest(params=params):
                response = self.get(**params)
                self.assertEqual(response.context["selected"]["page_size"], 50)
                self.assertEqual(len(response.context["data"]), 50)

    def test_a_size_that_is_not_offered_falls_back_to_50(self):
        # 501 is the one just past the new cap; the rest are malformed or tampered values.
        for bad_value in ("501", "1000", "0", "-50", "abc", "500.5", "50 0"):
            with self.subTest(page_size=bad_value):
                response = self.get(page_size=bad_value)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["selected"]["page_size"], 50)
                self.assertEqual(len(response.context["data"]), 50)

    def test_the_existing_sizes_still_work(self):
        for size in (50, 100, 200):
            with self.subTest(page_size=size):
                response = self.get(page_size=size)
                self.assertEqual(response.context["selected"]["page_size"], size)
                self.assertEqual(len(response.context["data"]), size)

    def test_a_full_page_of_500_can_be_ticked_and_bulk_updated(self):
        # The bulk form posts one selected_groups field per ticked row. 500 of them have to stay under
        # Django's DATA_UPLOAD_MAX_NUMBER_FIELDS (1000), or "tick all, Apply Update" would be a 400.
        html = self.get(page_size=500).content.decode()
        ticked = re.findall(r'name="selected_groups" value="(\d+)"', html)
        self.assertEqual(len(ticked), 500)

        response = self.client.post(reverse("bulk_update_rates"), {
            "selected_groups": ticked, "update_field": "status", "update_value": "INACTIVE",
        })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(RateMaster.objects.filter(status="INACTIVE").count(), 500)
        self.assertEqual(RateMaster.objects.filter(status="ACTIVE").count(), GROUP_COUNT - 500)
        self.assertTrue(AuditLog.objects.filter(action="BULK UPDATE", details__startswith="Updated 500 rows").exists())
