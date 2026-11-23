"""The standard Life file (one flat table for every insurer), the review of an upload against the live grid, and the
pending-upload flow that connects them: an upload is stored but not live until someone looks at what it changes."""
import datetime
import io
from unittest import mock

from django.contrib.auth.models import Group, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import QueryDict
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from openpyxl import Workbook, load_workbook

from insurance.models import AuditLog, LifeGridUpload, LifeInsurerAlias, LifeInsurerNote, LifePlanMapping, LifeRate
from insurance.services import life_mis
from insurance.services import life_standard as ls
from insurance.services.life_rates import (
    activate_life_upload, active_life_upload, default_label, discard_life_upload, save_life_upload, search_life_rates,
    selected_life_filters,
)
from insurance.services.life_review import BIG_MOVE_POINTS, review_upload
from insurance.services.life_standard import LifeFileError, read_standard_file, standard_template_workbook
from insurance.test_life_rates import _edelweiss, _workbook

HEADER = ["insurer", "category", "product", "variant", "ppt", "pt", "condition",
          "payout_yr1", "payout_yr2", "mg_yr1", "mg_yr2", "remarks"]


def _line(insurer="Acme Life", product="Alpha Plan", ppt="10", y1=20, y2=None, mg1=None, mg2=None,
          category="PAR(Trad)", variant="", pt="", condition="", remarks=""):
    """One data row for HEADER."""
    return [insurer, category, product, variant, ppt, pt, condition, y1, y2, mg1, mg2, remarks]


def _xlsx(rows, header=None, notes=None, extra_sheets=(), configure=None, title="Rates"):
    """The bytes of a standard file: a Rates sheet, an optional Notes sheet, any other sheets, and a last chance to edit."""
    wb = Workbook()
    ws = wb.active
    ws.title = title
    ws.append(header or HEADER)
    for row in rows:
        ws.append(row)
    if notes is not None:
        sheet = wb.create_sheet("Notes")
        sheet.append(["insurer", "note"])
        for row in notes:
            sheet.append(row)
    for name in extra_sheets:
        wb.create_sheet(name)
    if configure:
        configure(wb)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _problems(raw, name="g.xlsx"):
    """The LifeFileError a file raises (the test fails if the file is accepted)."""
    try:
        read_standard_file(raw, name)
    except LifeFileError as exc:
        return exc
    raise AssertionError("the file was accepted")


class StandardFileReaderTests(SimpleTestCase):
    def test_columns_are_found_by_name_in_any_order(self):
        header = ["Remarks", "PPT", "Payout Yr 1", "INSURER", "MG_YR1", "Product", "category", "payout_yr2"]
        rows = [["Valid till 30th Nov", ">=10", 50, "Bajaj Allianz Life", 66.7, "ACE", "PAR(Trad)", 2.5],
                ["", 5, 35.5, "Bajaj Allianz Life", "", "ACE", "PAR(Trad)", None]]
        result = read_standard_file(_xlsx(rows, header=header), "g.xlsx")
        first, second = result.rows
        self.assertEqual((first["insurer"], first["product"], first["category"], first["ppt"], first["remarks"]),
                         ("Bajaj Allianz Life", "ACE", "PAR(Trad)", ">=10", "Valid till 30th Nov"))
        self.assertEqual((first["y1"], first["y2"], first["m_y1"]), (50.0, 2.5, 66.7))
        self.assertEqual((second["ppt"], second["y1"], second["y2"], second["m_y1"]), ("5", 35.5, None, None))
        self.assertEqual([r["source_ref"] for r in result.rows], ["Row 2", "Row 3"])
        self.assertEqual([r["position"] for r in result.rows], [0, 1])
        self.assertEqual(result.sheet_counts, {"Bajaj Allianz Life": 2})

    def test_a_csv_is_read_with_a_bom_a_semicolon_or_windows_text(self):
        bom = "﻿insurer,product,payout_yr1\r\nAcme,Plan,12.5\r\n".encode("utf-8")
        self.assertEqual(read_standard_file(bom, "g.csv").rows[0]["y1"], 12.5)
        semicolon = b"insurer;product;payout_yr1\r\nAcme;Plan;12.5\r\n"
        self.assertEqual(read_standard_file(semicolon, "g.csv").rows[0]["product"], "Plan")
        windows = "insurer,product,payout_yr1\r\nAcme,Café Plan,5\r\n".encode("cp1252")
        self.assertEqual(read_standard_file(windows, "G.CSV").rows[0]["product"], "Café Plan")

    def test_payouts_are_percent_numbers(self):
        def percent_cell(wb):
            wb["Rates"]["H2"].number_format = "0.0%"          # payout_yr1 of the first data row, shown as a percent

        rows = [_line(y1=0.355), _line(product="B", y1="28%"), _line(product="C", y1="NA", y2=5),
                _line(product="D", y1=0), _line(product="E", y1="-", y2=" 7.5 ")]
        result = read_standard_file(_xlsx(rows, configure=percent_cell), "g.xlsx")
        self.assertEqual([(r["y1"], r["y2"]) for r in result.rows],
                         [(35.5, None), (28.0, None), (None, 5.0), (0.0, None), (None, 7.5)])

    def test_bad_payouts_are_each_explained(self):
        rows = [_line(y1=140), _line(product="B", y1=-1), _line(product="C", y1="10 & 15 Pay - 2%"),
                _line(product="D", y1="12,5"), _line(product="E", y1=True),
                _line(product="F", y1=datetime.datetime(2026, 1, 1))]
        caught = _problems(_xlsx(rows))
        text = "\n".join(caught.problems)
        self.assertEqual(caught.total, 6)
        self.assertIn("Row 2: payout_yr1: 140 is outside 0-100", text)
        self.assertIn("Row 3: payout_yr1: -1 is outside 0-100", text)
        self.assertIn("Row 4: payout_yr1: '10 & 15 Pay - 2%' is not a number. Put a written rule in remarks", text)
        self.assertIn("Row 5: payout_yr1: '12,5' uses a comma as the decimal mark; write it with a point (12.5)", text)
        self.assertIn("Row 6: payout_yr1: TRUE / FALSE is not a number", text)
        self.assertIn("Row 7: payout_yr1: a date is not a number", text)

    def test_every_problem_is_listed_with_its_row_number(self):
        rows = [_line(insurer=""), _line(product=""), _line(y1=None), _line(y1=140, insurer="", product="")]
        caught = _problems(_xlsx(rows))
        self.assertEqual(caught.total, 6)
        for expected in ("Row 2: insurer is empty.", "Row 3: product is empty.",
                         "Row 4: needs at least one BG payout (payout_yr1 ... payout_yr6_plus) or, for a rule that is "
                         "not a number, the rule in remarks.",
                         "Row 5: insurer is empty.", "Row 5: product is empty."):
            self.assertIn(expected, caught.problems)
        self.assertTrue(any(p.startswith("Row 5: payout_yr1: 140") for p in caught.problems))

    def test_a_row_that_is_only_a_written_rule_is_accepted_but_a_row_with_neither_is_not(self):
        rule = "Year 1: 30% of 1st year commission."
        result = read_standard_file(_xlsx([_line(), _line(product="Bonus", y1=None, remarks=rule)]), "g.xlsx")
        self.assertEqual(len(result.rows), 2)
        bonus = result.rows[1]
        self.assertEqual((bonus["remarks"], bonus["y1"], bonus["m_y1"]), (rule, None, None))
        self.assertEqual(len(_problems(_xlsx([_line(), _line(product="Empty", y1=None)])).problems), 1)

    def test_only_the_first_problems_are_kept_but_all_are_counted(self):
        rows = [_line(product=f"P{n}", y1=None) for n in range(ls.MAX_PROBLEMS + 25)]
        caught = _problems(_xlsx(rows))
        self.assertEqual(len(caught.problems), ls.MAX_PROBLEMS)
        self.assertEqual(caught.total, ls.MAX_PROBLEMS + 25 + 1)          # + "no row has any payout"

    def test_a_header_without_the_required_columns_is_refused(self):
        self.assertIn("has no 'product' column", _problems(b"insurer,payout_yr1\r\nAcme,5\r\n", "g.csv").problems[0])
        self.assertIn("has no payout column", _problems(b"insurer,product\r\nAcme,Plan\r\n", "g.csv").problems[0])
        self.assertIn("has no header row", _problems(b"hello,world\r\n1,2\r\n", "g.csv").problems[0])
        self.assertIn("has no data rows", _problems(b"insurer,product,payout_yr1\r\n", "g.csv").problems[0])

    def test_the_header_may_sit_below_a_title_and_blank_rows_are_skipped(self):
        def title(wb):
            ws = wb["Rates"]
            ws.insert_rows(1, amount=3)
            ws["A1"] = "Life grid Nov'26"
            ws.insert_rows(6)                                        # a blank row between the data rows
        result = read_standard_file(_xlsx([_line(), _line(product="B")], configure=title), "g.xlsx")
        self.assertEqual([r["product"] for r in result.rows], ["Alpha Plan", "B"])
        self.assertEqual([r["source_ref"] for r in result.rows], ["Row 5", "Row 7"])

    def test_a_date_where_a_ppt_should_be_is_refused(self):
        caught = _problems(_xlsx([_line(ppt=datetime.datetime(2026, 10, 5))]))
        self.assertIn("Row 2: ppt: Excel turned the entry into the date 05/10/2026", caught.problems[0])
        self.assertIn("Format the column as Text", caught.problems[0])

    def test_an_insurer_spelled_two_ways_is_refused(self):
        rows = [_line(insurer="Acme Life"), _line(insurer="Acme Life", product="B"), _line(insurer="acme  life", product="C")]
        caught = _problems(_xlsx(rows))
        self.assertEqual(caught.total, 1)
        self.assertIn("spelled more than one way", caught.problems[0])
        self.assertIn("'Acme Life' (2 rows)", caught.problems[0])
        self.assertIn("'acme life' (1 rows)", caught.problems[0])

    def test_unknown_columns_warn_and_the_export_only_columns_are_ignored_silently(self):
        header = ["id", "upload", "insurer", "product", "payout_yr1", "position", "is_edited", "edited_by",
                  "edited_at", "Mystery", "Other thing"]
        result = read_standard_file(_xlsx([[1, "Oct'26", "Acme", "Plan", 5, 0, "NO", "", "", "x", "y"]], header=header),
                                    "g.xlsx")
        self.assertEqual(result.warnings, ["Columns not recognised and ignored: Mystery, Other thing."])
        self.assertEqual(len(result.rows), 1)

    def test_only_the_first_sheet_with_the_columns_is_read(self):
        result = read_standard_file(_xlsx([_line()], extra_sheets=("Backup",)), "g.xlsx")
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.warnings, ["Sheet 'Backup' was not read (only the first sheet with the column names, "
                                           "and a sheet named Notes, are read)."])

    def test_exact_copies_are_kept_and_warned_about(self):
        result = read_standard_file(_xlsx([_line(), _line()]), "g.xlsx")
        self.assertEqual(len(result.rows), 2)
        self.assertTrue(any("is an exact copy" in warning for warning in result.warnings))

    def test_text_longer_than_the_stored_field_is_refused(self):
        caught = _problems(_xlsx([_line(product="x" * 300)]))
        self.assertEqual(caught.problems, ["Row 2: product: 300 characters, the most allowed is 255."])

    def test_source_ref_is_kept_unless_it_only_says_where_an_earlier_file_had_the_row(self):
        header = [*HEADER, "source_ref"]
        rows = [[*_line(), "Edelweiss!K3"], [*_line(product="B"), "Row 17"], [*_line(product="C"), "Added on screen"],
                [*_line(product="D"), ""]]
        result = read_standard_file(_xlsx(rows, header=header), "g.xlsx")
        self.assertEqual([r["source_ref"] for r in result.rows], ["Edelweiss!K3", "Row 3", "Row 4", "Row 5"])

    def test_formulas_without_stored_values_get_a_hint(self):
        def formula(wb):
            wb["Rates"]["H2"] = "=10+10"
        caught = _problems(_xlsx([_line(y1=None)], configure=formula))
        self.assertIn("formulas", "\n".join(caught.problems))

    def test_wrong_files(self):
        self.assertEqual(_problems(b"x", "grid.xls").problems, ["The standard file must be an .xlsx or a .csv file."])
        self.assertIn("could not be opened as an Excel workbook", _problems(b"not a zip", "g.xlsx").problems[0])

    def test_a_file_with_too_many_rows_is_refused(self):
        with mock.patch.object(ls, "MAX_ROWS", 2):
            caught = _problems(_xlsx([_line(product=f"P{n}") for n in range(3)]))
        self.assertIn("the most one upload takes is 2", caught.problems[0])


class StandardFileNotesTests(SimpleTestCase):
    def test_notes_keep_their_lines(self):
        result = read_standard_file(_xlsx([_line()], notes=[["Acme Life", "Line one\nLine  two\n\nLine three"]]), "g.xlsx")
        self.assertEqual(result.notes, {"Acme Life": "Line one\nLine two\nLine three"})

    def test_an_insurer_without_notes_is_skipped_and_notes_for_a_stranger_are_a_warning(self):
        result = read_standard_file(_xlsx([_line()], notes=[["Acme Life", ""], ["Nobody Life", "Some terms"]]), "g.xlsx")
        self.assertEqual(list(result.notes), ["Nobody Life"])
        self.assertEqual(result.warnings, ["Notes were given for 'Nobody Life', which has no rows in the file."])

    def test_notes_problems_are_listed(self):
        caught = _problems(_xlsx([_line()], notes=[["Acme Life", "a"], ["Acme Life", "b"], ["acme life", "c"], ["", "d"]]))
        text = "\n".join(caught.problems)
        self.assertIn("Notes row 3: the notes of 'Acme Life' are given twice.", text)
        self.assertIn("Notes row 4: 'acme life' is spelled differently from 'Acme Life'", text)
        self.assertIn("Notes row 5: insurer is empty.", text)

    def test_a_notes_sheet_without_its_columns_is_refused(self):
        def wrong(wb):
            wb["Notes"]["A1"], wb["Notes"]["B1"] = "who", "what"
        caught = _problems(_xlsx([_line()], notes=[["Acme Life", "a"]], configure=wrong))
        self.assertEqual(caught.problems, ["Notes: the first row of the Notes sheet needs the column names insurer and note."])


class StandardTemplateTests(SimpleTestCase):
    def test_the_template_has_headers_only_and_a_read_me(self):
        wb = standard_template_workbook()
        self.assertEqual(wb.sheetnames, ["Rates", "Notes", "Read me"])
        self.assertEqual([c.value for c in wb["Rates"][1]], list(ls.STANDARD_COLUMNS))
        self.assertEqual(wb["Rates"].max_row, 1)
        self.assertEqual([c.value for c in wb["Notes"][1]], ["insurer", "note"])
        guide = " ".join(str(cell.value) for row in wb["Read me"].iter_rows() for cell in row if cell.value)
        for needle in ("percent numbers", "at least one BG payout", "insurer", "payout_yr1", "mg_yr1", "source_ref"):
            self.assertIn(needle, guide)

    def test_ppt_and_pt_columns_are_text_so_excel_cannot_make_dates_of_them(self):
        ws = standard_template_workbook()["Rates"]
        for name in ("ppt", "pt", "product", "insurer"):
            letter = ls.get_column_letter(ls.STANDARD_COLUMNS.index(name) + 1)
            self.assertEqual(ws.column_dimensions[letter].number_format, "@", name)

    def test_the_example_in_the_read_me_uses_real_column_names(self):
        for name in ls._EXAMPLE[0]:
            self.assertIn(name, ls.STANDARD_COLUMNS)

    def test_an_unfilled_template_is_refused_instead_of_loading_examples(self):
        out = io.BytesIO()
        standard_template_workbook().save(out)
        caught = _problems(out.getvalue())
        self.assertEqual(caught.problems, ["Sheet 'Rates' has no data rows below its header."])


class UnreadableWordingTests(SimpleTestCase):
    """life_filters.ppt_unreadable / pt_unreadable: wording with words but nothing the rules can read, which the rules
    would otherwise read as "applies to every PPT / PT"."""

    def test_ppt_wording(self):
        from insurance.services import life_filters as lf
        for unreadable in ("Five Pay", "five", "as per plan"):
            self.assertTrue(lf.ppt_unreadable(unreadable), unreadable)
        for readable in ("", "   ", "All", "any", "NA", "-", "5", "5-10", ">=10", "10+", "7 to 9", "Single Pay", "SP",
                         "Regular", "Limited Pay", "12 & Above", "15 years & above", "Excluding 5 Pay", "Capped at 3 lakh"):
            self.assertFalse(lf.ppt_unreadable(readable), readable)

    def test_pt_wording(self):
        from insurance.services import life_filters as lf
        for unreadable in ("forever", "as per plan"):
            self.assertTrue(lf.pt_unreadable(unreadable), unreadable)
        for readable in ("", "All", "Any", "-", "20", "15/20", ">=20", "5 to 20 Years", "Whole Life", "RP", "Regular",
                         "(100 minus Entry Age) years"):
            self.assertFalse(lf.pt_unreadable(readable), readable)

    def test_the_helpers_agree_with_the_rules_they_mirror(self):
        from insurance.services import life_filters as lf
        for text in ("Five Pay", "10", "Single Pay", "All", "forever", "Whole Life", ">=20"):
            spec = lf.ppt_spec(text)
            if lf.ppt_unreadable(text):
                self.assertTrue(spec.states_none and not spec.modes and not spec.spans, text)
            spec = lf.pt_spec(text)
            if lf.pt_unreadable(text):
                self.assertTrue(spec.any_term and not spec.spans, text)


class LifeReviewMixin:
    """Two uploads built straight from dicts, the way the review reads them."""

    def setUp(self):
        LifeInsurerAlias.objects.all().delete()               # the seeded real-insurer spellings are not part of these tests
        LifePlanMapping.objects.all().delete()

    def upload(self, label, rows, live=False, pending=False):
        upload = LifeGridUpload.objects.create(label=label, is_active=live, is_pending=pending, row_count=len(rows))
        for position, values in enumerate(rows):
            LifeRate.objects.create(upload=upload, position=position, **{"insurer": "Acme Life", **values})
        return upload


def _row(product="Alpha Plan", ppt="10", y1=20.0, **extra):
    return {"product": product, "ppt": ppt, "payout_yr1": y1, **extra}


class LifeReviewTests(LifeReviewMixin, TestCase):
    def test_identical_uploads_show_no_difference(self):
        rows = [_row(), _row("Beta Plan", "5", 10.0, mg_yr1=15.0, remarks="x")]
        review = review_upload(self.upload("new", rows, pending=True), self.upload("live", rows, live=True))
        self.assertEqual((review.totals["changed"], review.totals["added"], review.totals["removed"]), (0, 0, 0))
        self.assertEqual(review.totals["unchanged"], 2)
        self.assertEqual(review.changes, [])
        self.assertEqual(review.checks, [])
        self.assertEqual(review.attention, 0)

    def test_changed_added_and_removed_rows(self):
        live = self.upload("live", [_row(), _row("Beta Plan", "5", 10.0), _row("Gamma Plan", "8", 15.0)], live=True)
        new = self.upload("new", [_row(), _row("Beta Plan", "5", 12.0, mg_yr1=20.0), _row("Delta Plan", "7", 9.0)],
                          pending=True)
        review = review_upload(new, live)
        self.assertEqual((review.totals["before"], review.totals["after"], review.totals["unchanged"],
                          review.totals["changed"], review.totals["added"], review.totals["removed"]), (3, 3, 1, 1, 1, 1))
        self.assertEqual([(c.kind, c.row["product"]) for c in review.changes],
                         [("changed", "Beta Plan"), ("added", "Delta Plan"), ("removed", "Gamma Plan")])
        self.assertEqual(review.changes[0].details, [("Year 1 BG", "10", "12"), ("Year 1 MG", "blank", "20")])
        self.assertFalse(review.changes[0].big)
        self.assertEqual((review.changes[1].summary, review.changes[2].summary), ("year 1 BG 9", "year 1 BG 15"))

    def test_a_reordered_grid_shows_no_difference(self):
        rows = [_row(), _row("Beta Plan", "5", 10.0), _row("Gamma Plan", "8", 15.0)]
        review = review_upload(self.upload("new", list(reversed(rows)), pending=True), self.upload("live", rows, live=True))
        self.assertEqual((review.totals["changed"], review.totals["added"], review.totals["removed"]), (0, 0, 0))

    def test_case_and_spacing_in_the_labels_do_not_make_a_new_row(self):
        live = self.upload("live", [_row("Alpha Plan", "10")], live=True)
        new = self.upload("new", [_row("alpha  plan", " 10 ")], pending=True)
        self.assertEqual(review_upload(new, live).changes, [])

    def test_rows_with_one_identity_are_matched_identical_first_then_in_order(self):
        live = self.upload("live", [_row(y1=10.0, remarks="a"), _row(y1=20.0, remarks="b")], live=True)
        same = self.upload("same", [_row(y1=20.0, remarks="b"), _row(y1=10.0, remarks="a")], pending=True)
        self.assertEqual(review_upload(same, live).changes, [])
        edited = self.upload("edited", [_row(y1=10.0, remarks="a"), _row(y1=25.0, remarks="b")], pending=True)
        review = review_upload(edited, live)
        self.assertEqual([(c.kind, c.details) for c in review.changes], [("changed", [("Year 1 BG", "20", "25")])])

    def test_remarks_and_mg_changes_are_details(self):
        live = self.upload("live", [_row(mg_yr1=30.0, remarks="old rule")], live=True)
        new = self.upload("new", [_row(mg_yr1=31.0, remarks="new rule")], pending=True)
        details = review_upload(new, live).changes[0].details
        self.assertEqual(details, [("Year 1 MG", "30", "31"), ("Remarks", "old rule", "new rule")])

    def test_big_moves_are_flagged_and_listed(self):
        live = self.upload("live", [_row(y1=20.0), _row("Beta Plan", "5", 10.0)], live=True)
        new = self.upload("new", [_row(y1=20.0 + BIG_MOVE_POINTS), _row("Beta Plan", "5", 14.0)], pending=True)
        review = review_upload(new, live)
        self.assertEqual([c.big for c in review.changes], [True, False])
        check = next(c for c in review.checks if c.title.startswith("BG payout moved by"))
        self.assertEqual((check.level, check.count), ("warn", 1))
        self.assertIn("Alpha Plan", check.examples[0])
        self.assertEqual(review.totals["big"], 1)

    def test_year_one_ups_and_downs_are_counted(self):
        live = self.upload("live", [_row(y1=20.0), _row("Beta Plan", "5", 30.0), _row("Gamma Plan", "8", 5.0)], live=True)
        new = self.upload("new", [_row(y1=25.0), _row("Beta Plan", "5", 28.0), _row("Gamma Plan", "8", 5.0)], pending=True)
        totals = review_upload(new, live).totals
        self.assertEqual((totals["up"], totals["down"], totals["changed"]), (1, 1, 2))

    def test_a_bg_payout_above_its_own_mg_is_flagged(self):
        new = self.upload("new", [_row(y1=50.0, mg_yr1=40.0), _row("Beta Plan", "5", 10.0, mg_yr1=15.0),
                                  _row("Gamma Plan", "8", 10.0)], pending=True)
        review = review_upload(new, None)
        check = next(c for c in review.checks if c.title == "BG payout above its own MG")
        self.assertEqual((check.level, check.count), ("warn", 1))
        self.assertIn("Year 1 BG 50 is above Year 1 MG 40", check.examples[0])

    def test_wording_the_rate_checker_cannot_read_is_flagged(self):
        rows = [_row(ppt="Five Pay"), _row("B", ppt="10", pt="forever"), _row("C", ppt="Single Pay", pt="Whole Life"),
                _row("D", ppt=">=10", pt="20"), _row("E", ppt="All", pt="Any"), _row("F", ppt="", pt="")]
        review = review_upload(self.upload("new", rows, pending=True), None)
        check = next(c for c in review.checks if c.title.startswith("PPT or PT wording"))
        self.assertEqual((check.level, check.count), ("warn", 2))
        self.assertIn("Acme Life: PPT 'Five Pay' (1 rows)", check.examples)
        self.assertIn("Acme Life: PT 'forever' (1 rows)", check.examples)

    def test_plan_mappings_and_insurer_aliases_that_would_stop_working_are_flagged(self):
        live = self.upload("live", [_row("Alpha Plan"), _row("Beta Plan", "5")], live=True)
        new = self.upload("new", [_row("Beta Plan", "5")], pending=True)
        LifePlanMapping.objects.create(insurer="Acme Life", plan_name="Alpha MIS", plan_key="alpha mis",
                                       grid_products="Alpha Plan")
        LifePlanMapping.objects.create(insurer="Acme Life", plan_name="Both MIS", plan_key="both mis",
                                       grid_products="Alpha Plan\nBeta Plan")
        LifePlanMapping.objects.create(insurer="Acme Life", plan_name="Beta MIS", plan_key="beta mis",
                                       grid_products="Beta Plan")
        review = review_upload(new, live)
        broken = next(c for c in review.checks if c.title == "Plan mappings that would stop working")
        self.assertEqual((broken.level, broken.count), ("warn", 1))
        self.assertIn("'Alpha MIS' reached 1 product(s) in the live grid and reaches none here", broken.examples[0])
        fewer = next(c for c in review.checks if c.title == "Plan mappings that reach fewer products")
        self.assertEqual((fewer.level, fewer.count), ("info", 1))

        gone = self.upload("gone", [_row("Beta Plan", insurer="Other Life")], pending=True)
        LifeInsurerAlias.objects.create(mis_name="Acme Life Ins Co", name_key="acme life ins co", insurer="Acme Life")
        alias = next(c for c in review_upload(gone, live).checks if c.title == "Insurer aliases that would stop working")
        self.assertEqual((alias.level, alias.count), ("warn", 1))
        self.assertIn("Acme Life: 1 MIS spelling(s), for example 'Acme Life Ins Co'", alias.examples)

    def test_the_insurer_table_says_what_looks_wrong(self):
        live = self.upload("live", [
            *[_row(f"A{n}", insurer="Shrinking Life") for n in range(4)],
            *[_row(f"B{n}", insurer="Gone Life") for n in range(2)],
            _row("C", insurer="Gamma Life"), _row("D", insurer="Delta Life (Ltd)"), _row("E", insurer="Steady Life")], live=True)
        new = self.upload("new", [
            *[_row(f"A{n}", insurer="Shrinking Life") for n in range(2)],
            _row("C", insurer="Gamma  Life"), _row("D", insurer="Delta Life Ltd"), _row("E", insurer="Steady Life"),
            _row("F", insurer="Brand New Life")], pending=True)
        table = {row["name"]: row for row in review_upload(new, live).insurers}
        flags = {name: [text for _, text in row["flags"]] for name, row in table.items()}
        self.assertEqual(flags["Steady Life"], [])
        self.assertEqual(flags["Shrinking Life"], ["Down from 4 to 2 rows"])
        self.assertEqual(flags["Gone Life"], ["Not in this upload (2 rows in the live grid)"])
        self.assertEqual(flags["Brand New Life"], ["New insurer"])
        self.assertTrue(any("Spelled 'Gamma  Life' here and 'Gamma Life' in the live grid" in text
                            for text in flags["Gamma  Life"]))
        self.assertTrue(any("Looks like 'Delta Life (Ltd)' in the live grid" in text for text in flags["Delta Life Ltd"]))
        self.assertTrue(any("'Delta Life Ltd' looks like the same insurer" in text for text in flags["Delta Life (Ltd)"]))
        self.assertEqual((table["Shrinking Life"]["before"], table["Shrinking Life"]["after"],
                          table["Shrinking Life"]["removed"]), (4, 2, 2))

    def test_without_a_live_grid_there_is_nothing_to_compare_but_the_checks_still_run(self):
        new = self.upload("new", [_row(ppt="Five Pay"), _row("B", y1=50.0, mg_yr1=40.0)], pending=True)
        review = review_upload(new, None)
        self.assertEqual((review.totals["before"], review.totals["after"], review.changes), (0, 2, []))
        self.assertEqual(review.insurers[0]["flags"], [])
        self.assertEqual({c.title for c in review.checks}, {"BG payout above its own MG",
                                                            "PPT or PT wording the Rate Checker cannot read"})

    def test_rows_that_are_only_a_written_rule_are_noted(self):
        rule = "Year 1: 30% of 1st year commission."
        new = self.upload("new", [_row(), _row("Bonus Commission", "", None, remarks=rule)], pending=True)
        check = next(c for c in review_upload(new, None).checks if c.title.startswith("Rows that are a written rule"))
        self.assertEqual((check.level, check.count), ("info", 1))
        self.assertIn(rule, check.examples[0])
        # a row with a number is not one, and the rule-only row is not a "no first-year payout" row either
        self.assertFalse(any(c.title == "Rows with no first-year BG payout" for c in review_upload(new, None).checks))

    def test_rows_with_no_first_year_payout_are_noted(self):
        new = self.upload("new", [_row(y1=None, payout_yr2=2.5), _row("B")], pending=True)
        check = next(c for c in review_upload(new, None).checks if c.title == "Rows with no first-year BG payout")
        self.assertEqual((check.level, check.count), ("info", 1))


class LifeUploadServiceTests(LifeReviewMixin, TestCase):
    def _result(self, rows=None):
        return read_standard_file(_xlsx(rows or [_line()]), "g.xlsx")

    def test_a_pending_upload_leaves_the_live_grid_alone(self):
        live = save_life_upload(self._result(), label="Oct'26", file_name="a.xlsx", user=None)
        pending = save_life_upload(self._result([_line(y1=30)]), label="Nov'26", file_name="b.xlsx", user=None,
                                   make_live=False)
        live.refresh_from_db()
        self.assertEqual((live.is_active, live.is_pending), (True, False))
        self.assertEqual((pending.is_active, pending.is_pending), (False, True))
        self.assertEqual(active_life_upload(), live)

    def test_making_a_pending_upload_live_clears_the_flag_and_retires_the_old_one(self):
        live = save_life_upload(self._result(), label="Oct'26", file_name="a.xlsx", user=None)
        pending = save_life_upload(self._result(), label="Nov'26", file_name="b.xlsx", user=None, make_live=False)
        activate_life_upload(pending)
        live.refresh_from_db(), pending.refresh_from_db()
        self.assertEqual((pending.is_active, pending.is_pending), (True, False))
        self.assertEqual((live.is_active, live.is_pending), (False, False))

    def test_only_a_pending_upload_can_be_discarded(self):
        live = save_life_upload(self._result(), label="Oct'26", file_name="a.xlsx", user=None)
        pending = save_life_upload(self._result(), label="Nov'26", file_name="b.xlsx", user=None, make_live=False)
        with self.assertRaises(ValueError):                         # the live grid
            discard_life_upload(live)
        activate_life_upload(pending)
        live.refresh_from_db()
        with self.assertRaises(ValueError):                         # an earlier upload is history
            discard_life_upload(live)
        later = save_life_upload(self._result(), label="Dec'26", file_name="c.xlsx", user=None, make_live=False)
        rows_before = LifeRate.objects.count()
        discard_life_upload(later)
        self.assertFalse(LifeGridUpload.objects.filter(pk=later.pk).exists())
        self.assertEqual(LifeRate.objects.count(), rows_before - 1)

    def test_notes_and_source_rows_are_stored(self):
        result = read_standard_file(_xlsx([_line()], notes=[["Acme Life", "Terms apply"]]), "g.xlsx")
        upload = save_life_upload(result, label="Nov'26", file_name="g.xlsx", user=None, make_live=False)
        self.assertEqual(LifeInsurerNote.objects.get(upload=upload).text, "Terms apply")
        row = upload.rates.get()
        self.assertEqual((row.source_ref, row.sheet, row.is_edited, row.payout_yr1), ("Row 2", "", False, 20.0))

    def test_default_label_knows_csv(self):
        self.assertEqual(default_label("Life grid Nov 2026.csv"), "Nov'26")
        self.assertEqual(default_label("whatever.csv"), "whatever")


class LifeStandardViewMixin:
    def setUp(self):
        LifeInsurerAlias.objects.all().delete()
        LifePlanMapping.objects.all().delete()
        self.viewer_group = Group.objects.get_or_create(name="Can_View_Life_Payout_Grid")[0]
        self.admin_group = Group.objects.get_or_create(name="Can_View_Life_Rate_Master")[0]
        self.viewer = User.objects.create_user(username="viewer", password="a-strong-test-password-1")
        self.viewer.groups.add(self.viewer_group)
        self.manager = User.objects.create_user(username="manager", password="a-strong-test-password-1")
        self.manager.groups.add(self.viewer_group, self.admin_group)
        self.client.force_login(self.manager)

    def live(self, label="Oct'26", rows=None):
        upload = LifeGridUpload.objects.create(label=label, is_active=True)
        for position, values in enumerate(rows or [_row(), _row("Beta Plan", "5", 10.0)]):
            LifeRate.objects.create(upload=upload, position=position, **{"insurer": "Acme Life", **values})
        upload.row_count = upload.rates.count()
        upload.save()
        return upload

    def post_standard(self, rows=None, name="grid.xlsx", content=None, **extra):
        data = {"standard_file": SimpleUploadedFile(name, content if content is not None else _xlsx(rows or [_line()])),
                **extra}
        return self.client.post(reverse("life_rate_master_upload_standard"), data)


class LifeStandardUploadViewTests(LifeStandardViewMixin, TestCase):
    def test_a_standard_file_is_stored_but_not_live_and_opens_its_review(self):
        live = self.live()
        response = self.post_standard([_line(y1=25), _line(product="Beta Plan", ppt="5", y1=10)],
                                      name="Life grid Nov 2026.xlsx")
        pending = LifeGridUpload.objects.get(is_pending=True)
        self.assertRedirects(response, reverse("life_rate_master_review", args=[pending.pk]))
        self.assertEqual((pending.label, pending.is_active, pending.row_count, pending.insurer_count,
                          pending.uploaded_by), ("Nov'26", False, 2, 1, self.manager))
        self.assertEqual(active_life_upload(), live)                                  # the live grid is untouched
        self.assertEqual(LifeGridUpload.objects.filter(is_active=True).count(), 1)
        log = AuditLog.objects.get(action="LIFE GRID UPLOAD")
        self.assertIn("Life grid Nov 2026.xlsx", log.details)
        self.assertIn("for review", log.details)

    def test_a_csv_works_and_a_label_can_be_typed(self):
        csv = b"insurer,product,payout_yr1\r\nAcme Life,Plan,12.5\r\n"
        self.post_standard(name="g.csv", content=csv, label="Festive grid")
        upload = LifeGridUpload.objects.get()
        self.assertEqual((upload.label, upload.is_pending, upload.rates.get().payout_yr1), ("Festive grid", True, 12.5))

    def test_the_checker_and_the_mis_engine_ignore_a_pending_upload(self):
        live = self.live()
        self.post_standard([_line(product="Only In Pending", y1=99)])
        found, _ = search_life_rates(active_life_upload(), selected_life_filters(QueryDict("")))
        self.assertEqual({row.product for row in found}, {"Alpha Plan", "Beta Plan"})
        grid = life_mis.load_life_grid()
        self.assertEqual((grid.upload_id, grid.label), (live.pk, "Oct'26"))
        self.assertEqual({row.product for rows in grid.rows.values() for row in rows}, {"Alpha Plan", "Beta Plan"})

    def test_a_refused_file_changes_nothing_and_its_problems_show_once(self):
        self.live()
        response = self.post_standard([_line(insurer=""), _line(y1=140)], name="bad.xlsx")
        self.assertRedirects(response, reverse("life_rate_master_uploads"), fetch_redirect_response=False)
        self.assertEqual(LifeGridUpload.objects.count(), 1)
        self.assertFalse(AuditLog.objects.filter(action="LIFE GRID UPLOAD").exists())
        page = self.client.get(reverse("life_rate_master_uploads"))
        self.assertContains(page, "'bad.xlsx' was not uploaded")
        self.assertContains(page, "Row 2: insurer is empty.")
        self.assertContains(page, "Row 3: payout_yr1: 140 is outside 0-100")
        self.assertContains(page, "Fix 2 problems")
        self.assertNotContains(self.client.get(reverse("life_rate_master_uploads")), "was not uploaded")

    def test_more_problems_than_are_listed_are_counted(self):
        rows = [_line(product=f"P{n}", y1=None) for n in range(60)]
        self.post_standard(rows)
        page = self.client.get(reverse("life_rate_master_uploads"))
        self.assertContains(page, "Fix 61 problems")
        self.assertContains(page, "and 21 more")                       # 40 are listed

    def test_wrong_and_missing_and_oversized_files(self):
        self.post_standard(name="grid.txt", content=b"x")
        self.assertContains(self.client.get(reverse("life_rate_master_uploads")), "must be an .xlsx or a .csv file")
        response = self.client.post(reverse("life_rate_master_upload_standard"), {}, follow=True)
        self.assertContains(response, "Choose the standard file")
        with mock.patch("insurance.views.life_rates.MAX_UPLOAD_BYTES", 10):
            response = self.client.post(reverse("life_rate_master_upload_standard"), {
                "standard_file": SimpleUploadedFile("big.csv", b"insurer,product,payout_yr1\r\nA,B,1\r\n")}, follow=True)
        self.assertContains(response, "is larger than")
        self.assertEqual(LifeGridUpload.objects.count(), 0)

    def test_the_old_workbook_goes_live_at_once_unless_review_is_ticked(self):
        live = self.live()
        data = {"workbook": SimpleUploadedFile("Life_Grid_working-Nov'26.xlsx", _workbook(_edelweiss).read())}
        response = self.client.post(reverse("life_rate_master_upload"), data)
        self.assertRedirects(response, reverse("life_rate_master"))
        self.assertTrue(LifeGridUpload.objects.get(label="Nov'26").is_active)

        data = {"workbook": SimpleUploadedFile("Life_Grid_working-Dec'26.xlsx", _workbook(_edelweiss).read()), "review": "1"}
        response = self.client.post(reverse("life_rate_master_upload"), data)
        pending = LifeGridUpload.objects.get(label="Dec'26")
        self.assertRedirects(response, reverse("life_rate_master_review", args=[pending.pk]))
        self.assertEqual((pending.is_active, pending.is_pending), (False, True))
        self.assertTrue(LifeGridUpload.objects.get(label="Nov'26").is_active)         # unchanged
        live.refresh_from_db()
        self.assertFalse(live.is_active)

    def test_only_the_rate_master_group_may_use_the_new_pages_and_the_uploads_are_post_only(self):
        pending = LifeGridUpload.objects.create(label="Nov'26", is_pending=True)
        self.client.force_login(self.viewer)
        for name, args in (("life_rate_master_upload_standard", ()), ("life_rate_master_template", ()),
                           ("life_rate_master_review", (pending.pk,)), ("life_rate_master_discard", (pending.pk,))):
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 403, name)
            self.assertEqual(self.client.post(reverse(name, args=args)).status_code, 403, name)
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(reverse("life_rate_master_upload_standard")).status_code, 405)
        self.assertEqual(self.client.get(reverse("life_rate_master_discard", args=[pending.pk])).status_code, 405)


class LifeReviewPageTests(LifeStandardViewMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.live_upload = self.live(rows=[_row(), _row("Beta Plan", "5", 10.0), _row("Gamma Plan", "8", 15.0),
                                           _row("Other", "7", 5.0, insurer="Other Life")])
        self.pending = LifeGridUpload.objects.create(label="Nov'26", file_name="nov.xlsx", is_pending=True,
                                                     uploaded_by=self.manager, warnings="Columns not recognised and ignored: Mystery.")
        for position, values in enumerate([_row(y1=35.0), _row("Beta Plan", "5", 11.0), _row("Delta Plan", "7", 9.0),
                                           _row("Other", "7", 5.0, insurer="Other Life")]):
            LifeRate.objects.create(upload=self.pending, position=position, **{"insurer": "Acme Life", **values})
        self.pending.row_count = 4
        self.pending.save()

    def test_the_review_shows_the_difference_and_what_to_do(self):
        response = self.client.get(reverse("life_rate_master_review", args=[self.pending.pk]))
        self.assertEqual(response.status_code, 200)
        review = response.context["review"]
        self.assertEqual((review.totals["changed"], review.totals["added"], review.totals["removed"],
                          review.totals["unchanged"]), (2, 1, 1, 1))
        self.assertContains(response, "Review: Nov&#x27;26")
        self.assertContains(response, "WAITING FOR REVIEW")
        self.assertContains(response, "keep using <strong>Oct&#x27;26</strong>")
        self.assertContains(response, reverse("life_rate_master_activate", args=[self.pending.pk]))
        self.assertContains(response, reverse("life_rate_master_discard", args=[self.pending.pk]))
        self.assertContains(response, "Year 1 BG")
        self.assertContains(response, "big move")                                    # 20 -> 35 is 15 points
        self.assertContains(response, "Notes recorded when the file was read")
        self.assertContains(response, "Columns not recognised and ignored: Mystery.")

    def test_the_filters_and_the_pager_narrow_the_changes(self):
        url = reverse("life_rate_master_review", args=[self.pending.pk])
        shown = lambda **params: [(c.kind, c.row["product"]) for c in self.client.get(url, params).context["page_obj"]]
        self.assertEqual(shown(kind="added"), [("added", "Delta Plan")])
        self.assertEqual(shown(kind="removed"), [("removed", "Gamma Plan")])
        self.assertEqual(len(shown()), 4)                                            # 2 changed, 1 added, 1 removed
        self.assertEqual(len(shown(kind="nonsense")), 4)                             # an unknown filter counts as not picked
        self.assertEqual(shown(insurer="Other Life"), [])
        with mock.patch("insurance.views.life_rates.REVIEW_PAGE_SIZE", 2):
            page = self.client.get(url).context["page_obj"]
            self.assertEqual((page.paginator.num_pages, len(page)), (2, 2))

    def test_a_review_without_a_live_grid(self):
        self.live_upload.is_active = False
        self.live_upload.save()
        response = self.client.get(reverse("life_rate_master_review", args=[self.pending.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No Life grid is live yet")
        self.assertNotContains(response, "What changed")

    def test_the_live_upload_has_nothing_to_review(self):
        response = self.client.get(reverse("life_rate_master_review", args=[self.live_upload.pk]), follow=True)
        self.assertRedirects(response, reverse("life_rate_master_uploads"))
        self.assertContains(response, "already the live grid")
        self.assertEqual(self.client.get(reverse("life_rate_master_review", args=[99999])).status_code, 404)

    def test_make_live_from_the_review(self):
        self.client.post(reverse("life_rate_master_activate", args=[self.pending.pk]))
        self.pending.refresh_from_db()
        self.live_upload.refresh_from_db()
        self.assertEqual((self.pending.is_active, self.pending.is_pending), (True, False))
        self.assertEqual((self.live_upload.is_active, self.live_upload.is_pending), (False, False))
        self.assertEqual(active_life_upload(), self.pending)
        self.assertTrue(AuditLog.objects.filter(action="LIFE GRID UPLOAD", details__contains="live Life grid").exists())

    def test_discard_deletes_only_an_upload_that_is_waiting(self):
        rows = self.pending.rates.count()
        response = self.client.post(reverse("life_rate_master_discard", args=[self.pending.pk]))
        self.assertRedirects(response, reverse("life_rate_master_uploads"), fetch_redirect_response=False)
        self.assertFalse(LifeGridUpload.objects.filter(pk=self.pending.pk).exists())
        self.assertEqual(LifeRate.objects.filter(upload=self.pending.pk).count(), 0)
        self.assertGreater(rows, 0)
        self.assertIn("Discarded the upload 'Nov'26'", AuditLog.objects.filter(action="LIFE GRID UPLOAD").latest("id").details)

        old = LifeGridUpload.objects.create(label="Sep'26")
        for upload in (self.live_upload, old):
            response = self.client.post(reverse("life_rate_master_discard", args=[upload.pk]), follow=True)
            self.assertContains(response, "Only an upload that is waiting for review can be discarded.")
            self.assertTrue(LifeGridUpload.objects.filter(pk=upload.pk).exists())


class LifeUploadsPageTests(LifeStandardViewMixin, TestCase):
    def test_the_uploads_page_shows_the_standard_file_first_and_each_upload_with_what_it_needs(self):
        old = LifeGridUpload.objects.create(label="Sep'26")
        live = self.live()
        pending = LifeGridUpload.objects.create(label="Nov'26", is_pending=True)
        response = self.client.get(reverse("life_rate_master_uploads"))
        self.assertContains(response, "Upload the standard file")
        self.assertContains(response, reverse("life_rate_master_upload_standard"))
        self.assertContains(response, reverse("life_rate_master_template"))
        self.assertContains(response, reverse("life_rate_master_export"))              # a live grid can be exported
        self.assertContains(response, 'name="review"')                                  # the old upload's review option
        self.assertContains(response, "WAITING FOR REVIEW")
        self.assertContains(response, reverse("life_rate_master_review", args=[pending.pk]))
        self.assertContains(response, reverse("life_rate_master_discard", args=[pending.pk]))
        self.assertContains(response, reverse("life_rate_master_activate", args=[old.pk]))
        self.assertNotContains(response, reverse("life_rate_master_activate", args=[pending.pk]))   # review first
        self.assertNotContains(response, reverse("life_rate_master_discard", args=[live.pk]))

    def test_nothing_uploaded_yet(self):
        response = self.client.get(reverse("life_rate_master_uploads"))
        self.assertContains(response, "Nothing uploaded yet")
        self.assertNotContains(response, reverse("life_rate_master_export"))

    def test_the_master_pages_say_when_the_upload_they_show_is_waiting_for_review(self):
        self.live()
        pending = LifeGridUpload.objects.create(label="Nov'26", is_pending=True)
        LifeRate.objects.create(upload=pending, insurer="Acme Life", product="Alpha Plan", payout_yr1=20.0)
        response = self.client.get(reverse("life_rate_master"), {"upload": pending.pk})
        self.assertContains(response, "which is waiting for review and is not live")
        self.assertContains(response, reverse("life_rate_master_review", args=[pending.pk]))
        self.assertNotContains(response, "an earlier upload")


class LifeTemplateAndRoundTripTests(LifeStandardViewMixin, TestCase):
    def test_the_template_downloads_as_a_workbook(self):
        response = self.client.get(reverse("life_rate_master_template"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.assertEqual(response["Content-Disposition"], 'attachment; filename="life_standard_template.xlsx"')
        self.assertEqual(load_workbook(io.BytesIO(response.content)).sheetnames, ["Rates", "Notes", "Read me"])

    def test_an_export_is_a_valid_standard_file_and_loads_back_the_same(self):
        upload = LifeGridUpload.objects.create(label="Oct'26", is_active=True)
        rows = [
            dict(insurer="Acme Life", sheet="Acme", section="First year", category="PAR(Trad)", product="Alpha Plan",
                 variant="Option A", ppt=">=10", pt="20", condition="Offline", payout_yr1=50.0, payout_deferred=2.5,
                 payout_yr2=1.25, payout_yr6_plus=0.4237, mg_yr1=66.7, mg_yr2=5.0, remarks="Valid till 30th Nov",
                 source_ref="Acme!K3", is_edited=True),
            dict(insurer="Acme Life", product="Beta Plan", ppt="5-10", payout_yr1=0.0, source_ref="Added on screen"),
            dict(insurer="Bravo Life", category="TERM", product="Gamma Plan", ppt="Single Pay", payout_yr1=12.0),
            dict(insurer="Bravo Life", product="Bonus Commission", ppt="Excluding Single Premium Plans",
                 remarks="Year 1: 30% of 1st year commission."),                       # a rule with no number
        ]
        for position, values in enumerate(rows):
            LifeRate.objects.create(upload=upload, position=position, **values)
        LifeInsurerNote.objects.create(upload=upload, insurer="Acme Life", text="GST excluded\nClawback in year 1")
        LifeInsurerNote.objects.create(upload=upload, insurer="Bravo Life", text="")           # an emptied note: not exported

        response = self.client.get(reverse("life_rate_master_export"))
        self.assertEqual(load_workbook(io.BytesIO(response.content)).sheetnames, ["Life rates", "Notes"])
        result = read_standard_file(response.content, "life_rates_Oct26.xlsx")

        self.assertEqual(result.warnings, [])                                            # the export-only columns are not "unknown"
        self.assertEqual(len(result.rows), 4)
        first, second, third, fourth = result.rows
        self.assertEqual((fourth["y1"], fourth["remarks"]), (None, "Year 1: 30% of 1st year commission."))
        for column in ("insurer", "sheet", "section", "category", "product", "variant", "ppt", "pt", "condition",
                       "remarks"):
            self.assertEqual(first[column], rows[0][column], column)
        self.assertEqual((first["y1"], first["d"], first["y2"], first["y6"], first["m_y1"], first["m_y2"]),
                         (50.0, 2.5, 1.25, 0.4237, 66.7, 5.0))
        self.assertEqual(first["source_ref"], "Acme!K3")                                  # a real source is kept
        self.assertEqual((second["ppt"], second["y1"], third["ppt"]), ("5-10", 0.0, "Single Pay"))
        self.assertEqual(second["source_ref"], "Row 3")                                   # "Added on screen" is not carried over
        self.assertEqual(result.notes, {"Acme Life": "GST excluded\nClawback in year 1"})
        self.assertEqual(result.sheet_counts, {"Acme Life": 2, "Bravo Life": 2})

    def test_an_edited_export_uploads_and_its_review_shows_exactly_the_edit(self):
        live = self.live(rows=[_row(), _row("Beta Plan", "5", 10.0)])
        raw = self.client.get(reverse("life_rate_master_export")).content
        wb = load_workbook(io.BytesIO(raw))
        ws = wb.active
        header = [cell.value for cell in ws[1]]
        ws.cell(row=2, column=header.index("payout_yr1") + 1, value=21.5)               # edit one payout
        out = io.BytesIO()
        wb.save(out)
        self.post_standard(name="edited.xlsx", content=out.getvalue())
        pending = LifeGridUpload.objects.get(is_pending=True)
        review = self.client.get(reverse("life_rate_master_review", args=[pending.pk])).context["review"]
        self.assertEqual((review.totals["changed"], review.totals["added"], review.totals["removed"],
                          review.totals["unchanged"]), (1, 0, 0, 1))
        self.assertEqual(review.changes[0].details, [("Year 1 BG", "20", "21.5")])
        self.assertEqual(review.attention, 0)
        live.refresh_from_db()
        self.assertTrue(live.is_active)
