"""Validity dates on Life rows (LifeRate.from_date / to_date): the standard file, the export, the edit and add forms, the
Rate Checker's Date box and the review. They follow Rate Master and Health Rate Master: both ends included, blank = no
limit on that side, so a grid with no dates behaves exactly as before. The MIS engine's use of them is tested in
test_mapping_engine_life.LifeDateWindowTests."""
import datetime
import io

from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from openpyxl import load_workbook

from insurance.models import AuditLog, LifeGridUpload, LifeRate
from insurance.services import life_standard as ls
from insurance.services.life_review import review_upload
from insurance.services.life_standard import read_standard_file, standard_template_workbook
from insurance.test_life_standard_file import HEADER, LifeReviewMixin, LifeStandardViewMixin, _line, _problems, _row, _xlsx

DATE_HEADER = [*HEADER, "from_date", "to_date"]
D = datetime.date


def _dated(start, end, **line):
    """One data row for DATE_HEADER."""
    return [*_line(**line), start, end]


def _read(rows, header=None):
    return read_standard_file(_xlsx(rows, header=header or DATE_HEADER), "g.xlsx")


class ValidityDatesReaderTests(SimpleTestCase):
    def test_excel_dates_and_the_text_spellings_are_all_read(self):
        rows = [
            _dated(datetime.datetime(2026, 11, 1), datetime.datetime(2026, 11, 30), product="A"),
            _dated("01/11/2026", "30/11/2026", product="B"),
            _dated("2026-11-01", "2026-11-30", product="C"),
            _dated("01-11-2026", "30.11.2026", product="D"),
            _dated("2026-11-01 00:00:00", "30/11/2026 00:00:00", product="E"),
        ]
        for row in _read(rows).rows:
            self.assertEqual((row["from_date"], row["to_date"]), (D(2026, 11, 1), D(2026, 11, 30)), row["product"])

    def test_valid_from_and_valid_till_are_accepted_as_the_column_names(self):
        header = [*HEADER, "Valid From", "Valid Till"]
        row = _read([_dated("01/11/2026", "30/11/2026")], header=header).rows[0]
        self.assertEqual((row["from_date"], row["to_date"]), (D(2026, 11, 1), D(2026, 11, 30)))
        row = _read([_dated(None, "30/11/2026")], header=[*HEADER, "valid_from", "valid_to"]).rows[0]
        self.assertEqual((row["from_date"], row["to_date"]), (None, D(2026, 11, 30)))

    def test_empty_and_na_cells_are_open_and_a_file_without_the_columns_has_no_dates(self):
        rows = _read([_dated(None, ""), _dated("NA", "-", product="B"), _dated(None, "30/11/2026", product="C")]).rows
        self.assertEqual([(r["from_date"], r["to_date"]) for r in rows],
                         [(None, None), (None, None), (None, D(2026, 11, 30))])
        plain = read_standard_file(_xlsx([_line()]), "g.xlsx").rows[0]
        self.assertEqual((plain["from_date"], plain["to_date"]), (None, None))

    def test_bad_dates_are_each_explained(self):
        rows = [_dated("30th Nov", None), _dated(None, 46000, product="B"), _dated("5/10/26", None, product="C"),
                _dated(True, None, product="D")]
        caught = _problems(_xlsx(rows, header=DATE_HEADER))
        text = "\n".join(caught.problems)
        self.assertEqual(caught.total, 4)
        self.assertIn("Row 2: from_date: '30th Nov' is not a date. Write it as DD/MM/YYYY (or YYYY-MM-DD).", text)
        self.assertIn("Row 3: to_date: 46000 is a number, not a date. Format the cell as a date (DD/MM/YYYY).", text)
        self.assertIn("Row 4: from_date: '5/10/26' is not a date.", text)           # a two-digit year is not guessed at
        self.assertIn("Row 5: from_date: TRUE / FALSE is not a date.", text)

    def test_a_window_that_ends_before_it_starts_is_refused(self):
        caught = _problems(_xlsx([_dated("30/11/2026", "01/11/2026")], header=DATE_HEADER))
        self.assertEqual(caught.problems, ["Row 2: from_date (30/11/2026) is after to_date (01/11/2026)."])

    def test_the_same_day_is_a_one_day_window(self):
        row = _read([_dated("30/11/2026", "30/11/2026")]).rows[0]
        self.assertEqual((row["from_date"], row["to_date"]), (D(2026, 11, 30), D(2026, 11, 30)))

    def test_a_date_column_given_twice_is_refused(self):
        caught = _problems(_xlsx([_dated("01/11/2026", "30/11/2026")], header=[*HEADER, "to_date", "Valid till"]))
        self.assertIn("The column 'to_date' appears more than once", caught.problems[0])

    def test_dates_do_not_make_two_rows_exact_copies_but_equal_windows_do(self):
        result = _read([_dated("01/11/2026", "30/11/2026"), _dated("01/12/2026", "31/12/2026")])
        self.assertEqual(result.warnings, [])
        result = _read([_dated("01/11/2026", "30/11/2026"), _dated("01/11/2026", "30/11/2026")])
        self.assertTrue(any("is an exact copy" in w for w in result.warnings))


class ValidityDatesTemplateTests(SimpleTestCase):
    def test_the_template_has_the_two_date_columns_formatted_as_dates_and_explains_them(self):
        wb = standard_template_workbook()
        ws = wb["Rates"]
        self.assertIn("from_date", [c.value for c in ws[1]])
        self.assertEqual(ls.STANDARD_COLUMNS[7:9], ("from_date", "to_date"))
        for name in ("from_date", "to_date"):
            letter = ls.get_column_letter(ls.STANDARD_COLUMNS.index(name) + 1)
            self.assertEqual(ws.column_dimensions[letter].number_format, "DD/MM/YYYY", name)
        guide = " ".join(str(cell.value) for row in wb["Read me"].iter_rows() for cell in row if cell.value)
        for needle in ("from_date, to_date", "both included", "inception date", "overlap"):
            self.assertIn(needle, guide)


class ValidityDatesViewTests(LifeStandardViewMixin, TestCase):
    def _edit_data(self, row, **over):
        data = {"category": "", "product": row.product, "variant": "", "ppt": row.ppt, "pt": "", "condition": "",
                "from_date": "", "to_date": "", "remarks": ""}
        data.update({name: "" for name in (*LifeRate.MG_FIELDS, *LifeRate.PAYOUT_FIELDS)})
        data["payout_yr1"] = "47.5"
        data.update(over)
        return data

    def _row(self, **extra):
        upload = LifeGridUpload.objects.create(label="Oct'26", is_active=True)
        return LifeRate.objects.create(upload=upload, insurer="Acme Life", product="Alpha Plan", ppt="10",
                                       payout_yr1=47.5, **extra)

    # -- the standard file and the review
    def test_a_standard_file_with_dates_is_stored_and_the_review_shows_a_changed_window(self):
        self.live(rows=[_row(category="")])
        self.post_standard(content=_xlsx([_dated(None, D(2026, 11, 30), category="")], header=DATE_HEADER))
        pending = LifeGridUpload.objects.get(is_pending=True)
        stored = pending.rates.get()
        self.assertEqual((stored.from_date, stored.to_date), (None, D(2026, 11, 30)))
        review = self.client.get(reverse("life_rate_master_review", args=[pending.pk])).context["review"]
        self.assertEqual([(c.kind, c.details) for c in review.changes], [("changed", [("Valid to", "blank", "30/11/2026")])])
        self.assertContains(self.client.get(reverse("life_rate_master_review", args=[pending.pk])),
                            "Valid to: <span class=\"life-was\">blank</span>")

    # -- the export
    def test_the_export_carries_the_dates_as_dates_and_loads_back(self):
        upload = LifeGridUpload.objects.create(label="Oct'26", is_active=True)
        LifeRate.objects.create(upload=upload, insurer="Acme Life", product="Alpha Plan", payout_yr1=20.0,
                                from_date=D(2026, 11, 1), to_date=D(2026, 11, 30))
        LifeRate.objects.create(upload=upload, insurer="Acme Life", product="Beta Plan", payout_yr1=10.0, position=1)
        response = self.client.get(reverse("life_rate_master_export"))
        ws = load_workbook(io.BytesIO(response.content)).worksheets[0]
        header = [cell.value for cell in ws[1]]
        self.assertEqual(header[header.index("condition") + 1:header.index("condition") + 3], ["from_date", "to_date"])
        first = {name: ws.cell(row=2, column=header.index(name) + 1) for name in ("from_date", "to_date")}
        self.assertEqual(first["from_date"].value, datetime.datetime(2026, 11, 1))
        self.assertEqual(first["from_date"].number_format, "DD/MM/YYYY")
        self.assertIsNone(ws.cell(row=3, column=header.index("from_date") + 1).value)
        rows = read_standard_file(response.content, "life_rates_Oct26.xlsx").rows
        self.assertEqual([(r["from_date"], r["to_date"]) for r in rows], [(D(2026, 11, 1), D(2026, 11, 30)), (None, None)])

    # -- the edit and add forms
    def test_the_edit_page_sets_clears_and_audits_the_dates(self):
        row = self._row()
        page = self.client.get(reverse("life_rate_master_edit", args=[row.pk]))
        self.assertContains(page, "Valid from")
        self.assertContains(page, 'name="from_date"')
        self.client.post(reverse("life_rate_master_edit", args=[row.pk]),
                         self._edit_data(row, from_date="2026-11-01", to_date="2026-11-30"))
        row.refresh_from_db()
        self.assertEqual((row.from_date, row.to_date, row.is_edited), (D(2026, 11, 1), D(2026, 11, 30), True))
        log = AuditLog.objects.get(action="LIFE RATE EDIT")
        self.assertIn("from_date: None -> '2026-11-01'", log.details)
        self.assertIn("to_date: None -> '2026-11-30'", log.details)
        self.client.post(reverse("life_rate_master_edit", args=[row.pk]), self._edit_data(row))
        row.refresh_from_db()
        self.assertEqual((row.from_date, row.to_date), (None, None))

    def test_a_window_that_ends_before_it_starts_is_refused_on_screen(self):
        row = self._row()
        response = self.client.post(reverse("life_rate_master_edit", args=[row.pk]),
                                    self._edit_data(row, from_date="2026-11-30", to_date="2026-11-01"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "The end date is before the start date.")
        row.refresh_from_db()
        self.assertEqual((row.from_date, row.to_date), (None, None))

    def test_a_row_added_on_screen_can_carry_dates_and_a_deleted_row_keeps_them_in_the_audit(self):
        upload = LifeGridUpload.objects.create(label="Oct'26", is_active=True)
        data = self._edit_data(LifeRate(product="Added Plan", ppt="5"), from_date="2026-12-01")
        data["insurer"] = "Acme Life"
        data["upload"] = upload.pk
        self.client.post(reverse("life_rate_master_add"), data)
        added = LifeRate.objects.get(product="Added Plan")
        self.assertEqual((added.from_date, added.to_date, added.added_on_screen), (D(2026, 12, 1), None, True))
        self.client.post(reverse("life_rate_master_delete", args=[added.pk]))
        log = AuditLog.objects.get(action="LIFE RATE DELETE")
        self.assertIn("from_date='2026-12-01'", log.details)

    # -- the pages
    def test_the_master_page_shows_a_rows_window_under_its_plan(self):
        self._row(from_date=D(2026, 11, 1), to_date=D(2026, 11, 30))
        self.assertContains(self.client.get(reverse("life_rate_master")), "Valid 01 Nov 2026 &ndash; 30 Nov 2026")


class LifeCheckerDateTests(LifeStandardViewMixin, TestCase):
    """The Rate Checker's Date box: the same inclusive, open-ended check Health Payout Rates uses."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.viewer)
        upload = LifeGridUpload.objects.create(label="Oct'26", is_active=True)
        for position, (product, y1, start, end) in enumerate([
                ("Ended Plan", 40.0, None, D(2026, 11, 30)), ("Later Plan", 45.0, D(2026, 12, 1), None),
                ("Always Plan", 10.0, None, None), ("Window Plan", 20.0, D(2026, 11, 1), D(2026, 11, 30))]):
            LifeRate.objects.create(upload=upload, insurer="Acme Life", product=product, payout_yr1=y1,
                                    from_date=start, to_date=end, position=position)

    def products(self, **params):
        response = self.client.get(reverse("life_payout_rates"), params)
        self.assertEqual(response.status_code, 200)
        return [row.product for row in response.context["data"]], response

    def test_a_date_keeps_only_the_rows_that_apply_on_it_ends_included(self):
        self.assertEqual(self.products(target_date="2026-11-30")[0], ["Ended Plan", "Window Plan", "Always Plan"])
        self.assertEqual(self.products(target_date="2026-12-01")[0], ["Later Plan", "Always Plan"])
        self.assertEqual(self.products(target_date="2026-10-31")[0], ["Ended Plan", "Always Plan"])
        self.assertEqual(self.products(target_date="01/12/2026")[0], ["Later Plan", "Always Plan"])      # DD/MM/YYYY too

    def test_no_date_or_a_date_that_is_not_one_keeps_every_row(self):
        self.assertEqual(len(self.products(q="plan")[0]), 4)
        self.assertEqual(len(self.products(target_date="not a date")[0]), 4)
        self.assertEqual(len(self.products(target_date="")[0]), 4)

    def test_the_date_works_with_the_other_filters(self):
        self.assertEqual(self.products(target_date="2026-11-30", q="window")[0], ["Window Plan"])
        self.assertEqual(self.products(target_date="2026-12-01", q="ended")[0], [])

    def test_the_page_offers_the_date_box_keeps_the_value_and_shows_each_rows_window(self):
        products, response = self.products(target_date="2026-11-30")
        self.assertContains(response, 'name="target_date"')
        self.assertContains(response, 'value="2026-11-30"')
        self.assertContains(response, "Valid till 30 Nov 2026")
        self.assertContains(response, "Valid 01 Nov 2026 &ndash; 30 Nov 2026")
        self.assertNotContains(response, "Valid from 01 Dec 2026")                    # that row does not apply on the 30th
        self.assertContains(self.client.get(reverse("life_payout_rates"), {"target_date": "2026-12-05"}),
                            "Valid from 01 Dec 2026")


class ValidityDatesReviewTests(LifeReviewMixin, TestCase):
    def test_a_changed_window_alone_is_a_change(self):
        live = self.upload("live", [_row(from_date=D(2026, 11, 1), to_date=D(2026, 11, 30))], live=True)
        new = self.upload("new", [_row(from_date=D(2026, 11, 1), to_date=D(2026, 12, 31))], pending=True)
        review = review_upload(new, live, today=D(2026, 11, 14))
        self.assertEqual([(c.kind, c.details) for c in review.changes], [("changed", [("Valid to", "30/11/2026", "31/12/2026")])])
        same = self.upload("same", [_row(from_date=D(2026, 11, 1), to_date=D(2026, 11, 30))], pending=True)
        self.assertEqual(review_upload(same, live, today=D(2026, 11, 14)).changes, [])

    def test_added_and_removed_rows_say_their_window(self):
        live = self.upload("live", [_row("Old Plan", to_date=D(2026, 11, 30))], live=True)
        new = self.upload("new", [_row("New Plan", from_date=D(2026, 12, 1))], pending=True)
        review = review_upload(new, live, today=D(2026, 11, 14))
        self.assertEqual([(c.kind, c.summary) for c in review.changes],
                         [("added", "year 1 BG 20, valid from 01/12/2026"), ("removed", "year 1 BG 20, valid until 30/11/2026")])

    def test_overlapping_windows_of_one_plan_with_different_payouts_are_flagged(self):
        new = self.upload("new", [_row(y1=40.0, to_date=D(2026, 12, 31)), _row(y1=45.0, from_date=D(2026, 12, 1))],
                          pending=True)
        review = review_upload(new, None, today=D(2026, 11, 14))
        check = next(c for c in review.checks if c.title == "Date windows of one plan that overlap")
        self.assertEqual((check.level, check.count), ("warn", 1))
        self.assertIn("until 31/12/2026 and from 01/12/2026 overlap", check.examples[0])

    def test_windows_that_do_not_overlap_or_pay_the_same_or_have_no_dates_are_not_flagged(self):
        titles = lambda rows: {c.title for c in review_upload(self.upload("n", rows, pending=True), None, today=D(2026, 11, 14)).checks}
        end = "Date windows of one plan that overlap"
        self.assertNotIn(end, titles([_row(y1=40.0, to_date=D(2026, 11, 30)), _row(y1=45.0, from_date=D(2026, 12, 1))]))
        self.assertNotIn(end, titles([_row(y1=40.0, to_date=D(2026, 12, 31)), _row(y1=40.0, from_date=D(2026, 12, 1))]))
        self.assertNotIn(end, titles([_row(y1=40.0), _row(y1=45.0)]))                # already duplicates, no date involved
        self.assertNotIn(end, titles([_row("Beta", y1=40.0, to_date=D(2026, 12, 31)), _row("Gamma", y1=45.0)]))
        self.assertIn(end, titles([_row(y1=40.0), _row(y1=45.0, from_date=D(2026, 12, 1))]))     # undated + dated overlap

    def test_rows_past_their_end_date_and_rows_that_have_not_started_are_noted(self):
        new = self.upload("new", [_row("Ended", to_date=D(2026, 11, 13)), _row("Today", to_date=D(2026, 11, 14)),
                                  _row("Ahead", from_date=D(2026, 11, 15)), _row("Open")], pending=True)
        checks = {c.title: c for c in review_upload(new, None, today=D(2026, 11, 14)).checks}
        ended = checks["Rows already past their end date"]
        ahead = checks["Rows that start in the future"]
        self.assertEqual((ended.level, ended.count, ahead.level, ahead.count), ("info", 1, "info", 1))
        self.assertIn("Ended", ended.examples[0])
        self.assertIn("Ahead", ahead.examples[0])
        undated = self.upload("undated", [_row("Open")], pending=True)
        self.assertEqual(review_upload(undated, None, today=D(2026, 11, 14)).checks, [])
