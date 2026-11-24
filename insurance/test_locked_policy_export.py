"""Locked Policy Dashboard (/locked-policy-dashboard/): its Excel export.

The file must hold exactly what the page would show the same person with the same filters, minus the page's cap of
300 rows, in the table's columns, with numbers and dates as real Excel values and text that cannot run as a formula.
"""
import io
import re
from datetime import datetime, timedelta

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook

from insurance.models import FuelTypeMaster, LockedPolicy, ProductMaster, RateGroup, RateMaster
from insurance.views.policy_lock import LOCKED_EXPORT_DATE_FORMAT, LOCKED_EXPORT_HEADERS

SHEET = "Locked Policies"


def aware(*when):
    """An aware datetime in the site's time zone (IST)."""
    return timezone.make_aware(datetime(*when))


class LockedExportCase(TestCase):
    """alice and bob may open the page (so each sees only their own rows), boss is ADMIN, carol has no access."""

    def setUp(self):
        self.page_group = Group.objects.get_or_create(name="Can_View_Locked_Policies")[0]
        self.admin = User.objects.create(username="boss")
        self.admin.groups.add(Group.objects.get_or_create(name="ADMIN")[0])
        self.alice, self.bob, self.carol = (User.objects.create(username=name) for name in ("alice", "bob", "carol"))
        for user in (self.alice, self.bob):
            user.groups.add(self.page_group)

        self.petrol = FuelTypeMaster.objects.create(name="Petrol")
        product = ProductMaster.objects.create(name="Private Car")
        self.group = RateGroup.objects.create(key_hash="locked-export-group")
        self.rate = RateMaster.objects.create(
            group=self.group, product=product, insurance_company="Acme General", status="ACTIVE", is_deleted="NO")
        self.ungrouped_rate = RateMaster.objects.create(
            product=product, insurance_company="Beta General", status="ACTIVE", is_deleted="NO")

    def lock(self, user, vehicle_no, holder="Ravi Kumar", *, rate="default", created=None, **fields):
        """A locked policy with a full set of values (override any through `fields`), made `created` (aware) if given."""
        values = {
            "product_name": "Private Car", "sub_product_name": "1+5", "insurance_company": "Acme General",
            "po_type": "On Net", "po_rate": 12.5, "po_flat_amount": 0.0, "rto_code": "MH12",
            "make_name": "MARUTI SWIFT", "fuel": str(self.petrol.id), "cc": "1197", "sc": "5", "mfg_year": "2020",
            "status": "LOCKED", "locked_at": aware(2026, 11, 20, 14, 30),
        }
        values.update(fields)
        policy = LockedPolicy.objects.create(
            source_rate=self.rate if rate == "default" else rate, vehicle_no=vehicle_no, policy_holder_name=holder,
            locked_by=user, **values)
        LockedPolicy.objects.filter(pk=policy.pk).update(created_at=created or aware(2026, 11, 20, 14, 30))
        return policy

    def get(self, viewer, params=None, name="export_locked_policies_xlsx"):
        self.client.force_login(viewer)
        return self.client.get(reverse(name), params or {})

    @staticmethod
    def read(response):
        """(sheet, header, rows) of the downloaded workbook."""
        ws = load_workbook(io.BytesIO(response.content))[SHEET]
        rows = list(ws.iter_rows(values_only=True))
        return ws, list(rows[0]), rows[1:]

    def vehicles(self, response):
        return [row[1] for row in self.read(response)[2]]


class LockedExportGateTests(LockedExportCase):
    def test_anonymous_visitors_are_sent_to_login(self):
        response = self.client.get(reverse("export_locked_policies_xlsx"))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse("login")))

    def test_someone_without_the_page_group_is_refused(self):
        self.assertEqual(self.get(self.carol).status_code, 403)

    def test_the_page_group_and_admin_are_let_in_and_get_an_excel_file(self):
        for viewer in (self.alice, self.admin):
            with self.subTest(viewer=viewer.username):
                response = self.get(viewer)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Content-Type"],
                                 "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                self.assertIn(f'filename="locked_policies_{timezone.localdate():%Y-%m-%d}.xlsx"',
                              response["Content-Disposition"])
                self.assertEqual(load_workbook(io.BytesIO(response.content)).sheetnames, [SHEET])


class LockedExportContentTests(LockedExportCase):
    def test_the_columns_are_the_pages_columns_in_the_pages_order(self):
        page = self.get(self.admin, name="locked_policy_dashboard").content.decode()
        page_headers = re.findall(r"<th>(.*?)</th>", page)
        unit = re.compile(r"\s*\(.*\)$")                        # the export adds the unit: "PO Rate (%)"
        self.assertEqual([unit.sub("", header) for header in LOCKED_EXPORT_HEADERS], page_headers)
        _, header, rows = self.read(self.get(self.admin))
        self.assertEqual(header, LOCKED_EXPORT_HEADERS)
        self.assertEqual(rows, [])

    def test_a_row_holds_the_values_as_text_numbers_and_dates(self):
        self.lock(self.alice, "MH12AB1234", created=aware(2026, 11, 21, 9, 5), locked_at=aware(2026, 11, 20, 18, 45))
        ws, _, (row,) = self.read(self.get(self.alice))
        values = dict(zip(LOCKED_EXPORT_HEADERS, row))
        self.assertEqual(values["Group ID"], self.group.id)
        self.assertEqual((values["Vehicle No."], values["Policy Holder Name"]), ("MH12AB1234", "Ravi Kumar"))
        self.assertEqual((values["Insurance Company"], values["Product"], values["Sub Product"], values["PO Type"]),
                         ("Acme General", "Private Car", "1+5", "On Net"))
        self.assertEqual((values["PO Rate (%)"], values["PO Flat Amount (₹)"]), (12.5, 0))
        self.assertEqual((values["RTO Code"], values["Make Name"], values["Fuel"]), ("MH12", "MARUTI SWIFT", "Petrol"))
        self.assertEqual((values["Status"], values["Locked By"]), ("LOCKED", "alice"))
        # CC, SC and MFG Year are stored as text; a plain number comes out as a number.
        self.assertEqual((values["CC"], values["SC"], values["MFG Year"]), (1197, 5, 2020))
        self.assertTrue(all(isinstance(values[name], int) for name in ("CC", "SC", "MFG Year")))
        # Times are the IST wall-clock times the page shows, as real Excel dates.
        for name, expected in (("Locked At", datetime(2026, 11, 20, 18, 45)), ("Created At", datetime(2026, 11, 21, 9, 5))):
            self.assertLess(abs(values[name] - expected), timedelta(seconds=1), name)
            column = LOCKED_EXPORT_HEADERS.index(name) + 1
            self.assertEqual(ws.cell(row=2, column=column).number_format, LOCKED_EXPORT_DATE_FORMAT)

    def test_group_id_falls_back_to_the_rates_own_id_and_is_empty_without_a_rate(self):
        self.lock(self.alice, "V-GROUPED")
        self.lock(self.alice, "V-UNGROUPED", rate=self.ungrouped_rate)
        self.lock(self.alice, "V-NO-RATE", rate=None)
        by_vehicle = {row[1]: row[0] for row in self.read(self.get(self.alice))[2]}
        self.assertEqual(by_vehicle, {"V-GROUPED": self.group.id, "V-UNGROUPED": self.ungrouped_rate.id,
                                      "V-NO-RATE": None})

    def test_fuel_is_the_name_the_page_shows_and_other_text_is_kept(self):
        self.lock(self.alice, "V-ID", fuel=str(self.petrol.id))
        self.lock(self.alice, "V-NAME", fuel="Diesel")
        self.lock(self.alice, "V-NONE", fuel="")
        fuel = {row[1]: row[LOCKED_EXPORT_HEADERS.index("Fuel")] for row in self.read(self.get(self.alice))[2]}
        self.assertEqual(fuel, {"V-ID": "Petrol", "V-NAME": "Diesel", "V-NONE": None})

    def test_only_plain_numbers_become_numbers(self):
        self.lock(self.alice, "V-A", cc="1197", sc="5", mfg_year="2020")
        self.lock(self.alice, "V-B", cc="1197-1498", sc="0012", mfg_year="1999.5")
        self.lock(self.alice, "V-C", cc="", sc=None, mfg_year=None)
        cc, sc, year = (LOCKED_EXPORT_HEADERS.index(name) for name in ("CC", "SC", "MFG Year"))
        got = {row[1]: (row[cc], row[sc], row[year]) for row in self.read(self.get(self.alice))[2]}
        self.assertEqual(got["V-A"], (1197, 5, 2020))
        self.assertEqual(got["V-B"], ("1197-1498", "0012", 1999.5))       # a range and leading zeros stay text
        self.assertEqual(got["V-C"], (None, None, None))

    def test_the_newest_lock_is_first_like_the_page(self):
        self.lock(self.alice, "V-OLD", created=aware(2026, 11, 1, 10, 0))
        self.lock(self.alice, "V-NEW", created=aware(2026, 11, 20, 10, 0))
        self.lock(self.alice, "V-MID", created=aware(2026, 11, 10, 10, 0))
        self.assertEqual(self.vehicles(self.get(self.alice)), ["V-NEW", "V-MID", "V-OLD"])

    def test_the_sheet_opens_ready_to_use_a_styled_frozen_header_and_a_filter(self):
        for vehicle in ("V-1", "V-2", "V-3"):
            self.lock(self.alice, vehicle)
        ws, _, rows = self.read(self.get(self.alice))
        self.assertEqual(ws.freeze_panes, "A2")
        self.assertEqual(ws.auto_filter.ref, f"A1:S{1 + len(rows)}")
        self.assertTrue(ws["A1"].font.bold)

    def test_an_export_with_no_matches_is_just_the_header(self):
        ws, header, rows = self.read(self.get(self.alice))
        self.assertEqual((header, rows), (LOCKED_EXPORT_HEADERS, []))
        self.assertEqual(ws.auto_filter.ref, "A1:S1")

    def test_text_that_would_run_as_a_formula_is_defused(self):
        plus = User.objects.create(username="+plus")
        plus.groups.add(self.page_group)
        self.lock(plus, "=1+1", holder="@SUM(A1)", make_name="-cmd", rto_code="+cmd")
        _, _, (row,) = self.read(self.get(plus))
        values = dict(zip(LOCKED_EXPORT_HEADERS, row))
        self.assertEqual((values["Vehicle No."], values["Policy Holder Name"], values["Make Name"], values["RTO Code"]),
                         ("'=1+1", "'@SUM(A1)", "'-cmd", "'+cmd"))
        self.assertEqual(values["Locked By"], "'+plus")


class LockedExportScopeTests(LockedExportCase):
    def setUp(self):
        super().setUp()
        self.lock(self.alice, "ALICE-1", holder="Ravi Kumar", created=aware(2026, 11, 3, 10, 0))
        self.lock(self.alice, "ALICE-2", holder="Sita Rao", insurance_company="Beta General", created=aware(2026, 11, 2, 10, 0))
        self.lock(self.bob, "BOB-1", holder="Ravi Kumar", created=aware(2026, 11, 4, 10, 0))
        self.lock(None, "ORPHAN-1", created=aware(2026, 11, 1, 10, 0))          # its locker's account was deleted

    def test_everyone_but_admin_gets_only_their_own_locks(self):
        self.assertEqual(self.vehicles(self.get(self.alice)), ["ALICE-1", "ALICE-2"])
        self.assertEqual(self.vehicles(self.get(self.bob)), ["BOB-1"])

    def test_admin_gets_every_lock_including_one_with_no_owner(self):
        self.assertEqual(self.vehicles(self.get(self.admin)), ["BOB-1", "ALICE-1", "ALICE-2", "ORPHAN-1"])

    def test_a_forged_locked_by_filter_never_reaches_someone_elses_locks(self):
        for forged in ("bob", "BOB", "boss"):
            with self.subTest(forged=forged):
                self.assertEqual(self.vehicles(self.get(self.alice, {"locked_by_user": forged})), [])
        self.assertEqual(self.vehicles(self.get(self.alice, {"locked_by_user": "alice"})), ["ALICE-1", "ALICE-2"])

    def test_each_filter_narrows_the_file_case_insensitively(self):
        cases = (
            ({"vehicle_no": "alice-1"}, ["ALICE-1"]),
            ({"policy_holder_name": "RAVI KUMAR"}, ["BOB-1", "ALICE-1", "ORPHAN-1"]),
            ({"insurance_company": "beta general"}, ["ALICE-2"]),
            ({"locked_by_user": "BOB"}, ["BOB-1"]),
            ({"policy_holder_name": "ravi kumar", "locked_by_user": "alice"}, ["ALICE-1"]),
            ({"vehicle_no": "nothing-like-this"}, []),
        )
        for params, expected in cases:
            with self.subTest(params=params):
                self.assertEqual(self.vehicles(self.get(self.admin, params)), expected)

    def test_blank_filters_are_ignored(self):
        params = {"vehicle_no": "", "policy_holder_name": " ", "insurance_company": "", "locked_by_user": ""}
        self.assertEqual(len(self.vehicles(self.get(self.admin, params))), 4)

    def test_a_filter_is_an_exact_match_not_a_search(self):
        self.assertEqual(self.vehicles(self.get(self.admin, {"vehicle_no": "ALICE"})), [])      # same rule as the page


class LockedExportMatchesPageTests(LockedExportCase):
    def test_the_file_and_the_table_hold_the_same_records_for_the_same_filters(self):
        self.lock(self.alice, "A-1", holder="Ravi Kumar", created=aware(2026, 11, 3, 10, 0))
        self.lock(self.alice, "A-2", holder="Sita Rao", insurance_company="Beta General", created=aware(2026, 11, 2, 10, 0))
        self.lock(self.bob, "B-1", holder="Ravi Kumar", created=aware(2026, 11, 4, 10, 0))
        for viewer in (self.alice, self.admin):
            for params in ({}, {"policy_holder_name": "ravi kumar"}, {"insurance_company": "Beta General"},
                           {"locked_by_user": "bob"}):
                with self.subTest(viewer=viewer.username, params=params):
                    page = self.get(viewer, params, name="locked_policy_dashboard")
                    self.assertEqual([row.vehicle_no for row in page.context["records"]],
                                     self.vehicles(self.get(viewer, params)))
                    self.assertEqual(page.context["total_records"], len(self.vehicles(self.get(viewer, params))))

    def test_the_page_stops_at_300_records_but_the_file_has_them_all(self):
        LockedPolicy.objects.bulk_create([
            LockedPolicy(vehicle_no=f"V{number:04d}", policy_holder_name="Ravi Kumar", locked_by=self.alice, status="LOCKED")
            for number in range(305)
        ])
        page = self.get(self.alice, name="locked_policy_dashboard")
        self.assertEqual((len(page.context["records"]), page.context["total_records"]), (300, 305))
        self.assertEqual(len(self.vehicles(self.get(self.alice))), 305)


class LockedExportButtonTests(LockedExportCase):
    def test_the_page_offers_the_export_and_the_link_carries_the_applied_filters(self):
        export = reverse("export_locked_policies_xlsx")
        plain = self.get(self.alice, name="locked_policy_dashboard")
        self.assertContains(plain, "Export Excel")
        self.assertContains(plain, f'href="{export}"')
        self.assertEqual(plain.context["export_query"], "")

        filtered = self.get(self.alice, {"vehicle_no": "MH12AB1234", "insurance_company": "Acme General", "locked_by_user": ""},
                            name="locked_policy_dashboard")
        self.assertEqual(filtered.context["export_query"], "vehicle_no=MH12AB1234&insurance_company=Acme+General")
        self.assertContains(filtered, f'href="{export}?vehicle_no=MH12AB1234&amp;insurance_company=Acme+General"')

    def test_the_link_opens_exactly_what_the_table_is_filtered_to(self):
        self.lock(self.alice, "A-1", holder="Ravi Kumar")
        self.lock(self.alice, "A-2", holder="Sita Rao")
        page = self.get(self.alice, {"policy_holder_name": "Sita Rao"}, name="locked_policy_dashboard")
        link = re.search(r'href="(/locked-policy-dashboard/export/[^"]*)"', page.content.decode()).group(1)
        downloaded = self.client.get(link.replace("&amp;", "&"))
        self.assertEqual(self.vehicles(downloaded), ["A-2"])

    def test_the_dashboard_still_shows_the_same_dropdowns_and_scoping(self):
        self.lock(self.alice, "A-1")
        self.lock(self.bob, "B-1")
        page = self.get(self.alice, name="locked_policy_dashboard")
        self.assertEqual(page.context["unique_vehicles"], ["A-1"])
        self.assertEqual(page.context["unique_users"], ["alice"])
        self.assertEqual(set(page.context["selected"]),
                         {"vehicle_no", "policy_holder_name", "insurance_company", "locked_by_user"})
