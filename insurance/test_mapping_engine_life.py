"""The MIS Payout Engine's Life route: a row whose Product is 'life' is matched to the live Life grid, and the first-year
payout is printed in Porate (services/life_mis.py holds the rules, mapping_engine._match_life_row the output)."""
import io
import tempfile
from datetime import date
from unittest import mock

import pandas as pd
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings

from insurance.mapping_engine import process_mis_mapping
from insurance.models import (
    HealthRateMaster, LifeGridUpload, LifeInsurerAlias, LifePlanMapping, LifeRate, MISFailedRow, MISFile, RateMaster,
)
from insurance.services import life_filters as lf
from insurance.services import life_mis

MATCH = "✅ MATCH"
NO_MATCH = "❌ NO MATCH"
MULTIPLE = "⚠️ MULTIPLE MATCHES"
BAD_DATA = "FAILED - BAD DATA"

MOTOR_INSURER = "Acme General Insurance"
HEALTH_INSURER = "Zenith Health Insurance"

PLAN = "Policy: product name  plan name"        # two spaces, as the real exports write it
PPT = "Policy: premium paying term  ppt"
PT = "Policy: policy termpt"


def _life(insurer="HDFC Life Insurance Co. Ltd.", plan="HDFC Life Sanchay Plus", ppt=6, pt=20, mode="Yearly",
          policy_type="Fresh", **extra):
    row = {
        "Product": "life", "Policy: insurance company": insurer, PLAN: plan, PPT: ppt, PT: pt,
        "Policy: premium mode": mode, "Policy Type": policy_type,
    }
    row.update(extra)
    return row


def _motor():
    return {"Product": "motor", "Policy: insurance company": MOTOR_INSURER}


class LifeEngineBase(TestCase):
    """Runs process_mis_mapping on a small CSV against a small Life grid."""

    def setUp(self):
        media = tempfile.TemporaryDirectory()
        self.addCleanup(media.cleanup)
        override = override_settings(MEDIA_ROOT=media.name)
        override.enable()
        self.addCleanup(override.disable)
        patcher = mock.patch("insurance.mapping_engine.connection")     # the worker closes the DB connection
        patcher.start()
        self.addCleanup(patcher.stop)

        # The engine refuses a run when RateMaster has no active rows at all.
        RateMaster.objects.create(
            insurance_company=MOTOR_INSURER, status="ACTIVE", is_deleted="NO", po_type="On Net", po_net_rate=12.5,
            pi_type="On Net", pi_net_rate=20, po_od_rate=0, po_tp_rate=0, pi_od_rate=0, pi_tp_rate=0,
        )
        self.upload = LifeGridUpload.objects.create(label="Oct'26", is_active=True)

    def grid(self, insurer, product, ppt="", pt="", yr1=10.0, **extra):
        return LifeRate.objects.create(
            upload=self.upload, insurer=insurer, product=product, ppt=ppt, pt=pt, payout_yr1=yr1, **extra)

    def alias(self, mis_name, insurer):
        LifeInsurerAlias.objects.update_or_create(
            name_key=life_mis.fold(mis_name), defaults={"mis_name": mis_name, "insurer": insurer})

    def plan_mapping(self, insurer, plan, grid_products):
        return LifePlanMapping.objects.create(
            insurer=insurer, plan_name=plan, plan_key=life_mis.fold(plan), grid_products=grid_products)

    def run_file(self, rows):
        buf = io.StringIO()
        pd.DataFrame(rows).to_csv(buf, index=False)
        self.mis = MISFile.objects.create(
            uploaded_file=SimpleUploadedFile("mis.csv", buf.getvalue().encode("utf-8")))
        process_mis_mapping(self.mis.id)
        self.mis.refresh_from_db()
        self.assertEqual(self.mis.status, "COMPLETED", self.mis.error_message)
        with self.mis.processed_file.open("rb") as f:
            return pd.read_csv(f)

    def one(self, row):
        """The output row of a file holding just this Life row."""
        return self.run_file([row]).iloc[0]


class LifeMatchTests(LifeEngineBase):
    def setUp(self):
        super().setUp()
        self.sanchay_5_7 = self.grid("HDFC Life", "Sanchay Plus", ppt="5 to 7", yr1=40.0)
        self.sanchay_10 = self.grid("HDFC Life", "Sanchay Plus", ppt=">=10", yr1=47.0)

    def test_a_life_row_matches_and_prints_the_first_year_rate(self):
        out = self.one(_life())
        self.assertEqual(out["Mapping Status"], MATCH)
        self.assertEqual(out["Porate"], 40.0)
        self.assertEqual(out["Displaygroupid"], self.sanchay_5_7.id)
        self.assertEqual(out["Potype"], life_mis.PAYOUT_TYPE)
        self.assertTrue(out["Failure Reason"].startswith("Matched Successfully — Life grid Oct'26: HDFC Life / Sanchay Plus"))
        self.assertIn(f"grid row {self.sanchay_5_7.id}", out["Failure Reason"])
        self.assertIn("plan found by name", out["Failure Reason"])

    def test_the_pay_term_picks_the_row(self):
        out = self.run_file([_life(ppt=6), _life(ppt=12)])
        self.assertEqual(list(out["Porate"]), [40.0, 47.0])
        self.assertEqual(list(out["Displaygroupid"]), [self.sanchay_5_7.id, self.sanchay_10.id])

    def test_only_the_rate_is_printed_never_a_rupee_amount_or_the_mg(self):
        out = self.one(_life(**{"Policy: gwp net premium": "50,000.00", "Policy: premium": 25000}))
        self.assertEqual(out["Mapping Status"], MATCH)
        for column in ("Pay-in Amt", "Pay-out Amt", "Margin Amt", "Margin Rate", "Pirate", "Pitype"):
            self.assertTrue(pd.isna(out[column]), column)

    def test_the_insurer_may_sit_in_a_different_column(self):
        row = _life()
        row["Policy: insurance company"] = None
        row["Policy: insurer name"] = "HDFC Life Insurance Co. Ltd."
        self.assertEqual(self.one(row)["Mapping Status"], MATCH)

    def test_the_plan_column_is_found_whatever_its_punctuation(self):
        row = _life()
        row["Policy: product name / plan name"] = row.pop(PLAN)
        self.assertEqual(self.one(row)["Mapping Status"], MATCH)

    def test_alike_rows_collapse_to_the_lowest_id(self):
        first = self.grid("HDFC Life", "Click 2 Achieve", ppt=">=5", yr1=47.0)
        self.grid("HDFC Life", "Click 2 Achieve", ppt="8 & 9", yr1=47.0)
        out = self.one(_life(plan="Click 2 Achieve", ppt=8))
        self.assertEqual(out["Mapping Status"], MATCH)
        self.assertEqual(out["Displaygroupid"], first.id)
        self.assertIn("lowest id", out["Failure Reason"])

    def test_rows_that_differ_are_multiple_matches_named_by_condition(self):
        self.alias("Tata AIA Life Insurance Co. Ltd.", "Tata AIA Life")
        offline = self.grid("Tata AIA Life", "SRP 2 Pay - Life Option", ppt="2", yr1=16.5, condition="Offline")
        digital = self.grid("Tata AIA Life", "SRP 2 Pay - Life Option", ppt="2", yr1=9.0, condition="Digital discount")
        self.plan_mapping("Tata AIA Life", "Tata AIA Sampoorna Raksha Promise", "SRP*")
        out = self.one(_life(insurer="Tata AIA Life Insurance Co. Ltd.", plan="Tata AIA Sampoorna Raksha Promise",
                             ppt=2, pt=30))
        self.assertEqual(out["Mapping Status"], MULTIPLE)
        reason = out["Failure Reason"]
        self.assertIn("Offline: 16.5%", reason)
        self.assertIn("Digital discount: 9%", reason)
        self.assertTrue(reason.endswith(f"Matching Group IDs: {offline.id}, {digital.id}"))
        self.assertTrue(pd.isna(out["Porate"]))

    def test_the_deferred_part_and_conditions_are_printed_not_applied(self):
        self.grid("HDFC Life", "Click 2 Protect Supreme", ppt=">=5", yr1=47.0, payout_deferred=15.0,
                  condition="Offline", remarks="85% to be paid in 1st Year")
        out = self.one(_life(plan="HDFC Life Click 2 Protect Supreme", ppt=10, pt=40))
        self.assertEqual(out["Mapping Status"], MATCH)
        self.assertEqual(out["Porate"], 47.0)
        self.assertIn("Deferred (paid at 2nd year on persistency): 15%", out["Addtnc"])
        self.assertIn("Manual Review Required: Condition: Offline | Remarks: 85% to be paid in 1st Year", out["Addtnc"])

    def test_a_row_without_conditions_has_no_addtnc(self):
        self.assertTrue(pd.isna(self.one(_life())["Addtnc"]))

    def test_a_grid_row_with_no_first_year_payout_is_no_match(self):
        self.grid("HDFC Life", "Zero Plan", ppt="10", yr1=None)
        out = self.one(_life(plan="Zero Plan", ppt=10))
        self.assertEqual(out["Mapping Status"], NO_MATCH)
        self.assertIn("has no first-year payout", out["Failure Reason"])


class LifeInsurerTests(LifeEngineBase):
    def setUp(self):
        super().setUp()
        self.grid("Bajaj Allianz Life", "ACE / ACE ADVANTAGE", ppt="5", yr1=35.5)
        self.grid("Digit Life", "Glow Plus", ppt="5", yr1=20.0)

    def test_an_alias_maps_the_mis_spelling_to_the_grid_insurer(self):
        self.alias("Bajaj Life Insurance Co. Ltd.", "Bajaj Allianz Life")
        out = self.one(_life(insurer="Bajaj Life Insurance Co. Ltd.", plan="Bajaj Life ACE", ppt=5))
        self.assertEqual(out["Mapping Status"], MATCH)
        self.assertEqual(out["Porate"], 35.5)

    def test_a_name_listing_several_products_is_matched_on_the_one_it_names(self):
        self.alias("Bajaj Life Insurance Co. Ltd.", "Bajaj Allianz Life")
        self.grid("Bajaj Allianz Life", "ELITE ASSURE", ppt="5", yr1=1.0)
        out = self.one(_life(insurer="Bajaj Life Insurance Co. Ltd.", plan="Bajaj Life ACE", ppt=5))
        self.assertEqual(out["Displaygroupid"], LifeRate.objects.get(product="ACE / ACE ADVANTAGE").id)

    def test_a_name_with_every_word_of_one_grid_insurer_needs_no_alias(self):
        out = self.one(_life(insurer="Digit Life Insurance Company Limited", plan="Glow Plus", ppt=5))
        self.assertEqual(out["Mapping Status"], MATCH)

    def test_a_general_insurers_name_is_flagged_never_tied_to_the_life_company(self):
        out = self.one(_life(insurer="Bajaj Allianz General Insurance Company Limited", plan="ACE", ppt=5))
        self.assertEqual(out["Mapping Status"], NO_MATCH)
        reason = out["Failure Reason"]
        self.assertTrue(reason.startswith("Failed on: Policy: insurance company — Insurer 'Bajaj Allianz General"))
        self.assertIn("General insurer's name on a Life policy", reason)

    def test_an_unknown_insurer_is_unmapped_not_guessed(self):
        out = self.one(_life(insurer="Max Life Insurance Co. Ltd.", plan="Whatever", ppt=5))
        self.assertEqual(out["Mapping Status"], NO_MATCH)
        self.assertIn("is not a known Life insurer — add it on Life Insurer Aliases", out["Failure Reason"])

    def test_an_alias_pointing_at_a_name_the_grid_does_not_have_says_so(self):
        self.alias("Kotak Life Insurance Co. Ltd.", "Kotak Mahindra Life")
        out = self.one(_life(insurer="Kotak Life Insurance Co. Ltd.", plan="Whatever", ppt=5))
        self.assertEqual(out["Mapping Status"], NO_MATCH)
        self.assertIn("which is not an insurer of the live Life grid", out["Failure Reason"])

    def test_a_blank_insurer_is_no_match(self):
        row = _life()
        row["Policy: insurance company"] = None
        out = self.one(row)
        self.assertEqual(out["Mapping Status"], NO_MATCH)
        self.assertIn("Insurer is blank", out["Failure Reason"])


class LifePlanTests(LifeEngineBase):
    def setUp(self):
        super().setUp()
        self.grid("HDFC Life", "Sanchay Plus", ppt="5 to 7", yr1=40.0)
        self.grid("HDFC Life", "Smart Protect Plan (any rider)", ppt=">=5", yr1=30.0)

    def test_a_blank_plan_name_cannot_be_matched(self):
        out = self.one(_life(plan=None))
        self.assertEqual(out["Mapping Status"], NO_MATCH)
        self.assertTrue(out["Failure Reason"].startswith("Failed on: Policy: product name / plan name — Plan name is blank"))

    def test_an_unmapped_plan_is_no_match_with_the_closest_products_as_a_hint(self):
        out = self.one(_life(plan="HDFC Life Smart Protect Plus", ppt=10))
        self.assertEqual(out["Mapping Status"], NO_MATCH)
        reason = out["Failure Reason"]
        self.assertIn("Plan 'HDFC Life Smart Protect Plus' is not mapped to any 'HDFC Life' Life grid product", reason)
        self.assertIn("add it on Life Plan Mapping", reason)
        self.assertIn("closest grid products: Smart Protect Plan (any rider)", reason)

    def test_the_mapping_makes_it_match(self):
        self.plan_mapping("HDFC Life", "HDFC Life Smart Protect Plus", "Smart Protect Plan (any rider)")
        out = self.one(_life(plan="HDFC Life Smart Protect Plus", ppt=10))
        self.assertEqual(out["Mapping Status"], MATCH)
        self.assertEqual(out["Porate"], 30.0)
        self.assertIn("plan found by mapping", out["Failure Reason"])

    def test_a_mapping_wins_over_a_name_match(self):
        self.plan_mapping("HDFC Life", "HDFC Life Sanchay Plus", "Smart Protect Plan (any rider)")
        out = self.one(_life(plan="HDFC Life Sanchay Plus", ppt=6))
        self.assertEqual(out["Porate"], 30.0)

    def test_a_mapping_ignores_case_punctuation_and_spacing(self):
        self.plan_mapping("HDFC Life", "hdfc  LIFE smart-protect plus", "smart protect plan (ANY rider)")
        self.assertEqual(self.one(_life(plan="HDFC Life Smart Protect Plus", ppt=10))["Mapping Status"], MATCH)

    def test_a_trailing_star_takes_every_product_that_starts_with_the_text(self):
        self.alias("Tata AIA Life Insurance Co. Ltd.", "Tata AIA Life")
        self.grid("Tata AIA Life", "SRP 3 Pay - Life Option", ppt="3", yr1=20.0)
        self.grid("Tata AIA Life", "SRPX Other Plan", ppt="3", yr1=1.0)
        self.plan_mapping("Tata AIA Life", "Tata AIA Sampoorna Raksha Promise", "SRP*")
        out = self.one(_life(insurer="Tata AIA Life Insurance Co. Ltd.", plan="Tata AIA Sampoorna Raksha Promise",
                             ppt=3, pt=30))
        self.assertEqual(out["Mapping Status"], MATCH)
        self.assertEqual(out["Porate"], 20.0)      # SRPX is not 'SRP' followed by a word boundary

    def test_a_mapping_whose_products_are_not_in_the_grid_says_so(self):
        self.plan_mapping("HDFC Life", "HDFC Life Gone Plan", "A Product Nobody Lists")
        out = self.one(_life(plan="HDFC Life Gone Plan", ppt=10))
        self.assertEqual(out["Mapping Status"], NO_MATCH)
        self.assertIn("none of its grid products is in the live Life grid", out["Failure Reason"])

    def test_a_name_that_fits_several_grid_products_is_ambiguous(self):
        self.grid("HDFC Life", "Click 2 Wealth / Sanchay Plus", ppt="5 to 7", yr1=1.0)
        out = self.one(_life(plan="Sanchay Plus", ppt=6))
        self.assertEqual(out["Mapping Status"], NO_MATCH)
        self.assertIn("matches 2 'HDFC Life' Life grid products by name", out["Failure Reason"])

    def test_the_names_stay_exact_not_fuzzy(self):
        # "Sanchay" is not "Sanchay Plus": a near name is a hint, never a match.
        out = self.one(_life(plan="HDFC Life Sanchay", ppt=6))
        self.assertEqual(out["Mapping Status"], NO_MATCH)
        self.assertIn("closest grid products: Sanchay Plus", out["Failure Reason"])


class LifeTermTests(LifeEngineBase):
    def setUp(self):
        super().setUp()
        self.grid("HDFC Life", "Sanchay Plus", ppt="5 to 7", pt="20", yr1=40.0)
        self.grid("Digit Life", "Glow Plus", ppt="SP", yr1=10.0)
        self.alias("Go-Digit Life Insurance Ltd.", "Digit Life")

    def test_a_pay_term_no_row_covers_is_no_match_and_shows_what_the_grid_states(self):
        out = self.one(_life(ppt=4))
        self.assertEqual(out["Mapping Status"], NO_MATCH)
        self.assertTrue(out["Failure Reason"].startswith("Failed on: Policy: premium paying term / ppt — PPT '4'"))
        self.assertIn("the grid states PPT: 5 to 7", out["Failure Reason"])

    def test_a_policy_term_no_row_covers_is_no_match(self):
        out = self.one(_life(ppt=6, pt=30))
        self.assertEqual(out["Mapping Status"], NO_MATCH)
        self.assertIn("Policy term '30' is not covered", out["Failure Reason"])

    def test_both_failures_are_reported_in_one_reason(self):
        out = self.one(_life(ppt=4, pt=30))
        reason = out["Failure Reason"]
        self.assertTrue(reason.startswith("Failed on: Policy: premium paying term / ppt"))
        self.assertIn(" | Also failed on: Policy: policy term / pt — Policy term '30'", reason)

    def test_a_single_pay_policy_matches_the_single_pay_rows(self):
        out = self.one(_life(insurer="Go-Digit Life Insurance Ltd.", plan="Glow Plus", ppt=1, pt=20))
        self.assertEqual(out["Mapping Status"], MATCH)
        self.assertEqual(out["Porate"], 10.0)

    def test_a_single_premium_mode_stands_for_a_ppt_of_one(self):
        out = self.one(_life(insurer="Go-Digit Life Insurance Ltd.", plan="Glow Plus", ppt=0, pt=20,
                             mode="Single Premium"))
        self.assertEqual(out["Mapping Status"], MATCH)

    def test_a_single_pay_policy_never_takes_a_numbered_row(self):
        out = self.one(_life(ppt=1, pt=20))
        self.assertEqual(out["Mapping Status"], NO_MATCH)
        self.assertIn("PPT '1'", out["Failure Reason"])

    def test_bad_pay_terms_are_bad_data_each_named(self):
        cases = {
            "a blank PPT": (_life(ppt=None), "is blank on this policy, so its pay term cannot be matched"),
            "a PPT of 0": (_life(ppt=0), "is 0, which cannot be matched to a pay term"),
            "a PPT that is not a number": (_life(ppt="ten"), "'ten' is not a whole number of years"),
            "a blank PT": (_life(pt=None), "is blank on this policy, so its policy term cannot be matched"),
            "a PPT longer than the term": (_life(ppt=30, pt=20), "is longer than Policy: policy term / pt"),
            "single pay with a long PPT": (_life(ppt=10, mode="Single"), "but the premium mode says single pay"),
        }
        for name, (row, wording) in cases.items():
            with self.subTest(name):
                out = self.one(row)
                self.assertEqual(out["Mapping Status"], BAD_DATA)
                self.assertTrue(out["Failure Reason"].startswith("Validation error — "), out["Failure Reason"])
                self.assertIn(wording, out["Failure Reason"])
                self.assertTrue(pd.isna(out["Porate"]))

    def test_bad_data_lists_every_problem_then_the_rate_failures(self):
        out = self.one(_life(ppt=None, pt=None, plan="No Such Plan"))
        reason = out["Failure Reason"]
        self.assertEqual(reason.count("Validation error — "), 2)
        self.assertIn(" | Also failed on: Policy: product name / plan name — Plan 'No Such Plan'", reason)

    def test_only_a_fresh_policy_gets_a_first_year_rate(self):
        for value in ("Endorsement Financial", "Renewal", None):
            with self.subTest(value):
                out = self.one(_life(policy_type=value))
                self.assertEqual(out["Mapping Status"], NO_MATCH)
                self.assertTrue(out["Failure Reason"].startswith("Failed on: Policy Type — Policy Type '"))
                self.assertIn("not a first-year ('Fresh') policy", out["Failure Reason"])

    def test_the_policy_type_is_read_case_and_space_insensitively(self):
        self.grid("HDFC Life", "Sanchay Plus", ppt=">=10", pt="20", yr1=47.0)
        self.assertEqual(self.one(_life(policy_type="  FRESH "))["Mapping Status"], MATCH)


class LifeRunTests(LifeEngineBase):
    def setUp(self):
        super().setUp()
        self.grid("HDFC Life", "Sanchay Plus", ppt="5 to 7", yr1=40.0)

    def test_no_live_grid_says_so_for_every_life_row(self):
        self.upload.is_active = False
        self.upload.save()
        out = self.one(_life())
        self.assertEqual(out["Mapping Status"], NO_MATCH)
        self.assertTrue(out["Failure Reason"].startswith("Failed on: Life grid — No Life grid is live"))

    def test_a_file_of_only_life_matches_does_not_crash(self):
        out = self.run_file([_life(), _life()])
        self.assertEqual(list(out["Mapping Status"]), [MATCH, MATCH])

    def test_a_file_of_only_health_matches_does_not_crash(self):
        HealthRateMaster.objects.create(
            insurance_company=HEALTH_INSURER, status="ACTIVE", is_deleted="NO", one_year_rate=5, payin_rate=8)
        health = {"Product": "health", "Policy: insurance company": HEALTH_INSURER,
                  "Policy: inception date": "01/08/2026", "Policy: expiry date": "31/07/2027"}
        out = self.run_file([health])
        self.assertEqual(out.loc[0, "Mapping Status"], MATCH)

    def test_motor_and_health_rows_in_the_same_file_are_untouched(self):
        out = self.run_file([_motor(), _life(), {"Product": "non_motor", "Policy: insurance company": MOTOR_INSURER}])
        self.assertEqual(list(out["Mapping Status"]), [MATCH, MATCH, NO_MATCH])
        self.assertEqual(out.loc[0, "Porate"], 12.5)
        self.assertTrue(out.loc[2, "Failure Reason"].startswith("Failed on: Product —"))
        self.assertIn("is not 'motor', 'health' or 'life'", out.loc[2, "Failure Reason"])

    def test_the_run_summary_counts_the_life_policies(self):
        self.run_file([_life(), _life(plan=None), _life(plan="Unknown Plan"), _life(policy_type="Renewal")])
        message = self.mis.error_message
        self.assertIn("Life: 4 policies, 1 matched", message)
        self.assertIn("1 no plan name", message)
        self.assertIn("1 plan not mapped", message)
        self.assertIn("1 not a first-year policy", message)

    def test_a_file_with_no_life_rows_has_no_life_sentence(self):
        self.run_file([_motor()])
        self.assertNotIn("Life:", self.mis.error_message)

    def test_failed_life_rows_keep_their_columns_and_insurer_for_the_screens(self):
        row = _life(plan="Unknown Plan", ppt=6, pt=20)
        row["Policy: insurance company"] = None
        row["Policy: ins company"] = "HDFC Life Insurance Co. Ltd."
        self.run_file([row])
        failed = MISFailedRow.objects.get(mis_file=self.mis)
        self.assertEqual(failed.insurer, "HDFC Life Insurance Co. Ltd.")
        self.assertEqual(failed.payload[PLAN], "Unknown Plan")
        self.assertEqual(failed.payload[PPT], 6)
        self.assertEqual(failed.payload[PT], 20)


class LifeDateWindowTests(LifeEngineBase):
    """A grid row's from_date / to_date against the policy's inception date: Health's RULE H2 for Life (both ends
    included, blank = no limit on that side, day-first dates, a grid with no dates is never narrowed)."""
    INCEPTION = "Policy: inception date"

    def policy(self, inception, **extra):
        return _life(**{self.INCEPTION: inception}, **extra)

    def test_a_grid_with_no_dates_is_never_narrowed(self):
        self.grid("HDFC Life", "Sanchay Plus", ppt="5 to 7", yr1=40.0)
        out = self.run_file([_life(), self.policy("15/11/2026"), self.policy(None), self.policy("not a date")])
        self.assertEqual(list(out["Mapping Status"]), [MATCH] * 4)
        self.assertEqual(list(out["Porate"]), [40.0] * 4)

    def test_the_inception_date_picks_the_row_whose_window_it_falls_in_ends_included(self):
        old = self.grid("HDFC Life", "Sanchay Plus", ppt="5 to 7", yr1=40.0, to_date=date(2026, 11, 30))
        new = self.grid("HDFC Life", "Sanchay Plus", ppt="5 to 7", yr1=45.0, from_date=date(2026, 12, 1))
        out = self.run_file([self.policy("15/11/2026"), self.policy("30/11/2026"), self.policy("01/12/2026"),
                             self.policy("20/12/2026")])
        self.assertEqual(list(out["Porate"]), [40.0, 40.0, 45.0, 45.0])
        self.assertEqual(list(out["Displaygroupid"]), [old.id, old.id, new.id, new.id])

    def test_dates_are_read_day_first(self):
        self.grid("HDFC Life", "Sanchay Plus", ppt="5 to 7", yr1=40.0, from_date=date(2026, 12, 1))
        self.assertEqual(self.one(self.policy("04/12/2026"))["Mapping Status"], MATCH)        # 4 December, not 12 April

    def test_a_date_outside_every_window_is_no_match_and_says_which_windows_the_grid_has(self):
        self.grid("HDFC Life", "Sanchay Plus", ppt="5 to 7", yr1=40.0, to_date=date(2026, 11, 30))
        out = self.one(self.policy("05/12/2026"))
        self.assertEqual(out["Mapping Status"], NO_MATCH)
        self.assertTrue(out["Failure Reason"].startswith(
            "Failed on: Policy: inception date — Inception date 05/12/2026 falls outside the date window of every "
            "remaining Life grid row of 'Sanchay Plus' for 'HDFC Life' (the grid states: until 30/11/2026)."))
        self.assertIn("1 inception date outside the grid rows' dates", self.mis.error_message)

    def test_a_blank_or_unreadable_inception_date_leaves_only_the_rows_with_no_dates(self):
        self.grid("HDFC Life", "Sanchay Plus", ppt="5 to 7", yr1=40.0, from_date=date(2026, 1, 1))
        out = self.run_file([self.policy(None), self.policy("not a date")])
        self.assertEqual(list(out["Mapping Status"]), [NO_MATCH, NO_MATCH])
        self.assertIn("Inception date is blank/unparseable on this policy, and no remaining Life grid row of "
                      "'Sanchay Plus' for 'HDFC Life' has an open (blank) date window.", out.loc[0, "Failure Reason"])
        open_row = self.grid("HDFC Life", "Sanchay Plus", ppt="5 to 7", yr1=50.0)
        out = self.run_file([self.policy(None)])
        self.assertEqual((out.loc[0, "Mapping Status"], out.loc[0, "Displaygroupid"]), (MATCH, open_row.id))

    def test_open_ended_windows(self):
        self.grid("HDFC Life", "Sanchay Plus", ppt="5 to 7", yr1=40.0, to_date=date(2026, 6, 30))
        self.grid("HDFC Life", "Sanchay Plus", ppt="5 to 7", yr1=45.0, from_date=date(2026, 7, 1))
        out = self.run_file([self.policy("01/01/2020"), self.policy("30/06/2026"), self.policy("01/07/2026"),
                             self.policy("31/12/2030")])
        self.assertEqual(list(out["Porate"]), [40.0, 40.0, 45.0, 45.0])

    def test_overlapping_windows_with_different_payouts_are_multiple_matches(self):
        self.grid("HDFC Life", "Sanchay Plus", ppt="5 to 7", yr1=40.0, to_date=date(2026, 12, 31))
        self.grid("HDFC Life", "Sanchay Plus", ppt="5 to 7", yr1=45.0, from_date=date(2026, 12, 1))
        self.assertEqual(self.one(self.policy("15/12/2026"))["Mapping Status"], MULTIPLE)
        self.assertEqual(self.one(self.policy("15/11/2026"))["Mapping Status"], MATCH)

    def test_a_matched_reason_names_the_dates_of_the_row(self):
        self.grid("HDFC Life", "Sanchay Plus", ppt="5 to 7", yr1=40.0, from_date=date(2026, 11, 1),
                  to_date=date(2026, 11, 30))
        out = self.one(self.policy("15/11/2026"))
        self.assertIn("The grid row applies 01/11/2026–30/11/2026.", out["Failure Reason"])

    def test_the_date_failure_is_listed_with_the_other_failures(self):
        self.grid("HDFC Life", "Sanchay Plus", ppt="5 to 7", yr1=40.0, to_date=date(2026, 11, 30))
        reason = self.one(self.policy("05/12/2026", ppt=10))["Failure Reason"]          # PPT 10: the grid row covers 5 to 7
        self.assertTrue(reason.startswith("Failed on: Policy: inception date — "))
        self.assertIn(" | Also failed on: Policy: premium paying term / ppt — ", reason)

    def test_the_date_stage_leaves_motor_rows_alone(self):
        self.grid("HDFC Life", "Sanchay Plus", ppt="5 to 7", yr1=40.0, to_date=date(2020, 1, 1))
        out = self.run_file([_motor(), self.policy("15/11/2026")])
        self.assertEqual(list(out["Mapping Status"]), [MATCH, NO_MATCH])
        self.assertEqual(out.loc[0, "Porate"], 12.5)


class LifeUnmappedListTests(LifeEngineBase):
    """The screens list what the engine could not place, read back from the stored failure reasons."""

    def setUp(self):
        super().setUp()
        self.grid("HDFC Life", "Sanchay Plus", ppt="5 to 7", yr1=40.0)

    def test_the_reasons_the_engine_writes_are_the_ones_the_screens_parse(self):
        self.run_file([
            _life(plan="HDFC Life Unknown One"), _life(plan="HDFC Life Unknown One"), _life(plan="hdfc life  UNKNOWN ONE"),
            _life(plan="HDFC Life Unknown Two"),
            _life(insurer="Max Life Insurance Co. Ltd."), _life(insurer="Bajaj Allianz General Insurance Company Limited"),
        ])
        plans, insurers = life_mis.unmapped_from_failed_rows()
        self.assertEqual([(p["insurer"], p["plan"], p["count"]) for p in plans],
                         [("HDFC Life", "HDFC Life Unknown One", 3), ("HDFC Life", "HDFC Life Unknown Two", 1)])
        self.assertEqual({i["name"]: i["count"] for i in insurers},
                         {"Max Life Insurance Co. Ltd.": 1, "Bajaj Allianz General Insurance Company Limited": 1})

    def test_what_has_been_mapped_since_drops_off_the_list(self):
        self.run_file([_life(plan="HDFC Life Unknown One"), _life(insurer="Max Life Insurance Co. Ltd.")])
        self.plan_mapping("HDFC Life", "HDFC Life Unknown One", "Sanchay Plus")
        self.alias("Max Life Insurance Co. Ltd.", "HDFC Life")
        self.assertEqual(life_mis.unmapped_from_failed_rows(), ([], []))

    def test_a_plan_that_fits_several_products_is_listed_too(self):
        self.grid("HDFC Life", "Click 2 Wealth / Sanchay Plus", ppt="5 to 7", yr1=1.0)
        self.run_file([_life(plan="Sanchay Plus")])
        plans, _ = life_mis.unmapped_from_failed_rows()
        self.assertEqual([(p["insurer"], p["plan"]) for p in plans], [("HDFC Life", "Sanchay Plus")])


class LifeSeedTests(TestCase):
    def test_the_real_mis_spellings_are_seeded_and_the_general_names_are_not(self):
        keys = set(LifeInsurerAlias.objects.values_list("name_key", flat=True))
        for spelling in ("India First Life Insurance Co.Ltd.", "Premirica Life Insurance Co. Ltd.",
                         "Life Insurance Corporation of India", "Aditya Birla SunLife Insurance Company Limited"):
            self.assertIn(life_mis.fold(spelling), keys, spelling)
        for general in ("Bajaj Allianz General Insurance Company Limited", "Shriram General Insurance Company Limited",
                        "Go-Digit General Insurance Limited", "TATA Aig General Insurance Company Limited",
                        "Generali Central Insurance Company Limited"):
            self.assertNotIn(life_mis.fold(general), keys, general)


class LifeHelperTests(SimpleTestCase):
    def test_fold_ignores_case_punctuation_and_spacing(self):
        self.assertEqual(life_mis.fold("Go-Digit  Life Insurance Ltd."), "go digit life insurance ltd")
        self.assertEqual(life_mis.fold(None), "")

    def test_text_of_treats_blank_and_null_like_cells_as_empty(self):
        for blank in (None, float("nan"), "", "  ", "nan", "None", "NULL", "-"):
            self.assertEqual(life_mis.text_of(blank), "", repr(blank))
        self.assertEqual(life_mis.text_of(" Term "), "Term")

    def test_whole_number_reads_the_shapes_the_mis_uses(self):
        self.assertEqual([life_mis.whole_number(v) for v in (12, "12", "12.0", 12.0, "1,200", " 5 ")],
                         [12, 12, 12, 12, 1200, 5])
        for bad in (None, "", "ten", "12.5", float("nan")):
            self.assertIsNone(life_mis.whole_number(bad), repr(bad))

    def test_columns_are_found_ignoring_spacing_and_punctuation(self):
        found = life_mis.find_life_columns(
            ["Product", "policy:  product name / plan name", "Policy: premium paying term  ppt", "Policy: policy termpt",
             "Policy: ins company", "Policy: insurance company"])
        self.assertEqual(found["plan"], ["policy:  product name / plan name"])
        self.assertEqual(found["insurer"], ["Policy: insurance company", "Policy: ins company"])
        self.assertEqual(found["ppt"], ["Policy: premium paying term  ppt"])
        self.assertEqual(found["mode"], [])

    def test_parse_grid_products_separates_exact_products_from_prefixes(self):
        exact, prefixes = life_mis.parse_grid_products("Sanchay Plus\n\n  SRP*  \nClick 2 Wealth / Sanchay Plus")
        self.assertEqual(exact, {"sanchay plus", "click 2 wealth sanchay plus"})
        self.assertEqual(prefixes, ["srp"])

    def test_the_gap_patterns_match_the_sentences_the_matcher_writes(self):
        plan = "Plan 'It's A Plan' is not mapped to any 'HDFC Life' Life grid product — add it on Life Plan Mapping."
        found = life_mis.UNMAPPED_PLAN_PATTERNS[0].search(plan)
        self.assertEqual((found.group(1), found.group(2)), ("It's A Plan", "HDFC Life"))
        self.assertIsNotNone(life_mis.UNKNOWN_INSURER_PATTERN.search(
            "Insurer 'X' is not a known Life insurer — add it on Life Insurer Aliases."))


class PolicyPptRuleTests(SimpleTestCase):
    """life_filters.policy_ppt_matches: the page's PPT and Payment Mode rules applied to one policy's own numbers."""

    def applies(self, wording, ppt, term=40, single_pay=False):
        return lf.policy_ppt_matches(lf.ppt_spec(wording), ppt, term, single_pay)

    def test_a_number_or_range_covers_the_pay_term(self):
        self.assertTrue(self.applies("5 to 7", 6))
        self.assertFalse(self.applies("5 to 7", 8))
        self.assertTrue(self.applies(">=10", 31))
        self.assertFalse(self.applies(">=10", 9))
        self.assertTrue(self.applies("12 & Above", 12))
        self.assertTrue(self.applies("10", 10))
        self.assertFalse(self.applies("10", 12))

    def test_a_row_that_states_no_ppt_applies_to_any(self):
        for wording in ("", "All", "Any"):
            self.assertTrue(self.applies(wording, 12), wording)

    def test_single_pay_takes_only_single_pay_rows(self):
        self.assertTrue(self.applies("SP", 1))
        self.assertTrue(self.applies("Single Premium", 1))
        self.assertTrue(self.applies("1", 1))
        self.assertFalse(self.applies("10", 1))
        self.assertFalse(self.applies("", 1))
        self.assertFalse(self.applies("5 to 7", 1))
        self.assertTrue(self.applies("SP", 0, single_pay=True))
        self.assertFalse(self.applies("10", 10, single_pay=True))

    def test_single_pay_rows_never_apply_to_a_limited_pay_policy(self):
        self.assertFalse(self.applies("SP", 10))

    def test_limited_and_regular_wording_follow_the_policy_term(self):
        self.assertTrue(self.applies("Limited", 10, term=40))
        self.assertFalse(self.applies("Limited", 40, term=40))
        self.assertTrue(self.applies("Regular", 40, term=40))
        self.assertFalse(self.applies("Regular", 10, term=40))
        self.assertFalse(self.applies("Limited", 10, term=None))

    def test_an_unknown_pay_term_matches_nothing_numeric(self):
        self.assertFalse(lf.policy_ppt_matches(lf.ppt_spec("10"), None, 20))
