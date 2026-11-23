"""The Life grid as one flat sheet for a dashboard (services/life_dashboard.py and the Dashboard tab of the Life Rate
Master): every payout is a row, no cell is empty, and nothing is made up to make it so."""
import io

from django.test import TestCase
from django.urls import reverse
from openpyxl import load_workbook

from insurance.models import LifeGridUpload, LifeRate
from insurance.services.life_dashboard import COLUMNS, SHEET_NAME, dashboard_data, dashboard_workbook
from insurance.test_life_standard_file import LifeStandardViewMixin


def _upload(label="Oct'26", rows=(), live=True):
    upload = LifeGridUpload.objects.create(label=label, is_active=live, row_count=len(rows))
    for position, values in enumerate(rows):
        LifeRate.objects.create(upload=upload, position=position, **{"insurer": "Acme Life", **values})
    return upload


def _row(product="Alpha Plan", ppt="10", y1=20.0, **extra):
    return {"product": product, "ppt": ppt, "payout_yr1": y1, **extra}


def _no_empty_cells(test, rows):
    test.assertTrue(rows)
    for row in rows:
        test.assertEqual(len(row), len(COLUMNS))
        for value in row:
            test.assertTrue(value is not None and value != "", row)


class DashboardDataTests(TestCase):
    def test_every_payout_is_a_row_with_no_empty_cell_and_a_year_the_grid_does_not_give_has_no_row(self):
        upload = _upload(rows=[_row(payout_yr1=41.0, payout_yr2=2.5, payout_yr6_plus=1.0, mg_yr1=55.0, mg_yr2=5.0)])
        data = dashboard_data(upload)
        _no_empty_cells(self, data.rows)
        got = [(r[6], r[7], r[8], r[9]) for r in data.rows]
        self.assertEqual(got, [("BG", "Year 1", 1, 41.0), ("MG", "Year 1", 1, 55.0), ("BG", "Year 2", 3, 2.5),
                               ("MG", "Year 2", 3, 5.0), ("BG", "Year 6+", 7, 1.0)])
        self.assertEqual((data.plans, data.insurers, data.bg_rows, data.mg_rows), (1, 1, 3, 2))
        self.assertEqual(data.rows[0][:6], ("Oct'26", LifeRate.objects.get().pk, "Acme Life", "Alpha Plan", "Alpha Plan", "10"))

    def test_a_deferred_part_is_its_own_year_between_year_1_and_year_2(self):
        data = dashboard_data(_upload(rows=[_row(payout_deferred=0.0, payout_yr2=2.0)]))
        self.assertEqual([(r[7], r[8], r[9]) for r in data.rows],
                         [("Year 1", 1, 20.0), ("At 2nd year", 2, 0.0), ("Year 2", 3, 2.0)])      # a stored 0 is a payout of 0

    def test_the_plan_name_merges_what_tells_rows_of_one_product_apart(self):
        data = dashboard_data(_upload(rows=[
            _row("SRP 2 Pay", "2", condition="Offline"), _row("SRP 2 Pay", "2", condition="Digital discount"),
            _row("Plan A", "6", variant="Option 1", condition="Base SA <= 20X", section="Renewal", pt="15/20"),
            _row("Plan B", "8", pt="12")]))
        names = [r[4] for r in data.rows]
        self.assertEqual(names, ["SRP 2 Pay | Offline", "SRP 2 Pay | Digital discount",
                                 "Plan A | Option 1 | Base SA <= 20X | Renewal | PT 15/20", "Plan B | PT 12"])
        self.assertEqual([r[3] for r in data.rows], ["SRP 2 Pay", "SRP 2 Pay", "Plan A", "Plan B"])      # product stays plain

    def test_two_plans_that_would_share_a_name_get_their_row_id(self):
        upload = _upload(rows=[_row(category="Par", y1=30.0), _row(category="Non Par", y1=40.0), _row("Other")])
        first, second, other = LifeRate.objects.filter(upload=upload).order_by("position")
        data = dashboard_data(upload)
        self.assertEqual({r[4] for r in data.rows}, {f"Alpha Plan | row {first.pk}", f"Alpha Plan | row {second.pk}", "Other"})
        self.assertEqual(data.renamed, 2)
        self.assertEqual(len({(r[2], r[4], r[5]) for r in data.rows}), 3)                        # unique now

    def test_a_row_that_is_only_a_written_rule_is_left_out_and_says_what_it_said(self):
        data = dashboard_data(_upload(rows=[_row(), _row("Bonus Commission", ppt="Excluding Single Premium Plans", y1=None,
                                                         remarks="Year 1: 30% of 1st year commission.")]))
        self.assertEqual(data.plans, 1)
        self.assertEqual([(i["reason"], i["plan"]) for i in data.left_out], [("No payout number", "Bonus Commission")])
        self.assertIn("Year 1: 30% of 1st year commission.", data.left_out[0]["detail"])

    def test_a_plan_with_no_ppt_is_left_out_with_its_payouts_listed(self):
        data = dashboard_data(_upload(rows=[_row(), _row("IPG", ppt="", y1=41.0, payout_yr2=0.5, mg_yr1=55.0)]))
        self.assertEqual(data.plans, 1)
        item = data.left_out[0]
        self.assertEqual((item["reason"], item["plan"]), ("No PPT stated", "IPG"))
        self.assertIn("BG Year 1 41%", item["detail"])
        self.assertIn("BG Year 2 0.5%", item["detail"])
        self.assertIn("MG Year 1 55%", item["detail"])

    def test_a_blank_ppt_is_restated_from_a_variant_that_reads_as_a_pay_term(self):
        upload = _upload(rows=[_row("Digit Glow Plus", ppt="", variant="Ltd pay 5", section="Digit ICON / Glow Plus"),
                               _row("Digit Glow Plus", ppt="", variant="Regular Pay", section="Digit ICON / Glow Plus"),
                               _row("Some Annuity", ppt="", variant="Immediate")])
        data = dashboard_data(upload)
        self.assertEqual([(r[5], r[4]) for r in data.rows],
                         [("Ltd pay 5", "Digit Glow Plus | Digit ICON / Glow Plus"),
                          ("Regular Pay", "Digit Glow Plus | Digit ICON / Glow Plus")])
        self.assertEqual(len(data.restated), 2)
        self.assertIn("'Ltd pay 5' was in the Variant column; it is now the PPT.", data.restated[0]["detail"])
        self.assertEqual([i["plan"] for i in data.left_out], ["Some Annuity | Immediate"])        # "Immediate" is not a pay term

    def test_a_blank_ppt_is_restated_from_a_range_in_the_product_name_but_not_from_other_numbers(self):
        upload = _upload(rows=[_row("ILP 7  - 9 Param Raksha Life Pro", ppt=""),
                               _row("Pure Term - Plan Option 1 to 7", ppt=""), _row("Guaranteed Income 20", ppt="")])
        data = dashboard_data(upload)
        self.assertEqual([(r[3], r[5]) for r in data.rows], [("ILP 7 - 9 Param Raksha Life Pro", "7-9")])
        self.assertIn("The product name states the pay term (7 - 9)", data.restated[0]["detail"])
        self.assertEqual({i["reason"] for i in data.left_out}, {"No PPT stated"})
        self.assertEqual(len(data.left_out), 2)

    def test_a_ppt_that_is_there_is_never_restated(self):
        data = dashboard_data(_upload(rows=[_row("ILP 7 - 9 Param", ppt="5"), _row("Plan", ppt="10", variant="Ltd pay 5")]))
        self.assertEqual(data.restated, [])
        self.assertEqual([r[5] for r in data.rows], ["5", "10"])

    def test_bg_only_leaves_the_mg_rows_out_and_a_plan_with_only_an_mg_out_too(self):
        upload = _upload(rows=[_row(mg_yr1=30.0, mg_yr2=3.0), _row("Mg Only", y1=None, mg_yr1=9.0)])
        both, bg_only = dashboard_data(upload), dashboard_data(upload, include_mg=False)
        self.assertEqual((both.bg_rows, both.mg_rows, both.plans), (1, 3, 2))
        self.assertEqual((bg_only.bg_rows, bg_only.mg_rows, bg_only.plans), (1, 0, 1))
        self.assertEqual({r[6] for r in bg_only.rows}, {"BG"})
        self.assertEqual([(i["plan"], i["reason"]) for i in bg_only.left_out], [("Mg Only", "No BG payout")])

    def test_a_row_with_no_product_is_left_out_and_never_named_for_it(self):
        data = dashboard_data(_upload(rows=[_row(product="", ppt="5")]))
        self.assertEqual((data.plans, [i["reason"] for i in data.left_out]), (0, ["No product name"]))

    def test_the_rows_follow_the_grid_then_the_year_with_bg_before_mg(self):
        data = dashboard_data(_upload(rows=[_row("Second", mg_yr1=1.0), _row("First", mg_yr1=2.0)]))
        self.assertEqual([(r[3], r[6]) for r in data.rows], [("Second", "BG"), ("Second", "MG"), ("First", "BG"), ("First", "MG")])

    def test_text_is_tidied_and_only_the_chosen_upload_is_read(self):
        old = _upload("Sep'26", [_row("Old Plan")], live=False)
        live = _upload("Oct'26", [_row("  Alpha   Plan ", ppt=" 10 ")])
        self.assertEqual([(r[0], r[3], r[5]) for r in dashboard_data(live).rows], [("Oct'26", "Alpha Plan", "10")])
        self.assertEqual([r[0] for r in dashboard_data(old).rows], ["Sep'26"])


class DashboardWorkbookTests(TestCase):
    def test_one_sheet_one_table_the_columns_in_order_typed_values_and_no_empty_cell(self):
        wb = dashboard_workbook(dashboard_data(_upload(rows=[_row(mg_yr1=30.0, payout_yr2=2.5), _row("Beta", "5", y1=10.0)])))
        self.assertEqual(wb.sheetnames, [SHEET_NAME])
        ws = wb[SHEET_NAME]
        self.assertEqual([c.value for c in ws[1]], list(COLUMNS))
        self.assertEqual(ws.max_row, 1 + 4)                                          # header + Alpha (BG, MG, BG) + Beta (BG)
        self.assertEqual(len(ws.tables), 1)
        self.assertEqual(ws.freeze_panes, "A2")
        for row in ws.iter_rows(min_row=2):
            self.assertTrue(all(c.value is not None and c.value != "" for c in row))
            self.assertIsInstance(row[1].value, int)                                              # plan_id
            self.assertIsInstance(row[8].value, int)                                              # year_order
            self.assertIsInstance(row[9].value, float)                                            # payout_pct

    def test_the_saved_file_reads_back_to_the_same_rows(self):
        data = dashboard_data(_upload(rows=[_row(mg_yr1=30.0, payout_yr2=2.5)]))
        out = io.BytesIO()
        dashboard_workbook(data).save(out)
        ws = load_workbook(io.BytesIO(out.getvalue()))[SHEET_NAME]
        self.assertEqual([tuple(c.value for c in row) for row in ws.iter_rows(min_row=2)], data.rows)

    def test_an_upload_with_nothing_to_hold_gives_a_header_only_sheet_without_a_table(self):
        ws = dashboard_workbook(dashboard_data(_upload(rows=[_row(y1=None)])))[SHEET_NAME]
        self.assertEqual((ws.max_row, len(ws.tables)), (1, 0))


class DashboardViewTests(LifeStandardViewMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.upload = self.live(rows=[_row(mg_yr1=30.0, payout_yr2=2.5), _row("Beta", "5", y1=10.0),
                                      _row("IPG", ppt="", y1=41.0),
                                      _row("Bonus Commission", ppt="All", y1=None, remarks="Year 1: 30% of 1st year commission.")])

    def test_the_tab_shows_what_the_sheet_holds_and_what_it_left_out(self):
        response = self.client.get(reverse("life_rate_master_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "One sheet for your dashboard")
        self.assertContains(response, reverse("life_rate_master_dashboard_download"))
        self.assertContains(response, "Download BG only")
        self.assertContains(response, "Every month")
        for column in COLUMNS:
            self.assertContains(response, f"<code>{column}</code>")
        data = response.context["data"]
        self.assertEqual((data.plans, data.bg_rows, data.mg_rows, len(data.left_out)), (2, 3, 1, 2))
        self.assertContains(response, "No PPT stated")
        self.assertContains(response, "No payout number")
        self.assertContains(response, "Year 1: 30% of 1st year commission.")
        self.assertContains(response, 'class="life-subtab active"')                           # the Dashboard tab is the active one

    def test_the_download_is_one_sheet_with_the_whole_live_grid(self):
        response = self.client.get(reverse("life_rate_master_dashboard_download"))
        self.assertEqual(response["Content-Type"], "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.assertEqual(response["Content-Disposition"], 'attachment; filename="life_dashboard_Oct26.xlsx"')
        wb = load_workbook(io.BytesIO(response.content))
        self.assertEqual(wb.sheetnames, [SHEET_NAME])
        rows = [[c.value for c in row] for row in wb[SHEET_NAME].iter_rows(min_row=2)]
        self.assertEqual(len(rows), 4)
        self.assertEqual({r[6] for r in rows}, {"BG", "MG"})
        self.assertTrue(all(v is not None for r in rows for v in r))

    def test_bg_only_download_has_no_mg_row_and_says_so_in_its_name(self):
        response = self.client.get(reverse("life_rate_master_dashboard_download"), {"mg": "0"})
        self.assertEqual(response["Content-Disposition"], 'attachment; filename="life_dashboard_Oct26_BG.xlsx"')
        ws = load_workbook(io.BytesIO(response.content))[SHEET_NAME]
        self.assertEqual({row[6].value for row in ws.iter_rows(min_row=2)}, {"BG"})

    def test_another_upload_can_be_chosen_and_the_page_says_it_is_not_live(self):
        old = LifeGridUpload.objects.create(label="Sep'26")
        LifeRate.objects.create(upload=old, insurer="Acme Life", product="Old Plan", ppt="7", payout_yr1=9.0)
        response = self.client.get(reverse("life_rate_master_dashboard"), {"upload": old.pk})
        self.assertContains(response, "Sep&#x27;26")
        self.assertContains(response, "an earlier upload")
        self.assertContains(response, f"?upload={old.pk}")
        download = self.client.get(reverse("life_rate_master_dashboard_download"), {"upload": old.pk})
        self.assertEqual(download["Content-Disposition"], 'attachment; filename="life_dashboard_Sep26.xlsx"')
        ws = load_workbook(io.BytesIO(download.content))[SHEET_NAME]
        self.assertEqual([row[3].value for row in ws.iter_rows(min_row=2)], ["Old Plan"])

    def test_nothing_uploaded_and_nothing_exportable(self):
        LifeGridUpload.objects.all().delete()
        self.assertContains(self.client.get(reverse("life_rate_master_dashboard")), "Nothing to export yet")
        response = self.client.get(reverse("life_rate_master_dashboard_download"))
        self.assertRedirects(response, reverse("life_rate_master_uploads"), fetch_redirect_response=False)
        empty = LifeGridUpload.objects.create(label="Empty", is_active=True)
        LifeRate.objects.create(upload=empty, insurer="Acme Life", product="Rule", ppt="5", remarks="a rule")
        response = self.client.get(reverse("life_rate_master_dashboard_download"), follow=True)
        self.assertRedirects(response, reverse("life_rate_master_dashboard"))
        self.assertContains(response, "has no payout rows the sheet can hold")

    def test_only_the_rate_master_group_may_open_it_and_the_uploads_tab_links_to_it(self):
        self.client.force_login(self.viewer)
        for name in ("life_rate_master_dashboard", "life_rate_master_dashboard_download"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 403, name)
        self.client.force_login(self.manager)
        self.assertContains(self.client.get(reverse("life_rate_master_uploads")), reverse("life_rate_master_dashboard"))
        self.assertContains(self.client.get(reverse("life_rate_master")), reverse("life_rate_master_dashboard"))
