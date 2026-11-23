"""Life MG: the Final MG beside each BG payout is read from the workbook (this file), stored with it, shown on the Life
Rate Master and never shown on the Rate Checker.

The reader is tested on small workbooks laid out like the real sheets (same anchor headers, same column letters); the
real workbook is not in the repository, so its Final MG columns were checked against it by comparing every row's BG with
the MG it comes from (BG is 75 % of MG in the first year and about 50 % in the renewals, on every sheet).
"""
import io
import re

from django.http import QueryDict
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from openpyxl import load_workbook

from insurance.models import AuditLog, LifeGridUpload, LifeRate
from insurance.services.life_rates import EXPORT_COLUMNS, life_rate_snapshot, save_life_upload, search_life_rates, selected_life_filters
from insurance.services.life_workbook import MG_FIELDS, SPEC_BY_NAME, read_life_workbook
from insurance.test_life_rates import LifeAccessMixin                        # a mixin (users and groups), not a test case
from insurance.test_life_rates import _bajaj, _edelweiss, _lic, _workbook   # the small-workbook helpers, not the tests


def _bajaj_with_mg(wb):
    ws = _bajaj(wb)
    for row, (first, second) in enumerate([(0.4733, 0.05), (0.52, 0.05), (0.6667, None)], start=4):
        ws[f"R{row}"] = first                          # Final MG, Fresh (the BG one is AF)
        if second is not None:
            ws[f"T{row}"] = second                     # Final MG, Year 2 (the BG one is AH)
    return ws


def _lic_with_mg(wb):
    ws = _lic(wb)
    ws["C5"], ws["D5"], ws["E5"], ws["F5"] = 20, 7.5, 7.5, 5          # Final MG: LIC writes whole percents
    return ws


def _tata_with_mg(wb):
    ws = wb.create_sheet("TATA")
    ws["X3"], ws["Y3"], ws["Z3"], ws["AA3"], ws["AB3"] = "Product", "Share at Issuance", "At 2nd Year", "Share", "At 2nd"
    ws["X4"] = "Protection"                                           # a heading row: it sets the category
    ws["X5"], ws["B5"], ws["C5"] = "SRP 2 Pay - Life Option", 0.5, 0.45
    ws["Y5"], ws["Z5"], ws["AA5"], ws["AB5"] = 0.3, 0, 0.27, 0       # BG: offline year 1 / 2nd year, digital year 1 / 2nd
    ws["P5"], ws["Q5"], ws["S5"], ws["T5"] = 0.4, 0.05, 0.36, 0.03   # Final MG of the same four
    return ws


def _pnb_with_mg(wb):
    ws = wb.create_sheet("PNB")
    ws["A2"], ws["B2"], ws["C2"] = "Product Name", "PPT", "Annualized Premium"
    ws["A3"], ws["B3"], ws["K3"] = "Century Plan", "5 Pay", "Till end of PPT"
    ws["T3"], ws["W3"], ws["X3"] = 0.29, 0.03, 0.015                  # BG: total year 1, total year 2, commission year 3
    ws["F3"], ws["I3"], ws["J3"] = 0.3898, 0.0678, 0.0339             # Final MG of the same three (14 columns left)
    return ws


def _india_first_with_mg(wb):
    ws = wb.create_sheet("India_First")
    ws["A2"] = "Products"
    ws["A3"], ws["B3"], ws["C3"], ws["D3"], ws["J3"] = "IndiaFirst Plan", 5, 10, 0.25, 0.19      # MG D, BG J (6 apart)
    ws["A6"] = "Products"                                              # the renewal table: its MG block is 15 apart
    ws["A8"], ws["B8"] = "IndiaFirst Plan", "PPT - 5 Yrs"
    for column in "DEFGH":
        ws[f"{column}8"] = 0.0085                                      # Final MG, Year 2 .. Year 6
    for column in ("S", "T", "U", "V", "W"):
        ws[f"{column}8"] = 0.005                                       # BG, Year 2 .. Year 6
    return ws


class LifeMgReaderTests(SimpleTestCase):
    def test_the_final_mg_is_read_beside_each_bg_payout(self):
        rows = [r for r in read_life_workbook(_workbook(_edelweiss)).rows if r["insurer"] == "Edelweiss Life"]
        self.assertEqual([r["y1"] for r in rows], [20.0, 28.0, 55.0])                         # BG, as before
        self.assertEqual([r["m_y1"] for r in rows], [30.0, 38.0, 65.0])                       # the MG it comes from
        for r in rows:
            self.assertEqual([r[name] for name in MG_FIELDS[1:]], [None] * 6)
            self.assertNotIn(10.0, [r["m_y1"]])                                              # the 0.1 working column

    def test_every_row_has_all_seven_mg_keys(self):
        for r in read_life_workbook(_workbook(_edelweiss)).rows:
            self.assertTrue(all(name in r for name in MG_FIELDS))

    def test_mg_by_year_on_a_wide_sheet(self):
        rows = [r for r in read_life_workbook(_workbook(_bajaj_with_mg)).rows if r["insurer"] == "Bajaj Allianz Life"]
        self.assertEqual([r["m_y1"] for r in rows], [47.33, 52.0, 66.67])
        self.assertEqual([r["m_y2"] for r in rows], [5.0, 5.0, None])
        self.assertEqual([r["y2"] for r in rows], [2.5, 2.5, None])                          # the BG next to it

    def test_a_row_without_an_mg_cell_has_a_blank_mg_and_keeps_its_bg(self):
        rows = [r for r in read_life_workbook(_workbook(_bajaj)).rows if r["insurer"] == "Bajaj Allianz Life"]
        self.assertEqual([r["y1"] for r in rows], [35.5, 39.0, 50.0])
        self.assertTrue(all(r[name] is None for r in rows for name in MG_FIELDS))

    def test_lic_writes_whole_percents_for_both_parts(self):
        rows = [r for r in read_life_workbook(_workbook(_lic_with_mg)).rows if r["insurer"] == "LIC of India"]
        first = rows[0]
        self.assertEqual((first["y1"], first["y2"], first["y6"]), (15.0, 3.75, 2.5))
        self.assertEqual((first["m_y1"], first["m_y2"], first["m_y3"], first["m_y4"], first["m_y5"], first["m_y6"]),
                         (20.0, 7.5, 7.5, 7.5, 7.5, 5.0))
        self.assertEqual(rows[1]["m_y1"], 20.0)                                              # the same cell, a second plan

    def test_tata_offline_and_digital_rows_each_get_their_own_mg(self):
        rows = [r for r in read_life_workbook(_workbook(_tata_with_mg)).rows if r["insurer"] == "Tata AIA Life"]
        offline = next(r for r in rows if r["condition"] == "Offline")
        digital = next(r for r in rows if r["condition"] == "Digital discount")
        self.assertEqual((offline["y1"], offline["d"], offline["m_y1"], offline["m_d"]), (30.0, 0.0, 40.0, 5.0))
        self.assertEqual((digital["y1"], digital["d"], digital["m_y1"], digital["m_d"]), (27.0, 0.0, 36.0, 3.0))
        self.assertEqual(offline["category"], "Protection")

    def test_pnb_year_three_mg_follows_the_years_the_bg_covers(self):
        rows = [r for r in read_life_workbook(_workbook(_pnb_with_mg)).rows if r["insurer"] == "PNB MetLife"]
        row = rows[0]
        self.assertEqual((row["y1"], row["y2"], row["y3"], row["y6"]), (29.0, 3.0, 1.5, 1.5))
        self.assertEqual((row["m_y1"], row["m_y2"], row["m_y3"], row["m_y6"]), (38.98, 6.78, 3.39, 3.39))

    def test_a_table_can_have_its_own_mg_offset(self):
        rows = [r for r in read_life_workbook(_workbook(_india_first_with_mg)).rows if r["insurer"] == "IndiaFirst Life"]
        first = next(r for r in rows if r["y1"] is not None)
        renewal = next(r for r in rows if r["y2"] is not None)
        self.assertEqual((first["y1"], first["m_y1"]), (19.0, 25.0))                         # 6 columns apart
        self.assertEqual([renewal[f"y{n}"] for n in range(2, 7)], [0.5] * 5)
        self.assertEqual([renewal[f"m_y{n}"] for n in range(2, 7)], [0.85] * 5)             # 15 columns apart

    def test_the_mg_columns_of_the_specs(self):
        self.assertEqual(SPEC_BY_NAME["Bajaj"].tables[0].mg["AF"], "R")            # Fresh: BG AF, MG R (14 apart)
        self.assertEqual(SPEC_BY_NAME["Kotak"].tables[0].mg["P"], "E")             # 11 apart
        self.assertEqual(SPEC_BY_NAME["India_First"].tables[0].mg["J"], "D")
        self.assertEqual(SPEC_BY_NAME["India_First"].tables[1].mg["S"], "D")       # the renewal table: 15 apart
        self.assertEqual(SPEC_BY_NAME["TATA"].tables[0].mg, {"Y": "P", "Z": "Q", "AA": "S", "AB": "T"})
        self.assertEqual(SPEC_BY_NAME["Pramerica"].tables[0].mg, {"AP": "Y", "AQ": "Z"})   # total, then the renewal
        for spec in SPEC_BY_NAME.values():
            for table in spec.tables:
                self.assertTrue(table.mg, f"{spec.name}: no Final MG columns for a table")
                self.assertEqual(set(table.mg) - set(table.payouts), set(), spec.name)


class LifeMgStorageTests(TestCase):
    def test_an_upload_stores_the_final_mg_beside_the_bg(self):
        upload = save_life_upload(read_life_workbook(_workbook(_edelweiss)), label="Oct'26", file_name="x.xlsx", user=None)
        rows = list(LifeRate.objects.filter(upload=upload).order_by("position"))
        self.assertEqual([(r.mg_yr1, r.payout_yr1) for r in rows], [(30.0, 20.0), (38.0, 28.0), (65.0, 55.0)])
        self.assertEqual([r.mg_yr2 for r in rows], [None] * 3)

    def test_the_model_names_the_mg_fields_like_the_payouts(self):
        self.assertEqual(LifeRate.MG_FIELDS, [name.replace("payout_", "mg_") for name in LifeRate.PAYOUT_FIELDS])


class LifeMgScreenTests(LifeAccessMixin, TestCase):
    """MG beside BG on the Life Rate Master (edit, add, bulk edit, export, audit); never on the Rate Checker."""

    def setUp(self):
        super().setUp()
        self.upload = LifeGridUpload.objects.create(label="Oct'26", is_active=True, row_count=2, insurer_count=1)
        self.row = LifeRate.objects.create(
            upload=self.upload, insurer="Acme Life", category="Par", product="Alpha Plan", ppt="10", position=0,
            mg_yr1=77.77, payout_yr1=35.5, mg_yr2=5.0, payout_yr2=2.5, mg_yr3=1.0)
        self.blank = LifeRate.objects.create(                       # an upload made before MG was kept: no MG
            upload=self.upload, insurer="Acme Life", category="Par", product="Beta Plan", payout_yr1=30.0, position=1)
        self.client.force_login(self.manager)

    def _row_html(self, response, row):
        return re.search(r'<tr id="row-%d">(.*?)</tr>' % row.pk, response.content.decode(), re.S).group(1)

    def test_the_master_shows_the_mg_above_the_bg_of_every_year(self):
        html = self._row_html(self.client.get(reverse("life_rate_master")), self.row)
        self.assertRegex(html, r'data-label="Year 1">\s*<span class="life-mg"[^>]*>77\.77%</span>\s*<span class="rate-highlight">35\.5%</span>')
        self.assertRegex(html, r'data-label="Year 2">\s*<span class="life-mg"[^>]*>5%</span>\s*2\.5%')
        self.assertRegex(html, r'data-label="Year 3">\s*<span class="life-mg"[^>]*>1%</span>\s*<span class="text-muted">-</span>')

    def test_a_row_without_mg_shows_a_dash_for_it(self):
        html = self._row_html(self.client.get(reverse("life_rate_master")), self.blank)
        self.assertRegex(html, r'data-label="Year 1">\s*<span class="life-mg"[^>]*>-</span>\s*<span class="rate-highlight">30%</span>')

    def test_the_master_says_which_line_is_which(self):
        response = self.client.get(reverse("life_rate_master"))
        self.assertContains(response, "MG")
        self.assertContains(response, "what the insurer pays")

    def test_the_rate_checker_never_shows_the_mg_not_even_to_an_admin(self):
        for user in (self.viewer, self.manager):
            self.client.force_login(user)
            response = self.client.get(reverse("life_payout_rates"), {"q": "plan"})
            self.assertContains(response, "35.5%")
            for text in ("77.77", "life-mg", "mg_yr", "MG"):
                self.assertNotContains(response, text, msg_prefix=f"{user.username}: ")

    def test_the_rate_checker_does_not_even_load_the_mg(self):
        rows, _ = search_life_rates(self.upload, selected_life_filters(QueryDict("q=plan")))
        self.assertTrue(rows)
        for row in rows:
            self.assertTrue(set(LifeRate.MG_FIELDS) <= row.get_deferred_fields())

    def _edit_data(self, row, **over):
        data = {"category": row.category, "product": row.product, "variant": "", "ppt": row.ppt, "pt": "",
                "condition": "", "remarks": ""}
        data.update({name: "" if getattr(row, name) is None else str(getattr(row, name))
                     for name in (*LifeRate.MG_FIELDS, *LifeRate.PAYOUT_FIELDS)})
        data.update(over)
        return data

    def test_mg_and_bg_are_edited_separately_and_both_are_audited(self):
        response = self.client.post(reverse("life_rate_master_edit", args=[self.row.pk]),
                                    self._edit_data(self.row, mg_yr1="50"), follow=False)
        self.assertEqual(response.status_code, 302)
        self.row.refresh_from_db()
        self.assertEqual((self.row.mg_yr1, self.row.payout_yr1, self.row.is_edited), (50.0, 35.5, True))
        self.assertIn("mg_yr1: 77.77 -> 50.0", AuditLog.objects.get(action="LIFE RATE EDIT").details)
        self.client.post(reverse("life_rate_master_edit", args=[self.row.pk]), self._edit_data(self.row, payout_yr1="36"))
        self.row.refresh_from_db()
        self.assertEqual((self.row.mg_yr1, self.row.payout_yr1), (50.0, 36.0))                # MG stayed

    def test_the_edit_page_has_a_mg_group_and_a_bg_group_and_checks_the_range(self):
        response = self.client.get(reverse("life_rate_master_edit", args=[self.row.pk]))
        for text in ("Year 1 MG (%)", "Year 6+ MG (%)", "Year 1 BG payout (%)", "Year 6+ BG payout (%)"):
            self.assertContains(response, text)
        response = self.client.post(reverse("life_rate_master_edit", args=[self.row.pk]),
                                    self._edit_data(self.row, mg_yr2="140"))
        self.assertContains(response, "between 0 and 100")
        self.row.refresh_from_db()
        self.assertEqual(self.row.mg_yr2, 5.0)

    def test_a_row_can_be_added_with_its_mg(self):
        data = self._edit_data(self.row, insurer="Acme Life", product="New Plan", mg_yr1="40", payout_yr1="30",
                               mg_yr2="", payout_yr2="", mg_yr3="", upload=self.upload.pk)
        self.client.post(reverse("life_rate_master_add"), data)
        new = LifeRate.objects.get(product="New Plan")
        self.assertEqual((new.mg_yr1, new.payout_yr1, new.mg_yr2), (40.0, 30.0, None))
        self.assertIn("mg_yr1=40.0", AuditLog.objects.get(action="LIFE RATE ADD").details)

    def test_bulk_edit_changes_the_mg_without_touching_the_bg(self):
        response = self.client.post(reverse("life_rate_master_bulk_update"), {
            "upload": self.upload.pk, "scope": "selected", "selected_rows": [self.row.pk],
            "field": "mg_yr1", "operation": "add", "value": "2.23"})
        self.assertEqual(response.status_code, 302)
        self.row.refresh_from_db()
        self.assertEqual((self.row.mg_yr1, self.row.payout_yr1), (80.0, 35.5))
        details = AuditLog.objects.get(action="LIFE BULK UPDATE").details
        self.assertIn("add 2.23 percentage points to Year 1 MG", details)

    def test_the_bulk_dialog_offers_the_mg_fields_and_the_bg_fields_in_separate_groups(self):
        html = self.client.get(reverse("life_rate_master")).content.decode()
        block = re.search(r'<select name="field"[^>]*>(.*?)</select>', html, re.S).group(1)
        self.assertEqual(re.findall(r'<option value="([^"]+)"', block),
                         [*LifeRate.MG_FIELDS, *LifeRate.PAYOUT_FIELDS,
                          "category", "product", "variant", "ppt", "pt", "condition", "remarks"])
        self.assertEqual(re.findall(r'<optgroup label="([^"]+)"', block), ["MG (%)", "BG payouts (%)", "Details"])

    def test_the_export_has_the_mg_columns_before_the_bg_ones(self):
        response = self.client.get(reverse("life_rate_master_export"))
        ws = load_workbook(io.BytesIO(response.content)).active
        header = [cell.value for cell in ws[1]]
        self.assertEqual(header, list(EXPORT_COLUMNS))
        self.assertEqual(header[header.index("to_date") + 1:header.index("remarks")],      # after the validity dates
                         [*LifeRate.MG_FIELDS, *LifeRate.PAYOUT_FIELDS])
        self.assertEqual(header[header.index("condition") + 1:header.index("condition") + 3], ["from_date", "to_date"])
        first = dict(zip(header, [cell.value for cell in ws[2]]))
        self.assertEqual((first["mg_yr1"], first["payout_yr1"], first["mg_yr3"], first["payout_yr3"]),
                         (77.77, 35.5, 1.0, None))

    def test_the_audit_snapshot_of_a_row_includes_its_mg(self):
        text = life_rate_snapshot(self.row)
        self.assertIn("mg_yr1=77.77", text)
        self.assertIn("payout_yr1=35.5", text)
