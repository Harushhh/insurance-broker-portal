"""Life payout rates: the workbook reader, saving an upload, the Rate Checker, the admin pages and their access wiring.

The reader is tested on small workbooks laid out like the real Edelweiss, LIC and Bajaj sheets (same anchor headers,
same column letters), because the real workbook (it holds the insurer rates) is not in the repository.
"""
import io
import re
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook, load_workbook

from insurance.constants import SECURITY_AUDIT_LOG_ACTIONS
from insurance.forms import LifeBulkUpdateForm
from insurance.models import AuditLog, LifeGridUpload, LifeInsurerNote, LifeRate
from insurance.services.life_rates import (
    BULK_FIELD_LABELS, EXPORT_COLUMNS, LIFE_MAX_RESULTS, activate_life_upload, bulk_audit_details, bulk_field_kind,
    default_label, life_notes_for, plan_bulk_update, save_life_upload, search_life_rates, selected_life_filters,
)
from insurance.services import life_filters as lf
from insurance.services.life_workbook import LifeLayoutError, read_life_workbook


def _edelweiss(wb, *, header="Catogory"):
    ws = wb.create_sheet("Edelweiss")
    ws["A1"], ws["H1"] = "Final MG", "Final BG"
    ws["A2"], ws["B2"], ws["C2"] = header, "Product", "PPT"
    ws["D2"], ws["K2"] = "Fresh", "Fresh"
    ws["A3"], ws["B3"] = "Par Saving", "Flexi Dream Plan (FDP)"
    for row, (ppt, mg, bg) in enumerate([(5, 0.3, 0.2), (8, 0.38, 0.28), (">=12", 0.65, 0.55)], start=3):
        ws[f"C{row}"], ws[f"D{row}"], ws[f"K{row}"] = ppt, mg, bg
    ws.merge_cells("A3:A5")
    ws.merge_cells("B3:B5")
    ws["A7"], ws["A8"] = "Notes", "Payout recovery will be initiated if 13M persistency is not maintained"
    ws["F3"] = 0.1                                   # a working column of the MG side: must never be read
    return ws


def _lic(wb):
    ws = wb.create_sheet("LIC")
    ws["A3"], ws["B3"], ws["M3"], ws["N3"], ws["O3"], ws["P3"] = (
        "PLANS", "Premium Paying Term", "1st Year", "2nd & 3rd Year", "4th & 5th Year", "6th Year Onwards")
    ws["A5"] = "714 - LIC's New Endowment Plan\n715 - LIC's New Jeevan Anand"
    ws["B5"] = "15 years & above"
    ws["M5"], ws["N5"], ws["O5"], ws["P5"] = 15, 3.75, 3.75, 2.5
    ws["A6"] = "752 - LIC's SIIP\nAP Rs 40,000 to Rs 74,000"
    ws["B6"] = "10 to 25 years"
    ws["M6"], ws["N6"], ws["O6"], ws["P6"] = 1.25, 1.75, 2.25, "2 and 3.5% in last year"
    return ws


def _bajaj(wb):
    ws = wb.create_sheet("Bajaj")
    for col, text in {"AB": "Category", "AC": "Product", "AD": "Varient", "AE": "PPT", "AF": "Share at Issuance"}.items():
        ws[f"{col}3"] = text
    for row, (ppt, y1, y2) in enumerate([(5, 0.355, 0.025), (6, 0.39, 0.025), (">=10", 0.5, "10 & 15 Pay - 2%")], start=4):
        ws[f"AB{row}"], ws[f"AC{row}"], ws[f"AD{row}"], ws[f"AE{row}"] = "PAR(Trad)", "ACE", "-", ppt
        ws[f"AF{row}"], ws[f"AH{row}"] = y1, y2
    ws["AM4"] = "Valid till 30th Nov"
    return ws


def _workbook(*makers, **kwargs):
    wb = Workbook()
    wb.remove(wb.active)
    for make in makers:
        make(wb, **kwargs) if make is _edelweiss else make(wb)
    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    return out


class LifeWorkbookReaderTests(SimpleTestCase):
    """The fixed-layout reader: what it takes from a sheet and when it refuses the file."""

    def test_reads_labels_and_the_bg_payout_only(self):
        result = read_life_workbook(_workbook(_edelweiss))
        rows = [r for r in result.rows if r["insurer"] == "Edelweiss Life"]
        self.assertEqual([r["ppt"] for r in rows], ["5", "8", ">=12"])
        # The merged category and product fill down; payouts are percents of the BG column K (0.2 -> 20).
        self.assertTrue(all(r["category"] == "Par Saving" and r["product"] == "Flexi Dream Plan (FDP)" for r in rows))
        self.assertEqual([r["y1"] for r in rows], [20.0, 28.0, 55.0])
        # Nothing from the MG side (the 0.3 / 0.38 / 0.65 or the working column) reaches a row.
        for r in rows:
            self.assertEqual([r[f] for f in ("d", "y2", "y3", "y4", "y5", "y6")], [None] * 6)
            self.assertNotIn(0.1, [r["y1"]])
        self.assertEqual(rows[0]["source_ref"], "Edelweiss!K3")

    def test_notes_below_the_table_are_kept_per_insurer(self):
        result = read_life_workbook(_workbook(_edelweiss))
        self.assertIn("persistency", result.notes["Edelweiss Life"])

    def test_missing_sheets_are_warnings_not_errors(self):
        result = read_life_workbook(_workbook(_edelweiss))
        self.assertTrue(any("Tata AIA Life" in w and "not in the workbook" in w for w in result.warnings))
        self.assertEqual(result.sheet_counts, {"Edelweiss Life": 3})

    def test_unknown_sheets_are_reported_and_ignored(self):
        def extra(wb):
            wb.create_sheet("Ratemas")
            wb.create_sheet("Mystery Life")
        result = read_life_workbook(_workbook(_edelweiss, extra))
        self.assertTrue(any("Mystery Life" in w for w in result.warnings))
        self.assertFalse(any("Ratemas" in w and "not a known" in w for w in result.warnings))

    def test_lic_plans_split_into_rows_and_percents_stay_percents(self):
        result = read_life_workbook(_workbook(_lic))
        rows = [r for r in result.rows if r["insurer"] == "LIC of India"]
        self.assertEqual([r["product"] for r in rows],
                         ["714 - LIC's New Endowment Plan", "715 - LIC's New Jeevan Anand", "752 - LIC's SIIP"])
        self.assertEqual((rows[0]["y1"], rows[0]["y2"], rows[0]["y3"], rows[0]["y4"], rows[0]["y5"], rows[0]["y6"]),
                         (15.0, 3.75, 3.75, 3.75, 3.75, 2.5))
        self.assertEqual(rows[2]["condition"], "AP Rs 40,000 to Rs 74,000")
        # A written rule in a payout cell becomes a remark, not a number.
        self.assertIsNone(rows[2]["y6"])
        self.assertIn("Year 6+: 2 and 3.5% in last year", rows[2]["remarks"])

    def test_bajaj_text_payouts_and_remark_columns(self):
        result = read_life_workbook(_workbook(_bajaj))
        rows = [r for r in result.rows if r["insurer"] == "Bajaj Allianz Life"]
        self.assertEqual([r["y1"] for r in rows], [35.5, 39.0, 50.0])
        self.assertEqual([r["y2"] for r in rows], [2.5, 2.5, None])
        self.assertIn("Year 2: 10 & 15 Pay - 2%", rows[2]["remarks"])
        self.assertIn("Valid till 30th Nov", rows[0]["remarks"])
        self.assertEqual(rows[0]["variant"], "")        # a "-" label means none

    def test_layout_change_stops_the_read_and_names_the_sheet(self):
        with self.assertRaises(LifeLayoutError) as caught:
            read_life_workbook(_workbook(_edelweiss, header="Segment"))
        message = str(caught.exception)
        self.assertIn("Edelweiss", message)
        self.assertIn("column A", message)

    def test_a_table_that_moved_down_is_still_found(self):
        def shifted(wb):
            ws = _edelweiss(wb)
            ws.insert_rows(1, amount=4)
        result = read_life_workbook(_workbook(shifted))
        self.assertEqual(len([r for r in result.rows if r["insurer"] == "Edelweiss Life"]), 3)

    def test_sheet_without_calculated_values_is_refused(self):
        def empty(wb):
            ws = wb.create_sheet("Edelweiss")
            ws["A2"], ws["B2"], ws["C2"] = "Catogory", "Product", "PPT"
            ws["A3"], ws["B3"], ws["C3"] = "Par", "Plan", 5          # labels but no payout in K
        with self.assertRaises(LifeLayoutError) as caught:
            read_life_workbook(_workbook(empty))
        self.assertIn("calculated values", str(caught.exception))

    def test_not_a_life_workbook(self):
        with self.assertRaises(LifeLayoutError):
            read_life_workbook(_workbook(lambda wb: wb.create_sheet("Sheet9")))
        with self.assertRaises(LifeLayoutError):
            read_life_workbook(io.BytesIO(b"this is not an excel file"))


class LifeFilterMappingTests(SimpleTestCase):
    """services/life_filters.py: the fixed dropdown values and how each reads against the workbook's wording."""

    def test_the_fixed_lists(self):
        self.assertEqual(lf.CATEGORIES, ("PAR", "NON PAR", "TERM", "ULIP", "TULIP", "ANNUITY / SAVINGS", "PENSION"))
        self.assertEqual(lf.PAYMENT_MODES, ("SP", "LP", "RP"))
        self.assertEqual(lf.PPT_CHOICES, tuple(str(n) for n in range(1, 14)) + (">13",))
        self.assertEqual(lf.PT_CHOICES, tuple(str(n) for n in range(1, 101)))

    def test_category_from_the_wording_then_the_plan_name(self):
        cases = [
            ("Non PAR(Trad)", "x", {"NON PAR"}), ("Trad- Non Par", "x", {"NON PAR"}), ("PAR(Trad)", "x", {"PAR"}),
            ("Par Saving", "Flexi Dream Plan", {"PAR"}), ("Saving- Non PAR", "Kotak Assured Savings Plan", {"NON PAR"}),
            ("Term- Non Par", "x", {"TERM"}), ("Protection", "SRP 3 Pay", {"TERM"}),
            ("Term/Protection (Protection deferment of 15% commercials)", "x", {"TERM"}),
            ("TULIP - Non Par", "x", {"TULIP"}), ("ULIP", "x", {"ULIP"}), ("Tulip", "x", {"TULIP"}),
            ("Annuaity- PAR", "Assured Gain Plus", {"ANNUITY / SAVINGS"}), ("Pension - Par", "Systematic Retirement Plan", {"PENSION"}),
            # the plan name adds PENSION without taking the row out of its wording's category
            ("Annuity", "GUARANTEED PENSION GOAL", {"ANNUITY / SAVINGS", "PENSION"}),
            ("Annuaity- Non Par", "Smart Pension Plus", {"ANNUITY / SAVINGS", "PENSION"}),
            ("Non-Par", "Fortune Guarantee Pension 10 Pay", {"NON PAR", "PENSION"}),
            ("ULIP - Annuity - Non Par", "Smart Pension Plan", {"ULIP", "PENSION"}),
            # no wording: the plan name's Term / Pension / Annuity / TULIP / ULIP words
            ("", "Gold Term - Life - Limited Pay", {"TERM"}), ("", "IndiaFirst Life TULIP - 6 Pay, 15PT", {"TULIP"}),
            ("", "Guaranteed Annuity Plan", {"ANNUITY / SAVINGS"}), ("", "Guaranteed Pension Plan", {"PENSION"}),
            ("", "Star ULIP Plus", {"ULIP"}),
            # nothing says: no category (shown only when Category is left blank)
            ("", "Century Plan", set()), ("", "Super Protection Plan", set()),
            ("ILP", "ILP 5 - 6 Pay Param Raksha Life Growth +", set()),
            ("ILP", "A Term Plan", set()),                    # a wording that maps to nothing is not re-sorted by name
        ]
        for category, product, expected in cases:
            self.assertEqual(set(lf.categories_of(category, product)), expected, (category, product))

    def test_ppt_wording_read_as_numbers_and_modes(self):
        cases = {
            "8": (((8, 8),), set()), "7 to 9": (((7, 9),), set()), "7-9": (((7, 9),), set()),
            "5 &6": (((5, 5), (6, 6)), set()), "5/6": (((5, 5), (6, 6)), set()), "5, 6 or 7": (((5, 5), (6, 6), (7, 7)), set()),
            "10+": (((10, None),), set()), ">=10": (((10, None),), set()), "12 & Above": (((12, None),), set()),
            "15 years & above": (((15, None),), set()), "12 Pay & Above": (((12, None),), set()),
            "10 to 14 years": (((10, 14),), set()), "<10": (((2, 9),), set()), "<=10": (((2, 10),), set()),
            "PPT - 5 Yrs": (((5, 5),), set()), "7 Pay": (((7, 7),), set()),
            "1": (((1, 1),), {"SP"}),                          # a PPT of exactly 1 is single pay
            "SP": ((), {"SP"}), "Single Pay": ((), {"SP"}), "Single Premium*": ((), {"SP"}),
            "RP": ((), {"RP"}), "Regular": ((), {"RP"}), "Pay/Regular Pay": ((), {"RP"}),
            "Limited": ((), {"LP"}), "Regular & Limited Pay": ((), {"LP", "RP"}),
            "Limited Pay - 10 to 30 Years": (((10, 30),), {"LP"}),
        }
        for text, (spans, modes) in cases.items():
            spec = lf.ppt_spec(text)
            self.assertEqual((spec.spans, set(spec.modes), spec.states_none), (spans, modes, False), text)
        for text in ("", "All", "Any", "PPT*6 capped at 55%", "Reg PPT ( 2.5* PPT capped at 35%)",
                     "Excluding Single Premium Plans and Plans 751 & 760", "Same as Policy Term"):
            self.assertTrue(lf.ppt_spec(text).states_none, text)

    def test_ppt_and_payment_mode_rules(self):
        def ok(text, mode="", pick=""):
            return lf.ppt_matches(lf.ppt_spec(text), mode, pick)
        # a number pick is numeric coverage; ">13" reaches 14 or more
        self.assertTrue(ok("7 to 9", pick="8") and not ok("7 to 9", pick="10"))
        self.assertTrue(ok("10+", pick="13") and ok("10+", pick=">13") and not ok("10+", pick="9"))
        self.assertTrue(ok("14", pick=">13") and not ok("13", pick=">13") and ok("13+", pick=">13"))
        self.assertTrue(ok("12 & Above", pick="12") and not ok("12 & Above", pick="11"))
        # no mode: rows that state no PPT stay, rows worded SP / RP / Limited are left out
        for blank in ("", "All"):
            self.assertTrue(ok(blank, pick="8") and ok(blank, pick=">13"))
        for worded in ("SP", "Single Pay", "RP", "Regular Pay", "Limited"):
            self.assertFalse(ok(worded, pick="8"), worded)
        self.assertTrue(ok("1", pick="1"))                     # picking 1 still finds the PPT 1 rows
        self.assertTrue(ok("7 to 9") and ok("SP"))             # nothing picked: everything passes
        # SP: the single-pay wording and PPT 1; the PPT pick is ignored
        self.assertTrue(ok("SP", "SP") and ok("Single Premium*", "SP") and ok("1", "SP") and ok("SP", "SP", "8"))
        self.assertFalse(ok("5", "SP") or ok("RP", "SP") or ok("", "SP"))
        # RP: the regular-pay wording, whatever PPT is picked
        self.assertTrue(ok("RP", "RP") and ok("Regular", "RP", "8") and ok("Regular & Limited Pay", "RP"))
        self.assertFalse(ok("5", "RP") or ok("SP", "RP"))
        # LP: every PPT number or range of 2+ years and the "Limited" wording; the pick narrows by number
        self.assertTrue(ok("5", "LP") and ok("7 to 9", "LP") and ok("10+", "LP") and ok("Limited", "LP"))
        self.assertFalse(ok("1", "LP") or ok("SP", "LP") or ok("RP", "LP") or ok("", "LP"))
        self.assertTrue(ok("7 to 9", "LP", "8") and not ok("7 to 9", "LP", "10"))
        self.assertTrue(ok("Limited", "LP", "8"))              # no number to narrow by: stays for any PPT
        self.assertTrue(ok("10+", "LP", ">13") and not ok("5", "LP", ">13"))

    def test_pt_wording_read_as_numbers(self):
        cases = {
            "20": ((20, 20),), "20 Years": ((20, 20),), "15/20": ((15, 15), (20, 20)), "12/15/20": ((12, 12), (15, 15), (20, 20)),
            ">=20": ((20, None),), ">20": ((21, None),), "<15": ((1, 14),), "15-19": ((15, 19),), "25 - 40": ((25, 40),),
            "5 to 20 Years": ((5, 20),), "PT -7 (PO Period 30)": ((7, 7),), "PT - 6/8 (PO Period 25 & 30)": ((6, 6), (8, 8)),
        }
        for text, spans in cases.items():
            self.assertEqual(lf.pt_spec(text).spans, spans, text)
            self.assertFalse(lf.pt_spec(text).any_term, text)

    def test_pt_rules(self):
        def ok(text, pick):
            return lf.pt_matches(lf.pt_spec(text), pick)
        self.assertTrue(ok("20", "20") and not ok("20", "25"))
        self.assertTrue(ok("15/20", "20") and ok(">=20", "40") and not ok(">=20", "19") and ok("15-19", "17"))
        # no PT, "Any", "All", regular pay and whole life match every PT
        for open_text in ("", "Any", "All", "RP", "Whole Life", "(100 minus Entry Age) years"):
            self.assertTrue(ok(open_text, "37"), open_text)
        self.assertTrue(ok("20", ""))                           # nothing picked


class LifeServiceTests(TestCase):
    """Saving uploads, the active upload and the search."""

    def _save(self, *makers, label="Oct'26"):
        return save_life_upload(read_life_workbook(_workbook(*makers)), label=label, file_name="grid.xlsx", user=None)

    def test_default_label_comes_from_the_file_name(self):
        self.assertEqual(default_label("Life_Grid_working-Oct'26.xlsx"), "Oct'26")
        self.assertEqual(default_label("Life grid Nov 2026.xlsx"), "Nov'26")
        self.assertEqual(default_label("whatever.xlsx"), "whatever")

    def test_a_new_upload_becomes_the_only_active_one_and_keeps_the_old_rows(self):
        first = self._save(_edelweiss, label="Oct'26")
        second = self._save(_edelweiss, _lic, label="Nov'26")
        first.refresh_from_db()
        self.assertFalse(first.is_active)
        self.assertTrue(second.is_active)
        self.assertEqual(LifeGridUpload.objects.filter(is_active=True).count(), 1)
        self.assertEqual(first.rates.count(), 3)
        self.assertEqual(second.row_count, second.rates.count())
        self.assertEqual(second.insurer_count, 2)
        self.assertEqual(LifeInsurerNote.objects.filter(upload=first).count(), 1)

    def test_switching_back_to_an_older_upload(self):
        first = self._save(_edelweiss, label="Oct'26")
        self._save(_edelweiss, label="Nov'26")
        activate_life_upload(first)
        self.assertEqual(list(LifeGridUpload.objects.filter(is_active=True)), [first])

    def test_warns_when_an_insurer_disappears_or_shrinks(self):
        self._save(_edelweiss, _lic, label="Oct'26")
        second = self._save(_edelweiss, label="Nov'26")
        self.assertIn("LIC of India had 3 rows in 'Oct'26' and has none in this upload.", second.warnings)

    def test_stored_payouts_are_percents_and_the_row_is_not_marked_edited(self):
        upload = self._save(_edelweiss)
        row = upload.rates.get(ppt="5")
        self.assertEqual((row.payout_yr1, row.insurer, row.is_edited), (20.0, "Edelweiss Life", False))

    def _rows(self, upload):
        for ppt, y1, product in [("5", 40.0, "Alpha Plan"), ("8", None, "Beta Plan"), ("10", 55.5, "Gamma Plan"),
                                 ("12", 40.0, "Alpha Plan Two")]:
            LifeRate.objects.create(upload=upload, insurer="Acme Life", category="PAR(Trad)",
                                    product=product, ppt=ppt, pt="20", payout_yr1=y1,
                                    remarks="pays on persistency" if ppt == "12" else "")
        LifeRate.objects.create(upload=upload, insurer="Other Life", category="TERM",
                                product="Delta", ppt="5", payout_yr1=10.0)

    def test_search_ranks_by_first_year_payout_with_blanks_last(self):
        upload = LifeGridUpload.objects.create(label="t", is_active=True)
        self._rows(upload)
        rows, total = search_life_rates(upload, selected_life_filters(_QD("")))
        self.assertEqual(total, 5)
        self.assertEqual([r.product for r in rows][:2], ["Gamma Plan", "Alpha Plan"])
        self.assertEqual(rows[-1].product, "Beta Plan")

    def test_search_filters_and_every_word_must_match(self):
        upload = LifeGridUpload.objects.create(label="t", is_active=True)
        self._rows(upload)
        self.assertEqual(search_life_rates(upload, selected_life_filters(_QD("insurer=Other Life")))[1], 1)
        self.assertEqual(search_life_rates(upload, selected_life_filters(_QD("category=PAR&ppt=8")))[1], 1)
        self.assertEqual(search_life_rates(upload, selected_life_filters(_QD("category=TERM")))[1], 1)
        self.assertEqual(search_life_rates(upload, selected_life_filters(_QD("product=Delta")))[1], 1)
        self.assertEqual(search_life_rates(upload, selected_life_filters(_QD("q=term")))[1], 1)
        self.assertEqual(search_life_rates(upload, selected_life_filters(_QD("q=alpha two")))[1], 1)
        self.assertEqual(search_life_rates(upload, selected_life_filters(_QD("q=persistency")))[1], 1)
        self.assertEqual(search_life_rates(upload, selected_life_filters(_QD("q=alpha missing")))[1], 0)

    def test_search_is_capped_but_counts_every_match(self):
        upload = LifeGridUpload.objects.create(label="t", is_active=True)
        LifeRate.objects.bulk_create(
            [LifeRate(upload=upload, insurer="Acme Life", product=f"P{i}", payout_yr1=float(i % 50))
             for i in range(LIFE_MAX_RESULTS + 20)])
        rows, total = search_life_rates(upload, selected_life_filters(_QD("")))
        self.assertEqual((len(rows), total), (LIFE_MAX_RESULTS, LIFE_MAX_RESULTS + 20))

    def test_search_only_looks_at_the_given_upload(self):
        old = LifeGridUpload.objects.create(label="old")
        live = LifeGridUpload.objects.create(label="live", is_active=True)
        LifeRate.objects.create(upload=old, insurer="Acme Life", product="Old")
        LifeRate.objects.create(upload=live, insurer="Acme Life", product="New")
        rows, _ = search_life_rates(live, selected_life_filters(_QD("")))
        self.assertEqual([r.product for r in rows], ["New"])


class LifeMappedSearchTests(TestCase):
    """Category, Payment Mode, PPT and PT through the search service, on rows worded the way the workbook words them."""

    def setUp(self):
        self.upload = LifeGridUpload.objects.create(label="t", is_active=True)

    def _add(self, product, ppt="", pt="", category="", payout=10.0):
        LifeRate.objects.create(upload=self.upload, insurer="Acme Life", product=product, ppt=ppt, pt=pt,
                                category=category, payout_yr1=payout)

    def _found(self, query):
        rows, total = search_life_rates(self.upload, selected_life_filters(_QD(query)))
        self.assertEqual(total, len(rows))
        return sorted(r.product for r in rows)

    def _ppt_rows(self):
        for name, ppt in [("sp", "SP"), ("single", "Single Pay"), ("one", "1"), ("five", "5"), ("seven_nine", "7 to 9"),
                          ("ten_plus", "10+"), ("limited", "Limited"), ("rp", "RP"), ("regular", "Regular Pay"),
                          ("blank", ""), ("all", "All")]:
            self._add(name, ppt=ppt)

    def test_payment_mode(self):
        self._ppt_rows()
        self.assertEqual(self._found("payment_mode=SP"), ["one", "single", "sp"])
        self.assertEqual(self._found("payment_mode=RP"), ["regular", "rp"])
        self.assertEqual(self._found("payment_mode=LP"), ["five", "limited", "seven_nine", "ten_plus"])

    def test_the_ppt_pick_with_and_without_a_payment_mode(self):
        self._ppt_rows()
        self.assertEqual(self._found("ppt=8"), ["all", "blank", "seven_nine"])
        self.assertEqual(self._found("ppt=1"), ["all", "blank", "one"])
        self.assertEqual(self._found("ppt=%3E13"), ["all", "blank", "ten_plus"])
        self.assertEqual(self._found("payment_mode=LP&ppt=8"), ["limited", "seven_nine"])
        self.assertEqual(self._found("payment_mode=LP&ppt=%3E13"), ["limited", "ten_plus"])
        self.assertEqual(self._found("payment_mode=RP&ppt=8"), ["regular", "rp"])
        self.assertEqual(self._found("payment_mode=SP&ppt=8"), ["one", "single", "sp"])      # the PPT is ignored

    def test_pt_pick(self):
        for name, pt in [("exact", "20"), ("list", "15/20"), ("open", ">=20"), ("other", "10"), ("blank", ""),
                         ("whole", "Whole Life")]:
            self._add(name, pt=pt)
        self.assertEqual(self._found("pt=20"), ["blank", "exact", "list", "open", "whole"])
        self.assertEqual(self._found("pt=10"), ["blank", "other", "whole"])
        self.assertEqual(self._found("pt=20&product=exact"), ["exact"])

    def test_category_and_the_pension_rows_that_show_under_both(self):
        self._add("both", category="Annuity")
        LifeRate.objects.filter(product="both").update(product="Guaranteed Pension Goal")
        self._add("Alpha", category="Non PAR(Trad)")
        self._add("Gold Term Life")                                  # no wording: sorted by its plan name
        self._add("Century Plan")                                    # says nothing: only with no Category
        self._add("Param Raksha", category="ILP")
        self.assertEqual(self._found("category=PENSION"), ["Guaranteed Pension Goal"])
        self.assertEqual(self._found("category=ANNUITY / SAVINGS"), ["Guaranteed Pension Goal"])
        self.assertEqual(self._found("category=NON PAR"), ["Alpha"])
        self.assertEqual(self._found("category=TERM"), ["Gold Term Life"])
        self.assertEqual(self._found("category=PAR"), [])
        self.assertEqual(len(self._found("insurer=Acme Life")), 5)   # the unsorted rows are reachable with no Category

    def test_the_filters_combine_and_stay_ranked(self):
        self._add("low", ppt="10", category="PAR(Trad)", payout=20.0)
        self._add("high", ppt="10+", category="Par", payout=50.0)
        self._add("other", ppt="5", category="Par", payout=60.0)
        rows, total = search_life_rates(self.upload, selected_life_filters(_QD("category=PAR&ppt=12")))
        self.assertEqual([r.product for r in rows], ["high"])
        rows, total = search_life_rates(self.upload, selected_life_filters(_QD("category=PAR&payment_mode=LP")))
        self.assertEqual([r.product for r in rows], ["other", "high", "low"])

    def test_values_the_page_does_not_offer_are_ignored(self):
        self._add("a", ppt="5", category="PAR(Trad)")
        selected = selected_life_filters(_QD("category=NOPE&payment_mode=XX&ppt=99&pt=500"))
        self.assertEqual((selected["category"], selected["payment_mode"], selected["ppt"], selected["pt"]), ("", "", "", ""))
        self.assertEqual(self._found("category=NOPE&ppt=99"), ["a"])
        self.assertEqual(selected_life_filters(_QD("category=par&payment_mode=lp"))["category"], "PAR")
        self.assertEqual(selected_life_filters(_QD("category=par&payment_mode=lp"))["payment_mode"], "LP")


def _QD(query):
    from django.http import QueryDict
    return QueryDict(query)


class LifeAccessMixin:
    def setUp(self):
        self.viewer_group = Group.objects.get_or_create(name="Can_View_Life_Payout_Grid")[0]
        self.admin_group = Group.objects.get_or_create(name="Can_View_Life_Rate_Master")[0]
        self.viewer = User.objects.create_user(username="viewer", password="a-strong-test-password-1")
        self.viewer.groups.add(self.viewer_group)
        self.manager = User.objects.create_user(username="manager", password="a-strong-test-password-1")
        self.manager.groups.add(self.viewer_group, self.admin_group)


class LifeRateCheckerPageTests(LifeAccessMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.upload = LifeGridUpload.objects.create(label="Oct'26", is_active=True, row_count=2, insurer_count=1)
        LifeRate.objects.create(upload=self.upload, insurer="Acme Life", category="Par", product="Alpha Plan",
                                variant="Gold", ppt="10", pt="20", payout_yr1=47.5, payout_yr2=2.5,
                                remarks="Year 6+: 3%")
        LifeRate.objects.create(upload=self.upload, insurer="Acme Life", category="Par", product="Beta Plan",
                                ppt="5", payout_yr1=30.0, is_edited=True)
        LifeInsurerNote.objects.create(upload=self.upload, insurer="Acme Life", text="All payouts are exclusive of GST")

    def test_gate(self):
        url = reverse("life_payout_rates")
        self.assertEqual(self.client.get(url).status_code, 302)                    # anonymous -> login
        nobody = User.objects.create_user(username="nobody", password="a-strong-test-password-1")
        self.client.force_login(nobody)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_nothing_is_searched_until_the_form_is_used(self):
        self.client.force_login(self.viewer)
        response = self.client.get(reverse("life_payout_rates"))
        self.assertFalse(response.context["has_searched"])
        self.assertEqual(response.context["data"], [])
        self.assertContains(response, "Oct&#x27;26")                                # the live grid is named

    def test_results_are_ranked_and_only_filled_columns_show(self):
        self.client.force_login(self.viewer)
        response = self.client.get(reverse("life_payout_rates"), {"insurer": "Acme Life"})
        self.assertEqual([r.product for r in response.context["data"]], ["Alpha Plan", "Beta Plan"])
        self.assertEqual([attr for attr, _ in response.context["columns"]], ["payout_yr1", "payout_yr2"])
        self.assertContains(response, "47.5%")
        self.assertContains(response, "Year 6+: 3%")
        self.assertContains(response, "All payouts are exclusive of GST")          # the insurer's notes
        self.assertNotContains(response, "Changed on screen")                       # no trace of the edit feature here

    def test_the_product_dropdown_is_narrowed_from_insurer_and_category(self):
        self.client.force_login(self.viewer)
        response = self.client.get(reverse("life_payout_rates"))
        self.assertIn(["Acme Life", "PAR", "Alpha Plan"], response.context["filter_rows"])

    @staticmethod
    def _options(response, name):
        import re
        html = response.content.decode()
        block = re.search(r'<select name="%s"[^>]*>(.*?)</select>' % name, html, re.S).group(1)
        return [v for v in re.findall(r'value="([^"]*)"', block) if v]

    def test_the_insurer_dropdown_lists_every_insurer_of_the_live_grid(self):
        LifeRate.objects.create(upload=self.upload, insurer="Bravo Life", product="Gamma Plan", payout_yr1=12.0)
        other = LifeGridUpload.objects.create(label="Sep'26")                       # an earlier upload: not offered
        LifeRate.objects.create(upload=other, insurer="Old Life", product="Old Plan", payout_yr1=9.0)
        self.client.force_login(self.viewer)
        response = self.client.get(reverse("life_payout_rates"))
        self.assertEqual(self._options(response, "insurer"), ["Acme Life", "Bravo Life"])
        self.assertEqual(response.context["insurer_list"], ["Acme Life", "Bravo Life"])
        # the picked insurers stay selected, and a stale link to an insurer the grid no longer has is not lost
        response = self.client.get(reverse("life_payout_rates"), {"insurer": ["Bravo Life", "Gone Life"]})
        html = response.content.decode()
        block = re.search(r'<select name="insurer"[^>]*>(.*?)</select>', html, re.S).group(1)
        self.assertEqual(re.findall(r'<option value="([^"]*)"\s*(selected)?', block),
                         [("Acme Life", ""), ("Bravo Life", "selected"), ("Gone Life", "selected")])

    def test_the_filter_lists_are_exactly_the_fixed_ones(self):
        self.client.force_login(self.viewer)
        response = self.client.get(reverse("life_payout_rates"))
        self.assertEqual(self._options(response, "category"),
                         ["PAR", "NON PAR", "TERM", "ULIP", "TULIP", "ANNUITY / SAVINGS", "PENSION"])
        self.assertEqual(self._options(response, "payment_mode"), ["SP", "LP", "RP"])
        self.assertEqual(self._options(response, "ppt"), [str(n) for n in range(1, 14)] + ["&gt;13"])
        self.assertEqual(self._options(response, "pt"), [str(n) for n in range(1, 101)])

    def test_single_pay_hides_the_ppt_field_and_the_search_ignores_it(self):
        self.client.force_login(self.viewer)
        self.assertNotContains(self.client.get(reverse("life_payout_rates"), {"payment_mode": "LP"}),
                               'style="display: none;"')
        response = self.client.get(reverse("life_payout_rates"), {"payment_mode": "SP", "ppt": "8"})
        self.assertContains(response, 'id="pptGroup" style="display: none;"')
        self.assertContains(response, "disabled")
        self.assertEqual(response.context["data"], [])               # no single-pay rows in this grid
        LifeRate.objects.create(upload=self.upload, insurer="Acme Life", product="One Shot", ppt="SP", payout_yr1=3.0)
        response = self.client.get(reverse("life_payout_rates"), {"payment_mode": "SP", "ppt": "8"})
        self.assertEqual([r.product for r in response.context["data"]], ["One Shot"])

    def test_ppt_and_payment_mode_through_the_page(self):
        self.client.force_login(self.viewer)
        response = self.client.get(reverse("life_payout_rates"), {"payment_mode": "LP", "ppt": "10"})
        self.assertEqual([r.product for r in response.context["data"]], ["Alpha Plan"])
        response = self.client.get(reverse("life_payout_rates"), {"payment_mode": "RP"})
        self.assertEqual(response.context["data"], [])

    def test_unknown_filter_values_are_ignored(self):
        self.client.force_login(self.viewer)
        response = self.client.get(reverse("life_payout_rates"), {"category": "NOPE", "ppt": "99", "pt": "500"})
        self.assertEqual(len(response.context["data"]), 2)
        self.assertEqual(response.context["selected"]["category"], "")

    def test_the_checker_never_edits_anything_whoever_looks_at_it(self):
        """Rows are edited on the Life Rate Master only; the checker has no Edit column, link or edited tag, not even
        for a user in the Rate Master group."""
        for user in (self.viewer, self.manager):
            self.client.force_login(user)
            response = self.client.get(reverse("life_payout_rates"), {"q": "plan"})
            self.assertContains(response, "Alpha Plan")
            self.assertContains(response, "Beta Plan")                              # the row that was edited on screen
            for text in ("/life-rate-master/edit/", "life-c-edit", "life-d-edit", "Edit this row", "life-edited",
                         "Changed on screen"):
                self.assertNotContains(response, text, msg_prefix=f"{user.username}: ")

    def test_no_upload_yet(self):
        LifeGridUpload.objects.all().delete()
        self.client.force_login(self.viewer)
        response = self.client.get(reverse("life_payout_rates"), {"q": "x"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No Life payout grid has been uploaded yet")

    def test_sidebar_and_tabs(self):
        self.client.force_login(self.viewer)
        response = self.client.get(reverse("life_payout_rates"))
        self.assertContains(response, reverse("rate_checker"))                      # the sidebar's Rate Checker link
        self.assertContains(response, "bi-shield-fill-plus")                        # the Life tab
        self.assertNotContains(response, reverse("life_rate_master"))               # no Master Data link for a viewer
        self.client.force_login(self.manager)
        self.assertContains(self.client.get(reverse("life_payout_rates")), reverse("life_rate_master"))


class LifeRateMasterTests(LifeAccessMixin, TestCase):
    def _post_workbook(self, maker=_edelweiss, name="Life_Grid_working-Nov'26.xlsx", **extra):
        data = {"workbook": SimpleUploadedFile(name, _workbook(maker).read()), **extra}
        return self.client.post(reverse("life_rate_master_upload"), data)

    def test_the_admin_pages_need_the_rate_master_group(self):
        self.client.force_login(self.viewer)
        for name, args in (("life_rate_master", ()), ("life_rate_master_notes", ()), ("life_rate_master_uploads", ()),
                           ("life_rate_master_upload", ()), ("life_rate_master_activate", (1,)),
                           ("life_rate_master_edit", (1,)), ("life_rate_master_note_add", ()),
                           ("life_rate_master_note_edit", (1,))):
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 403, name)
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(reverse("life_rate_master")).status_code, 200)

    def test_upload_makes_the_workbook_live_and_is_logged(self):
        self.client.force_login(self.manager)
        response = self._post_workbook()
        self.assertRedirects(response, reverse("life_rate_master"))
        upload = LifeGridUpload.objects.get()
        self.assertEqual((upload.label, upload.is_active, upload.row_count, upload.uploaded_by),
                         ("Nov'26", True, 3, self.manager))
        log = AuditLog.objects.get(action="LIFE GRID UPLOAD")
        self.assertIn("Life_Grid_working-Nov'26.xlsx", log.details)

    def test_upload_label_can_be_typed(self):
        self.client.force_login(self.manager)
        self._post_workbook(label="Festive grid")
        self.assertEqual(LifeGridUpload.objects.get().label, "Festive grid")

    def test_a_changed_layout_uploads_nothing_and_leaves_the_live_grid_alone(self):
        self.client.force_login(self.manager)
        self._post_workbook()
        response = self.client.post(reverse("life_rate_master_upload"), {
            "workbook": SimpleUploadedFile("bad.xlsx", _workbook(_edelweiss, header="Segment").read())}, follow=True)
        self.assertContains(response, "Nothing was uploaded")
        self.assertContains(response, "Edelweiss")
        self.assertEqual(LifeGridUpload.objects.count(), 1)
        self.assertTrue(LifeGridUpload.objects.get().is_active)

    def test_wrong_file_types_and_missing_files_are_refused(self):
        self.client.force_login(self.manager)
        response = self.client.post(reverse("life_rate_master_upload"), {
            "workbook": SimpleUploadedFile("grid.csv", b"a,b")}, follow=True)
        self.assertContains(response, "must be an .xlsx file")
        response = self.client.post(reverse("life_rate_master_upload"), {}, follow=True)
        self.assertContains(response, "Choose the Life workbook")
        self.assertEqual(LifeGridUpload.objects.count(), 0)
        self.assertEqual(self.client.get(reverse("life_rate_master_upload")).status_code, 405)

    def test_make_live_switches_back_to_an_older_upload(self):
        self.client.force_login(self.manager)
        self._post_workbook(label="Oct'26")
        self._post_workbook(label="Nov'26")
        old = LifeGridUpload.objects.get(label="Oct'26")
        self.client.post(reverse("life_rate_master_activate", args=[old.pk]))
        self.assertEqual(LifeGridUpload.objects.get(is_active=True), old)

    def _row(self):
        upload = LifeGridUpload.objects.create(label="Oct'26", is_active=True)
        return LifeRate.objects.create(upload=upload, insurer="Acme Life", product="Alpha Plan", ppt="10",
                                       payout_yr1=47.5, payout_yr2=2.5)

    def _edit_data(self, row, **over):
        data = {"category": "", "product": row.product, "variant": "", "ppt": row.ppt, "pt": "", "condition": "",
                "payout_yr1": "47.5", "payout_deferred": "", "payout_yr2": "2.5", "payout_yr3": "",
                "payout_yr4": "", "payout_yr5": "", "payout_yr6_plus": "", "remarks": ""}
        data.update(over)
        return data

    def test_editing_a_payout_marks_the_row_logs_it_and_returns_to_the_filtered_master_page(self):
        self.client.force_login(self.manager)
        row = self._row()
        back = "/life-rate-master/?insurer=Acme+Life&page=2"
        response = self.client.post(reverse("life_rate_master_edit", args=[row.pk]),
                                    self._edit_data(row, payout_yr1="45.25", next=back))
        self.assertRedirects(response, back, fetch_redirect_response=False)
        row.refresh_from_db()
        self.assertEqual((row.payout_yr1, row.is_edited, row.edited_by), (45.25, True, self.manager))
        self.assertIsNotNone(row.edited_at)
        log = AuditLog.objects.get(action="LIFE RATE EDIT")
        self.assertIn("payout_yr1: 47.5 -> 45.25", log.details)

    def test_an_unchanged_form_is_not_an_edit(self):
        self.client.force_login(self.manager)
        row = self._row()
        self.client.post(reverse("life_rate_master_edit", args=[row.pk]), self._edit_data(row))
        row.refresh_from_db()
        self.assertFalse(row.is_edited)
        self.assertFalse(AuditLog.objects.filter(action="LIFE RATE EDIT").exists())

    def test_payouts_must_be_percents(self):
        self.client.force_login(self.manager)
        row = self._row()
        response = self.client.post(reverse("life_rate_master_edit", args=[row.pk]),
                                    self._edit_data(row, payout_yr1="140"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "between 0 and 100")
        row.refresh_from_db()
        self.assertEqual(row.payout_yr1, 47.5)

    def test_next_must_stay_on_this_site(self):
        self.client.force_login(self.manager)
        row = self._row()
        response = self.client.post(reverse("life_rate_master_edit", args=[row.pk]),
                                    self._edit_data(row, payout_yr1="40", next="https://evil.example/steal"))
        self.assertRedirects(response, reverse("life_rate_master"), fetch_redirect_response=False)

    def test_without_a_next_the_edit_returns_to_the_upload_the_row_belongs_to(self):
        self.client.force_login(self.manager)
        row = self._row()
        old = LifeGridUpload.objects.create(label="Sep'26")
        old_row = LifeRate.objects.create(upload=old, insurer="Acme Life", product="Old Plan", payout_yr1=10.0)
        for target, expected in ((row, reverse("life_rate_master")),
                                 (old_row, reverse("life_rate_master") + f"?upload={old.pk}")):
            response = self.client.post(reverse("life_rate_master_edit", args=[target.pk]),
                                        self._edit_data(target, payout_yr1="11"))
            self.assertRedirects(response, expected, fetch_redirect_response=False)

    def test_the_edit_page_shows_where_the_row_came_from_and_who_changed_it(self):
        self.client.force_login(self.manager)
        row = self._row()
        row.source_ref, row.section, row.is_edited, row.edited_by = "Acme!C5", "Fresh", True, self.viewer
        row.edited_at = timezone.now()
        row.save()
        response = self.client.get(reverse("life_rate_master_edit", args=[row.pk]))
        for text in ("Acme!C5", "Fresh", "Oct&#x27;26 (live)", "by viewer", reverse("life_rate_master")):
            self.assertContains(response, text)

    def test_clearing_a_payout_leaves_it_blank(self):
        self.client.force_login(self.manager)
        row = self._row()
        self.client.post(reverse("life_rate_master_edit", args=[row.pk]), self._edit_data(row, payout_yr2=""))
        row.refresh_from_db()
        self.assertIsNone(row.payout_yr2)

    def test_the_rate_master_tabs_link_to_life(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("life_rate_master"))
        self.assertContains(response, 'class="rm-tab active"')
        self.assertNotContains(response, "Soon")


class LifeRateMasterDataPageTests(LifeAccessMixin, TestCase):
    """The Life Rate Master's Payout rows page: every stored row of an upload, filtered, paged, with an Edit button."""

    def setUp(self):
        super().setUp()
        self.upload = LifeGridUpload.objects.create(label="Oct'26", is_active=True, row_count=4, insurer_count=2)

        def make(**kwargs):
            return LifeRate.objects.create(upload=self.upload, **kwargs)

        self.alpha = make(insurer="Acme Life", sheet="Acme", section="Fresh", category="Par", product="Alpha Plan",
                          variant="Gold", ppt="10", pt="20", payout_yr1=47.5, payout_yr2=2.5, source_ref="Acme!C5",
                          position=0)
        self.beta = make(insurer="Acme Life", sheet="Acme", category="Par", product="Beta Plan", ppt="5",
                         payout_yr1=30.0, remarks="Valid till 30th Nov", source_ref="Acme!C6", position=1,
                         is_edited=True, edited_by=self.manager, edited_at=timezone.now())
        self.gamma = make(insurer="Bravo Life", sheet="Bravo", category="", product="Gamma Plan", payout_yr1=12.0,
                          source_ref="Bravo!B9", position=2)
        self.delta = make(insurer="Bravo Life", sheet="Bravo", category="Term", product="Delta Term", payout_yr1=8.0,
                          source_ref="Bravo!B10", position=3)
        self.client.force_login(self.manager)

    def _page(self, **params):
        return self.client.get(reverse("life_rate_master"), params)

    @staticmethod
    def _ids(response):
        return [row.id for row in response.context["page_obj"]]

    def test_every_stored_row_is_listed_in_workbook_order_with_an_edit_button(self):
        response = self._page()
        self.assertEqual(self._ids(response), [self.alpha.id, self.beta.id, self.gamma.id, self.delta.id])
        for row in (self.alpha, self.delta):
            self.assertContains(response, reverse("life_rate_master_edit", args=[row.id]))
        self.assertContains(response, "next=/life-rate-master/")                    # Edit brings you back to this page
        for text in ("Acme!C5", "Fresh", "Gold", "Valid till 30th Nov", "47.5%", "life-edited"):
            self.assertContains(response, text)

    def test_the_edit_button_remembers_the_filters_it_was_clicked_under(self):
        response = self._page(insurer="Acme Life")
        self.assertContains(response, "next=/life-rate-master/%3Finsurer%3DAcme%2BLife")

    def test_the_header_counts_the_stored_rows(self):
        response = self._page()
        self.assertEqual(response.context["stats"], {"rows": 4, "insurers": 2, "edited": 1, "with_mg": 0})
        self.assertEqual(response.context["live_upload"], self.upload)

    def test_all_seven_payout_columns_show_even_when_no_row_has_a_value(self):
        response = self._page(insurer="Bravo Life")
        self.assertEqual([header for _, header in response.context["payout_columns"]],
                         ["Year 1", "At 2nd year", "Year 2", "Year 3", "Year 4", "Year 5", "Year 6+"])
        self.assertContains(response, "Year 6+")

    def test_filters(self):
        self.assertEqual(self._ids(self._page(insurer="Bravo Life")), [self.gamma.id, self.delta.id])
        self.assertEqual(self._ids(self._page(category="Par")), [self.alpha.id, self.beta.id])
        self.assertEqual(self._ids(self._page(category="__blank__")), [self.gamma.id])
        self.assertEqual(self._ids(self._page(edited="1")), [self.beta.id])
        self.assertEqual(self._ids(self._page(q="acme!c6")), [self.beta.id])        # the workbook cell is searchable
        self.assertEqual(self._ids(self._page(q="gold alpha")), [self.alpha.id])     # every word must match
        self.assertEqual(self._ids(self._page(q="gold delta")), [])
        self.assertEqual(self._ids(self._page(insurer="Acme Life", category="Par", q="beta")), [self.beta.id])
        self.assertContains(self._page(q="nothing like this"), "No payout rows match")

    def test_a_value_the_upload_does_not_have_counts_as_not_picked(self):
        response = self._page(insurer="Nope Life", category="Nope", page_size="7")
        selected = response.context["selected"]
        self.assertEqual((selected["insurer"], selected["category"], selected["page_size"]), ("", "", 50))
        self.assertEqual(len(self._ids(response)), 4)

    def test_rows_are_paged_and_the_pager_keeps_the_filters(self):
        for n in range(130):
            LifeRate.objects.create(upload=self.upload, insurer="Bulk Life", product=f"Plan {n}", position=10 + n)
        response = self._page(insurer="Bulk Life")
        self.assertEqual((len(self._ids(response)), response.context["page_obj"].paginator.num_pages), (50, 3))
        self.assertContains(response, "Showing 1&ndash;50 of 130")
        self.assertContains(response, "?insurer=Bulk+Life&page=2")
        self.assertEqual(len(self._ids(self._page(insurer="Bulk Life", page_size="100", page="2"))), 30)
        self.assertEqual(len(self._ids(self._page(insurer="Bulk Life", page="99"))), 30)    # past the end: the last page
        self.assertEqual(len(self._ids(self._page(insurer="Bulk Life", page="x"))), 50)     # not a number: the first page

    def test_an_earlier_upload_can_be_looked_at_and_made_live_from_here(self):
        old = LifeGridUpload.objects.create(label="Sep'26", row_count=1)
        old_row = LifeRate.objects.create(upload=old, insurer="Acme Life", product="Old Plan", payout_yr1=9.0)
        response = self._page(upload=old.pk)
        self.assertEqual(self._ids(response), [old_row.id])
        self.assertContains(response, "an earlier upload")
        self.assertContains(response, reverse("life_rate_master_activate", args=[old.pk]))
        self.assertContains(response, f"?upload={old.pk}")                           # the sections and Reset keep it
        self.assertEqual(response.context["live_upload"], self.upload)
        response = self._page(upload=99999)                                          # unknown: the live upload
        self.assertEqual(response.context["upload"], self.upload)
        self.assertNotContains(response, "an earlier upload")

    def test_the_three_sections_link_to_each_other(self):
        for name in ("life_rate_master", "life_rate_master_notes", "life_rate_master_uploads"):
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 200, name)
            for target in ("life_rate_master", "life_rate_master_notes", "life_rate_master_uploads"):
                self.assertContains(response, f'href="{reverse(target)}"', msg_prefix=f"{name} -> {target}: ")

    def test_no_upload_yet(self):
        LifeGridUpload.objects.all().delete()
        response = self._page()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No Life workbook uploaded yet")
        self.assertContains(response, reverse("life_rate_master_uploads"))


class LifeRateMasterNotesAndUploadsTests(LifeAccessMixin, TestCase):
    """The insurer notes (view and edit) and the uploads list of the Life Rate Master."""

    def setUp(self):
        super().setUp()
        self.upload = LifeGridUpload.objects.create(label="Oct'26", is_active=True, row_count=2, insurer_count=2)
        LifeRate.objects.create(upload=self.upload, insurer="Acme Life", product="Alpha Plan", payout_yr1=47.5, position=0)
        LifeRate.objects.create(upload=self.upload, insurer="Bravo Life", product="Gamma Plan", payout_yr1=12.0, position=1)
        self.note = LifeInsurerNote.objects.create(
            upload=self.upload, insurer="Acme Life", text="All payouts are exclusive of GST")
        self.orphan = LifeInsurerNote.objects.create(upload=self.upload, insurer="Zeta Life", text="Notes without rows")
        self.client.force_login(self.manager)

    def test_the_notes_page_lists_every_insurer_with_or_without_notes(self):
        response = self.client.get(reverse("life_rate_master_notes"))
        self.assertEqual([(r["insurer"], r["rows"], bool(r["note"])) for r in response.context["note_rows"]],
                         [("Acme Life", 1, True), ("Bravo Life", 1, False), ("Zeta Life", 0, True)])
        self.assertContains(response, "All payouts are exclusive of GST")
        self.assertContains(response, "No notes")
        self.assertContains(response, "Add notes")                                   # offered where there are none
        self.assertContains(response, reverse("life_rate_master_note_edit", args=[self.note.pk]))

    def test_editing_notes_saves_logs_and_returns_to_the_page_it_came_from(self):
        back = "/life-rate-master/notes/?upload=1"
        response = self.client.post(reverse("life_rate_master_note_edit", args=[self.note.pk]),
                                    {"text": "Exclusive of GST; clawback within 13 months", "next": back})
        self.assertRedirects(response, back, fetch_redirect_response=False)
        self.note.refresh_from_db()
        self.assertEqual(self.note.text, "Exclusive of GST; clawback within 13 months")
        log = AuditLog.objects.get(action="LIFE NOTE EDIT")
        self.assertIn("Acme Life", log.details)
        self.assertIn("All payouts are exclusive of GST", log.details)               # what it was
        self.assertIn("clawback within 13 months", log.details)                      # what it became

    def test_unchanged_notes_are_not_logged_and_next_stays_on_this_site(self):
        response = self.client.post(reverse("life_rate_master_note_edit", args=[self.note.pk]),
                                    {"text": self.note.text, "next": "https://evil.example/steal"})
        self.assertRedirects(response, reverse("life_rate_master_notes"), fetch_redirect_response=False)
        self.assertFalse(AuditLog.objects.filter(action="LIFE NOTE EDIT").exists())

    def test_an_edited_note_reaches_the_rate_checker_and_an_emptied_one_disappears(self):
        self.client.post(reverse("life_rate_master_note_edit", args=[self.note.pk]), {"text": "New terms apply"})
        response = self.client.get(reverse("life_payout_rates"), {"insurer": "Acme Life"})
        self.assertContains(response, "New terms apply")
        self.client.post(reverse("life_rate_master_note_edit", args=[self.note.pk]), {"text": ""})
        self.assertEqual(life_notes_for(self.upload, ["Acme Life"]), {})
        response = self.client.get(reverse("life_payout_rates"), {"insurer": "Acme Life"})
        self.assertNotContains(response, "Notes &amp; conditions")

    def test_the_edit_page_names_the_insurer_and_the_upload(self):
        response = self.client.get(reverse("life_rate_master_note_edit", args=[self.note.pk]))
        self.assertContains(response, "Edit notes: Acme Life")
        self.assertContains(response, "All payouts are exclusive of GST")

    def test_notes_can_be_started_for_an_insurer_that_has_none(self):
        response = self.client.post(reverse("life_rate_master_note_add"),
                                    {"upload": self.upload.pk, "insurer": "Bravo Life"})
        note = LifeInsurerNote.objects.get(upload=self.upload, insurer="Bravo Life")
        self.assertEqual(note.text, "")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(reverse("life_rate_master_note_edit", args=[note.pk]) + "?next="))
        self.client.post(reverse("life_rate_master_note_edit", args=[note.pk]), {"text": "Payout is net of GST"})
        note.refresh_from_db()
        self.assertEqual(note.text, "Payout is net of GST")                         # saving it is an ordinary notes edit
        self.assertTrue(AuditLog.objects.filter(action="LIFE NOTE EDIT", details__contains="Bravo Life").exists())
        self.client.post(reverse("life_rate_master_note_add"), {"upload": self.upload.pk, "insurer": "Bravo Life"})
        self.assertEqual(LifeInsurerNote.objects.filter(upload=self.upload, insurer="Bravo Life").count(), 1)

    def test_notes_cannot_be_started_for_an_insurer_the_upload_does_not_have(self):
        response = self.client.post(reverse("life_rate_master_note_add"),
                                    {"upload": self.upload.pk, "insurer": "Nobody Life"}, follow=True)
        self.assertContains(response, "no payout rows in this upload")
        self.assertFalse(LifeInsurerNote.objects.filter(insurer="Nobody Life").exists())
        self.assertEqual(self.client.post(reverse("life_rate_master_note_add"), {"upload": "x"}).status_code, 404)
        self.assertEqual(self.client.get(reverse("life_rate_master_note_add")).status_code, 405)

    def test_the_uploads_page_offers_view_rows_and_make_live(self):
        old = LifeGridUpload.objects.create(label="Sep'26", row_count=1)
        response = self.client.get(reverse("life_rate_master_uploads"))
        self.assertContains(response, reverse("life_rate_master_upload"))                      # the upload form
        self.assertContains(response, f"{reverse('life_rate_master')}?upload={old.pk}")         # View rows of the old one
        self.assertContains(response, reverse("life_rate_master_activate", args=[old.pk]))
        self.assertNotContains(response, reverse("life_rate_master_activate", args=[self.upload.pk]))   # live already
        self.assertNotContains(response, "to edit rows")

    def test_making_an_upload_live_returns_to_the_uploads_page(self):
        old = LifeGridUpload.objects.create(label="Sep'26")
        response = self.client.post(reverse("life_rate_master_activate", args=[old.pk]))
        self.assertRedirects(response, reverse("life_rate_master_uploads"), fetch_redirect_response=False)
        self.assertEqual(LifeGridUpload.objects.get(is_active=True), old)


class LifeBulkPlanTests(SimpleTestCase):
    """What a bulk operation does to rows, worked out without saving anything (no database)."""

    @staticmethod
    def _rows(*values, column="payout_yr1"):
        return [LifeRate(id=i + 1, insurer="Acme Life", product=f"Plan {i + 1}", **{column: value})
                for i, value in enumerate(values)]

    def test_only_the_payouts_and_the_descriptive_fields_can_be_bulk_edited(self):
        self.assertEqual(
            set(BULK_FIELD_LABELS),
            {*LifeRate.MG_FIELDS, *LifeRate.PAYOUT_FIELDS, "category", "product", "variant", "ppt", "pt", "condition",
             "remarks"})
        for column in ("id", "upload", "insurer", "sheet", "section", "source_ref", "position", "is_edited",
                       "edited_at", "edited_by", "nonsense"):
            self.assertEqual(bulk_field_kind(column), "", column)
            self.assertNotIn(column, BULK_FIELD_LABELS)
        self.assertEqual((bulk_field_kind("payout_yr3"), bulk_field_kind("remarks"), bulk_field_kind("category")),
                         ("payout", "remarks", "text"))

    def test_set_and_clear(self):
        plan = plan_bulk_update(self._rows(10.0, 20.0, None), "payout_yr1", "set", Decimal("20"))
        self.assertEqual([(row.id, old, new) for row, old, new in plan.changes], [(1, 10.0, 20.0), (3, None, 20.0)])
        self.assertEqual((plan.unchanged, plan.skipped, plan.errors), (1, 0, []))
        plan = plan_bulk_update(self._rows(10.0, None), "payout_yr1", "clear", None)
        self.assertEqual([(old, new) for _, old, new in plan.changes], [(10.0, None)])
        self.assertEqual(plan.unchanged, 1)                                         # the blank one stays blank

    def test_add_is_exact_and_rounds_to_four_decimals(self):
        plan = plan_bulk_update(self._rows(16.5, 0.4237, 0.1), "payout_yr1", "add", Decimal("0.1"))
        self.assertEqual([new for _, _, new in plan.changes], [16.6, 0.5237, 0.2])
        plan = plan_bulk_update(self._rows(10.0), "payout_yr1", "add", Decimal("-0.25"))
        self.assertEqual([new for _, _, new in plan.changes], [9.75])

    def test_multiply_rounds_half_up_to_four_decimals(self):
        plan = plan_bulk_update(self._rows(63.5, 0.4237), "payout_yr1", "multiply", Decimal("0.5"))
        self.assertEqual([new for _, _, new in plan.changes], [31.75, 0.2119])      # 0.21185 -> 0.2119

    def test_a_blank_payout_has_nothing_to_add_to_but_can_be_set(self):
        plan = plan_bulk_update(self._rows(None, 5.0), "payout_yr1", "add", Decimal("1"))
        self.assertEqual((plan.skipped, [row.id for row, _, _ in plan.changes]), (1, [2]))
        plan = plan_bulk_update(self._rows(None, 5.0), "payout_yr1", "multiply", Decimal("2"))
        self.assertEqual(plan.skipped, 1)

    def test_a_row_that_would_leave_0_to_100_is_an_error(self):
        plan = plan_bulk_update(self._rows(63.5, 10.0), "payout_yr1", "add", Decimal("40"))
        self.assertEqual(len(plan.errors), 1)
        self.assertIn("#1", plan.errors[0])
        self.assertIn("103.5", plan.errors[0])
        plan = plan_bulk_update(self._rows(10.0), "payout_yr1", "add", Decimal("-20"))
        self.assertEqual(len(plan.errors), 1)
        plan = plan_bulk_update(self._rows(10.0), "payout_yr1", "multiply", Decimal("11"))
        self.assertEqual(len(plan.errors), 1)                                       # 110

    def test_text_fields(self):
        rows = self._rows("Offline", "", column="remarks")
        plan = plan_bulk_update(rows, "remarks", "append", "valid till Nov")
        self.assertEqual([new for _, _, new in plan.changes], ["Offline valid till Nov", "valid till Nov"])
        plan = plan_bulk_update(self._rows("Offline", "", column="remarks"), "remarks", "clear", None)
        self.assertEqual(([new for _, _, new in plan.changes], plan.unchanged), ([""], 1))
        plan = plan_bulk_update(self._rows("Par", column="category"), "category", "set", "PAR")
        self.assertEqual([new for _, _, new in plan.changes], ["PAR"])
        plan = plan_bulk_update(self._rows("Par", column="ppt"), "ppt", "set", "x" * 81)       # PPT holds 80 characters
        self.assertEqual((plan.changes, len(plan.errors)), ([], 1))

    def test_the_audit_text_names_the_operation_the_rows_and_the_first_changes_only(self):
        class Upload:
            label = "Oct'26"
        rows = self._rows(*[10.0] * 120)
        plan = plan_bulk_update(rows, "payout_yr1", "add", Decimal("1"))
        text = bulk_audit_details(Upload, "payout_yr1", "add", Decimal("1"), "120 selected rows", plan)
        self.assertIn("add 1 percentage points to Year 1 BG payout", text)
        self.assertIn("120 selected rows; 120 changed", text)
        self.assertIn("#1: 10.0 -> 11.0", text)
        self.assertIn("#100: 10.0 -> 11.0", text)
        self.assertNotIn("#101:", text)
        self.assertIn("and 20 more", text)


class LifeBulkFormTests(SimpleTestCase):
    def _form(self, **data):
        return LifeBulkUpdateForm({"field": "payout_yr1", "operation": "set", "value": "17.5", **data})

    def _cleaned_value(self, **data):
        form = self._form(**data)
        self.assertTrue(form.is_valid(), form.errors)
        return form.cleaned_data["value"]

    def test_valid_payout_operations(self):
        self.assertEqual(self._cleaned_value(), Decimal("17.5"))
        self.assertEqual(self._cleaned_value(operation="add", value="-0.25"), Decimal("-0.25"))
        self.assertEqual(self._cleaned_value(value="17.5%"), Decimal("17.5"))                   # a % sign is allowed
        form = self._form(operation="clear", value="")
        self.assertTrue(form.is_valid())
        self.assertIsNone(form.cleaned_data["value"])

    def test_invalid_input_is_refused(self):
        for data in ({"field": "insurer"}, {"field": "source_ref"}, {"field": ""}, {"operation": "delete"},
                     {"operation": "multiply", "field": "remarks", "value": "2"}, {"value": "abc"}, {"value": ""},
                     {"value": "150"}, {"value": "-1"}, {"operation": "add", "value": "101"},
                     {"operation": "multiply", "value": "-0.5"}, {"value": "NaN"}, {"value": "Infinity"},
                     {"field": "category", "operation": "set", "value": ""},
                     {"field": "ppt", "operation": "set", "value": "x" * 81}):
            self.assertFalse(self._form(**data).is_valid(), data)

    def test_text_operations(self):
        form = self._form(field="remarks", operation="append", value="  valid till Nov  ")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["value"], "valid till Nov")


class LifeMasterRowsMixin(LifeAccessMixin):
    """An active upload with five stored rows (two insurers), and the manager logged in."""

    def setUp(self):
        super().setUp()
        self.upload = LifeGridUpload.objects.create(label="Oct'26", is_active=True, row_count=5, insurer_count=2)

        def make(**kwargs):
            return LifeRate.objects.create(upload=self.upload, **kwargs)

        self.a1 = make(insurer="Acme Life", sheet="Acme", category="Par", product="Alpha Plan", payout_yr1=47.5,
                       payout_yr2=2.5, position=0)
        self.a2 = make(insurer="Acme Life", sheet="Acme", category="Par", product="Beta Plan", payout_yr1=30.0, position=1)
        self.a3 = make(insurer="Acme Life", sheet="Acme", category="Term", product="Gamma Plan", position=2)    # no payouts
        self.b1 = make(insurer="Bravo Life", sheet="Bravo", category="Par", product="Delta Plan", payout_yr1=12.0,
                       position=3)
        self.b2 = make(insurer="Bravo Life", sheet="Bravo", category="Par", product="Echo Plan", payout_yr1=0.4237,
                       position=4)
        self.client.force_login(self.manager)

    @staticmethod
    def _messages(response):
        return [str(message) for message in get_messages(response.wsgi_request)]


class LifeRateMasterAddAndDeleteTests(LifeMasterRowsMixin, TestCase):
    def _add_data(self, **over):
        data = {"upload": self.upload.pk, "insurer": "Acme Life", "category": "Par", "product": "New Plan",
                "variant": "", "ppt": "10", "pt": "20", "condition": "", "payout_yr1": "21.5", "payout_deferred": "",
                "payout_yr2": "", "payout_yr3": "", "payout_yr4": "", "payout_yr5": "", "payout_yr6_plus": "",
                "remarks": "typed in"}
        data.update(over)
        return data

    def test_the_add_form_offers_the_insurers_of_the_upload(self):
        response = self.client.get(reverse("life_rate_master_add"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<datalist id="lifeInsurers">')
        self.assertContains(response, '<option value="Acme Life">')
        self.assertContains(response, '<option value="Bravo Life">')
        self.assertNotContains(response, "Delete this row")
        response = self.client.get(reverse("life_rate_master_add"), {"insurer": "Bravo Life"})
        self.assertContains(response, 'value="Bravo Life"')                          # prefilled from the filter

    def test_a_new_row_goes_after_the_last_row_of_its_insurer(self):
        response = self.client.post(reverse("life_rate_master_add"), self._add_data())
        new = LifeRate.objects.get(product="New Plan")
        order = list(LifeRate.objects.filter(upload=self.upload).values_list("product", flat=True))
        self.assertEqual(order, ["Alpha Plan", "Beta Plan", "Gamma Plan", "New Plan", "Delta Plan", "Echo Plan"])
        self.assertEqual((new.sheet, new.source_ref, new.is_edited, new.edited_by, new.payout_yr1),
                         ("Acme", "Added on screen", True, self.manager, 21.5))
        self.assertTrue(new.added_on_screen)
        self.assertIsNotNone(new.edited_at)
        self.assertTrue(response["Location"].startswith(reverse("life_rate_master") + "?insurer=Acme+Life&page=1"))
        self.assertTrue(response["Location"].endswith(f"#row-{new.pk}"))
        self.assertIn(f"Added row #{new.pk}", self._messages(response)[0])

    def test_a_new_insurer_goes_last_and_the_upload_counts_follow(self):
        self.client.post(reverse("life_rate_master_add"), self._add_data(insurer="  Zeta Life ", product="Zed"))
        new = LifeRate.objects.get(product="Zed")
        self.assertEqual((new.insurer, new.position, new.sheet), ("Zeta Life", 5, ""))
        self.upload.refresh_from_db()
        self.assertEqual((self.upload.row_count, self.upload.insurer_count), (6, 3))

    def test_the_added_row_shows_in_the_checker_and_the_master_marks_it_added(self):
        self.client.post(reverse("life_rate_master_add"), self._add_data())
        self.assertContains(self.client.get(reverse("life_payout_rates"), {"q": "new plan"}), "21.5%")
        response = self.client.get(reverse("life_rate_master"), {"edited": "1"})
        self.assertContains(response, "life-edited")
        self.assertContains(response, ">added<")
        self.assertContains(response, "Added on screen")

    def test_adding_is_logged_with_every_value(self):
        self.client.post(reverse("life_rate_master_add"), self._add_data())
        new = LifeRate.objects.get(product="New Plan")
        log = AuditLog.objects.get(action="LIFE RATE ADD")
        self.assertEqual(log.user, self.manager)
        for text in (f"#{new.pk}", "'Oct'26'", "insurer='Acme Life'", "product='New Plan'", "payout_yr1=21.5",
                     "remarks='typed in'", "source_ref='Added on screen'"):
            self.assertIn(text, log.details)

    def test_an_incomplete_row_is_refused(self):
        for over, expected in (({"insurer": ""}, "required"), ({"product": " "}, "Enter the product"),
                               ({"payout_yr1": ""}, "at least one BG payout"), ({"payout_yr1": "140"}, "between 0 and 100")):
            response = self.client.post(reverse("life_rate_master_add"), self._add_data(**over))
            self.assertEqual(response.status_code, 200, over)
            self.assertContains(response, expected, msg_prefix=f"{over}: ")
        self.assertEqual(LifeRate.objects.count(), 5)
        self.assertFalse(AuditLog.objects.filter(action="LIFE RATE ADD").exists())

    def test_a_row_can_be_added_to_an_earlier_upload(self):
        old = LifeGridUpload.objects.create(label="Sep'26")
        response = self.client.post(reverse("life_rate_master_add"), self._add_data(upload=old.pk))
        new = LifeRate.objects.get(product="New Plan")
        self.assertEqual(new.upload, old)
        self.assertIn(f"upload={old.pk}", response["Location"])
        self.assertEqual(LifeRate.objects.filter(upload=self.upload).count(), 5)       # the live one is untouched

    def test_nothing_can_be_added_before_a_workbook_is_uploaded(self):
        LifeGridUpload.objects.all().delete()
        response = self.client.get(reverse("life_rate_master_add"))
        self.assertRedirects(response, reverse("life_rate_master_uploads"), fetch_redirect_response=False)

    def test_deleting_a_row_removes_it_logs_every_value_and_fixes_the_counts(self):
        response = self.client.post(reverse("life_rate_master_delete", args=[self.a1.pk]),
                                    {"next": "/life-rate-master/?insurer=Acme+Life&page=1"})
        self.assertRedirects(response, "/life-rate-master/?insurer=Acme+Life&page=1", fetch_redirect_response=False)
        self.assertFalse(LifeRate.objects.filter(pk=self.a1.pk).exists())
        self.upload.refresh_from_db()
        self.assertEqual((self.upload.row_count, self.upload.insurer_count), (4, 2))
        log = AuditLog.objects.get(action="LIFE RATE DELETE")
        self.assertEqual(log.user, self.manager)
        for text in (f"#{self.a1.pk}", "'Oct'26'", "product='Alpha Plan'", "payout_yr1=47.5", "payout_yr2=2.5",
                     "insurer='Acme Life'"):
            self.assertIn(text, log.details)
        self.assertIn("Deleted row", self._messages(response)[0])

    def test_deleting_the_last_row_of_an_insurer_drops_it_from_the_count_but_keeps_its_notes(self):
        note = LifeInsurerNote.objects.create(upload=self.upload, insurer="Bravo Life", text="Terms")
        for row in (self.b1, self.b2):
            self.client.post(reverse("life_rate_master_delete", args=[row.pk]))
        self.upload.refresh_from_db()
        self.assertEqual((self.upload.row_count, self.upload.insurer_count), (3, 1))
        self.assertTrue(LifeInsurerNote.objects.filter(pk=note.pk).exists())

    def test_delete_is_post_only_stays_on_this_site_and_404s_for_unknown_rows(self):
        self.assertEqual(self.client.get(reverse("life_rate_master_delete", args=[self.a1.pk])).status_code, 405)
        self.assertTrue(LifeRate.objects.filter(pk=self.a1.pk).exists())
        response = self.client.post(reverse("life_rate_master_delete", args=[self.a2.pk]),
                                    {"next": "https://evil.example/steal"})
        self.assertRedirects(response, reverse("life_rate_master"), fetch_redirect_response=False)
        self.assertEqual(self.client.post(reverse("life_rate_master_delete", args=[999999])).status_code, 404)

    def test_the_edit_page_has_a_delete_button_that_asks_first(self):
        response = self.client.get(reverse("life_rate_master_edit", args=[self.a1.pk]))
        self.assertContains(response, reverse("life_rate_master_delete", args=[self.a1.pk]))
        self.assertContains(response, "return confirm(")
        self.assertContains(response, "Delete row")

    def test_the_master_page_offers_add_and_export(self):
        response = self.client.get(reverse("life_rate_master"), {"insurer": "Acme Life"})
        self.assertContains(response, reverse("life_rate_master_add") + "?next=")
        self.assertContains(response, "insurer=Acme+Life")
        self.assertContains(response, reverse("life_rate_master_export") + "?insurer=Acme+Life")

    def test_the_security_audit_log_lists_the_three_new_actions(self):
        for action in ("LIFE RATE ADD", "LIFE RATE DELETE", "LIFE BULK UPDATE"):
            self.assertIn(action, SECURITY_AUDIT_LOG_ACTIONS)


class LifeRateMasterBulkUpdateTests(LifeMasterRowsMixin, TestCase):
    def _bulk(self, **data):
        base = {"upload": self.upload.pk, "scope": "selected", "next": "/life-rate-master/?insurer=Acme+Life"}
        base.update(data)
        return self.client.post(reverse("life_rate_master_bulk_update"), base)

    def test_the_page_has_tick_boxes_and_a_dialog_with_exactly_the_whitelisted_fields(self):
        response = self.client.get(reverse("life_rate_master"), {"insurer": "Acme Life", "q": "plan"})
        html = response.content.decode()
        self.assertEqual(html.count('name="selected_rows"'), 3)
        self.assertContains(response, 'name="scope"')
        self.assertContains(response, 'name="expected_count" value="3"')
        self.assertContains(response, 'name="insurer" value="Acme Life"')
        block = re.search(r'<select name="field"[^>]*>(.*?)</select>', html, re.S).group(1)
        self.assertEqual(re.findall(r'<option value="([^"]+)"', block),
                         [*LifeRate.MG_FIELDS, *LifeRate.PAYOUT_FIELDS,
                          "category", "product", "variant", "ppt", "pt", "condition", "remarks"])
        self.assertContains(response, "lifeBulkModal")

    def test_the_checker_has_no_tick_boxes_or_bulk_form(self):
        response = self.client.get(reverse("life_payout_rates"), {"q": "plan"})
        for text in ("life-row-check", "selected_rows", "lifeBulkForm", "bulk-update", "Add row", "Export Excel"):
            self.assertNotContains(response, text)

    def test_selected_rows_are_changed_stamped_and_logged(self):
        response = self._bulk(selected_rows=[self.a1.pk, self.a2.pk, self.b1.pk], field="payout_yr1",
                              operation="add", value="0.5")
        self.assertRedirects(response, "/life-rate-master/?insurer=Acme+Life", fetch_redirect_response=False)
        for row, expected in ((self.a1, 48.0), (self.a2, 30.5), (self.b1, 12.5)):
            row.refresh_from_db()
            self.assertEqual((row.payout_yr1, row.is_edited, row.edited_by), (expected, True, self.manager))
            self.assertIsNotNone(row.edited_at)
        self.b2.refresh_from_db()
        self.assertEqual((self.b2.payout_yr1, self.b2.is_edited), (0.4237, False))              # not ticked
        log = AuditLog.objects.get(action="LIFE BULK UPDATE")
        self.assertEqual(log.user, self.manager)
        for text in ("add 0.5 percentage points to Year 1 BG payout", "3 selected rows", f"#{self.a1.pk}: 47.5 -> 48.0",
                     "'Oct'26'"):
            self.assertIn(text, log.details)
        self.assertIn("3 rows changed", self._messages(response)[0])

    def test_every_row_matching_the_filters_is_changed_not_only_the_page(self):
        for n in range(60):
            LifeRate.objects.create(upload=self.upload, insurer="Acme Life", product=f"Bulk {n}", payout_yr1=10.0,
                                    position=10 + n)
        response = self._bulk(scope="filtered", insurer="Acme Life", expected_count=63, field="payout_yr1",
                              operation="add", value="1")
        self.assertEqual(LifeRate.objects.filter(insurer="Acme Life", is_edited=True).count(), 62)   # a3 has no payout
        self.assertEqual(LifeRate.objects.filter(insurer="Acme Life", payout_yr1=11.0).count(), 60)
        self.b1.refresh_from_db()
        self.assertFalse(self.b1.is_edited)
        message = self._messages(response)[0]
        self.assertIn("62 rows changed", message)
        self.assertIn("1 skipped (blank payout)", message)
        log = AuditLog.objects.get(action="LIFE BULK UPDATE")
        self.assertIn("all 63 rows matching the filters (insurer 'Acme Life')", log.details)
        self.assertIn(f"#{self.a1.pk}: 47.5 -> 48.5", log.details)                          # 62 changes: all of them listed
        self.assertNotIn(" more", log.details)

    def test_a_stale_row_count_stops_the_bulk_edit(self):
        response = self._bulk(scope="filtered", insurer="Acme Life", expected_count=2, field="payout_yr1",
                              operation="set", value="5")
        self.assertIn("changed since the page was loaded", self._messages(response)[0])
        self.a1.refresh_from_db()
        self.assertEqual(self.a1.payout_yr1, 47.5)
        self.assertFalse(AuditLog.objects.filter(action="LIFE BULK UPDATE").exists())

    def test_identity_and_system_fields_are_refused(self):
        for column in ("insurer", "sheet", "section", "source_ref", "position", "upload", "id", "is_edited",
                       "edited_by", "edited_at", "nonsense"):
            response = self._bulk(selected_rows=[self.a1.pk], field=column, operation="set", value="X")
            messages = self._messages(response)
            self.assertTrue(messages and "No rows were changed" in messages[0], column)
        self.a1.refresh_from_db()
        self.assertEqual((self.a1.insurer, self.a1.sheet, self.a1.source_ref, self.a1.position, self.a1.is_edited),
                         ("Acme Life", "Acme", "", 0, False))
        self.assertFalse(AuditLog.objects.filter(action="LIFE BULK UPDATE").exists())

    def test_one_row_leaving_0_to_100_refuses_the_whole_operation(self):
        response = self._bulk(selected_rows=[self.a1.pk, self.a2.pk], field="payout_yr1", operation="add", value="60")
        message = self._messages(response)[0]
        self.assertIn("No rows were changed", message)
        self.assertIn(f"#{self.a1.pk}", message)
        self.assertIn("107.5", message)                                                      # 47.5 + 60
        for row, expected in ((self.a1, 47.5), (self.a2, 30.0)):
            row.refresh_from_db()
            self.assertEqual((row.payout_yr1, row.is_edited), (expected, False))            # a2 (90) would have been fine
        self.assertFalse(AuditLog.objects.filter(action="LIFE BULK UPDATE").exists())

    def test_blank_payouts_are_skipped_and_the_result_is_rounded(self):
        response = self._bulk(selected_rows=[self.a3.pk, self.b2.pk], field="payout_yr1", operation="multiply",
                              value="0.5")
        self.b2.refresh_from_db()
        self.a3.refresh_from_db()
        self.assertEqual(self.b2.payout_yr1, 0.2119)
        self.assertIsNone(self.a3.payout_yr1)
        self.assertFalse(self.a3.is_edited)
        self.assertIn("1 skipped (blank payout)", self._messages(response)[0])

    def test_text_fields_and_clearing(self):
        self._bulk(selected_rows=[self.a1.pk, self.a2.pk], field="category", operation="set", value="PAR")
        self._bulk(selected_rows=[self.a1.pk], field="remarks", operation="append", value="valid till Nov")
        self._bulk(selected_rows=[self.a1.pk], field="payout_yr2", operation="clear", value="")
        self.a1.refresh_from_db()
        self.a2.refresh_from_db()
        self.assertEqual((self.a1.category, self.a2.category, self.a1.remarks, self.a1.payout_yr2),
                         ("PAR", "PAR", "valid till Nov", None))
        self.assertEqual(AuditLog.objects.filter(action="LIFE BULK UPDATE").count(), 3)

    def test_rows_of_another_upload_are_never_touched(self):
        other = LifeGridUpload.objects.create(label="Sep'26")
        foreign = LifeRate.objects.create(upload=other, insurer="Acme Life", product="Old", payout_yr1=10.0)
        response = self._bulk(selected_rows=[foreign.pk], field="payout_yr1", operation="set", value="99")
        self.assertIn("no rows were selected", self._messages(response)[0])
        foreign.refresh_from_db()
        self.assertEqual(foreign.payout_yr1, 10.0)

    def test_a_change_that_changes_nothing_is_not_an_edit_and_is_not_logged(self):
        response = self._bulk(selected_rows=[self.a1.pk], field="payout_yr1", operation="set", value="47.5")
        self.assertIn("Nothing to change", self._messages(response)[0])
        self.a1.refresh_from_db()
        self.assertFalse(self.a1.is_edited)
        self.assertFalse(AuditLog.objects.filter(action="LIFE BULK UPDATE").exists())

    def test_nothing_ticked_and_bad_values_change_nothing(self):
        for data in ({"selected_rows": []}, {"selected_rows": [self.a1.pk], "value": "abc"},
                     {"selected_rows": [self.a1.pk], "value": "250"}):
            response = self._bulk(field="payout_yr1", operation="set", **({"value": "5"} | data))
            self.assertIn("No rows were changed", self._messages(response)[0], data)
        self.a1.refresh_from_db()
        self.assertEqual(self.a1.payout_yr1, 47.5)

    def test_bulk_edit_is_post_only_and_needs_the_rate_master_group(self):
        self.assertEqual(self.client.get(reverse("life_rate_master_bulk_update")).status_code, 405)
        self.client.force_login(self.viewer)
        response = self.client.post(reverse("life_rate_master_bulk_update"), {
            "upload": self.upload.pk, "scope": "selected", "selected_rows": [self.a1.pk], "field": "payout_yr1",
            "operation": "set", "value": "1"})
        self.assertEqual(response.status_code, 403)
        self.a1.refresh_from_db()
        self.assertEqual(self.a1.payout_yr1, 47.5)


class LifeRateMasterExportTests(LifeMasterRowsMixin, TestCase):
    def _sheet(self, **params):
        response = self.client.get(reverse("life_rate_master_export"), params)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        ws = load_workbook(io.BytesIO(response.content)).active
        header = [cell.value for cell in ws[1]]
        return response, header, [dict(zip(header, [cell.value for cell in row])) for row in ws.iter_rows(min_row=2)]

    def test_the_header_is_the_stored_field_names(self):
        _, header, rows = self._sheet()
        self.assertEqual(header, list(EXPORT_COLUMNS))
        self.assertEqual(header[:3], ["id", "upload", "insurer"])
        for name in LifeRate.PAYOUT_FIELDS:
            self.assertIn(name, header)
        self.assertEqual(len(rows), 5)

    def test_every_stored_value_is_exported_in_workbook_order(self):
        _, _, rows = self._sheet()
        self.assertEqual([row["product"] for row in rows],
                         ["Alpha Plan", "Beta Plan", "Gamma Plan", "Delta Plan", "Echo Plan"])
        first = rows[0]
        self.assertEqual((first["id"], first["upload"], first["insurer"], first["sheet"], first["category"],
                          first["payout_yr1"], first["payout_yr2"], first["is_edited"], first["position"]),
                         (self.a1.pk, "Oct'26", "Acme Life", "Acme", "Par", 47.5, 2.5, "NO", 0))
        self.assertIsNone(rows[2]["payout_yr1"])                                  # blank payouts are blank cells
        self.assertEqual(rows[4]["payout_yr1"], 0.4237)

    def test_the_export_follows_the_filters_and_is_not_limited_to_a_page(self):
        for n in range(60):
            LifeRate.objects.create(upload=self.upload, insurer="Bulk Life", product=f"Plan {n}", position=10 + n)
        _, _, rows = self._sheet()
        self.assertEqual(len(rows), 65)                                            # more than one page of 50
        _, _, rows = self._sheet(insurer="Bravo Life")
        self.assertEqual([row["product"] for row in rows], ["Delta Plan", "Echo Plan"])
        _, _, rows = self._sheet(category="Term")
        self.assertEqual([row["product"] for row in rows], ["Gamma Plan"])
        _, _, rows = self._sheet(q="beta")
        self.assertEqual([row["product"] for row in rows], ["Beta Plan"])

    def test_edits_show_who_and_when_and_text_starting_with_an_equals_sign_stays_text(self):
        self.client.post(reverse("life_rate_master_bulk_update"), {
            "upload": self.upload.pk, "scope": "selected", "selected_rows": [self.a2.pk], "field": "remarks",
            "operation": "set", "value": "=100% of premium"})
        _, _, rows = self._sheet(edited="1")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["is_edited"], rows[0]["edited_by"], rows[0]["remarks"]),
                         ("YES", "manager", "=100% of premium"))
        self.assertRegex(rows[0]["edited_at"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_an_earlier_upload_can_be_exported_and_the_file_is_named_after_it(self):
        old = LifeGridUpload.objects.create(label="Sep 26")
        LifeRate.objects.create(upload=old, insurer="Acme Life", product="Old Plan", payout_yr1=9.0)
        response, _, rows = self._sheet(upload=old.pk)
        self.assertEqual([row["product"] for row in rows], ["Old Plan"])
        self.assertEqual(response["Content-Disposition"], 'attachment; filename="life_rates_Sep26.xlsx"')
        response, _, rows = self._sheet(upload=99999)                              # unknown: the live upload
        self.assertEqual(len(rows), 5)
        self.assertEqual(response["Content-Disposition"], 'attachment; filename="life_rates_Oct26.xlsx"')

    def test_nothing_to_export_before_an_upload_and_the_group_is_needed(self):
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(reverse("life_rate_master_export")).status_code, 403)
        self.client.force_login(self.manager)
        LifeGridUpload.objects.all().delete()
        response = self.client.get(reverse("life_rate_master_export"))
        self.assertRedirects(response, reverse("life_rate_master_uploads"), fetch_redirect_response=False)


class LifeAccessWiringTests(TestCase):
    """The groups exist after migrating, Life has its own checkbox in Access Control and does not ride on Rate Checker."""

    def test_the_two_life_groups_are_seeded_by_a_migration(self):
        self.assertEqual(
            set(Group.objects.filter(name__in=["Can_View_Life_Payout_Grid", "Can_View_Life_Rate_Master"])
                .values_list("name", flat=True)),
            {"Can_View_Life_Payout_Grid", "Can_View_Life_Rate_Master"})

    def test_the_rate_checker_checkbox_stands_for_motor_and_health_only(self):
        from insurance.access import PAGE_GROUPS, RATE_CHECKER_GROUPS, RATE_CHECKER_TABS
        self.assertEqual(RATE_CHECKER_GROUPS, {"Can_View_Policy_Locker", "Can_View_Health_Payout_Rates"})
        self.assertEqual([name for _, name in RATE_CHECKER_TABS][-1], "life_payout_rates")
        self.assertIn("Can_View_Life_Rate_Master", PAGE_GROUPS)

    def _access_post(self, staff, user, pages):
        self.client.force_login(staff)
        return self.client.post(reverse("user_management"), {
            "action": "update_user_access", "user_id": user.id, f"pages_{user.id}": pages})

    def test_access_control_grants_and_keeps_the_life_groups(self):
        staff = User.objects.create_user(username="boss", password="a-strong-test-password-1", is_staff=True)
        user = User.objects.create_user(username="agent", password="a-strong-test-password-1")
        self._access_post(staff, user, ["Rate_Checker", "Can_View_Life_Payout_Grid", "Can_View_Life_Rate_Master"])
        self.assertEqual(
            set(user.groups.values_list("name", flat=True)),
            {"Can_View_Policy_Locker", "Can_View_Health_Payout_Rates", "Can_View_Life_Payout_Grid",
             "Can_View_Life_Rate_Master"})
        # Saving again without the Life boxes ticked removes them, and "Rate Checker" alone never grants Life.
        self._access_post(staff, user, ["Rate_Checker"])
        self.assertEqual(
            set(user.groups.values_list("name", flat=True)),
            {"Can_View_Policy_Locker", "Can_View_Health_Payout_Rates"})

    def test_access_control_page_offers_the_life_checkboxes(self):
        staff = User.objects.create_user(username="boss", password="a-strong-test-password-1", is_staff=True)
        self.client.force_login(staff)
        response = self.client.get(reverse("user_management"))
        self.assertContains(response, 'value="Can_View_Life_Payout_Grid"')
        self.assertContains(response, 'value="Can_View_Life_Rate_Master"')
